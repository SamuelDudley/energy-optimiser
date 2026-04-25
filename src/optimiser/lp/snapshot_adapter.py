"""Adapter from `LPSolution` + `LPDispatch` to the legacy `PlannerOutput`.

Why this exists: `TickSnapshot.output` is currently typed as `PlannerOutput`,
and the snapshot writer/replay engine read those fields. Bumping the schema
to natively store `LPSolution` is a separate, larger change (cascades into
replay, snapshot tests, retention semantics). For now we keep the snapshot
shape stable and translate at the boundary — the LP is the source of truth,
this adapter just produces a snapshot-compatible view of it.

Mapping rules:
  - `battery_action`: derived from the dispatch kind/mode (not the LP's raw
    battery_kw — we want the action that was actually commanded to the
    inverter, including the deadband collapse to SELF_CONSUME).
  - `charge_limit_kw` / `discharge_limit_kw`: the magnitude cap on the
    relevant side, 0 on the other. Mirrors what was written to 40032/40034.
  - `target_soc`: end-of-slot SOC from the LP solution. Used by the replay
    engine and for snapshot debugging; not commanded to the inverter.
  - `load_commands`: pass through the LP's slot-0 commands unchanged.
  - `grid_export_limit_kw`: pass through the LP's chosen export limit.
  - `reason`: synthesised from solve status + the dispatch kind, so the
    snapshot is informative without needing to crack open the LPSolution.
"""

from __future__ import annotations

from ..types import BatteryAction, PlannerOutput, RemoteEMSControlMode
from .dispatch import DispatchKind, LPDispatch
from .result import LPSolution

# Map the inverter mode we actually wrote to the legacy BatteryAction.
# This is what was *commanded*, not what the LP "wanted" pre-deadband.
#
# Mode 4 (CHARGE_PV_FIRST) stays in the table for historical-snapshot
# replay only — the dispatch path retired it 2026-04-24 (see
# SPEC-ENERGY-01.md §5.4). Mode 5 (DISCHARGE_PV_FIRST) maps to
# DISCHARGE_PV.
_MODE_TO_ACTION: dict[RemoteEMSControlMode, BatteryAction] = {
    RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION: BatteryAction.SELF_CONSUME,
    RemoteEMSControlMode.COMMAND_CHARGING_GRID_FIRST: BatteryAction.CHARGE_GRID,
    RemoteEMSControlMode.COMMAND_CHARGING_PV_FIRST: BatteryAction.CHARGE_PV,
    RemoteEMSControlMode.COMMAND_DISCHARGING_PV_FIRST: BatteryAction.DISCHARGE_PV,
    RemoteEMSControlMode.COMMAND_DISCHARGING_ESS_FIRST: BatteryAction.DISCHARGE_ESS,
    RemoteEMSControlMode.STANDBY: BatteryAction.STANDBY,
}


def lp_solution_to_planner_output(
    solution: LPSolution,
    dispatch: LPDispatch,
) -> PlannerOutput:
    """Translate an LP outcome to the snapshot-compatible PlannerOutput shape.

    Mode-keyed mapping covers the bulk of the cases via `_MODE_TO_ACTION`.
    The one exception: a mode-2 dispatch can carry kind=CHARGE (the
    PV-dominant adaptive-trim path). Pure mode-keyed lookup would log
    it as SELF_CONSUME, losing the LP's intent. Special-case it explicitly.
    """
    if (
        dispatch.kind == DispatchKind.CHARGE
        and dispatch.mode == RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION
    ):
        action = BatteryAction.CHARGE_PV
    else:
        action = _MODE_TO_ACTION.get(dispatch.mode, BatteryAction.SELF_CONSUME)

    if dispatch.kind == DispatchKind.CHARGE:
        charge_limit_kw = dispatch.cap_kw
        discharge_limit_kw = 0.0
    elif dispatch.kind == DispatchKind.DISCHARGE:
        charge_limit_kw = 0.0
        discharge_limit_kw = dispatch.cap_kw
    else:
        # SELF_CONSUME — neither cap is being asserted
        charge_limit_kw = 0.0
        discharge_limit_kw = 0.0

    target_soc = (
        solution.slot_0.soc_pct_end
        if solution.slot_0 is not None
        else 50.0  # fallback default; only hit when solution is degenerate
    )

    reason = (
        f"lp:{solution.status.value}:{dispatch.kind.value}"
        if solution.reason is None
        else f"lp:{solution.status.value}:{dispatch.kind.value}:{solution.reason}"
    )

    return PlannerOutput(
        battery_action=action,
        charge_limit_kw=charge_limit_kw,
        discharge_limit_kw=discharge_limit_kw,
        target_soc=target_soc,
        load_commands=list(solution.load_commands),
        grid_export_limit_kw=solution.grid_export_limit_kw,
        reason=reason,
    )


def fallback_planner_output(reason: str) -> PlannerOutput:
    """Snapshot-shape output for ticks where we couldn't run the LP at all
    (e.g. solver unavailable on startup, or breaker latched without a
    successful probe yet). Mirrors `set_fallback()` semantics on the
    inverter: SELF_CONSUME, no caps, no load commands."""
    return PlannerOutput(
        battery_action=BatteryAction.SELF_CONSUME,
        charge_limit_kw=0.0,
        discharge_limit_kw=0.0,
        target_soc=50.0,
        load_commands=[],
        grid_export_limit_kw=None,
        reason=reason,
    )
