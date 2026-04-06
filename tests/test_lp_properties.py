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
    return sol, dispatch_from_slot(sol.slot_0)


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
        remaining to defer charging and still fill up before the peak."""
        # Short PV window (8 intervals = 4h), peak starts right after
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
        # 15c during PV, 50c peak starts at interval 8 (right when PV ends)
        prices = _varying_prices(
            [15.0] * 8 + [50.0] * 10 + [15.0] * 20,
            export=5.0,
        )
        sol, disp = _solve(
            soc=20.0,
            pv_kw=5.0,
            pv=pv,
            load_kw=1.0,
            prices=prices,
        )
        # 4kW PV surplus for only 4h — must start charging immediately to
        # fill battery before peak. Stored kWh worth 50c × 0.9 = 45c later
        # vs 5c export revenue now.
        assert sol.slot_0.pv_to_battery_kw > 1.0


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

    def test_does_not_discharge_below_floor(self) -> None:
        """SOC at floor — LP should not discharge even if price is high."""
        cfg = BatteryConfig(soc_floor_pct=10.0)
        sol, disp = _solve(
            soc=10.0,
            prices=_prices(slot_0_import=80.0, rest_import=10.0),
            battery_config=cfg,
        )
        # At floor: battery_kw should be ≥ 0 (charge or idle)
        assert sol.slot_0.battery_kw >= -0.1

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
        # 20c now, 22c later: 2c spread. At W=2.5 c/kWh one-way + 90% eff,
        # break-even is ~7.8c — 2c falls well short.
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
