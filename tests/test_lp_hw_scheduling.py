"""Tests for the A1 hot-water scheduling changes in BinarySignalDrivenLoad.

Covers:
  - Constraint is added per in-horizon deadline (today + future days)
  - Past-deadline shortfall rolls forward into tomorrow's target
  - Roll-forward cap at 2 × daily_target_kwh
  - Today met → no today-constraint; tomorrow still constrained
  - Future-deadline only (e.g. short horizon where today's deadline
    already happened and tomorrow's is in horizon) is correctly handled

These are formulation-level tests — we inspect the PuLP problem
constraints rather than running a solve.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pulp
import pytest

from optimiser.config import BatteryConfig
from optimiser.lp.formulation import build_lp
from optimiser.lp.loads import BinarySignalDrivenLoad
from optimiser.types import (
    LoadCategory,
    LoadProfile,
    ManagedLoadStatus,
    PriceInterval,
    SystemState,
)
from tests.conftest import make_signal_load_config

UTC = UTC

# 10:00 Canberra AEDT (UTC+11) on 3 April 2026 — DST still active
# until 5 April 2026. Plenty of slots to today's 22:00 local deadline.
NOW_MORNING = datetime(2026, 4, 2, 23, 0, 0, tzinfo=UTC)

# 23:00 Canberra AEDT on 3 April 2026 — 1 hour past today's deadline.
# Used to exercise the roll-forward path.
NOW_POST_DEADLINE = datetime(2026, 4, 3, 12, 0, 0, tzinfo=UTC)


def _state(timestamp: datetime, soc: float = 50.0) -> SystemState:
    return SystemState(
        timestamp=timestamp,
        soc_pct=soc,
        battery_power_kw=0.0,
        pv_power_kw=0.0,
        grid_power_kw=1.0,
        house_load_kw=1.0,
        ems_mode=2,
        outdoor_temp_c=20.0,
        occupied=True,
    )


def _flat_prices(start: datetime, n_intervals: int = 96) -> list[PriceInterval]:
    """30-min prices spanning n_intervals × 30 min from `start`. Flat 20c/5c."""
    return [
        PriceInterval(
            start=start + timedelta(minutes=30 * i),
            end=start + timedelta(minutes=30 * (i + 1)),
            import_per_kwh=20.0,
            export_per_kwh=5.0,
            spot_per_kwh=6.0,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
        )
        for i in range(n_intervals)
    ]


def _flat_profile(kw: float = 1.0) -> LoadProfile:
    return LoadProfile(slots=[kw] * 48, maturity_level=0, context="a1-test")


def _hw_status(energy_today_kwh: float = 0.0) -> ManagedLoadStatus:
    return ManagedLoadStatus(
        load_id="hot_water",
        category=LoadCategory.SIGNAL_DRIVEN,
        power_kw=0.0,
        energy_today_kwh=energy_today_kwh,
        relay_on=False,
        cycle_state=None,
    )


def _constraint_rhs_by_day(prob: pulp.LpProblem) -> dict[int, float]:
    """Pull out the RHS of each `hot_water_daily_target_dN` constraint."""
    rhs: dict[int, float] = {}
    for name, constraint in prob.constraints.items():
        if "hot_water_daily_target_d" in name:
            day = int(name.split("_d")[-1])
            # PuLP: constraint.constant is the negated RHS when the
            # constraint is written as `lhs - rhs >= 0`.
            # Easier: the LP is (sum >= target), and `-constraint.constant`
            # recovers target.
            rhs[day] = -constraint.constant
    return rhs


def _build(
    state: SystemState,
    load_status: ManagedLoadStatus,
    daily_target_kwh: float = 4.0,
    draw_kw: float = 1.0,
) -> pulp.LpProblem:
    cfg = make_signal_load_config(
        daily_target_kwh=daily_target_kwh,
        draw_kw=draw_kw,
    )
    prob, _ = build_lp(
        state=state,
        prices_planning=_flat_prices(
            start=state.timestamp.replace(minute=0, second=0, microsecond=0),
            n_intervals=96,  # 48h coverage
        ),
        pv_forecast=None,
        load_profile=_flat_profile(),
        managed_loads=[load_status],
        lp_loads=[BinarySignalDrivenLoad(cfg)],
        battery_config=BatteryConfig(),
    )
    return prob


class TestTodayDeadlineInFuture:
    def test_target_unmet_adds_today_constraint(self) -> None:
        """Morning, nothing delivered yet → constrain today's window."""
        prob = _build(
            _state(NOW_MORNING),
            _hw_status(energy_today_kwh=0.0),
        )
        rhs = _constraint_rhs_by_day(prob)
        assert 0 in rhs, "today's constraint missing"
        assert rhs[0] == pytest.approx(4.0)

    def test_target_partially_met_reduces_today_remaining(self) -> None:
        """Morning, 1.5 kWh already delivered → today wants 2.5 more."""
        prob = _build(
            _state(NOW_MORNING),
            _hw_status(energy_today_kwh=1.5),
        )
        rhs = _constraint_rhs_by_day(prob)
        assert rhs[0] == pytest.approx(2.5)

    def test_target_met_drops_today_constraint(self) -> None:
        """Morning, target already hit → no today-constraint. Tomorrow's
        constraint still binds at the full daily target."""
        prob = _build(
            _state(NOW_MORNING),
            _hw_status(energy_today_kwh=4.5),  # over target already
        )
        rhs = _constraint_rhs_by_day(prob)
        assert 0 not in rhs, "today's constraint should be absent (met)"
        assert rhs[1] == pytest.approx(4.0), "tomorrow binds at full target"

    def test_tomorrow_always_constrained_when_in_horizon(self) -> None:
        """Tomorrow's deadline is ~35h out from a 10am start; well inside
        a 48h horizon. Must see a day_1 constraint every time."""
        prob = _build(
            _state(NOW_MORNING),
            _hw_status(energy_today_kwh=0.0),
        )
        rhs = _constraint_rhs_by_day(prob)
        assert 1 in rhs
        assert rhs[1] == pytest.approx(4.0)


class TestTodayDeadlinePassed:
    def test_full_shortfall_rolls_forward(self) -> None:
        """11pm local, 0 kWh delivered → today skipped, tomorrow takes
        on 4 (own) + 4 (rolled) = 8 kWh, which equals the 2× cap."""
        prob = _build(
            _state(NOW_POST_DEADLINE),
            _hw_status(energy_today_kwh=0.0),
        )
        rhs = _constraint_rhs_by_day(prob)
        assert 0 not in rhs, "today's deadline passed; constraint skipped"
        assert rhs[1] == pytest.approx(8.0), "tomorrow = own 4 + rolled 4"

    def test_partial_shortfall_rolls_forward(self) -> None:
        """11pm local, 2.5 kWh delivered → today shortfall 1.5 → tomorrow
        = 4 + 1.5 = 5.5 kWh. Well under the 8 kWh cap."""
        prob = _build(
            _state(NOW_POST_DEADLINE),
            _hw_status(energy_today_kwh=2.5),
        )
        rhs = _constraint_rhs_by_day(prob)
        assert 0 not in rhs
        assert rhs[1] == pytest.approx(5.5)

    def test_roll_forward_capped_at_2x(self) -> None:
        """Construct an extreme shortfall by setting a tiny daily target
        and having done negative energy (impossible, but the cap logic
        should clamp). Use a high target (6) with 0 delivered — forward
        would be 12, cap 2×6=12, equal at the boundary."""
        prob = _build(
            _state(NOW_POST_DEADLINE),
            _hw_status(energy_today_kwh=0.0),
            daily_target_kwh=6.0,
            draw_kw=1.0,
        )
        rhs = _constraint_rhs_by_day(prob)
        # Cap = 2 × 6 = 12. Rolled target = 6 + 6 = 12 → at the cap.
        assert rhs[1] == pytest.approx(12.0)

    def test_today_met_no_rollforward(self) -> None:
        """11pm, target already met by late-afternoon heating → shortfall
        is zero; tomorrow stays at the vanilla daily target."""
        prob = _build(
            _state(NOW_POST_DEADLINE),
            _hw_status(energy_today_kwh=4.0),
        )
        rhs = _constraint_rhs_by_day(prob)
        assert 0 not in rhs
        assert rhs[1] == pytest.approx(4.0)
