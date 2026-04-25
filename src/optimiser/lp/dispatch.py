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

# Hysteresis margin for the mode-3-vs-mode-2 charge-source split. The LP
# decomposes a charge into `grid_to_battery_kw` and `pv_to_battery_kw`;
# their relative magnitudes pick the dispatch path (grid-dominant ⇒ mode
# 3, otherwise mode 2 + adaptive trim). HiGHS often returns near-equal
# decompositions where the two values differ by sub-watt numerical
# noise — without a margin, two ticks with effectively identical inputs
# can flip mode and change the entire write path. Require grid to lead
# PV by at least this margin before switching to mode 3; ties (and
# near-ties) stay on mode 2.
MODE_SWITCH_HYSTERESIS_KW: float = 0.05

# Buffer above current SOC, retained for backwards compatibility on the
# advisory `target_soc_pct` field (snapshot / metrics consumers). No
# longer written to a register — the 2026-04-25 cutoff-pinned-at-ceiling
# probe (see probe_no_cutoff.py) showed that 40032 alone is a sufficient
# rate knob for both mode-2 charge (adaptive trim) and idle (cap=0), so
# the per-tick cutoff write is retired. Cutoff stays at its startup
# ceiling (assert_battery_soc_limits writes it once at boot).
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

    `cap_kw` semantics by branch:
      - mode 2 + CHARGE: the LP's intended charge rate. Used as the
        adaptive trim floor (Phase-B write to 40032).
      - mode 2 + SELF_CONSUME (idle): 0 — also runs the adaptive trim,
        with `lp_rate` floor of 0 so the trim collapses to "soak any
        PV beyond the export cap". 40032 is *not* zeroed any more —
        that left unforecast PV surplus for the inverter to curtail
        rather than store. Discharge still flows if PV < load (40032
        caps charge only).
      - mode 3 charge: the LP's intended grid-charge rate, written to
        40032 directly.
      - mode 5/6 discharge: physical max_discharge_kw (LP's signed
        intent is preserved separately on `signed_intent_kw`).

    `target_soc_pct` is advisory only as of 2026-04-25 — kept for
    snapshot replay and Prometheus observability, never written to a
    register. The charge-cutoff register (40047) stays at its startup
    ceiling (set by `assert_battery_soc_limits`) and 40032 alone
    governs charge rate. See `probe_no_cutoff.py` for the validation.
    """

    mode: RemoteEMSControlMode
    cap_kw: float  # see above; ≥ 0
    signed_intent_kw: float  # the LP's signed battery_kw (+ charge, − discharge)
    kind: DispatchKind
    target_soc_pct: float | None = None


def _safe_cutoff_pct(target_pct: float, current_soc_pct: float) -> float:
    """Clamp the advisory charge-cutoff to be ≥ current SOC + small buffer.

    No longer used to drive register writes (cutoff is pinned at the
    startup ceiling — see `LPDispatch.target_soc_pct` docstring). Kept
    so the advisory `target_soc_pct` shown in snapshots / Prometheus
    matches what the LP planned, with the same buffer applied for
    consumer compatibility. Clamps to [0, 100].
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
      |battery_kw| < DEADBAND_KW       → SELF_CONSUME (mode 2), cap = 0
      battery_kw > 0, grid-dominant    → CHARGE_GRID_FIRST (mode 3), cap = battery_kw
      battery_kw > 0, PV-dominant      → SELF_CONSUMPTION (mode 2), cap = battery_kw (LP rate)
      battery_kw < 0, PV > threshold   → DISCHARGE_PV_FIRST (mode 5), cap = max_discharge_kw
      battery_kw < 0, PV ≤ threshold   → DISCHARGE_ESS_FIRST (mode 6), cap = max_discharge_kw

    PV-dominant charge uses **mode 2 with adaptive trim on 40032** as of
    2026-04-25. Phase A pins 40032 = max so the cascade soaks all PV
    and we can read true MPP from telemetry; Phase B trims 40032 so
    battery + export split rather than cascade-saturate (see
    `clients/sigenergy.py::_apply_mode2_adaptive_charge`). Mode 4
    (`COMMAND_CHARGING_PV_FIRST`) stays in the enum for replay
    compatibility but is never emitted.

    Idle (`|battery_kw| < DEADBAND_KW`) also runs the adaptive trim
    (with `lp_rate=0`) so the cascade soaks any PV surplus over the
    export cap into the battery — see
    `clients/sigenergy.py::_apply_mode2_adaptive_charge`. Earlier
    behaviour wrote `40032=0`, which left unforecast PV for the cascade
    to curtail rather than store; the user-visible symptom was the
    battery sitting at zero while ~1 kW of PV was discarded. Discharge
    continues to flow when PV < load (40032 caps charge only).

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

    `current_soc_pct` is the live SOC at the start of this slot —
    retained for the advisory `target_soc_pct` field on idle dispatches.

    `measured_pv_kw` is the live PV reading from telemetry; None means
    the caller doesn't have it (replay, smoke, tests) — we fall back to
    the LP's planned PV flows in slot_0 as a proxy.
    """
    battery_kw = slot_0.battery_kw

    if abs(battery_kw) < DEADBAND_KW:
        # Idle — write 40032 = 0 so the mode-2 cascade can't charge the
        # battery from PV. Surplus exports up to the DNSP cap; discharge
        # remains available if PV < load (40032 caps charge only).
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
        # pv-only), use mode 2 with adaptive trim so the inverter charges
        # from PV at a rate that lets surplus also flow to export rather
        # than cascade-saturating the battery first.
        if (
            slot_0.grid_to_battery_kw
            > slot_0.pv_to_battery_kw + MODE_SWITCH_HYSTERESIS_KW
        ):
            return LPDispatch(
                mode=RemoteEMSControlMode.COMMAND_CHARGING_GRID_FIRST,
                cap_kw=battery_kw,
                signed_intent_kw=battery_kw,
                kind=DispatchKind.CHARGE,
            )
        return LPDispatch(
            mode=RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION,
            cap_kw=battery_kw,
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
        # Mode 3 (grid-first): cap_kw is the LP's planned rate; over-cap
        # is meaningful — the inverter is charging harder than we asked,
        # costing more grid import than planned.
        # Mode 2 (PV-dominant, adaptive trim): the trim formula
        # intentionally lets the battery charge faster than `cap_kw`
        # (the LP rate) when measured surplus exceeds LP_rate + export_cap
        # — that's the whole point of the adaptive split. Don't false-
        # positive on it. The trim itself bounds physical rate at 40032.
        if (
            dispatch.mode != RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION
            and measured_kw > dispatch.cap_kw * CAP_OVERSHOOT_TOLERANCE
        ):
            return DeviationKind.OVER_CAP
        return DeviationKind.OK

    # DISCHARGE: measured should be ≤ 0 (within floor)
    if measured_kw > deviation_floor_kw:
        return DeviationKind.WRONG_DIRECTION
    if -measured_kw > dispatch.cap_kw * CAP_OVERSHOOT_TOLERANCE:
        return DeviationKind.OVER_CAP
    return DeviationKind.OK
