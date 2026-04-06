"""Tests for the stochastic (P10/P50/P90) LP formulation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pulp
import pytest

from optimiser.config import BatteryConfig, ManagedLoadConfig
from optimiser.lp.constants import (
    HORIZON_HOURS,
)
from optimiser.lp.formulation import build_stochastic_lp
from optimiser.lp.loads import BinarySignalDrivenLoad
from optimiser.lp.result import SolveStatus
from optimiser.lp.solver import _solver, solve_stochastic
from optimiser.types import (
    LoadCategory,
    LoadProfile,
    ManagedLoadStatus,
    PriceInterval,
    PVForecast,
    SystemState,
)

UTC = UTC
NOW = datetime(2026, 4, 2, 22, 0, 0, tzinfo=UTC)  # 09:00 Canberra Apr 3


def _state(soc: float = 50.0) -> SystemState:
    return SystemState(
        timestamp=NOW,
        soc_pct=soc,
        battery_power_kw=0.0,
        pv_power_kw=0.0,
        grid_power_kw=1.0,
        house_load_kw=1.0,
        ems_mode=2,
        outdoor_temp_c=20.0,
        occupied=True,
    )


def _flat_prices(import_c: float = 20.0, export_c: float = 5.0) -> list[PriceInterval]:
    n_intervals = HORIZON_HOURS * 2
    return [
        PriceInterval(
            start=NOW + timedelta(minutes=30 * i),
            end=NOW + timedelta(minutes=30 * (i + 1)),
            import_per_kwh=import_c,
            export_per_kwh=export_c,
            spot_per_kwh=import_c * 0.3,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
        )
        for i in range(n_intervals)
    ]


def _flat_profile(kw: float = 1.0) -> LoadProfile:
    return LoadProfile(slots=[kw] * 48, maturity_level=0, context="lp-test")


def _hw_cfg() -> ManagedLoadConfig:
    return ManagedLoadConfig(
        load_id="hot_water",
        category=LoadCategory.SIGNAL_DRIVEN,
        shelly_host="test",
        has_relay=True,
        daily_target_kwh=4.0,
        draw_kw=1.0,
        deadline_hour_local=22,
    )


def _hw_status(energy_today: float = 0.0) -> ManagedLoadStatus:
    return ManagedLoadStatus(
        load_id="hot_water",
        category=LoadCategory.SIGNAL_DRIVEN,
        power_kw=0.0,
        energy_today_kwh=energy_today,
        relay_on=False,
        cycle_state=None,
    )


def _pv_forecast(
    p50_kw: float, p10_kw: float, p90_kw: float, hours_long: int = 6
) -> list[PVForecast]:
    """A 30-min cadence PV forecast spanning the next `hours_long` hours,
    with explicit P10/P50/P90 values.
    """
    n = hours_long * 2
    return [
        PVForecast(
            start=NOW + timedelta(minutes=30 * i),
            end=NOW + timedelta(minutes=30 * (i + 1)),
            pv_estimate_kw=p50_kw,
            pv_estimate10_kw=p10_kw,
            pv_estimate90_kw=p90_kw,
        )
        for i in range(n)
    ]


# ── Scaffolding tests ───────────────────────────────────────────


class TestStochasticBuilds:
    def test_solves_with_default_scenarios(self) -> None:
        sol = solve_stochastic(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=_pv_forecast(p50_kw=5.0, p10_kw=3.0, p90_kw=7.0),
            load_profile=_flat_profile(),
            managed_loads=[_hw_status()],
            lp_loads=[BinarySignalDrivenLoad(_hw_cfg())],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), sol.reason
        assert sol.slot_0 is not None
        # Should mention multiple scenarios in the reason string
        assert "scenarios" in sol.reason

    def test_custom_scenario_weights(self) -> None:
        """Solves with a custom weighting (e.g. heavily pessimistic)."""
        sol = solve_stochastic(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=_pv_forecast(p50_kw=5.0, p10_kw=3.0, p90_kw=7.0),
            load_profile=_flat_profile(),
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(),
            scenario_weights={"p10": 0.7, "p50": 0.2, "p90": 0.1},
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)

    def test_weights_must_sum_to_one(self) -> None:
        with pytest.raises(ValueError, match="sum to 1"):
            build_stochastic_lp(
                state=_state(),
                prices_planning=_flat_prices(),
                pv_forecast=None,
                load_profile=_flat_profile(),
                managed_loads=[],
                lp_loads=[],
                battery_config=BatteryConfig(),
                scenario_weights={"p10": 0.5, "p50": 0.4},  # sums to 0.9
            )

    def test_single_scenario_collapses_to_deterministic(self) -> None:
        """One scenario with weight 1 should solve to the same cost as the
        deterministic LP (both reduce to identical formulations).
        """
        from optimiser.lp.solver import solve as solve_det

        det = solve_det(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=_pv_forecast(p50_kw=5.0, p10_kw=3.0, p90_kw=7.0),
            load_profile=_flat_profile(),
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        stoch = solve_stochastic(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=_pv_forecast(p50_kw=5.0, p10_kw=3.0, p90_kw=7.0),
            load_profile=_flat_profile(),
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(),
            scenario_weights={"p50": 1.0},
            timeout_s=30.0,
        )
        assert det.status == SolveStatus.OPTIMAL
        assert stoch.status == SolveStatus.OPTIMAL
        assert det.expected_total_cost_cents == pytest.approx(
            stoch.expected_total_cost_cents,
            rel=0.01,
        )


# ── Non-anticipativity properties ───────────────────────────────


class TestNonAnticipativity:
    def test_slot_0_battery_kw_identical_across_scenarios(self) -> None:
        prob, svars = build_stochastic_lp(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=_pv_forecast(p50_kw=5.0, p10_kw=3.0, p90_kw=7.0),
            load_profile=_flat_profile(),
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(),
        )
        # Solve and inspect slot-0 battery decisions across scenarios
        prob.solve(_solver(30.0))
        assert pulp.LpStatus[prob.status] == "Optimal"

        slot_0_decisions = []
        for name, vars in svars.scenarios.items():
            net = (
                (vars.bat_charge_grid[0].value() or 0.0)
                + (vars.bat_charge_pv[0].value() or 0.0)
                - (vars.bat_discharge[0].value() or 0.0)
            )
            slot_0_decisions.append((name, net))

        baseline = slot_0_decisions[0][1]
        for name, val in slot_0_decisions:
            assert val == pytest.approx(baseline, abs=0.001), (
                f"non-anticipativity violated: {name} slot-0 bat={val:.3f}, baseline={baseline:.3f}"
            )

    def test_slot_1_battery_kw_can_differ(self) -> None:
        """Stage-2 (slot 1+) decisions are scenario-specific, so a
        sufficiently different forecast should produce different plans.
        """
        prob, svars = build_stochastic_lp(
            state=_state(),
            # Sharp price valley in slot 1
            prices_planning=_make_price_valley(),
            pv_forecast=_pv_forecast(p50_kw=5.0, p10_kw=0.0, p90_kw=10.0, hours_long=6),
            load_profile=_flat_profile(),
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(),
        )
        prob.solve(_solver(30.0))

        # P10 (no PV) likely uses grid; P90 (lots of PV) likely uses PV.
        # We're not asserting a specific decision — just that scenarios
        # are FREE to differ in stage 2 (which they should, given the
        # different inputs).
        slot_1_p10 = svars.scenarios["p10"].bat_charge_pv[1].value() or 0.0
        slot_1_p90 = svars.scenarios["p90"].bat_charge_pv[1].value() or 0.0
        # P90 has more PV, so should put more PV into the battery
        assert slot_1_p90 >= slot_1_p10 - 0.01

    def test_relay_slot_0_identical_across_scenarios(self) -> None:
        """The HW relay decision at slot 0 must also be scenario-independent."""
        cfg = _hw_cfg()
        prob, svars = build_stochastic_lp(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=_pv_forecast(p50_kw=5.0, p10_kw=3.0, p90_kw=7.0),
            load_profile=_flat_profile(),
            managed_loads=[_hw_status()],
            lp_loads=[BinarySignalDrivenLoad(cfg)],
            battery_config=BatteryConfig(),
        )
        prob.solve(_solver(30.0))

        slot_0_relays = []
        for name, vars in svars.scenarios.items():
            relay_var = vars.loads["hot_water"].extras["relay"][0]
            slot_0_relays.append((name, relay_var.value() or 0.0))

        baseline = slot_0_relays[0][1]
        for name, val in slot_0_relays:
            assert val == pytest.approx(baseline, abs=0.01), (
                f"slot-0 relay differs: {name}={val}, baseline={baseline}"
            )


# ── Behavioural properties ──────────────────────────────────────


class TestStochasticBehaviour:
    def test_pessimistic_weights_reduce_pv_reliance(self) -> None:
        """Heavily P10-weighted solve should plan less aggressive
        PV-dependent moves than P50-heavy.

        Setup: SOC at 90% (near full), expensive prices NOW, cheap PV
        coming. The optimal stage-1 move depends on whether you trust
        the PV forecast.
        """
        prices = _flat_prices(import_c=30.0, export_c=2.0)
        # Big PV upside: P10=0kW (no sun), P50=5kW, P90=10kW
        pv = _pv_forecast(p50_kw=5.0, p10_kw=0.0, p90_kw=10.0, hours_long=6)

        common = dict(
            state=_state(soc=80.0),
            prices_planning=prices,
            pv_forecast=pv,
            load_profile=_flat_profile(kw=1.0),
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )

        # Optimistic: trust PV will refill, willing to discharge now
        opt_sol = solve_stochastic(
            **common,
            scenario_weights={"p10": 0.05, "p50": 0.10, "p90": 0.85},
        )
        # Pessimistic: don't trust PV, hold the battery
        pes_sol = solve_stochastic(
            **common,
            scenario_weights={"p10": 0.85, "p50": 0.10, "p90": 0.05},
        )
        assert opt_sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        assert pes_sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)

        # Pessimistic should discharge less (more conservative)
        opt_discharge = max(0.0, -opt_sol.slot_0.battery_kw)
        pes_discharge = max(0.0, -pes_sol.slot_0.battery_kw)
        assert pes_discharge <= opt_discharge + 0.5, (
            f"pessimistic discharged {pes_discharge:.2f}kW; "
            f"optimistic discharged {opt_discharge:.2f}kW"
        )


# ── Helpers used by tests ───────────────────────────────────────


def _make_price_valley() -> list[PriceInterval]:
    """Prices: 30c flat, with a cheap valley (5c) at slot index 1 (5–10 min in)."""
    n_intervals = HORIZON_HOURS * 2
    prices: list[PriceInterval] = []
    for i in range(n_intervals):
        # Slot 1 in 5-min terms ≈ 30-min interval 0 (slots 0-5) — make slot 0–30min cheap
        if i == 0:
            prices.append(
                PriceInterval(
                    start=NOW + timedelta(minutes=30 * i),
                    end=NOW + timedelta(minutes=30 * (i + 1)),
                    import_per_kwh=5.0,
                    export_per_kwh=5.0,
                    spot_per_kwh=1.5,
                    renewables_pct=80.0,
                    spike_status="none",
                    descriptor="low",
                )
            )
        else:
            prices.append(
                PriceInterval(
                    start=NOW + timedelta(minutes=30 * i),
                    end=NOW + timedelta(minutes=30 * (i + 1)),
                    import_per_kwh=30.0,
                    export_per_kwh=5.0,
                    spot_per_kwh=9.0,
                    renewables_pct=40.0,
                    spike_status="none",
                    descriptor="neutral",
                )
            )
    return prices


# ── S4: base scenario selection ──────────────────────────────────


class TestBaseScenarioSelection:
    """`build_stochastic_lp` picks the heaviest-weighted scenario as the
    base. The base is where `dispatch_from_slot` reads the grid-vs-PV
    charge decomposition from — non-anticipativity ties the *net* kW but
    not the split. Heaviest-weighted = most likely PV outcome = right
    basis for the mode-3-vs-mode-4 dispatch decision."""

    def test_default_weights_select_p50_as_base(self) -> None:
        prob, svars = build_stochastic_lp(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(),
        )
        # Default weights: p10=0.20, p50=0.60, p90=0.20 — p50 is heaviest
        assert svars.base_scenario == "p50"

    def test_custom_weights_select_heaviest(self) -> None:
        prob, svars = build_stochastic_lp(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(),
            scenario_weights={"p10": 0.7, "p50": 0.2, "p90": 0.1},
        )
        assert svars.base_scenario == "p10"
