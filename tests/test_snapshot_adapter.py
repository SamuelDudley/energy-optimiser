"""Tests for the LP→PlannerOutput snapshot adapter.

Critical for §3.3 + replay continuity: the dispatch path now sends
mode 2 with kind=CHARGE for PV-charge intent, but the snapshot needs
to record that as `BatteryAction.CHARGE_PV` so historical replay
analyses (vs the old mode-4 snapshots) compare apples to apples.

Historical mode-4 snapshots also still need to map → CHARGE_PV
(the enum value lingers in `RemoteEMSControlMode` for exactly this
reason).
"""

from __future__ import annotations

from datetime import UTC, datetime

from optimiser.lp.dispatch import DispatchKind, LPDispatch
from optimiser.lp.result import LPSolution, SlotDecision, SolveStatus
from optimiser.lp.snapshot_adapter import (
    fallback_planner_output,
    lp_solution_to_planner_output,
)
from optimiser.types import BatteryAction, RemoteEMSControlMode


def _slot(soc_pct_end: float = 70.0) -> SlotDecision:
    return SlotDecision(
        slot_start=datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC),
        battery_kw=2.0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        pv_to_house_kw=1.0,
        pv_to_battery_kw=2.0,
        pv_to_export_kw=0.0,
        soc_pct_end=soc_pct_end,
    )


def _solution(slot: SlotDecision | None = None) -> LPSolution:
    return LPSolution(
        status=SolveStatus.OPTIMAL,
        slot_0=slot if slot is not None else _slot(),
        forward_trajectory=[slot] if slot is not None else [_slot()],
        load_commands=[],
        grid_export_limit_kw=5.0,
        expected_total_cost_cents=100.0,
        solve_time_ms=1234.0,
        reason="test",
    )


class TestMode2ChargeMapping:
    """The §3.3 special case — mode 2 + kind=CHARGE."""

    def test_mode2_charge_maps_to_charge_pv(self) -> None:
        """A mode-2 dispatch with kind=CHARGE is the §3.3 PV-charge
        path. The adapter must NOT log it as SELF_CONSUME (which is
        what a pure mode-keyed lookup would produce); it must surface
        as CHARGE_PV so replay can compare it against pre-§3.3
        mode-4 snapshots that also logged CHARGE_PV."""
        dispatch = LPDispatch(
            mode=RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION,
            cap_kw=0.0,
            signed_intent_kw=2.0,
            kind=DispatchKind.CHARGE,
            target_soc_pct=80.0,
        )
        out = lp_solution_to_planner_output(_solution(), dispatch)
        assert out.battery_action == BatteryAction.CHARGE_PV

    def test_mode2_self_consume_still_maps_to_self_consume(self) -> None:
        """An idle mode-2 dispatch with kind=SELF_CONSUME maps to
        SELF_CONSUME. The kind is the disambiguating signal."""
        dispatch = LPDispatch(
            mode=RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION,
            cap_kw=0.0,
            signed_intent_kw=0.0,
            kind=DispatchKind.SELF_CONSUME,
            target_soc_pct=60.1,
        )
        out = lp_solution_to_planner_output(_solution(), dispatch)
        assert out.battery_action == BatteryAction.SELF_CONSUME


class TestHistoricalReplayCompat:
    """Pre-§3.3 mode-4 snapshots must still round-trip through the
    adapter — that's why the mode-4 entry stays in the table even
    though the live dispatch never emits it."""

    def test_mode4_still_maps_to_charge_pv(self) -> None:
        dispatch = LPDispatch(
            mode=RemoteEMSControlMode.COMMAND_CHARGING_PV_FIRST,
            cap_kw=3.0,
            signed_intent_kw=3.0,
            kind=DispatchKind.CHARGE,
        )
        out = lp_solution_to_planner_output(_solution(), dispatch)
        assert out.battery_action == BatteryAction.CHARGE_PV

    def test_mode3_grid_charge_unchanged(self) -> None:
        dispatch = LPDispatch(
            mode=RemoteEMSControlMode.COMMAND_CHARGING_GRID_FIRST,
            cap_kw=5.0,
            signed_intent_kw=5.0,
            kind=DispatchKind.CHARGE,
        )
        out = lp_solution_to_planner_output(_solution(), dispatch)
        assert out.battery_action == BatteryAction.CHARGE_GRID

    def test_mode5_discharge_pv_first_maps_to_discharge_pv(self) -> None:
        dispatch = LPDispatch(
            mode=RemoteEMSControlMode.COMMAND_DISCHARGING_PV_FIRST,
            cap_kw=10.0,
            signed_intent_kw=-3.0,
            kind=DispatchKind.DISCHARGE,
        )
        out = lp_solution_to_planner_output(_solution(), dispatch)
        assert out.battery_action == BatteryAction.DISCHARGE_PV

    def test_mode6_discharge_unchanged(self) -> None:
        dispatch = LPDispatch(
            mode=RemoteEMSControlMode.COMMAND_DISCHARGING_ESS_FIRST,
            cap_kw=10.0,
            signed_intent_kw=-3.0,
            kind=DispatchKind.DISCHARGE,
        )
        out = lp_solution_to_planner_output(_solution(), dispatch)
        assert out.battery_action == BatteryAction.DISCHARGE_ESS


class TestFallback:
    def test_fallback_output_is_self_consume(self) -> None:
        out = fallback_planner_output("breaker latched")
        assert out.battery_action == BatteryAction.SELF_CONSUME
        assert out.charge_limit_kw == 0.0
        assert out.discharge_limit_kw == 0.0
        assert out.reason == "breaker latched"
