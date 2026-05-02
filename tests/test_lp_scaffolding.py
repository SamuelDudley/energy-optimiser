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
    BinarySignalDrivenContinuousLoad,
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


def _state(soc: float = 50.0, now: datetime = NOW) -> SystemState:
    return SystemState(
        timestamp=now,
        soc_pct=soc,
        battery_power_kw=0.0,
        pv_power_kw=0.0,
        grid_power_kw=1.0,
        house_load_kw=1.0,
        ems_mode=2,
        outdoor_temp_c=20.0,
        occupied=True,
    )


def _flat_prices(
    import_c: float = 20.0, export_c: float = 5.0, start: datetime = NOW
) -> list[PriceInterval]:
    """Build prices_planning covering the full LP horizon at 30-min cadence."""
    n_intervals = HORIZON_HOURS * 2  # 30-min
    return [
        PriceInterval(
            start=start + timedelta(minutes=30 * i),
            end=start + timedelta(minutes=30 * (i + 1)),
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

    def test_partial_last_day_below_capacity_skipped_not_infeasible(self) -> None:
        """A future iteration whose in-horizon window is too short to
        meet day_target (e.g. Amber's prices truncate the LP horizon
        partway through day 2) must skip the constraint, not return
        infeasible. Reproduces the 2026-05-02 production failure where
        day 2 had only 4 h of in-horizon window vs a 4 kWh / 0.9 kW
        target = 3.6 kWh max-achievable.
        """
        # 32 hours of prices (instead of 48). With NOW = 09:00 Apr 3
        # local, day 2's window starts at Apr 5 00:00 local = Apr 4
        # 14:00 UTC, but horizon ends at Apr 4 06:00 UTC — day 2 is
        # entirely outside the horizon. Use 42h to make day 2 partial.
        n_intervals = 42 * 2  # 30-min cadence, 42h
        prices = [
            PriceInterval(
                start=NOW + timedelta(minutes=30 * i),
                end=NOW + timedelta(minutes=30 * (i + 1)),
                import_per_kwh=20.0,
                export_per_kwh=5.0,
                spot_per_kwh=6.0,
                renewables_pct=40.0,
                spike_status="none",
                descriptor="neutral",
            )
            for i in range(n_intervals)
        ]
        cfg = _hw_cfg()  # target=4, draw=1.0, deadline=22
        sol = solve(
            state=_state(),
            prices_planning=prices,
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[_hw_status()],
            lp_loads=[BinarySignalDrivenLoad(cfg)],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), (
            f"LP must skip un-meetable partial last day, got: {sol.reason}"
        )

    def test_unmeetable_today_rolls_forward_not_infeasible(self) -> None:
        """If today's remaining window can't physically deliver the
        daily target, the LP must roll the unmet target forward to
        tomorrow rather than return infeasible. Reproduces the 2026-
        05-02 19:11 deploy failure (deployed late, only ~3 h left
        before today's 22:00 deadline → max 2.7 kWh achievable
        against a 4 kWh target → infeasible without this fix).
        """
        # 19:00 Canberra Apr 3 = 09:00 UTC. ~3 h to today's 22:00
        # deadline → today caps at 0.9 × 3 = 2.7 kWh (< 4 kWh target).
        # Tomorrow's 22 h window holds the rolled-forward 8 kWh easily.
        late_now = datetime(2026, 4, 3, 9, 0, 0, tzinfo=UTC)
        cfg = ManagedLoadConfig(
            load_id="hot_water",
            category=LoadCategory.SIGNAL_DRIVEN,
            shelly_host="test",
            has_relay=True,
            daily_target_kwh=4.0,
            draw_kw=0.9,
            deadline_hour_local=22,
        )
        sol = solve(
            state=_state(now=late_now),
            prices_planning=_flat_prices(start=late_now),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[_hw_status()],
            lp_loads=[BinarySignalDrivenLoad(cfg)],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), (
            f"LP should roll forward, not fail infeasible: {sol.reason}"
        )


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
            f"LP failed to return SOC below ceiling by end of horizon: final={final:.2f}%"
        )

    def test_initial_soc_below_floor_stays_feasible(self) -> None:
        """Mirror case: initial SOC < effective floor must also solve."""
        cfg = BatteryConfig(soc_floor_pct=15.0, soc_ceiling_pct=95.0, backup_soc_pct=15.0)
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
            f"cost={sol.expected_total_cost_cents:.0f}c — penalty not subtracted?"
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
        assert any("oven" in r.message for r in caplog.records), (
            "expected a warning naming the skipped load"
        )

    def test_build_lp_loads_signal_driven_continuous(self) -> None:
        cfg = ManagedLoadConfig(
            load_id="hot_water",
            category=LoadCategory.SIGNAL_DRIVEN_CONTINUOUS,
            shelly_host="test",
            has_relay=True,
            daily_target_kwh=4.0,
            draw_kw=1.0,
            min_on_slots=6,
            min_off_slots=4,
        )
        loads = build_lp_loads([cfg])
        assert len(loads) == 1
        assert isinstance(loads[0], BinarySignalDrivenContinuousLoad)
        assert loads[0].load_id == "hot_water"


# ── BinarySignalDrivenContinuousLoad: block constraints ──────────


def _hw_continuous_cfg(min_on: int = 6, min_off: int = 4, target: float = 4.0) -> ManagedLoadConfig:
    return ManagedLoadConfig(
        load_id="hot_water",
        category=LoadCategory.SIGNAL_DRIVEN_CONTINUOUS,
        shelly_host="test",
        has_relay=True,
        daily_target_kwh=target,
        draw_kw=1.0,
        deadline_hour_local=22,
        min_on_slots=min_on,
        min_off_slots=min_off,
    )


def _relay_runs(traj: list, draw: float = 1.0) -> list[tuple[int, int]]:
    """Return list of (start_index, length) for each contiguous on-block.

    'On' is `load_kw['hot_water'] >= draw/2` to handle LP relaxation of
    future binaries (slot 0 is a true binary; slot ≥1 may be fractional).
    """
    on = [d.load_kw.get("hot_water", 0.0) >= draw / 2 for d in traj]
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(on):
        if on[i]:
            j = i
            while j < len(on) and on[j]:
                j += 1
            runs.append((i, j - i))
            i = j
        else:
            i += 1
    return runs


class TestSignalDrivenContinuous:
    def test_constructor_requires_min_on_and_min_off(self) -> None:
        cfg = ManagedLoadConfig(
            load_id="hot_water",
            category=LoadCategory.SIGNAL_DRIVEN_CONTINUOUS,
            shelly_host="test",
            has_relay=True,
            daily_target_kwh=4.0,
            draw_kw=1.0,
            min_on_slots=None,
            min_off_slots=None,
        )
        with pytest.raises(ValueError, match="min_on_slots and min_off_slots"):
            BinarySignalDrivenContinuousLoad(cfg)

    def test_runs_in_blocks_meeting_min_on(self) -> None:
        """Each on-block in the LP plan is ≥ min_on slots wide (modulo
        the final block which may be truncated by horizon end)."""
        cfg = _hw_continuous_cfg(min_on=6, min_off=4)
        sol = solve(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[_hw_status()],
            lp_loads=[BinarySignalDrivenContinuousLoad(cfg)],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), sol.reason
        runs = _relay_runs(sol.forward_trajectory)
        assert runs, "LP must schedule at least one HW run-block to meet target"
        n_slots = len(sol.forward_trajectory)
        for idx, (start, length) in enumerate(runs):
            ends_at_horizon = (start + length) >= n_slots
            if ends_at_horizon:
                continue  # truncation by horizon end is allowed
            assert length >= cfg.min_on_slots, (
                f"on-block #{idx} at slot {start} is {length} slots, "
                f"below min_on_slots={cfg.min_on_slots}"
            )

    def test_off_gaps_meet_min_off(self) -> None:
        """Gap between two on-blocks is ≥ min_off slots wide."""
        cfg = _hw_continuous_cfg(min_on=6, min_off=4)
        sol = solve(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[_hw_status()],
            lp_loads=[BinarySignalDrivenContinuousLoad(cfg)],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), sol.reason
        runs = _relay_runs(sol.forward_trajectory)
        for prev, nxt in zip(runs, runs[1:], strict=False):
            gap = nxt[0] - (prev[0] + prev[1])
            assert gap >= cfg.min_off_slots, (
                f"off-gap between blocks {prev} and {nxt} is {gap} slots, "
                f"below min_off_slots={cfg.min_off_slots}"
            )

    def test_carryover_holds_relay_on_when_block_unfinished(self) -> None:
        """Cross-tick: relay was turned on `elapsed` ago (< min_on_slots).
        Slot 0 must be forced ON, even with the daily target already met.
        """
        cfg = _hw_continuous_cfg(min_on=6, min_off=4)
        # Daily target satisfied → LP would otherwise pick OFF
        status = ManagedLoadStatus(
            load_id="hot_water",
            category=LoadCategory.SIGNAL_DRIVEN_CONTINUOUS,
            power_kw=1.0,
            energy_today_kwh=4.0,  # already at target
            relay_on=True,
            cycle_state=None,
            relay_state_since=NOW - timedelta(minutes=10),  # 2 slots in
        )
        sol = solve(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[status],
            lp_loads=[BinarySignalDrivenContinuousLoad(cfg)],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), sol.reason
        # 10 min elapsed @ 5-min slots = 2 slots in. min_on = 6 →
        # remaining = 4. Slots 0..3 must be ON.
        for k in range(4):
            assert sol.forward_trajectory[k].load_kw.get("hot_water", 0.0) >= 0.99, (
                f"slot {k} should be forced ON by carry-over, got "
                f"{sol.forward_trajectory[k].load_kw.get('hot_water', 0.0):.2f}"
            )

    def test_carryover_holds_relay_off_when_cooldown_unfinished(self) -> None:
        """Mirror case: relay just turned off, must stay off for min_off slots."""
        cfg = _hw_continuous_cfg(min_on=6, min_off=4)
        status = ManagedLoadStatus(
            load_id="hot_water",
            category=LoadCategory.SIGNAL_DRIVEN_CONTINUOUS,
            power_kw=0.0,
            energy_today_kwh=0.0,  # daily target unmet → LP wants ON
            relay_on=False,
            cycle_state=None,
            relay_state_since=NOW - timedelta(minutes=5),  # 1 slot in
        )
        sol = solve(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[status],
            lp_loads=[BinarySignalDrivenContinuousLoad(cfg)],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), sol.reason
        # 5 min elapsed = 1 slot. min_off = 4 → remaining = 3 forced OFF.
        for k in range(3):
            assert sol.forward_trajectory[k].load_kw.get("hot_water", 0.0) <= 0.01, (
                f"slot {k} should be forced OFF by carry-over, got "
                f"{sol.forward_trajectory[k].load_kw.get('hot_water', 0.0):.2f}"
            )

    def test_carryover_releases_after_block_complete(self) -> None:
        """When elapsed ≥ min_on, no carry-over constraint — LP free
        to turn off. With daily target met, LP picks OFF at slot 0."""
        cfg = _hw_continuous_cfg(min_on=6, min_off=4)
        status = ManagedLoadStatus(
            load_id="hot_water",
            category=LoadCategory.SIGNAL_DRIVEN_CONTINUOUS,
            power_kw=1.0,
            energy_today_kwh=4.0,
            relay_on=True,
            cycle_state=None,
            relay_state_since=NOW - timedelta(minutes=60),  # 12 slots — well past
        )
        sol = solve(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[status],
            lp_loads=[BinarySignalDrivenContinuousLoad(cfg)],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), sol.reason
        assert sol.forward_trajectory[0].load_kw.get("hot_water", 0.0) <= 0.01, (
            "with target met and carry-over expired, LP should pick OFF"
        )

    def test_no_carryover_when_relay_state_since_unset(self) -> None:
        """Status with relay_state_since=None (e.g. fresh startup) → no
        carry-over constraint; LP behaves as if no prior commitment."""
        cfg = _hw_continuous_cfg(min_on=6, min_off=4)
        status = ManagedLoadStatus(
            load_id="hot_water",
            category=LoadCategory.SIGNAL_DRIVEN_CONTINUOUS,
            power_kw=0.0,
            energy_today_kwh=4.0,
            relay_on=True,  # currently on but timestamp unknown
            cycle_state=None,
            relay_state_since=None,
        )
        sol = solve(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[status],
            lp_loads=[BinarySignalDrivenContinuousLoad(cfg)],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        # Just verify it solves — without relay_state_since, no carry-over
        # binding; LP free to pick whatever fits the daily target.
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), sol.reason

    def test_schedule_off_forces_all_slots_today_to_zero(self) -> None:
        """schedule_overrides[today]='off' → relay forced to 0 every slot
        of today's local-calendar window; daily target relaxed (not
        rolled forward)."""
        # NOW = 2026-04-02 22:00 UTC = 09:00 Canberra Apr 3
        today_local = "2026-04-03"
        cfg = ManagedLoadConfig(
            load_id="hot_water",
            category=LoadCategory.SIGNAL_DRIVEN_CONTINUOUS,
            shelly_host="test",
            has_relay=True,
            daily_target_kwh=4.0,
            draw_kw=1.0,
            min_on_slots=6,
            min_off_slots=4,
            schedule_overrides={today_local: "off"},
        )
        sol = solve(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[_hw_status()],
            lp_loads=[BinarySignalDrivenContinuousLoad(cfg)],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), sol.reason
        # All slots whose local date is today must be zero.
        from optimiser.time_utils import utc_to_local

        today_slots = [
            d
            for d in sol.forward_trajectory
            if utc_to_local(d.slot_start).date().isoformat() == today_local
        ]
        assert today_slots, "expected at least one slot in today's local window"
        for d in today_slots:
            assert d.load_kw.get("hot_water", 0.0) == 0.0, (
                f"slot {d.slot_start.isoformat()} should be forced OFF, "
                f"got {d.load_kw.get('hot_water', 0.0):.2f}"
            )

    def test_schedule_on_forces_all_slots_today_to_one(self) -> None:
        """schedule_overrides[today]='on' → relay forced to 1 every slot
        of today's local-calendar window."""
        today_local = "2026-04-03"
        cfg = ManagedLoadConfig(
            load_id="hot_water",
            category=LoadCategory.SIGNAL_DRIVEN_CONTINUOUS,
            shelly_host="test",
            has_relay=True,
            daily_target_kwh=4.0,
            draw_kw=1.0,
            min_on_slots=6,
            min_off_slots=4,
            schedule_overrides={today_local: "on"},
        )
        sol = solve(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[_hw_status()],
            lp_loads=[BinarySignalDrivenContinuousLoad(cfg)],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), sol.reason
        from optimiser.time_utils import utc_to_local

        today_slots = [
            d
            for d in sol.forward_trajectory
            if utc_to_local(d.slot_start).date().isoformat() == today_local
        ]
        assert today_slots
        for d in today_slots:
            assert d.load_kw.get("hot_water", 0.0) >= 0.99, (
                f"slot {d.slot_start.isoformat()} should be forced ON, "
                f"got {d.load_kw.get('hot_water', 0.0):.2f}"
            )

    def test_schedule_off_overrides_carryover_on_block(self) -> None:
        """A live min-on block (relay just turned on) must yield to a
        same-day 'off' override — relay forced 0 immediately, not held."""
        today_local = "2026-04-03"
        cfg = ManagedLoadConfig(
            load_id="hot_water",
            category=LoadCategory.SIGNAL_DRIVEN_CONTINUOUS,
            shelly_host="test",
            has_relay=True,
            daily_target_kwh=4.0,
            draw_kw=1.0,
            min_on_slots=6,
            min_off_slots=4,
            schedule_overrides={today_local: "off"},
        )
        # Carry-over says "still in min-on block" — should be overridden.
        status = ManagedLoadStatus(
            load_id="hot_water",
            category=LoadCategory.SIGNAL_DRIVEN_CONTINUOUS,
            power_kw=1.0,
            energy_today_kwh=0.5,
            relay_on=True,
            cycle_state=None,
            relay_state_since=NOW - timedelta(minutes=5),  # 1 slot into block
        )
        sol = solve(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[status],
            lp_loads=[BinarySignalDrivenContinuousLoad(cfg)],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), sol.reason
        # Slot 0 must be OFF despite the in-progress block.
        assert sol.forward_trajectory[0].load_kw.get("hot_water", 0.0) == 0.0

    def test_schedule_overrides_invalid_state_rejected_at_parse(self) -> None:
        """An unknown state string fails fast at config parse time."""
        from optimiser.config import _parse_schedule_overrides

        with pytest.raises(ValueError, match="expected one of"):
            _parse_schedule_overrides({"2026-05-02": "paused"}, "hot_water")

    def test_schedule_overrides_invalid_date_rejected_at_parse(self) -> None:
        from optimiser.config import _parse_schedule_overrides

        with pytest.raises(ValueError, match="not a YYYY-MM-DD date"):
            _parse_schedule_overrides({"tomorrow": "off"}, "hot_water")

    def test_continuous_meets_daily_target(self) -> None:
        """Block constraints don't break the inherited daily-target sum."""
        cfg = _hw_continuous_cfg(min_on=6, min_off=4, target=4.0)
        sol = solve(
            state=_state(),
            prices_planning=_flat_prices(),
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[_hw_status()],
            lp_loads=[BinarySignalDrivenContinuousLoad(cfg)],
            battery_config=BatteryConfig(),
            timeout_s=30.0,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), sol.reason
        slot_hours = SLOT_MINUTES / 60.0
        hw_total = sum(
            d.load_kw.get("hot_water", 0.0) * slot_hours
            for d in sol.forward_trajectory
            if d.slot_start < NOW + timedelta(hours=13)  # before 22:00 deadline
        )
        assert hw_total >= 4.0 - 0.01, f"HW only delivered {hw_total:.2f} kWh"


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
            start=prices[0].start,
            end=prices[0].end,
            import_per_kwh=30.0,
            export_per_kwh=5.0,
            spot_per_kwh=9.0,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
            forecast_predicted=2.0,
        )
        sol = solve(
            state=_state(soc=30.0),
            prices_planning=prices,
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[],
            lp_loads=[],
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
            start=prices[0].start,
            end=prices[0].end,
            import_per_kwh=30.0,
            export_per_kwh=0.0,
            spot_per_kwh=9.0,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
            export_forecast_predicted=30.0,
        )
        sol = solve(
            state=_state(soc=90.0),
            prices_planning=prices,
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[],
            lp_loads=[],
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
            managed_loads=[],
            lp_loads=[],
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
            start=prices[0].start,
            end=prices[0].end,
            import_per_kwh=30.0,
            export_per_kwh=1.0,
            spot_per_kwh=9.0,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
            export_forecast_predicted=-1.0,
        )
        sol = solve(
            state=_state(soc=50.0),
            prices_planning=prices,
            pv_forecast=None,
            load_profile=_flat_profile(),
            managed_loads=[],
            lp_loads=[],
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
