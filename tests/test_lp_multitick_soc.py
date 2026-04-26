"""Closed-loop multi-tick SOC-bound tests.

A single-tick LP test only inspects what the LP *plans* — it can pass
even when sequential solves drift the actual SOC into a bad place
(the planner re-evaluates each tick against an updated initial SOC,
and small per-tick over-discharges can compound). The 2026-04-25
overnight regression is the canonical example: every individual tick
looked locally rational, but 8 hours of sequential ticks drained the
pack from 70% to 0.1%.

These tests roll the simulation forward: solve → apply slot 0 to the
SOC → advance one slot → re-solve. They assert on the *realised* SOC
trajectory, not the planned one.

To keep runtime bounded we use the deterministic `solve` (not the
stochastic three-scenario solve), drive the LP with synthetic price
profiles that exaggerate the failure modes, and run ~24 ticks (= 2h
at 5-min cadence). Each solve is ~150 ms so the file should land
around 5 s.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from optimiser.config import BatteryConfig
from optimiser.lp.constants import HORIZON_HOURS, SLOT_MINUTES
from optimiser.lp.dispatch import dispatch_from_slot
from optimiser.lp.result import SolveStatus
from optimiser.lp.solver import solve
from optimiser.types import LoadProfile, PriceInterval, PVForecast, SystemState


NOW = datetime(2026, 4, 25, 8, 0, 0, tzinfo=UTC)  # 18:00 Canberra
PLANNING_INTERVALS = HORIZON_HOURS * 2  # 30-min planning intervals
SLOT_HOURS = SLOT_MINUTES / 60.0


# ── Fixture builders ─────────────────────────────────────────────


def _state(soc: float, ts: datetime) -> SystemState:
    return SystemState(
        timestamp=ts,
        soc_pct=soc,
        battery_power_kw=0.0,
        pv_power_kw=0.0,
        grid_power_kw=1.0,
        house_load_kw=1.0,
        ems_mode=2,
        outdoor_temp_c=20.0,
        occupied=True,
    )


def _flat_prices(import_c: float, export_c: float, anchor: datetime) -> list[PriceInterval]:
    """Flat price across the horizon, anchored at the given timestamp."""
    return [
        PriceInterval(
            start=anchor + timedelta(minutes=30 * i),
            end=anchor + timedelta(minutes=30 * (i + 1)),
            import_per_kwh=import_c,
            export_per_kwh=export_c,
            spot_per_kwh=import_c * 0.3,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
        )
        for i in range(PLANNING_INTERVALS)
    ]


def _evening_peak_prices(
    anchor: datetime,
    peak_export_c: float = 30.0,
    baseline_export_c: float = 5.0,
    import_c: float = 35.0,
    peak_slots: tuple[int, int] = (0, 8),
) -> list[PriceInterval]:
    """Evening-peak shape: high export revenue across the first
    `peak_slots` 30-min intervals (= 4h by default), modest export
    after. Import is held above export everywhere to avoid the
    phantom grid-to-grid arbitrage that an export>import LP can
    exploit (the inverter physically can't do simultaneous
    import+export, but the LP formulation has no constraint
    forbidding it — separate issue)."""
    intervals: list[PriceInterval] = []
    for i in range(PLANNING_INTERVALS):
        in_peak = peak_slots[0] <= i < peak_slots[1]
        ep = peak_export_c if in_peak else baseline_export_c
        intervals.append(
            PriceInterval(
                start=anchor + timedelta(minutes=30 * i),
                end=anchor + timedelta(minutes=30 * (i + 1)),
                import_per_kwh=import_c,
                export_per_kwh=ep,
                spot_per_kwh=import_c * 0.3,
                renewables_pct=40.0,
                spike_status="none",
                descriptor="neutral",
            )
        )
    return intervals


def _profile(kw: float = 1.0) -> LoadProfile:
    return LoadProfile(slots=[kw] * 48, maturity_level=0, context="multitick")


def _morning_peak_prices(
    anchor: datetime,
    peak_import_c: float = 60.0,
    midday_import_c: float = 12.0,
    evening_import_c: float = 30.0,
    export_c: float = 5.0,
) -> list[PriceInterval]:
    """Realistic morning-peak shape:
        - first 6 30-min slots (3 h, peak):     `peak_import_c`
        - next 18 slots (~9h, midday/cheap):    `midday_import_c`
        - next 8 slots (4h, evening peak):      `evening_import_c`
        - tail:                                  `midday_import_c`
    Used to test "we entered a tick already below the floor — did the
    LP panic-buy at the morning peak instead of waiting for cheap
    slots?" That's the exact pattern from the 2026-04-25 overnight
    failure: the LP drained the pack overnight, then the pack sat at
    ~0.1% as the morning peak loomed — would the LP have crash-charged
    at peak prices to recover the floor?"""
    intervals: list[PriceInterval] = []
    for i in range(PLANNING_INTERVALS):
        if i < 6:
            ip = peak_import_c
        elif i < 24:
            ip = midday_import_c
        elif i < 32:
            ip = evening_import_c
        else:
            ip = midday_import_c
        intervals.append(
            PriceInterval(
                start=anchor + timedelta(minutes=30 * i),
                end=anchor + timedelta(minutes=30 * (i + 1)),
                import_per_kwh=ip,
                export_per_kwh=export_c,
                spot_per_kwh=ip * 0.3,
                renewables_pct=40.0,
                spike_status="none",
                descriptor="neutral",
            )
        )
    return intervals


# ── Closed-loop simulator ────────────────────────────────────────


def _step_soc(
    soc: float,
    battery_kw: float,
    capacity_kwh: float,
    eta: float,
) -> float:
    """Advance SOC by one slot under the LP's sign convention.

    `battery_kw` follows the SlotDecision convention: positive = charge,
    negative = discharge. Charge applies efficiency once (matches the
    LP's `eta * (charge_grid + charge_pv)` term in the dynamics — the
    other half of round-trip is amortised on the discharge side per
    the formulation).
    """
    if battery_kw >= 0:
        delta_kwh = battery_kw * SLOT_HOURS * eta
    else:
        delta_kwh = battery_kw * SLOT_HOURS  # discharge: no eta multiplier
    return soc + (delta_kwh / capacity_kwh) * 100.0


def _shift_prices(
    prices: list[PriceInterval], by_minutes: int
) -> list[PriceInterval]:
    """Slide the price series forward in time, keeping the same shape.
    The LP's 30-min anchoring stays internally consistent because we
    solve at the new anchor on the next tick."""
    return [
        replace(p, start=p.start + timedelta(minutes=by_minutes), end=p.end + timedelta(minutes=by_minutes))
        for p in prices
    ]


def _run_loop(
    *,
    initial_soc: float,
    n_ticks: int,
    prices_at: list[PriceInterval],
    cfg: BatteryConfig,
    pv: list[PVForecast] | None = None,
) -> list[tuple[datetime, float, float]]:
    """Step the LP forward `n_ticks` slots. Each tick: solve → apply
    slot 0 to SOC → advance time → re-solve.

    Returns a list of (timestamp, soc_at_start_of_tick, slot0_battery_kw).
    """
    soc = initial_soc
    ts = NOW
    prices = list(prices_at)
    trajectory: list[tuple[datetime, float, float]] = []

    for _ in range(n_ticks):
        sol = solve(
            state=_state(soc=soc, ts=ts),
            prices_planning=prices,
            pv_forecast=pv,
            load_profile=_profile(),
            managed_loads=[],
            lp_loads=[],
            battery_config=cfg,
        )
        assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), (
            f"LP failed at tick ts={ts}: {sol.status.value}"
        )
        assert sol.slot_0 is not None
        bat_kw = sol.slot_0.battery_kw
        trajectory.append((ts, soc, bat_kw))
        soc = _step_soc(
            soc, bat_kw, cfg.capacity_kwh, cfg.round_trip_efficiency
        )
        # Physics: SOC can't go below zero (BMS protects).
        soc = max(soc, 0.0)
        ts = ts + timedelta(minutes=SLOT_MINUTES)
        prices = _shift_prices(prices, SLOT_MINUTES)

    return trajectory


# ── Tests ────────────────────────────────────────────────────────


class TestClosedLoopFloor:
    """Sequential-solve assertions — the layer that catches drift bugs
    a one-shot solve cannot."""

    def test_does_not_drain_below_floor_during_evening_peak(self) -> None:
        """Reproduces the overnight 2026-04-25 failure mode in
        miniature: ~4h evening peak with 30c export revenue, then
        modest export after. Pre-fix, the LP emptied the pack across
        sequential ticks. Post-fix, the hard per-slot floor stops
        discharge once SOC reaches it.

        Start near the floor so the discharge headroom is small — only
        a few sequential ticks of unbounded discharge would cross the
        floor under the broken design."""
        cfg = BatteryConfig(soc_floor_pct=15.0)
        prices = _evening_peak_prices(NOW, peak_export_c=30.0, import_c=35.0)
        traj = _run_loop(
            initial_soc=18.0,
            n_ticks=24,  # 2h at 5-min cadence
            prices_at=prices,
            cfg=cfg,
        )

        for ts, soc, bat in traj:
            assert soc >= cfg.soc_floor_pct - 0.5, (
                f"SOC drifted below floor at {ts}: soc={soc:.2f}, "
                f"slot-0 plan was {bat:.2f} kW"
            )

    def test_floor_holds_from_high_soc_through_long_evening(self) -> None:
        """The overnight pattern in long-form: start with high SOC and a
        sustained profitable export window. The pack should drain
        toward the floor and then stop, not continue down to zero."""
        cfg = BatteryConfig(soc_floor_pct=15.0)
        # Long peak window (~12h at 30-min anchoring) with profitable
        # export. The 35c import baseline keeps export < import so the
        # LP can't exploit the grid-arb loophole.
        prices = _evening_peak_prices(
            NOW,
            peak_export_c=30.0,
            baseline_export_c=5.0,
            import_c=35.0,
            peak_slots=(0, 24),
        )
        traj = _run_loop(
            initial_soc=70.0,
            n_ticks=60,  # 5h at 5-min cadence
            prices_at=prices,
            cfg=cfg,
        )

        socs = [soc for _, soc, _ in traj]
        assert socs[0] >= 65.0
        # The LP must actually discharge — otherwise the test is
        # vacuous (an LP that always idles trivially respects every
        # floor). 5% over 5h is a soft threshold, satisfied by any
        # nontrivial discharge pattern.
        assert min(socs) <= 65.0, (
            f"min SOC was {min(socs):.2f} — LP didn't exercise the "
            f"discharge path at all; test is vacuous"
        )
        # And the floor must hold across every tick.
        for ts, soc, bat in traj:
            assert soc >= cfg.soc_floor_pct - 0.5, (
                f"SOC drifted below floor at {ts}: soc={soc:.2f}"
            )

    def test_no_panic_buy_recovery_under_flat_prices(self) -> None:
        """Multi-tick variant: starting just below floor with flat
        moderate prices, the LP should not aggressively grid-charge to
        recover. Some terminal-floor-driven recovery is expected
        (~5 kWh over 48h, well under 1 kW continuous), but the
        per-tick slot-0 charge rate must stay modest. A panic burst
        would show as multi-kW slot-0 charges in the live system."""
        cfg = BatteryConfig(soc_floor_pct=15.0)
        prices = _flat_prices(import_c=22.0, export_c=8.0, anchor=NOW)
        traj = _run_loop(
            initial_soc=10.0,  # below floor
            n_ticks=24,
            prices_at=prices,
            cfg=cfg,
        )

        # No tick should plan a >2 kW slot-0 charge. (Even terminal
        # recovery should land on cheap slots far from "now"; flat
        # prices give the LP no reason to compress recovery into the
        # live slot.)
        for ts, soc, bat in traj:
            assert bat < 2.0, (
                f"slot-0 charge {bat:.2f} kW at {ts} — panic-buy "
                f"regression (SOC was {soc:.2f}%)"
            )


class TestClosedLoopSubFloorEntry:
    """Specifically: the LP enters a tick already below the configured
    floor (post-fallback re-entry, BMS quirk, or — most realistically —
    the tank ran the previous tick down through the floor). The hard
    floor must clamp to the realised SOC (no infeasibility), and the
    LP must not panic-buy at expensive slots to recover the configured
    floor.

    These tests roll multi-hour sequential solves so the same tick gets
    re-evaluated as state evolves — catches drift where each
    individually-rational tick adds up to a panic-buy across the
    horizon."""

    def test_morning_peak_entry_does_not_panic_buy(self) -> None:
        """Realistic 2026-04-25 follow-on: pack drained overnight, SOC
        at 10% as a 60c morning peak hits. The LP must not crash-charge
        through the peak — it must defer recovery to the cheaper midday
        slots (12c, ~1.5h away) that follow."""
        cfg = BatteryConfig(soc_floor_pct=15.0)
        prices = _morning_peak_prices(
            NOW,
            peak_import_c=60.0,
            midday_import_c=12.0,
            evening_import_c=30.0,
        )
        # 36 ticks = 3 hours = covers the full morning peak window
        # (first 6 30-min slots = 3h).
        traj = _run_loop(
            initial_soc=10.0,
            n_ticks=36,
            prices_at=prices,
            cfg=cfg,
        )

        # During the morning-peak window, slot-0 grid-charging must
        # stay tiny. Panic-buy would show as multi-kW charges at peak
        # prices to "satisfy the floor" — exactly the regression we
        # walked back from on 2026-04-25 (under a different mechanism).
        # The first 36 ticks are all within the 3h peak window since
        # we shift prices forward at 5min/tick (peak ends at relative
        # i=6 30-min slots = 3h = 36 ticks).
        for ts, soc, bat in traj:
            assert bat < 1.0, (
                f"slot-0 charge {bat:.2f} kW at {ts} during 60c peak "
                f"— panic-buy at expensive slot (SOC was {soc:.2f}%)"
            )
        # And SOC must never drop further below the clamped sub-floor
        # (10%) — the LP shouldn't discharge into the morning peak
        # either, even though the price spread looks tempting.
        for ts, soc, bat in traj:
            assert soc >= 10.0 - 0.5, (
                f"SOC drifted below clamped sub-floor at {ts}: "
                f"soc={soc:.2f}"
            )

    def test_evening_drain_then_morning_peak_round_trip(self) -> None:
        """Two-phase scenario stitched into one closed-loop run: drain
        through an evening export window then face a morning import
        peak. Verifies that:

        1. The evening drain stops at the floor (15%), not 0%.
        2. With the floor preserved, the morning peak doesn't trigger
           grid panic-buy — the LP rides the morning out on stored
           energy, not on imported peak-priced grid.

        This is the user-stated requirement in essence: don't panic-
        buy under 15%, but also don't sell below it.
        """
        cfg = BatteryConfig(soc_floor_pct=15.0)
        # Evening export peak (4h) → overnight low → morning import
        # peak (3h after that). Build by hand so we can shape the
        # transitions cleanly.
        intervals: list[PriceInterval] = []
        for i in range(PLANNING_INTERVALS):
            if i < 8:                # evening peak: 4h, high export
                ip, ep = 35.0, 25.0
            elif i < 16:             # late night: 4h, low both
                ip, ep = 12.0, 4.0
            elif i < 22:             # morning peak: 3h, high import
                ip, ep = 60.0, 8.0
            else:                    # midday: low import, free PV in real life
                ip, ep = 12.0, 4.0
            intervals.append(
                PriceInterval(
                    start=NOW + timedelta(minutes=30 * i),
                    end=NOW + timedelta(minutes=30 * (i + 1)),
                    import_per_kwh=ip,
                    export_per_kwh=ep,
                    spot_per_kwh=ip * 0.3,
                    renewables_pct=40.0,
                    spike_status="none",
                    descriptor="neutral",
                )
            )
        # Run through the evening-peak window (8 × 30 min = 4h = 48
        # 5-min ticks). At 5-min cadence the prices stay in the
        # evening-peak band for the whole window.
        traj = _run_loop(
            initial_soc=50.0,        # comfortable starting point
            n_ticks=48,              # 4h evening peak window
            prices_at=intervals,
            cfg=cfg,
        )

        # 1. Floor held across the whole evening peak.
        for ts, soc, bat in traj:
            assert soc >= cfg.soc_floor_pct - 0.5, (
                f"SOC drifted below floor at {ts}: soc={soc:.2f} "
                f"— evening peak drained the pack"
            )
        # 2. The pack actually drained meaningfully (test isn't
        # vacuous): export was profitable here so the LP should cycle.
        socs = [s for _, s, _ in traj]
        assert min(socs) <= 40.0, (
            f"min SOC {min(socs):.2f} — LP didn't exercise discharge"
        )


class TestClosedLoopCeiling:
    """Mirror of the floor tests for the soft ceiling — verify the
    ceiling actually holds under sequential solves with cheap import +
    expensive forward peak."""

    def test_does_not_overcharge_through_ceiling(self) -> None:
        cfg = BatteryConfig(soc_ceiling_pct=85.0, soc_floor_pct=15.0)
        # Cheap import now, expensive future — encourages charging hard.
        # Use varying prices: low for first 4h, high after.
        prices = []
        for i in range(PLANNING_INTERVALS):
            imp = 5.0 if i < 8 else 60.0
            prices.append(
                PriceInterval(
                    start=NOW + timedelta(minutes=30 * i),
                    end=NOW + timedelta(minutes=30 * (i + 1)),
                    import_per_kwh=imp,
                    export_per_kwh=imp - 2.0,
                    spot_per_kwh=imp * 0.3,
                    renewables_pct=40.0,
                    spike_status="none",
                    descriptor="neutral",
                )
            )
        traj = _run_loop(
            initial_soc=78.0,  # close to ceiling, so a few aggressive
            # ticks would breach it under a broken design
            n_ticks=24,
            prices_at=prices,
            cfg=cfg,
        )

        for ts, soc, bat in traj:
            assert soc <= cfg.soc_ceiling_pct + 1.0, (
                f"SOC drifted above ceiling at {ts}: soc={soc:.2f}"
            )


# ── Configurability sanity ───────────────────────────────────────


@pytest.mark.parametrize("floor", [10.0, 15.0, 25.0, 40.0])
def test_floor_is_configurable(floor: float) -> None:
    """The hard per-slot floor honours whatever value the operator
    sets in BatteryConfig.soc_floor_pct. Single solve at a high
    discharge incentive — the LP must respect each configured floor
    individually rather than hard-coding 15%."""
    cfg = BatteryConfig(soc_floor_pct=floor)
    prices = _evening_peak_prices(NOW, peak_export_c=30.0, import_c=35.0)
    sol = solve(
        state=_state(soc=floor + 5.0, ts=NOW),
        prices_planning=prices,
        pv_forecast=None,
        load_profile=_profile(),
        managed_loads=[],
        lp_loads=[],
        battery_config=cfg,
    )
    assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    for slot in sol.forward_trajectory:
        assert slot.soc_pct_end >= floor - 0.05, (
            f"configured floor={floor} not respected: "
            f"planned soc={slot.soc_pct_end:.3f}"
        )


@pytest.mark.parametrize("ceiling", [70.0, 85.0, 95.0, 100.0])
def test_ceiling_is_configurable(ceiling: float) -> None:
    """The soft per-slot ceiling honours BatteryConfig.soc_ceiling_pct."""
    cfg = BatteryConfig(soc_ceiling_pct=ceiling, soc_floor_pct=15.0)
    # Cheap-now, expensive-later: maximum charging incentive.
    prices = []
    for i in range(PLANNING_INTERVALS):
        imp = 2.0 if i < 8 else 80.0
        prices.append(
            PriceInterval(
                start=NOW + timedelta(minutes=30 * i),
                end=NOW + timedelta(minutes=30 * (i + 1)),
                import_per_kwh=imp,
                export_per_kwh=imp - 1.0,
                spot_per_kwh=imp * 0.3,
                renewables_pct=40.0,
                spike_status="none",
                descriptor="neutral",
            )
        )
    sol = solve(
        state=_state(soc=ceiling - 5.0, ts=NOW),
        prices_planning=prices,
        pv_forecast=None,
        load_profile=_profile(),
        managed_loads=[],
        lp_loads=[],
        battery_config=cfg,
    )
    assert sol.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    for slot in sol.forward_trajectory:
        assert slot.soc_pct_end <= ceiling + 0.5, (
            f"configured ceiling={ceiling} not respected: "
            f"planned soc={slot.soc_pct_end:.3f}"
        )
