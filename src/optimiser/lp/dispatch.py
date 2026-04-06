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

from ..types import RemoteEMSControlMode
from .result import SlotDecision

# Below this threshold the LP's request is in the noise. Hand off to the
# inverter's own self-consumption logic — it handles small-signal balancing
# better than we can. Matches the deadband from the architecture discussion.
DEADBAND_KW: float = 0.1


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
    """

    mode: RemoteEMSControlMode
    cap_kw: float  # magnitude (≥ 0); 0 for SELF_CONSUME
    signed_intent_kw: float  # the LP's signed battery_kw (+ charge, − discharge)
    kind: DispatchKind


def dispatch_from_slot(slot_0: SlotDecision) -> LPDispatch:
    """Turn the LP's slot-0 decision into an inverter-ready dispatch.

    Mapping:
      |battery_kw| < DEADBAND_KW    → SELF_CONSUME (mode 2), cap = 0
      battery_kw > 0, grid-dominant → CHARGE_GRID_FIRST (mode 3), cap = battery_kw
      battery_kw > 0, PV-dominant   → CHARGE_PV_FIRST   (mode 4), cap = battery_kw
      battery_kw < 0                → DISCHARGE_ESS_FIRST (mode 6), cap = |battery_kw|

    "Grid-dominant" means the LP plans to source more of the charge from grid
    than from PV in this slot. We then ask the inverter to prefer that source
    (the "first" in the mode name); the cap is the *total* intended charge
    rate and the inverter supplies the remainder from the secondary source if
    the primary is short.

    DISCHARGE_PV_FIRST (mode 5) is intentionally not used: it lets the
    inverter skip battery discharge entirely if PV happens to cover house
    load, which contradicts the LP's intent when it asks to discharge.
    """
    battery_kw = slot_0.battery_kw

    if abs(battery_kw) < DEADBAND_KW:
        return LPDispatch(
            mode=RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION,
            cap_kw=0.0,
            signed_intent_kw=battery_kw,
            kind=DispatchKind.SELF_CONSUME,
        )

    if battery_kw > 0:
        # Charging. Decide grid-first vs PV-first based on which source
        # is contributing more in the LP solution. With pv_to_battery == 0
        # (no PV available), grid-dominant is the only sensible choice.
        # When PV ≥ grid contribution, prefer PV-first to avoid paying for
        # grid when free solar exists.
        pv_kw = slot_0.pv_to_battery_kw
        grid_kw = max(0.0, battery_kw - pv_kw)
        mode = (
            RemoteEMSControlMode.COMMAND_CHARGING_GRID_FIRST
            if grid_kw > pv_kw
            else RemoteEMSControlMode.COMMAND_CHARGING_PV_FIRST
        )
        return LPDispatch(
            mode=mode,
            cap_kw=battery_kw,
            signed_intent_kw=battery_kw,
            kind=DispatchKind.CHARGE,
        )

    # Discharging. Always ESS-first per the discussion above.
    return LPDispatch(
        mode=RemoteEMSControlMode.COMMAND_DISCHARGING_ESS_FIRST,
        cap_kw=-battery_kw,
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
        if measured_kw > dispatch.cap_kw * CAP_OVERSHOOT_TOLERANCE:
            return DeviationKind.OVER_CAP
        return DeviationKind.OK

    # DISCHARGE: measured should be ≤ 0 (within floor)
    if measured_kw > deviation_floor_kw:
        return DeviationKind.WRONG_DIRECTION
    if -measured_kw > dispatch.cap_kw * CAP_OVERSHOOT_TOLERANCE:
        return DeviationKind.OVER_CAP
    return DeviationKind.OK
