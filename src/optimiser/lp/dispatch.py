"""Maps an LP slot-0 decision to a Sigenergy EMS dispatch (mode + cap).

Why this lives here, not in `clients/sigenergy.py`: the inverter client should
know about Sigenergy registers and modes; the LP-to-mode mapping is policy
that depends on what the LP solved for. Keeping them separate means a
hypothetical second inverter brand could implement the same dispatch
abstraction differently.

Key design: we never use mode 0 (PCS_REMOTE_CONTROL with continuous setpoint).
That mode requires us to predict house load to the kW-second, which is
impossible — every load transient leaks as unintended grid flow. Instead we
use load-following modes 3/4/6, which let the inverter handle sub-second load
response within a magnitude cap supplied by the LP.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto

from ..config import BatteryConfig
from ..types import RemoteEMSControlMode
from .result import SlotDecision

# Below this threshold the LP's request is in the noise. Hand off to the
# inverter's own self-consumption logic — it handles small-signal balancing
# better than we can. Matches the deadband from the architecture discussion.
DEADBAND_KW: float = 0.1

# Above this PV reading we pick mode 5 over mode 6 for discharge intent.
# Rationale: mode 6 zeroes PV generation (verified on hardware, see
# SIGENERGY-MODES.md). Any PV > 0.2 kW that we'd lose to mode 6's
# pathological behaviour is worth routing through mode 5 instead — mode 5
# load-follows, using PV first and topping up from battery if short.
PV_PRODUCING_THRESHOLD_KW: float = 0.2

# Buffer added above current SOC when writing the charge cutoff under
# mode 2. The 2026-04-24 hardware probe showed that writing cutoff at
# or below current SOC is a leaky idle signal — the inverter trickles
# ~1 kW of PV in rather than fully stopping. The +0.1% buffer puts the
# cutoff unambiguously above the current SOC reading so the inverter
# sees a clear ceiling. See PLAN-3.3.md "Probe results" for data.
CUTOFF_BUFFER_PCT: float = 0.1


class DispatchKind(StrEnum):
    """High-level intent for verification and logging.

    SELF_CONSUME is distinct from CHARGE/DISCHARGE because verification
    doesn't apply (we're not asserting a direction or magnitude).
    """

    SELF_CONSUME = auto()
    CHARGE = auto()
    DISCHARGE = auto()


@dataclass(frozen=True, slots=True)
class LPDispatch:
    """A concrete EMS command derived from the LP's slot-0 decision.

    `mode` and `cap_kw` go to the inverter. `signed_intent_kw` and `kind`
    are kept for the watcher and snapshot, so we can verify the inverter
    respected our intent without re-deriving it from raw register values.

    `target_soc_pct` is set only for mode-2 dispatches (idle and PV-
    dominant charge under §3.3): it's the value we write to register
    40047 to express "charge PV up to here, then stop". Already clamped
    by `dispatch_from_slot` to be safely above current SOC — apply
    paths can write it as-is. None for mode-3 charge and mode-5/6
    discharge, which don't consult 40047 in the apply path.
    """

    mode: RemoteEMSControlMode
    cap_kw: float  # magnitude (≥ 0); 0 for SELF_CONSUME
    signed_intent_kw: float  # the LP's signed battery_kw (+ charge, − discharge)
    kind: DispatchKind
    target_soc_pct: float | None = None


def _safe_cutoff_pct(target_pct: float, current_soc_pct: float) -> float:
    """Clamp a charge-cutoff target so it sits unambiguously above current SOC.

    Required by the 2026-04-24 hardware probe: the inverter trickles
    PV in when cutoff equals or sits below current SOC. The +0.1%
    buffer puts the cutoff above the current reading (within float
    quantisation) so the inverter sees a clear ceiling. Also clamps
    to [0, 100] for register sanity.
    """
    return max(0.0, min(100.0, max(target_pct, current_soc_pct + CUTOFF_BUFFER_PCT)))


def dispatch_from_slot(
    slot_0: SlotDecision,
    battery_config: BatteryConfig,
    *,
    current_soc_pct: float,
    measured_pv_kw: float | None = None,
) -> LPDispatch:
    """Turn the LP's slot-0 decision into an inverter-ready dispatch.

    Mapping:
      |battery_kw| < DEADBAND_KW       → SELF_CONSUME (mode 2), target = current_soc + buffer
      battery_kw > 0, grid-dominant    → CHARGE_GRID_FIRST (mode 3), cap = battery_kw
      battery_kw > 0, PV-dominant      → SELF_CONSUMPTION (mode 2), target = soc_pct_end
      battery_kw < 0, PV > threshold   → DISCHARGE_PV_FIRST (mode 5), cap = max_discharge_kw
      battery_kw < 0, PV ≤ threshold   → DISCHARGE_ESS_FIRST (mode 6), cap = max_discharge_kw

    PV-dominant charge uses **mode 2 + dynamic charge_cut_off_soc** rather
    than mode 4 (`COMMAND_CHARGING_PV_FIRST`). Mode 4's reg 40032 is a
    target, not a ceiling — when PV droops mid-slot the inverter pulls
    grid to hit the target, which is a silent grid-draw hazard in
    cloudy conditions. Mode 2's priority cascade
    (`PV → load → battery (up to cutoff) → export → curtail`) achieves
    "charge from PV up to a ceiling" with no grid-draw risk and no
    transient-margin tuning knob. See `SIGENERGY-MODES.md` and
    `PLAN-3.3.md` for the full rationale.

    Mode 4 stays in `RemoteEMSControlMode` for historical replay only;
    this dispatch never emits it.

    Idle (`|battery_kw| < DEADBAND_KW`) also routes through mode 2 with
    `target = current_soc + buffer`. Reason: if a previous tick wrote a
    higher cutoff (legitimate charge), an idle tick that didn't rewrite
    40047 would let PV continue charging up to the stale ceiling. Always
    writing the cutoff each tick keeps the "charge ceiling" honest.

    Charge cap (mode-3 path) uses the LP's intended rate: grid charge is
    directly controllable and exceeding the plan wastes money.

    Discharge cap uses the *physical* max_discharge_kw, NOT the LP's
    point estimate of house load. The LP plans around an expected load;
    if actual load spikes above that (kettle, AC, oven), a tight cap
    would force the shortfall to grid import at full retail.

    Discharge mode selection (5 vs 6) is driven by whether PV is
    producing meaningfully now. Mode 6 zeroes PV generation entirely
    (verified on hardware, see SIGENERGY-MODES.md).

    "Grid-dominant" reads the LP variable `slot_0.grid_to_battery_kw`
    directly (vs the previous behaviour of inferring it via subtraction).
    Cleaner and avoids rounding artefacts.

    `current_soc_pct` is the live SOC at the start of this slot — used
    for the cutoff clamp on the mode-2 paths.

    `measured_pv_kw` is the live PV reading from telemetry; None means
    the caller doesn't have it (replay, smoke, tests) — we fall back to
    the LP's planned PV flows in slot_0 as a proxy.
    """
    battery_kw = slot_0.battery_kw

    if abs(battery_kw) < DEADBAND_KW:
        # Idle — write current_soc + buffer as the cutoff so any stale
        # higher cutoff from a previous tick is overwritten.
        return LPDispatch(
            mode=RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION,
            cap_kw=0.0,
            signed_intent_kw=battery_kw,
            kind=DispatchKind.SELF_CONSUME,
            target_soc_pct=_safe_cutoff_pct(current_soc_pct, current_soc_pct),
        )

    if battery_kw > 0:
        # Charging. Read grid contribution directly from the LP solution
        # (no inference). When grid > pv we want explicit mode-3 grid
        # charging so the cap (40032) is honoured; when pv ≥ grid (or
        # pv-only), use mode 2 + cutoff so the inverter charges from PV
        # up to the planned end-of-slot SOC and never grid-draws.
        if slot_0.grid_to_battery_kw > slot_0.pv_to_battery_kw:
            return LPDispatch(
                mode=RemoteEMSControlMode.COMMAND_CHARGING_GRID_FIRST,
                cap_kw=battery_kw,
                signed_intent_kw=battery_kw,
                kind=DispatchKind.CHARGE,
            )
        return LPDispatch(
            mode=RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION,
            cap_kw=0.0,
            signed_intent_kw=battery_kw,
            kind=DispatchKind.CHARGE,
            target_soc_pct=_safe_cutoff_pct(slot_0.soc_pct_end, current_soc_pct),
        )

    # Discharging. Pick mode 5 if PV is producing, mode 6 otherwise.
    pv_signal_kw = (
        measured_pv_kw
        if measured_pv_kw is not None
        else (
            slot_0.pv_to_house_kw + slot_0.pv_to_battery_kw + slot_0.pv_to_export_kw
        )
    )
    if pv_signal_kw > PV_PRODUCING_THRESHOLD_KW:
        discharge_mode = RemoteEMSControlMode.COMMAND_DISCHARGING_PV_FIRST
    else:
        discharge_mode = RemoteEMSControlMode.COMMAND_DISCHARGING_ESS_FIRST
    return LPDispatch(
        mode=discharge_mode,
        cap_kw=battery_config.max_discharge_kw,
        signed_intent_kw=battery_kw,
        kind=DispatchKind.DISCHARGE,
    )


# ── Verification ─────────────────────────────────────────────────


# Tolerate the inverter slightly exceeding the cap (e.g. brief overshoot
# during ramp). 5% over the cap before we call it a deviation.
CAP_OVERSHOOT_TOLERANCE: float = 1.05


class DeviationKind(StrEnum):
    """Outcome of comparing measured battery power against the dispatch."""

    OK = auto()  # within direction + cap
    WRONG_DIRECTION = auto()  # commanded charge but discharging (or vice versa)
    OVER_CAP = auto()  # magnitude exceeds cap × tolerance
    NOT_VERIFIED = auto()  # SELF_CONSUME mode — no assertion to check


def verify_battery_response(
    dispatch: LPDispatch,
    measured_kw: float,
    deviation_floor_kw: float = 0.3,
) -> DeviationKind:
    """Check whether the measured battery power respects our dispatch intent.

    Sub-cap operation is OK: the inverter is allowed to discharge less than
    the cap (or charge less) if real-time load doesn't demand it. We only
    flag wrong direction or magnitude exceeding the cap.

    `deviation_floor_kw` suppresses false positives near zero crossings:
    a 50W reading on the wrong side of zero isn't a real deviation, just
    measurement noise.
    """
    if dispatch.kind == DispatchKind.SELF_CONSUME:
        return DeviationKind.NOT_VERIFIED

    if dispatch.kind == DispatchKind.CHARGE:
        # Commanded charging — measured should be ≥ 0 (within floor).
        # Negative measurement = inverter is discharging when we asked
        # for charge. Direction wrong.
        if measured_kw < -deviation_floor_kw:
            return DeviationKind.WRONG_DIRECTION
        # cap_kw > 0 → mode 3 grid-charge with the LP's planned rate.
        # Over-cap is meaningful: the inverter is charging harder than
        # we asked, costing more grid import than planned.
        # cap_kw == 0 → mode 2 PV-charge via cutoff. There's no
        # meaningful instantaneous cap (the inverter charges at whatever
        # PV produces, bounded only by the physical 13 kW DC limit).
        # Skip the over-cap check; the cutoff (40047) bounds total
        # energy via end-of-slot SOC, not instantaneous power.
        if dispatch.cap_kw > 0 and measured_kw > dispatch.cap_kw * CAP_OVERSHOOT_TOLERANCE:
            return DeviationKind.OVER_CAP
        return DeviationKind.OK

    # DISCHARGE: measured should be ≤ 0 (within floor)
    if measured_kw > deviation_floor_kw:
        return DeviationKind.WRONG_DIRECTION
    if -measured_kw > dispatch.cap_kw * CAP_OVERSHOOT_TOLERANCE:
        return DeviationKind.OVER_CAP
    return DeviationKind.OK
