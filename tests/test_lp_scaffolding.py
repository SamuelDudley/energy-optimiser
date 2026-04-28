"""Sanity checks for the LP scaffolding.

These are end-to-end smoke tests: build a problem from synthetic data,
solve it, verify basic properties (status, SOC bounds, energy balance).
The LP isn't executing in production yet — this proves the formulation
is well-posed and the solver is callable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from optimiser.config import BatteryConfig, ManagedLoadConfig
from optimiser.lp.constants import HORIZON_HOURS, SLOT_MINUTES
from optimiser.lp.loads import (
    BinarySignalDrivenLoad,
    ObservableLoad,
    build_lp_loads,
)
from optimiser.lp.result import SolveStatus
from optimiser.lp.solver import solve
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
    """Build prices_planning covering the full LP horizon at 30-min cadence."""
    n_intervals = HORIZON_HOURS * 2  # 30-min
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


# ── Smoke: it solves ─────────────────────────────────────────────


class TestLPSolves:
    def test_minimal_problem_solves(self) -> None:
        """No loads, flat prices — LP is trivially feasible.

        With flat prices and SOC > floor, the LP correctly discharges the
        battery (1c/kWh wear) over importing grid (20c/kWh), so the
        meaningful assertion is just that house load is met somehow.
        """
        sol = solve(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), sol.reason
        assert sol.slot_0 is not None
        # House gets its 1 kW from somewhere — battery, grid, or PV
        slot_0 = sol.slot_0
        supplied = (
            slot_0.pv_to_house_kw
            + slot_0.grid_import_kw
            + max(0.0, -slot_0.battery_kw)  # discharge contribution
        )
        assert supplied == pytest.approx(1.0, abs=0.05)

    def test_solves_with_signal_driven_load(self) -> None:
        """HW load present — LP must schedule enough relay-on time to
        meet the 4 kWh daily target.
        """
        cfg = _hw_cfg()
        sol = solve(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[_hw_status()],
            lp_loads=[BinarySignalDrivenLoad(cfg)],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), sol.reason
        # Sum of HW power × slot_hours across the trajectory should hit ~4 kWh
        slot_hours = SLOT_MINUTES / 60.0
        hw_total = sum(
            d.load_kw.get("hot_water", 0.0) * slot_hours
            for d in sol.forward_trajectory
            if d.slot_start < NOW + timedelta(hours=13)  # before 22:00 deadline
        )
        assert hw_total >= 4.0 - 0.01, f"HW only delivered {hw_total:.2f} kWh"


# ── Properties: SOC bounds ───────────────────────────────────────


class TestSOCBounds:
    def test_soc_stays_within_bounds(self) -> None:
        cfg = BatteryConfig(soc_floor_pct=10.0, soc_ceiling_pct=95.0)
        sol = solve(
            state=_state(soc=50.0),
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[],
            lp_loads=[],
            battery_config=cfg,
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        for d in sol.forward_trajectory:
            assert cfg.soc_floor_pct - 0.1 <= d.soc_pct_end <= cfg.soc_ceiling_pct + 0.1

    def test_initial_soc_above_ceiling_stays_feasible(self) -> None:
        """Initial SOC > ceiling (e.g. mode 2 charged past our limit) must
        not cause infeasibility. LP should solve, SOC should converge back
        into the operating band as fast as discharge physics allow.
        """
        cfg = BatteryConfig(soc_floor_pct=15.0, soc_ceiling_pct=95.0)
        sol = solve(
            state=_state(soc=100.0),  # battery is full — above ceiling
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[],
            lp_loads=[],
            battery_config=cfg,
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), sol.reason
        # The LP has planned some trajectory — either coming down or at
        # least respecting physical limits. No hard assertion on timing
        # of return-to-band (depends on discharge kW and load), but the
        # final SOC should be ≤ ceiling by end of horizon.
        final = sol.forward_trajectory[-1].soc_pct_end
        assert final <= cfg.soc_ceiling_pct + 0.5, (
            f"LP failed to return SOC below ceiling by end of horizon: "
            f"final={final:.2f}%"
        )

    def test_initial_soc_below_floor_stays_feasible(self) -> None:
        """Mirror case: initial SOC < effective floor must also solve."""
        cfg = BatteryConfig(
            soc_floor_pct=15.0, soc_ceiling_pct=95.0, backup_soc_pct=15.0
        )
        sol = solve(
            state=_state(soc=5.0),  # battery is near empty — below floor
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[],
            lp_loads=[],
            battery_config=cfg,
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), sol.reason

    def test_reported_cost_excludes_soc_slack_penalty(self) -> None:
        """§3.4: SOC_BOUND_PENALTY * slack is an internal regulariser, not
        a real grid cost. When the initial SOC forces recovery through
        the slack variables, the reported economic cost must not be
        inflated by the ~1e4 penalty coefficient.
        """
        cfg = BatteryConfig(soc_floor_pct=15.0, soc_ceiling_pct=95.0)
        # Initial SOC well above ceiling — forces multiple slots of
        # soc_over_ceiling slack activation during recovery.
        sol = solve(
            state=_state(soc=100.0),
            prices_planning=_flat_prices(),  # 20c import / 5c export
            pv_forecast=None,
            load_profile=_flat_profile(),  # 1 kW flat load
            managed_loads=[],
            lp_loads=[],
            battery_config=cfg,
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), sol.reason
        # Economic-cost sanity bound: a 48h horizon at 1 kW average load,
        # 20c import, ~5c export sits well under 2000c even in the worst
        # arbitrage. The raw (penalty-inflated) objective would be 1e5+.
        assert sol.expected_total_cost_cents < 2000.0, (
            f"cost={sol.expected_total_cost_cents:.0f}c — penalty not "
            "subtracted?"
        )


# ── Properties: economic behaviour ───────────────────────────────


class TestEconomicBehaviour:
    def test_charges_during_cheap_window(self) -> None:
        """Sharp price valley (5c) inside an otherwise expensive day (30c).
        LP should charge the battery during the cheap window.
        """
        # 24 intervals of 30c, then 4 intervals (2h) of 5c, then 24h of 30c
        prices = _flat_prices(import_c=30.0)
        for i in range(24, 28):
            old = prices[i]
            prices[i] = PriceInterval(
                start=old.start,
                end=old.end,
                import_per_kwh=5.0,
                export_per_kwh=5.0,
                spot_per_kwh=1.5,
                renewables_pct=80.0,
                spike_status="none",
                descriptor="low",
            )
        sol = solve(
            state=_state(soc=30.0),  # plenty of headroom to charge
            prices_planning=prices,
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        # Total grid charging over the cheap window (slots covering 12h–14h)
        slot_hours = SLOT_MINUTES / 60.0
        cheap_charge = sum(
            d.battery_kw * slot_hours
            for d in sol.forward_trajectory
            if (NOW + timedelta(hours=12) <= d.slot_start < NOW + timedelta(hours=14))
            and d.battery_kw > 0
        )
        # Should have charged at least a few kWh during the cheap window
        assert cheap_charge > 5.0, f"only charged {cheap_charge:.1f} kWh in cheap window"

    def test_curtail_penalty_prefers_charge_over_curtail_in_flat_pricing(
        self,
    ) -> None:
        """Without a curtail penalty, the LP charges wear-cost (2.5c/kWh)
        while curtailing is free — on a flat-priced midday it would
        rather throw surplus PV away than store it. The penalty
        (PV_CURTAIL_PENALTY_PER_KWH = 1c/kWh) flips the tie so the LP
        prefers charging up to the soft ceiling.

        Setup: flat 20c import, 1c export (positive but tiny), 5kW PV
        for 4h, 1kW house load, battery starts at 60%. The 4h surplus
        produces ~16 kWh of PV-side energy beyond the export cap; the
        LP could absorb most of it into the battery up to the ceiling
        before falling back on curtail.
        """
        prices = _flat_prices(import_c=20.0, export_c=1.0)
        pv_forecast = [
            PVForecast(
                start=NOW,
                end=NOW + timedelta(hours=4),
                pv_estimate_kw=5.0,
                pv_estimate10_kw=4.0,
                pv_estimate90_kw=6.0,
            ),
        ]
        sol = solve(
            state=_state(soc=60.0),
            prices_planning=prices,
            pv_forecast=pv_forecast,
            load_profile=_flat_profile(kw=1.0),
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        slot_hours = SLOT_MINUTES / 60.0
        pv_to_bat = sum(
            d.pv_to_battery_kw * slot_hours
            for d in sol.forward_trajectory
            if NOW <= d.slot_start < NOW + timedelta(hours=4)
        )
        # Under the curtail penalty the LP should prefer charge over
        # curtail for any kWh whose stored value is non-negative.
        # Expect at least a couple kWh of PV → battery during the window.
        assert pv_to_bat > 2.0, (
            f"with curtail penalty, expected meaningful pv→battery "
            f"during flat-priced PV surplus; got {pv_to_bat:.2f} kWh"
        )

    def test_5min_overlay_lets_lp_see_intra_30min_negative_spike(
        self,
    ) -> None:
        """Service merges 5-min over 30-min so `_price_at` finds 5-min
        first within the overlapping window. A 5-min negative-import
        spike inside a flat-priced 30-min interval should drive the LP
        to grid-charge during just those 5-min slots.

        Slot timing: NOW is on a 30-min boundary; the spike window is
        slots 1–3 (covering NOW+5..NOW+20).
        """
        # Baseline 30-min prices: flat 20c import, 5c export
        prices_30min = _flat_prices(import_c=20.0, export_c=5.0)
        # 5-min "current and next-30min" cover, with a negative spike
        # (e.g. -10c import) over slots 1..3
        prices_5min: list[PriceInterval] = []
        for i in range(12):  # 12 × 5 min = 60 min ahead
            spike = 1 <= i <= 3
            prices_5min.append(
                PriceInterval(
                    start=NOW + timedelta(minutes=5 * i),
                    end=NOW + timedelta(minutes=5 * (i + 1)),
                    import_per_kwh=(-10.0 if spike else 20.0),
                    export_per_kwh=5.0,
                    spot_per_kwh=5.0,
                    renewables_pct=40.0,
                    spike_status="none",
                    descriptor="neutral" if not spike else "extremelyLow",
                )
            )
        # Merge in the same order service.py does: 5-min first
        merged = prices_5min + prices_30min

        sol = solve(
            state=_state(soc=50.0),
            prices_planning=merged,
            pv_forecast=None,  # no PV — isolate the price effect
            load_profile=_flat_profile(kw=1.0),
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)

        # Spike slots (1..3) should show grid-charging; non-spike slots
        # in the same first-hour window should not.
        spike_bat = [d.battery_kw for d in sol.forward_trajectory[1:4]]
        flat_bat = [d.battery_kw for d in sol.forward_trajectory[5:8]]
        assert all(b > 0.5 for b in spike_bat), (
            f"expected grid-charge during -10c spike slots; got {spike_bat}"
        )
        assert all(b < 0.5 for b in flat_bat), (
            f"expected no charging in flat 20c slots after spike; got {flat_bat}"
        )

    def test_negative_import_tie_break_prefers_charging(self) -> None:
        """Symmetric counterpart to the negative-export tie-break. At
        ip = 0 the LP is indifferent between idle and grid-charge once
        wear-cost is overcome by future use; with the import reward
        active, the LP deterministically prefers to charge from a free
        grid into the battery rather than discharge-into-free-import.

        Setup: 4h of zero-priced import inside a flat 20c day. SOC
        starts at 30% so charging has clear future use; the test checks
        that grid-side battery charge during the free window strictly
        exceeds discharge.
        """
        # Flat 20c import, 5c export — typical day. Insert a 4h ip=0
        # window starting at hour 6.
        prices = _flat_prices(import_c=20.0, export_c=5.0)
        for i in range(12, 20):  # 8 × 30-min = 4h, indices 12..19
            old = prices[i]
            prices[i] = PriceInterval(
                start=old.start,
                end=old.end,
                import_per_kwh=0.0,
                export_per_kwh=5.0,
                spot_per_kwh=0.0,
                renewables_pct=80.0,
                spike_status="none",
                descriptor="free",
            )
        sol = solve(
            state=_state(soc=30.0),
            prices_planning=prices,
            pv_forecast=None,
            load_profile=_flat_profile(kw=1.0),
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        slot_hours = SLOT_MINUTES / 60.0
        free_window_start = NOW + timedelta(hours=6)
        free_window_end = NOW + timedelta(hours=10)
        grid_charge = sum(
            d.grid_to_battery_kw * slot_hours
            for d in sol.forward_trajectory
            if free_window_start <= d.slot_start < free_window_end
        )
        assert grid_charge > 1.0, (
            f"with ip=0 the LP should soak free grid into battery; "
            f"only saw {grid_charge:.2f} kWh of grid_to_battery"
        )

    def test_negative_export_drives_pv_to_battery(self) -> None:
        """Negative export price + PV available → LP should soak PV into
        battery rather than export it.
        """
        prices = _flat_prices(import_c=20.0, export_c=-3.0)  # negative export
        # Provide some PV
        pv_forecast = [
            PVForecast(
                start=NOW,
                end=NOW + timedelta(hours=4),
                pv_estimate_kw=5.0,
                pv_estimate10_kw=4.0,
                pv_estimate90_kw=6.0,
            ),
        ]
        sol = solve(
            state=_state(soc=40.0),
            prices_planning=prices,
            pv_forecast=pv_forecast,
            load_profile=_flat_profile(kw=1.0),
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        # During the PV window: export should be tiny, pv_to_battery should be high
        slot_hours = SLOT_MINUTES / 60.0
        pv_to_bat = sum(
            d.pv_to_battery_kw * slot_hours
            for d in sol.forward_trajectory
            if NOW <= d.slot_start < NOW + timedelta(hours=4)
        )
        pv_to_export = sum(
            d.pv_to_export_kw * slot_hours
            for d in sol.forward_trajectory
            if NOW <= d.slot_start < NOW + timedelta(hours=4)
        )
        assert pv_to_bat > pv_to_export, (
            f"with negative export, expected pv→battery ({pv_to_bat:.1f}) > "
            f"pv→export ({pv_to_export:.1f})"
        )


# ── Factory ──────────────────────────────────────────────────────


class TestLoadFactory:
    def test_build_lp_loads_signal_driven(self) -> None:
        loads = build_lp_loads([_hw_cfg()])
        assert len(loads) == 1
        assert isinstance(loads[0], BinarySignalDrivenLoad)
        assert loads[0].load_id == "hot_water"

    def test_build_lp_loads_observable(self) -> None:
        cfg = ManagedLoadConfig(
            load_id="mains",
            category=LoadCategory.OBSERVABLE,
            shelly_host="test",
        )
        loads = build_lp_loads([cfg])
        assert len(loads) == 1
        assert isinstance(loads[0], ObservableLoad)

    def test_unknown_category_skipped(self, caplog) -> None:
        cfg = ManagedLoadConfig(
            load_id="oven",
            category=LoadCategory.PRECONDITIONABLE,
            shelly_host="test",
        )
        with caplog.at_level("WARNING", logger="optimiser.lp.loads"):
            loads = build_lp_loads([cfg])
        assert loads == []
        assert any("oven" in r.message for r in caplog.records), \
            "expected a warning naming the skipped load"


# ── Horizon truncation + terminal SOC ────────────────────────────


class TestHorizonTruncationAndTerminalSOC:
    """S1 fixes: LP horizon follows priced coverage, terminal SOC backstop."""

    def test_horizon_truncates_to_priced_coverage(self) -> None:
        """LP is given 12h of prices; horizon must shrink from 48h → 12h
        rather than extrapolating the last price."""
        from optimiser.lp.formulation import build_lp

        short_prices = _flat_prices()[:24]  # 12h of 30-min intervals
        prob, lpvars = build_lp(
            state=_state(soc=50.0),
            prices_planning=short_prices,
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(),
        )
        # 12h / 5min = 144 slots
        assert len(lpvars.slots) == 144
        # Last slot must fall strictly before the last price interval's end
        assert lpvars.slots[-1] < short_prices[-1].end

    def test_terminal_soc_floor_respected_when_discharge_is_cheap(self) -> None:
        """Cheap evening peak across the whole horizon + high SOC start —
        LP wants to discharge aggressively, but must hold terminal floor."""
        from optimiser.lp.constants import TERMINAL_SOC_FLOOR_PCT

        sol = solve(
            state=_state(soc=80.0),
            prices_planning=_flat_prices(
                import_c=100.0, export_c=80.0
            ),  # always lucrative to export
            pv_forecast=None,
            load_profile=_flat_profile(kw=0.0),  # no house load to buffer against
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(soc_floor_pct=10.0),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        terminal_soc = sol.forward_trajectory[-1].soc_pct_end
        # Allow a small solver tolerance below the floor
        assert terminal_soc >= TERMINAL_SOC_FLOOR_PCT - 0.1, (
            f"terminal SOC {terminal_soc:.2f} below floor {TERMINAL_SOC_FLOOR_PCT}"
        )

    def test_build_lp_rejects_empty_prices(self) -> None:
        """Empty prices must raise — the caller handles 'no prices' by
        routing to SELF_CONSUME, never by calling build_lp."""
        from optimiser.lp.formulation import build_lp

        with pytest.raises(ValueError, match="non-empty prices_planning"):
            build_lp(
                state=_state(),
                prices_planning=[],
                pv_forecast=None,
                load_profile=_flat_profile(),
                managed_loads=[],
                lp_loads=[],
                battery_config=BatteryConfig(),
            )

    def test_price_at_raises_out_of_range(self) -> None:
        """Direct probe of the helper: slots past the forecast must raise,
        not silently return prices[-1]."""
        from optimiser.lp.formulation import _price_at

        prices = _flat_prices()[:4]  # 2h coverage
        # 3h past NOW is past coverage
        with pytest.raises(ValueError, match="No price interval covers"):
            _price_at(prices, NOW + timedelta(hours=3))

    def test_house_load_at_raises_on_misshaped_profile(self) -> None:
        """B1: the 48-slot contract is enforced. A bad profile raises
        rather than silently returning a magic default."""
        from optimiser.lp.formulation import _house_load_at

        # NOW → slot index 36 (18:00 local). Use a 10-slot profile so
        # slot 36 is out of range.
        bad = LoadProfile(slots=[1.0] * 10, maturity_level=0, context="bad")
        with pytest.raises(ValueError, match="expected 48"):
            _house_load_at(bad, NOW)

    def test_lp_uses_forecast_predicted_over_perkwh(self) -> None:
        """When Amber supplies advancedPrice.predicted, the LP should
        prefer it over `perKwh` for the cost objective. Amber explicitly
        recommends `predicted` for forecasting.

        Scenario: slot 0 has perKwh=100c but predicted=5c — predicted is
        dramatically cheaper. The LP should charge aggressively at slot 0
        despite the high perKwh, because the cost term is driven by
        predicted."""
        prices = _flat_prices(import_c=30.0)
        # Override slot 0: perKwh stays 30, but predicted is 2 (very cheap)
        prices[0] = PriceInterval(
            start=prices[0].start, end=prices[0].end,
            import_per_kwh=30.0, export_per_kwh=5.0,
            spot_per_kwh=9.0, renewables_pct=40.0,
            spike_status="none", descriptor="neutral",
            forecast_predicted=2.0,
        )
        sol = solve(
            state=_state(soc=30.0),
            prices_planning=prices,
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[], lp_loads=[],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        # Should charge heavily at slot 0 because it's "really" 2c
        assert sol.slot_0.battery_kw > 2.0, (
            f"expected strong charge at slot 0 (predicted=2c), got {sol.slot_0.battery_kw:.2f}"
        )

    def test_lp_uses_export_forecast_predicted_over_export_per_kwh(self) -> None:
        """Symmetric with the import-side test: when Amber's feedIn
        channel supplies advancedPrice.predicted, the LP must prefer
        export_forecast_predicted over export_per_kwh in the cost
        objective.

        Scenario: SOC starts high so the LP has battery to discharge,
        all slots have export_per_kwh=0 (boring) EXCEPT slot 0 has
        export_forecast_predicted=30 (highly profitable). Expectation:
        LP discharges/exports at slot 0 to capture the predicted-only
        revenue. This proves the resolver is reading the new field.
        """
        prices = _flat_prices(import_c=30.0, export_c=0.0)
        prices[0] = PriceInterval(
            start=prices[0].start, end=prices[0].end,
            import_per_kwh=30.0, export_per_kwh=0.0,
            spot_per_kwh=9.0, renewables_pct=40.0,
            spike_status="none", descriptor="neutral",
            export_forecast_predicted=30.0,
        )
        sol = solve(
            state=_state(soc=90.0),
            prices_planning=prices,
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[], lp_loads=[],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        # Negative battery_kw = discharging. Predicted-30c export should
        # be far above wear (2.5c) so the LP exports.
        assert sol.slot_0.battery_kw < -1.0, (
            "expected discharge into the predicted-30c export at slot 0, "
            f"got battery_kw={sol.slot_0.battery_kw:.2f}"
        )
        assert sol.slot_0.grid_export_kw > 0.5, (
            f"expected export at slot 0, got {sol.slot_0.grid_export_kw:.2f}"
        )

    def test_lp_falls_back_to_export_per_kwh_when_predicted_none(self) -> None:
        """When export_forecast_predicted is None (settled intervals or
        feedIn channel without advancedPrice), the LP must fall back to
        export_per_kwh. Mirror of the import-side fallback path.

        Scenario: same shape as the previous test, but predicted is None
        and export_per_kwh is 0. With nothing to gain, the LP should
        idle the battery (or at least not aggressively discharge).
        """
        prices = _flat_prices(import_c=30.0, export_c=0.0)
        # Default — no export_forecast_predicted set, so it's None.
        sol = solve(
            state=_state(soc=90.0),
            prices_planning=prices,
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[], lp_loads=[],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        # No incentive to discharge to grid — and wear cost > 0 export
        # revenue. Slot 0 should not push battery into export.
        assert sol.slot_0.grid_export_kw < 0.5, (
            "expected near-zero export when both export_per_kwh and "
            f"predicted are 0/None, got {sol.slot_0.grid_export_kw:.2f}"
        )

    def test_export_tie_break_keys_off_resolved_ep(self) -> None:
        """The EXPORT_TIE_BREAK_PENALTY (in `lp/constants.py`) fires at
        `ep ≤ 0`. The boundary must key off the *resolved* `ep`
        (predicted-or-export_per_kwh), not raw export_per_kwh.

        Scenario: export_per_kwh = +1c (above the boundary, no penalty
        if it were the resolved value) but export_forecast_predicted =
        -1c (below the boundary, tie-break should fire). With SOC high
        and PV present we'd otherwise expect the LP to export to the
        positive raw price; the tie-break penalty plus the negative
        predicted should bias toward storing instead.
        """
        prices = _flat_prices(import_c=30.0, export_c=1.0)
        prices[0] = PriceInterval(
            start=prices[0].start, end=prices[0].end,
            import_per_kwh=30.0, export_per_kwh=1.0,
            spot_per_kwh=9.0, renewables_pct=40.0,
            spike_status="none", descriptor="neutral",
            export_forecast_predicted=-1.0,
        )
        sol = solve(
            state=_state(soc=50.0),
            prices_planning=prices,
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[], lp_loads=[],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        # Negative predicted means exporting costs the customer; the LP
        # must not discharge the battery into a loss-making export at
        # slot 0. Allow tiny rounding (< 0.05 kW).
        assert sol.slot_0.grid_export_kw < 0.05, (
            "battery should not discharge into negative-predicted export "
            f"at slot 0, got grid_export={sol.slot_0.grid_export_kw:.3f}"
        )
