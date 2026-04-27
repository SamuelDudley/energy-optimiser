"""LP property tests: assert the optimiser makes sensible decisions.

These replace the old greedy planner tests (`TestSpikeProtection`,
`TestPriceArbitrage`, etc.) with LP-native assertions. Each test builds
a specific market scenario, runs a real HiGHS solve, and checks the
slot-0 decision against expected economic behaviour.

Uses the deterministic `solve` (not stochastic) for speed — the
properties should hold regardless of PV scenario weighting. Battery-only
(no managed loads) to isolate battery behaviour.

Each test runs a real solve (~100-200ms). Total suite: ~2s.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from optimiser.config import BatteryConfig
from optimiser.lp.constants import HORIZON_HOURS
from optimiser.lp.dispatch import DispatchKind, dispatch_from_slot
from optimiser.lp.result import SolveStatus
from optimiser.lp.solver import solve
from optimiser.types import LoadProfile, PriceInterval, PVForecast, SystemState

UTC = UTC
NOW = datetime(2026, 4, 3, 7, 0, 0, tzinfo=UTC)  # 18:00 Canberra (evening)
N_INTERVALS = HORIZON_HOURS * 2  # 30-min planning intervals


# ── Helpers ──────────────────────────────────────────────────────


def _state(soc: float = 50.0, pv_kw: float = 0.0) -> SystemState:
    return SystemState(
        timestamp=NOW,
        soc_pct=soc,
        battery_power_kw=0.0,
        pv_power_kw=pv_kw,
        grid_power_kw=1.0,
        house_load_kw=1.0,
        ems_mode=2,
        outdoor_temp_c=20.0,
        occupied=True,
    )


def _prices(
    slot_0_import: float = 20.0,
    rest_import: float = 20.0,
    export: float = 5.0,
) -> list[PriceInterval]:
    """First slot at `slot_0_import`, remainder at `rest_import`."""
    intervals = []
    for i in range(N_INTERVALS):
        imp = slot_0_import if i == 0 else rest_import
        intervals.append(
            PriceInterval(
                start=NOW + timedelta(minutes=30 * i),
                end=NOW + timedelta(minutes=30 * (i + 1)),
                import_per_kwh=imp,
                export_per_kwh=export,
                spot_per_kwh=imp * 0.3,
                renewables_pct=40.0,
                spike_status="none",
                descriptor="neutral",
            )
        )
    return intervals


def _varying_prices(
    imports: list[float],
    export: float = 5.0,
) -> list[PriceInterval]:
    """Build prices from a list, padding with the last value to fill horizon."""
    padded = imports + [imports[-1]] * (N_INTERVALS - len(imports))
    return [
        PriceInterval(
            start=NOW + timedelta(minutes=30 * i),
            end=NOW + timedelta(minutes=30 * (i + 1)),
            import_per_kwh=padded[i],
            export_per_kwh=export,
            spot_per_kwh=padded[i] * 0.3,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
        )
        for i in range(N_INTERVALS)
    ]


def _pv_forecast(kw: float) -> list[PVForecast]:
    """Flat PV forecast across the full horizon."""
    return [
        PVForecast(
            start=NOW + timedelta(minutes=30 * i),
            end=NOW + timedelta(minutes=30 * (i + 1)),
            pv_estimate_kw=kw,
            pv_estimate10_kw=kw * 0.7,
            pv_estimate90_kw=kw * 1.3,
        )
        for i in range(N_INTERVALS)
    ]


def _profile(kw: float = 1.0) -> LoadProfile:
    return LoadProfile(slots=[kw] * 48, maturity_level=0, context="lp-prop")


def _solve(
    soc: float = 50.0,
    prices: list[PriceInterval] | None = None,
    pv: list[PVForecast] | None = None,
    load_kw: float = 1.0,
    pv_kw: float = 0.0,
    battery_config: BatteryConfig | None = None,
):
    """Convenience: build + solve, return (solution, dispatch)."""
    sol = solve(
        state=_state(soc=soc, pv_kw=pv_kw),
        prices_planning=prices or _prices(),
        pv_forecast=pv,
        load_profile=_profile(kw=load_kw),
        managed_loads=[],
        lp_loads=[],
        battery_config=battery_config or BatteryConfig(),
    )
    assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), (
        f"LP failed: {sol.status.value} — {sol.reason}"
    )
    assert sol.slot_0 is not None
    return sol, dispatch_from_slot(
        sol.slot_0, battery_config or BatteryConfig(), current_soc_pct=soc
    )


# ── Charge behaviour ─────────────────────────────────────────────


class TestLPChargeDecisions:
    def test_charges_during_cheap_slot(self) -> None:
        """Current slot is cheap (5c), future is expensive (40c). LP should
        charge now to discharge later — the spread covers round-trip loss."""
        sol, disp = _solve(
            soc=30.0,
            prices=_prices(slot_0_import=5.0, rest_import=40.0),
        )
        assert disp.kind == DispatchKind.CHARGE
        assert sol.slot_0.battery_kw > 0.5  # nontrivial charge

    def test_charges_on_negative_import(self) -> None:
        """Negative import price — grid is paying us to consume. LP should
        charge aggressively regardless of future prices."""
        sol, disp = _solve(
            soc=30.0,
            prices=_prices(slot_0_import=-5.0, rest_import=10.0),
        )
        assert disp.kind == DispatchKind.CHARGE
        assert sol.slot_0.battery_kw > 1.0  # aggressive, not timid

    def test_does_not_charge_above_ceiling(self) -> None:
        """SOC at ceiling — LP should not charge even if price is cheap."""
        cfg = BatteryConfig(soc_ceiling_pct=90.0)
        sol, disp = _solve(
            soc=90.0,
            prices=_prices(slot_0_import=2.0, rest_import=40.0),
            battery_config=cfg,
        )
        # At ceiling: battery_kw should be ≤ 0 (discharge or idle)
        assert sol.slot_0.battery_kw <= 0.1

    def test_charges_from_pv_when_surplus(self) -> None:
        """PV exceeds house load during day, expensive evening imminent.
        LP should store PV surplus now because there isn't enough PV time
        remaining to defer charging and still fill up before the peak.

        Note: at wear=5c (one-way) the round-trip cost is 10c, so the
        peak-vs-now spread must comfortably exceed 10c to justify
        storing PV instead of exporting it. Earlier the spread was
        50c-vs-5c which clears at wear=2.5 but not 5; this test now
        uses a 80c peak which clears comfortably under both regimes
        (preserving the original test intent — "LP stores PV when peak
        is imminent")."""
        pv = []
        for i in range(N_INTERVALS):
            kw = 5.0 if i < 8 else 0.0
            pv.append(
                PVForecast(
                    start=NOW + timedelta(minutes=30 * i),
                    end=NOW + timedelta(minutes=30 * (i + 1)),
                    pv_estimate_kw=kw,
                    pv_estimate10_kw=kw * 0.7,
                    pv_estimate90_kw=kw * 1.3,
                )
            )
        # 15c during PV, 80c peak starts at interval 8 — wide enough
        # spread that storing PV beats exporting it even at wear=5c.
        prices = _varying_prices(
            [15.0] * 8 + [80.0] * 10 + [15.0] * 20,
            export=5.0,
        )
        sol, disp = _solve(
            soc=20.0,
            pv_kw=5.0,
            pv=pv,
            load_kw=1.0,
            prices=prices,
        )
        # Total PV-to-battery across the 4h PV window (48 5-min slots).
        # The exact slot in which the LP charges is a HiGHS tie-break;
        # what matters is that it stores PV at all rather than
        # exporting it through the cheap window then importing at the
        # peak. (Earlier this test asserted slot 0 specifically — that
        # held under wear=2.5c by tie-break luck. Under wear=5c the
        # LP slightly prefers deferring within the window.)
        pv_window_charge_kwh = sum(
            s.pv_to_battery_kw for s in sol.forward_trajectory[:48]
        ) * (5 / 60)
        assert pv_window_charge_kwh > 2.0, (
            f"LP should store PV during cheap window for upcoming peak — "
            f"got {pv_window_charge_kwh:.2f} kWh stored"
        )


# ── Discharge behaviour ──────────────────────────────────────────


class TestLPDischargeDecisions:
    def test_discharges_during_expensive_slot(self) -> None:
        """Current slot is expensive (50c), future is cheap (10c). LP should
        discharge now — it can refill cheaply later."""
        sol, disp = _solve(
            soc=70.0,
            prices=_prices(slot_0_import=50.0, rest_import=10.0),
        )
        assert disp.kind == DispatchKind.DISCHARGE
        assert sol.slot_0.battery_kw < -0.5

    def test_holds_at_floor_under_import_spike(self) -> None:
        """Hard floor: starting AT the floor with a massive import spike,
        the LP must not plan a discharge that drops SOC below the floor.
        Replaces the panic-buy regression seen on 2026-04-25 (LP would
        empty the pack overnight chasing peak prices) without bringing
        back the 1e4 floor penalty that forced grid-charging recovery."""
        cfg = BatteryConfig(soc_floor_pct=10.0)
        sol, disp = _solve(
            soc=10.0,
            prices=_prices(slot_0_import=80.0, rest_import=10.0),
            battery_config=cfg,
        )
        # Slot 0: cannot discharge below the floor.
        assert sol.slot_0.battery_kw >= -0.01
        # And no slack penalty drove panic-buy grid-charging either.
        assert sol.slot_0.grid_to_battery_kw < 0.01

    def test_does_not_grid_charge_to_recover_floor(self) -> None:
        """Sub-floor entry (e.g. SOC=8% with floor=15%): LP should not
        grid-charge purely to recover the floor — flat prices give it
        no economic reason. The hard floor is clamped to state.soc_pct
        for feasibility, and there's no slack penalty pushing recovery."""
        cfg = BatteryConfig(soc_floor_pct=15.0)
        sol, disp = _solve(
            soc=8.0,
            prices=_prices(slot_0_import=20.0, rest_import=20.0),
            battery_config=cfg,
        )
        # Flat 20c prices → cycling loses on wear (~7.8c break-even).
        # No grid-charging back to floor.
        assert sol.slot_0.grid_to_battery_kw < 0.01
        # And no discharge below the (clamped) sub-floor SOC.
        assert sol.forward_trajectory[0].soc_pct_end >= 8.0 - 0.1

    def test_spike_price_triggers_discharge(self) -> None:
        """Very expensive current slot (100c) with cheap future (15c).
        LP should discharge to offset house load rather than importing."""
        sol, disp = _solve(
            soc=60.0,
            prices=_prices(slot_0_import=100.0, rest_import=15.0),
        )
        assert disp.kind == DispatchKind.DISCHARGE
        # Discharges at house load to avoid 100c import — may not exceed
        # house load since export is only 5c
        assert sol.slot_0.battery_kw < -0.5
        assert sol.slot_0.grid_import_kw < 0.1  # avoided the expensive import


# ── Arbitrage spread ─────────────────────────────────────────────


class TestLPArbitrageSpread:
    def test_flat_prices_no_cycling(self) -> None:
        """When all prices are the same and the battery starts at the
        terminal-SOC floor, the LP shouldn't charge — no price spread to
        exploit, wear cost makes every cycle a net loss, and the terminal
        SOC constraint is already satisfied so there's no forced top-up.

        (Starting below the terminal floor would force a slow top-up to
        meet the end-of-horizon constraint; that's correct behaviour but
        obscures this test's intent.)"""
        from optimiser.lp.constants import TERMINAL_SOC_FLOOR_PCT

        cfg = BatteryConfig(soc_floor_pct=10.0)
        sol, disp = _solve(
            soc=TERMINAL_SOC_FLOOR_PCT,  # at terminal floor; no forced top-up
            prices=_prices(slot_0_import=20.0, rest_import=20.0),
            battery_config=cfg,
        )
        # Should import for house load, not charge the battery
        assert sol.slot_0.battery_kw < 0.1  # no charging
        assert sol.slot_0.grid_import_kw > 0.5  # importing for house

    def test_small_spread_not_worth_cycling(self) -> None:
        """Spread exists but is too small to cover round-trip efficiency loss
        + wear cost. LP should hold."""
        # 20c now, 22c later: 2c spread. At W=5.0 c/kWh one-way + 90% eff,
        # break-even is ~13.3c — 2c falls well short.
        sol, disp = _solve(
            soc=50.0,
            prices=_prices(slot_0_import=20.0, rest_import=22.0),
        )
        assert abs(sol.slot_0.battery_kw) < 0.5

    def test_large_spread_triggers_charge(self) -> None:
        """5c now, 40c later: massive spread easily covers round-trip loss.
        LP should charge."""
        sol, disp = _solve(
            soc=30.0,
            prices=_prices(slot_0_import=5.0, rest_import=40.0),
        )
        assert disp.kind == DispatchKind.CHARGE


# ── Export behaviour ─────────────────────────────────────────────


class TestLPExportBehaviour:
    def test_export_capped_at_5kw(self) -> None:
        """Even with high PV and discharge, grid export must not exceed 5kW.
        This is a hard constraint from the DNSP."""
        sol, disp = _solve(
            soc=80.0,
            pv_kw=10.0,
            pv=_pv_forecast(10.0),
            load_kw=1.0,
            # High export price to incentivise maximum export
            prices=_prices(slot_0_import=5.0, rest_import=5.0, export=50.0),
        )
        assert sol.slot_0.grid_export_kw <= 5.01  # float tolerance

    def test_positive_export_pins_register_to_dnsp_cap(self) -> None:
        """When the LP plans any positive export, the register-40038 cap
        written to the inverter is the DNSP max (battery_config.export_limit_kw),
        not the LP's point-estimate. Leaving it at the point estimate meant
        transient PV above the plan was silently throttled by the inverter's
        MPPT — solar-curtailment-at-midday bug (2026-04-22)."""
        sol, _ = _solve(
            soc=80.0,
            pv_kw=10.0,
            pv=_pv_forecast(10.0),
            load_kw=1.0,
            prices=_prices(slot_0_import=5.0, rest_import=5.0, export=50.0),
        )
        # LP planned some positive export in slot 0
        assert sol.slot_0.grid_export_kw > 0.1
        # Register write equals the DNSP hard cap, not the plan
        assert sol.grid_export_limit_kw == BatteryConfig().export_limit_kw

    def test_zero_planned_export_writes_zero_cap(self) -> None:
        """Symmetric check: when the LP wants zero export (negative price),
        the register is pinned to 0 so the inverter doesn't export on
        transient PV windfall we'd pay for."""
        # Copy of test_negative_export_price_curtails' setup — short PV
        # window, negative export price — but asserts the register side.
        pv = []
        for i in range(N_INTERVALS):
            kw = 8.0 if i < 6 else 0.0
            pv.append(
                PVForecast(
                    start=NOW + timedelta(minutes=30 * i),
                    end=NOW + timedelta(minutes=30 * (i + 1)),
                    pv_estimate_kw=kw,
                    pv_estimate10_kw=kw * 0.7,
                    pv_estimate90_kw=kw * 1.3,
                )
            )
        sol, _ = _solve(
            soc=20.0,
            pv_kw=8.0,
            pv=pv,
            load_kw=1.0,
            prices=_varying_prices([20.0] * 6 + [50.0] * 10 + [20.0] * 20, export=-5.0),
        )
        assert sol.slot_0.grid_export_kw < 0.1
        assert sol.grid_export_limit_kw == 0.0

    def test_exports_over_curtail_at_low_positive_export_price(self) -> None:
        """When the plan saturates the battery (filling to ~ceiling) and PV
        still spills past house+battery, the LP must allocate the spill to
        grid_export rather than pv_curtailed at any positive ep — even a
        token 1c. Curtail carries a 1c penalty; export at 1c earns 1c.
        Net gap is 2c/kWh in favour of export, regardless of wear cost
        (wear is on the charge path, not on direct PV→grid).

        Failure mode this guards against: an objective change that
        accidentally makes curtail cheaper than export at marginal ep.
        """
        # 8 kW PV for first 16 slots (8h × 8 kW = 64 kWh) — comfortably
        # more than battery headroom (40 kWh × 0.7 = 28 kWh) plus 8h of
        # 1 kW house. Battery WILL saturate; remainder must go somewhere.
        pv = []
        for i in range(N_INTERVALS):
            kw = 8.0 if i < 16 else 0.0
            pv.append(
                PVForecast(
                    start=NOW + timedelta(minutes=30 * i),
                    end=NOW + timedelta(minutes=30 * (i + 1)),
                    pv_estimate_kw=kw,
                    pv_estimate10_kw=kw * 0.7,
                    pv_estimate90_kw=kw * 1.3,
                )
            )
        # ep=1c flat across the whole horizon. No future peak — there is
        # no economic reason to store-for-later. The LP's choice for the
        # forced overflow is purely export-vs-curtail.
        prices = _varying_prices([20.0] * N_INTERVALS, export=1.0)
        sol, _ = _solve(
            soc=30.0,
            pv_kw=8.0,
            pv=pv,
            load_kw=1.0,
            prices=prices,
        )
        # 5-min slot width — forward_trajectory is 5-min granularity even
        # though the inputs were 30-min. PV forecast covers slots [0, 96)
        # at 30-min ⇒ trajectory slots [0, 576) at 5-min. PV is on for
        # the first 16×6 = 96 slots.
        dt_h = 5 / 60
        pv_window = sol.forward_trajectory[: 16 * 6]
        # Energy balance: curtailed = PV_total − (to_house + to_bat + to_export).
        total_pv_in = 8.0 * 8.0  # 8 kW × 8 h
        used_kwh = sum(
            (s.pv_to_house_kw + s.pv_to_battery_kw + s.pv_to_export_kw) * dt_h
            for s in pv_window
        )
        curtailed_kwh = max(0.0, total_pv_in - used_kwh)
        exported_kwh = sum(s.pv_to_export_kw * dt_h for s in pv_window)
        assert curtailed_kwh < 0.5, (
            f"LP curtailed {curtailed_kwh:.2f} kWh at ep=1c — should have "
            f"exported (curtail penalty 1c, export +1c, gap 2c/kWh)."
        )
        assert exported_kwh > 5.0, (
            f"LP exported only {exported_kwh:.2f} kWh — fixture likely "
            f"didn't actually saturate the battery."
        )

    def test_negative_export_price_curtails(self) -> None:
        """When export price is negative, the LP should not export. With a
        short PV window and an expensive evening, it should store PV in the
        battery rather than curtailing."""
        # Short PV window, peak right after
        pv = []
        for i in range(N_INTERVALS):
            kw = 8.0 if i < 6 else 0.0
            pv.append(
                PVForecast(
                    start=NOW + timedelta(minutes=30 * i),
                    end=NOW + timedelta(minutes=30 * (i + 1)),
                    pv_estimate_kw=kw,
                    pv_estimate10_kw=kw * 0.7,
                    pv_estimate90_kw=kw * 1.3,
                )
            )
        prices = _varying_prices(
            [20.0] * 6 + [50.0] * 10 + [20.0] * 20,
            export=-5.0,
        )
        sol, disp = _solve(
            soc=20.0,
            pv_kw=8.0,
            pv=pv,
            load_kw=1.0,
            prices=prices,
        )
        # Negative export: must not export (it costs money)
        assert sol.slot_0.grid_export_kw < 0.1
        # With 50c peak coming and no time to defer: store PV now
        assert sol.slot_0.pv_to_battery_kw > 1.0


# ── Multi-slot lookahead ─────────────────────────────────────────


class TestLPLookahead:
    def test_holds_soc_for_future_peak(self) -> None:
        """Current slot is moderately expensive (25c) but a much more
        expensive slot comes in 2 hours (60c). LP should hold SOC now and
        discharge into the future peak — this is the implicit evening
        reserve that the greedy planner needed explicit heuristics for."""
        prices = _varying_prices(
            [25.0, 25.0, 25.0, 25.0, 60.0, 60.0, 60.0, 60.0] + [15.0] * 20,
        )
        sol, disp = _solve(soc=60.0, prices=prices)
        # Slot 0 at 25c with 60c coming soon: LP should NOT discharge now
        # (or only a little for house load). The big discharge comes at 60c.
        assert sol.slot_0.battery_kw > -1.5  # not aggressively discharging

    def test_charges_before_peak_if_cheap_window(self) -> None:
        """Cheap window now (8c), then expensive peak later (50c). LP should
        charge during the cheap window to have SOC available for discharge
        during the peak."""
        prices = _varying_prices(
            [8.0, 8.0, 8.0, 8.0, 50.0, 50.0, 50.0, 50.0] + [20.0] * 20,
        )
        sol, disp = _solve(soc=30.0, prices=prices)
        assert disp.kind == DispatchKind.CHARGE
        assert sol.slot_0.battery_kw > 1.0

    def test_grid_charges_on_cloudy_day_for_evening_peak(self) -> None:
        """Cloudy day (PV=0 throughout) with a cheap mid-day import window
        followed by an expensive evening peak: the LP should grid-charge
        during the cheap window so it can self-supply (or export) at peak.

        Battery starts at floor (16% with floor=15%) so the only way to
        have anything to discharge at peak is to grid-charge first.
        Peak ip=50c — saving the 50c retail import via stored cheap kWh
        is worth ~32c/kWh after wear (50 − 8 − 5 − 5 = 32c). Spread is
        wide enough that it should fire even at conservative wear.

        Guards against regressions where wear cost or some other lever
        accidentally suppresses grid-arb on the cloudy-day case the LP
        should be most useful for.
        """
        cfg = BatteryConfig(soc_floor_pct=15.0)
        # 30-min planning intervals: 0–7 cheap (4h), 8–13 peak (3h),
        # 14–47 medium. Total 48 intervals = 24h horizon.
        prices = _varying_prices(
            [8.0] * 8 + [50.0] * 6 + [15.0] * 34,
            export=5.0,
        )
        pv = _pv_forecast(0.0)
        sol, _ = _solve(
            soc=16.0,  # at floor — battery has nothing to give
            pv=pv,
            pv_kw=0.0,
            load_kw=1.0,
            prices=prices,
            battery_config=cfg,
        )
        # 5-min trajectory: cheap window = first 8 × 6 = 48 slots,
        # peak window = next 6 × 6 = 36 slots.
        cheap = sol.forward_trajectory[:48]
        peak = sol.forward_trajectory[48:84]
        grid_charge_during_cheap = sum(s.grid_to_battery_kw for s in cheap) * (5 / 60)
        discharge_during_peak = -sum(s.battery_kw for s in peak) * (5 / 60)
        assert grid_charge_during_cheap > 3.0, (
            f"LP grid-charged only {grid_charge_during_cheap:.2f} kWh in the "
            f"cheap (8c) window. Battery started at floor, peak is 50c retail, "
            f"so grid-arb should clearly fire."
        )
        assert discharge_during_peak > 2.0, (
            f"LP discharged only {discharge_during_peak:.2f} kWh during the "
            f"50c peak — expected it to self-supply house from stored kWh."
        )


# ── SOC-bound trajectory checks ──────────────────────────────────
#
# A single-tick LP test that only inspects slot 0 misses bugs where the
# LP plans to drop below the floor (or above the ceiling) several slots
# *into* the horizon — exactly the failure mode that masked the
# 2026-04-25 floor-retirement regression. These tests assert on the
# whole `forward_trajectory`, so a "discharge below floor in slot 12"
# plan fails immediately even though slot 0 looks innocent.


class TestLPSOCBoundsTrajectory:
    def test_floor_holds_across_full_horizon_under_evening_peak(self) -> None:
        """Starting just above floor (16% with floor=15%) and an evening
        peak ladder in the horizon: the LP must not plan ANY slot below
        the floor, even far out where slot-0 isn't the binding tick."""
        cfg = BatteryConfig(soc_floor_pct=15.0)
        # Cheap-then-peak ladder: encourages discharge several slots in.
        prices = _varying_prices(
            [10.0] * 4 + [70.0] * 12 + [10.0] * 32,
        )
        sol, disp = _solve(soc=16.0, prices=prices, battery_config=cfg)
        for i, slot in enumerate(sol.forward_trajectory):
            # Tiny float tolerance; the constraint is hard so this is
            # really just guarding against solver-roundoff noise.
            assert slot.soc_pct_end >= cfg.soc_floor_pct - 0.05, (
                f"slot {i} planned soc={slot.soc_pct_end:.3f} "
                f"violates floor {cfg.soc_floor_pct}"
            )

    def test_floor_holds_under_huge_export_revenue(self) -> None:
        """High export price across the horizon would, under the old
        post-2026-04-25 design, drain the pack to zero (the overnight
        symptom that kicked off this fix). With the hard floor it must
        not."""
        cfg = BatteryConfig(soc_floor_pct=15.0)
        # Export price > import price across the whole horizon — the LP
        # has every reason to dump the battery to grid.
        prices = [
            PriceInterval(
                start=NOW + timedelta(minutes=30 * i),
                end=NOW + timedelta(minutes=30 * (i + 1)),
                import_per_kwh=15.0,
                export_per_kwh=40.0,
                spot_per_kwh=5.0,
                renewables_pct=40.0,
                spike_status="none",
                descriptor="neutral",
            )
            for i in range(N_INTERVALS)
        ]
        sol, disp = _solve(soc=80.0, prices=prices, battery_config=cfg)
        for i, slot in enumerate(sol.forward_trajectory):
            assert slot.soc_pct_end >= cfg.soc_floor_pct - 0.05, (
                f"slot {i} planned soc={slot.soc_pct_end:.3f} "
                f"violates floor {cfg.soc_floor_pct}"
            )

    def test_starting_below_floor_does_not_panic_buy_at_spike(self) -> None:
        """Sub-floor entry (SOC=8% with floor=15%) with an import SPIKE
        at slot 0: the LP must not grid-charge to recover the floor at
        the spike price. Defer to the cheap slots that follow.

        This is the operationally critical case — only slot 0 actually
        gets executed. A flat-price variant of this test is degenerate
        (HiGHS clusters terminal-floor recovery on an arbitrary slot at
        max rate); with price variation the LP unambiguously picks the
        cheap end."""
        cfg = BatteryConfig(soc_floor_pct=15.0)
        # 60c spike right now, 10c rest of horizon. Terminal-floor
        # recovery (~5 kWh) should land on the 10c slots, not the 60c.
        sol, disp = _solve(
            soc=8.0,
            prices=_prices(slot_0_import=60.0, rest_import=10.0),
            battery_config=cfg,
        )
        # Trajectory respects the clamped sub-floor.
        for i, slot in enumerate(sol.forward_trajectory):
            assert slot.soc_pct_end >= 8.0 - 0.1, (
                f"slot {i} planned soc={slot.soc_pct_end:.3f} below 8%"
            )
        # Slot 0 must not panic-buy at the spike — this is what the
        # live system actually executes.
        assert sol.slot_0.grid_to_battery_kw < 0.1, (
            f"slot 0 grid-charged {sol.slot_0.grid_to_battery_kw:.2f} kW "
            f"at 60c spike — panic-buy regression"
        )

    def test_ceiling_holds_across_horizon(self) -> None:
        """Ceiling is soft (slack-penalised) but at SOC_BOUND_PENALTY=1e4
        per %-slot, no realistic arbitrage can override it. Trajectory
        should respect it just like the floor."""
        cfg = BatteryConfig(soc_ceiling_pct=85.0)
        # Cheap now, expensive later — encourages charging hard.
        prices = _varying_prices([5.0] * 8 + [60.0] * 40)
        sol, disp = _solve(soc=70.0, prices=prices, battery_config=cfg)
        for i, slot in enumerate(sol.forward_trajectory):
            assert slot.soc_pct_end <= cfg.soc_ceiling_pct + 0.5, (
                f"slot {i} planned soc={slot.soc_pct_end:.3f} "
                f"violates ceiling {cfg.soc_ceiling_pct}"
            )


# ── Hour-of-day terminal-floor lookup ────────────────────────────


class TestTerminalFloorTable:
    """Unit tests for the staged hour-of-day terminal-floor function in
    `lp/constants.py`. The function is not yet wired into the LP — these
    tests just lock in the table's shape and lookup logic so when it
    ships, behaviour is the same as designed."""

    def test_morning_peak_window_higher_than_pv_peak(self) -> None:
        from optimiser.lp.constants import terminal_soc_floor_pct
        from datetime import datetime, timezone
        # 06:00 NEM (morning peak) vs 12:00 NEM (PV peak)
        morning = datetime(2026, 4, 27, 6, 0, tzinfo=timezone.utc)
        midday = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
        assert terminal_soc_floor_pct(morning) > terminal_soc_floor_pct(midday)

    def test_evening_peak_higher_than_post_peak(self) -> None:
        from optimiser.lp.constants import terminal_soc_floor_pct
        from datetime import datetime, timezone
        # 18:00 NEM (peak) vs 21:00 NEM (post-peak)
        peak = datetime(2026, 4, 27, 18, 0, tzinfo=timezone.utc)
        post = datetime(2026, 4, 27, 21, 0, tzinfo=timezone.utc)
        assert terminal_soc_floor_pct(peak) > terminal_soc_floor_pct(post)

    def test_pv_peak_minimum(self) -> None:
        """Midday PV-peak hour is the global minimum of the table."""
        from optimiser.lp.constants import terminal_soc_floor_pct
        from datetime import datetime, timezone
        midday = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
        v_min = terminal_soc_floor_pct(midday)
        for h in range(24):
            t = datetime(2026, 4, 27, h, 0, tzinfo=timezone.utc)
            assert terminal_soc_floor_pct(t) >= v_min

    def test_table_covers_all_24_hours(self) -> None:
        """No hour falls through to the legacy fallback."""
        from optimiser.lp.constants import (
            TERMINAL_SOC_FLOOR_PCT,
            terminal_soc_floor_pct,
            _TERMINAL_FLOOR_BY_NEM_HOUR,
        )
        from datetime import datetime, timezone
        # If any hour falls through, terminal_soc_floor_pct() returns the
        # legacy 20% scalar — but the table itself shouldn't miss any.
        covered = set()
        for hr_range, _ in _TERMINAL_FLOOR_BY_NEM_HOUR:
            covered.update(hr_range)
        assert covered == set(range(24)), (
            f"hours not covered: {set(range(24)) - covered}"
        )
        # Smoke: every hour returns a finite, sane number in [10, 50].
        for h in range(24):
            t = datetime(2026, 4, 27, h, 0, tzinfo=timezone.utc)
            v = terminal_soc_floor_pct(t)
            assert 10.0 <= v <= 50.0, f"hour {h} → {v} outside sane range"

    def test_legacy_constant_unchanged(self) -> None:
        """The scalar `TERMINAL_SOC_FLOOR_PCT` is still 20% — kept
        in source as documentation of the prior heuristic and as a
        defensive fallback inside `terminal_soc_floor_pct()`. The LP
        no longer reads it directly."""
        from optimiser.lp.constants import TERMINAL_SOC_FLOOR_PCT
        assert TERMINAL_SOC_FLOOR_PCT == 20.0


# ── Hour-of-day terminal floor — LP integration ─────────────────


class TestLPHourAwareTerminalFloor:
    """End-to-end: the LP's terminal-slot SOC must respect the
    hour-of-day terminal floor, which depends on what NEM hour the
    last slot of the horizon lands on. Wired into formulation.py
    via `terminal_soc_floor_pct(slots[n-1] + UTC+10)`.

    Trick: control the terminal NEM hour via the `state.timestamp`,
    since `slots[n-1] = state.timestamp + 48h - 30min`. With
    `state.timestamp = T0` and HORIZON_HOURS = 48, terminal NEM hour
    is `(T0 + 48h + 10h).hour` (UTC → NEM).
    """

    def _build_solve(self, anchor_utc: datetime, prices_for_discharge: bool):
        """Build a deterministic LP whose horizon terminates at
        `anchor_utc + HORIZON_HOURS - 30min`. Strong discharge prices
        push the LP to drain the battery to the per-slot floor where
        possible — the terminal slot then settles at the per-NEM-hour
        terminal floor (whichever is higher)."""
        state = SystemState(
            timestamp=anchor_utc,
            soc_pct=70.0,  # well above any floor; LP can drain
            battery_power_kw=0.0,
            pv_power_kw=0.0,
            grid_power_kw=1.0,
            house_load_kw=1.0,
            ems_mode=2,
            outdoor_temp_c=20.0,
            occupied=True,
        )
        # Flat 50c export, 5c import — discharging is profitable, no
        # need to refill. LP wants to dump SOC to as low as possible.
        if prices_for_discharge:
            ip, ep = 5.0, 50.0
        else:
            ip, ep = 20.0, 20.0
        prices = [
            PriceInterval(
                start=anchor_utc + timedelta(minutes=30 * i),
                end=anchor_utc + timedelta(minutes=30 * (i + 1)),
                import_per_kwh=ip,
                export_per_kwh=ep,
                spot_per_kwh=ip * 0.3,
                renewables_pct=40.0,
                spike_status="none",
                descriptor="neutral",
            )
            for i in range(N_INTERVALS)
        ]
        sol = solve(
            state=state,
            prices_planning=prices,
            pv_forecast=None,
            load_profile=_profile(kw=1.0),
            managed_loads=[],
            lp_loads=[],
            battery_config=BatteryConfig(soc_floor_pct=15.0),
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        return sol

    def test_morning_peak_terminal_higher_than_pv_peak_terminal(self) -> None:
        """LP whose horizon ends at NEM 06:00 (morning-peak bucket,
        floor 30%) must terminate at higher SOC than one whose horizon
        ends at NEM 12:00 (PV-peak bucket, floor 15%). Both with
        identical strong-discharge prices."""
        # T + 48h at NEM 06:00 → T at UTC 20:00 the day before. Setting
        # T = day-before-yesterday 20:00 UTC.
        morning_anchor = datetime(2026, 4, 1, 20, 0, tzinfo=UTC)
        pv_peak_anchor = datetime(2026, 4, 2, 2, 0, tzinfo=UTC)
        # Sanity-check arithmetic on the anchors:
        from optimiser.lp.constants import HORIZON_HOURS
        assert (morning_anchor + timedelta(hours=HORIZON_HOURS) + timedelta(hours=10)).hour == 6
        assert (pv_peak_anchor + timedelta(hours=HORIZON_HOURS) + timedelta(hours=10)).hour == 12

        morning_sol = self._build_solve(morning_anchor, prices_for_discharge=True)
        pv_sol = self._build_solve(pv_peak_anchor, prices_for_discharge=True)

        morning_terminal = morning_sol.forward_trajectory[-1].soc_pct_end
        pv_terminal = pv_sol.forward_trajectory[-1].soc_pct_end

        # The PV-peak terminal SOC should be at the per-slot floor (15%).
        # The morning-peak one should be at the higher hour-of-day floor (30%).
        assert pv_terminal == pytest.approx(15.0, abs=1.0), (
            f"PV-peak terminal expected ~15% per the table, got {pv_terminal:.2f}"
        )
        assert morning_terminal >= 28.0, (
            f"Morning-peak terminal expected ≥28% (table says 30%), got {morning_terminal:.2f}"
        )
        # And the difference should track the table delta.
        assert morning_terminal - pv_terminal >= 10.0, (
            f"Expected morning-peak terminal ≥10% above PV-peak; got "
            f"diff = {morning_terminal - pv_terminal:.2f}"
        )

    def test_evening_peak_terminal_holds_high(self) -> None:
        """Evening-peak terminal NEM hour (17–20 bucket → 28%): LP
        must terminate at ≥28% even when prices encourage discharge."""
        from optimiser.lp.constants import HORIZON_HOURS
        # T + 48h at NEM 18:00 → T at UTC 08:00 the day before.
        evening_anchor = datetime(2026, 4, 1, 8, 0, tzinfo=UTC)
        assert (evening_anchor + timedelta(hours=HORIZON_HOURS) + timedelta(hours=10)).hour == 18
        sol = self._build_solve(evening_anchor, prices_for_discharge=True)
        terminal = sol.forward_trajectory[-1].soc_pct_end
        assert terminal >= 27.5, (
            f"Evening-peak terminal expected ≥28% per table, got {terminal:.2f}"
        )
