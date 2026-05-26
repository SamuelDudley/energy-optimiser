"""LP behaviour under user-strategy mode overrides."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from optimiser.config import BatteryConfig
from optimiser.lp.constants import SLOT_MINUTES
from optimiser.lp.loads import build_lp_loads
from optimiser.lp.result import SolveStatus
from optimiser.lp.solver import solve_stochastic
from optimiser.modes import ModeOverrides
from optimiser.types import (
    LoadProfile,
    PriceInterval,
    PVForecast,
    SystemState,
)

NOW = datetime(2026, 5, 19, 4, 0, 0, tzinfo=UTC)  # 14:00 Canberra
SLOT = timedelta(minutes=SLOT_MINUTES)


def _state(soc: float = 50.0) -> SystemState:
    return SystemState(
        timestamp=NOW,
        soc_pct=soc,
        battery_power_kw=0.0,
        pv_power_kw=0.0,
        grid_power_kw=0.0,
        house_load_kw=0.0,
        ems_mode=2,
        outdoor_temp_c=20.0,
        occupied=True,
    )


def _prices(per_slot_import: list[float], per_slot_export: list[float]) -> list[PriceInterval]:
    """Build prices_planning at slot cadence. PriceInterval requires
    several fields beyond the import/export numbers; fill the rest with
    benign defaults that don't influence the LP."""
    assert len(per_slot_import) == len(per_slot_export)
    return [
        PriceInterval(
            start=NOW + SLOT * i,
            end=NOW + SLOT * (i + 1),
            import_per_kwh=per_slot_import[i],
            export_per_kwh=per_slot_export[i],
            spot_per_kwh=per_slot_import[i] * 0.3,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
        )
        for i in range(len(per_slot_import))
    ]


def _profile(kw: float = 2.0) -> LoadProfile:
    # Flat 48-slot half-hour profile.
    return LoadProfile(slots=[kw] * 48, maturity_level=0, context="test")


def _battery() -> BatteryConfig:
    return BatteryConfig(
        capacity_kwh=40.0,
        max_ac_charge_kw=10.0,
        max_dc_charge_kw=13.0,
        max_discharge_kw=10.0,
        round_trip_efficiency=0.92,
        soc_floor_pct=10.0,
        soc_ceiling_pct=95.0,
        backup_soc_pct=15.0,
        discharge_cutoff_pct=10.0,
    )


def test_buy_mode_blocks_bat_charge_grid_above_ceiling() -> None:
    """Slot 2 has price 15c, ceiling=10c → LP must set bat_charge_grid[2]=0."""
    n_slots = 12
    # Cheap, cheap, SPIKE, cheap...
    imports = [5.0, 5.0, 15.0] + [5.0] * (n_slots - 3)
    exports = [3.0] * n_slots
    # Start well below terminal-floor SOC so the LP must charge from grid
    # to recover; with buy-mode active the spike slot must still refuse.
    state = _state(soc=5.0)

    overrides = ModeOverrides(
        buy_active_at=tuple([True] * n_slots),
        buy_ceiling_c_per_kwh=10.0,
        conserve_active_at=tuple([False] * n_slots),
        conserve_floor_c_per_kwh=None,
    )
    result = solve_stochastic(
        state=state,
        prices_planning=_prices(imports, exports),
        pv_forecast=None,
        load_profile=_profile(2.0),
        managed_loads=[],
        lp_loads=build_lp_loads(configs=[]),
        battery_config=_battery(),
        mode_overrides=overrides,
    )
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    base = result.forward_trajectory
    assert base[2].grid_to_battery_kw == pytest.approx(0.0, abs=1e-6)
    # Cheap slots can charge.
    assert base[1].grid_to_battery_kw > 0.0


def test_buy_mode_forbids_battery_export() -> None:
    """During buy window, grid_export must come from PV only."""
    n_slots = 12
    # High export price would normally entice battery → grid.
    imports = [5.0] * n_slots
    exports = [50.0] * n_slots  # very high
    state = _state(soc=90.0)  # high SOC so LP would happily discharge

    overrides = ModeOverrides(
        buy_active_at=tuple([True] * n_slots),
        buy_ceiling_c_per_kwh=10.0,
        conserve_active_at=tuple([False] * n_slots),
        conserve_floor_c_per_kwh=None,
    )
    result = solve_stochastic(
        state=state,
        prices_planning=_prices(imports, exports),
        pv_forecast=None,
        load_profile=_profile(2.0),
        managed_loads=[],
        lp_loads=build_lp_loads(configs=[]),
        battery_config=_battery(),
        mode_overrides=overrides,
    )
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    base = result.forward_trajectory
    for slot in base:
        # Battery cannot contribute to export.
        assert slot.grid_export_kw <= slot.pv_to_export_kw + 1e-6


def test_conserve_mode_blocks_all_export_below_floor() -> None:
    """Below floor: total grid_export must be 0 (no battery, no PV).
    Above floor: no extra constraint — battery and PV may both export."""
    n_slots = 12
    imports = [25.0] * n_slots
    # Low export at slot 2 only; others are above the floor.
    exports = [20.0, 20.0, 5.0] + [20.0] * (n_slots - 3)
    pv = [
        PVForecast(
            start=NOW + SLOT * i,
            end=NOW + SLOT * (i + 1),
            pv_estimate_kw=6.0,
            pv_estimate10_kw=6.0,
            pv_estimate90_kw=6.0,
        )
        for i in range(n_slots)
    ]
    state = _state(soc=90.0)  # high SOC so LP would happily discharge

    overrides = ModeOverrides(
        buy_active_at=tuple([False] * n_slots),
        buy_ceiling_c_per_kwh=None,
        conserve_active_at=tuple([True] * n_slots),
        conserve_floor_c_per_kwh=15.0,
    )
    result = solve_stochastic(
        state=state,
        prices_planning=_prices(imports, exports),
        pv_forecast=pv,
        load_profile=_profile(2.0),
        managed_loads=[],
        lp_loads=build_lp_loads(configs=[]),
        battery_config=_battery(),
        mode_overrides=overrides,
    )
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    base = result.forward_trajectory
    # Slot 2 (ep=5c < floor=15c): total export must be 0 even with PV.
    assert base[2].grid_export_kw == pytest.approx(0.0, abs=1e-6)


def test_conserve_mode_allows_battery_export_above_floor() -> None:
    """Above the floor, battery is free to contribute to grid_export.
    Setup: no PV, high SOC, export price well above the floor and well
    above the round-trip wear/RTE break-even — LP should empty the
    battery to grid rather than hold it."""
    n_slots = 12
    imports = [5.0] * n_slots  # cheap import so no banking incentive
    exports = [40.0] * n_slots  # well above the floor, well above wear break-even
    state = _state(soc=90.0)  # high SOC, plenty of headroom above floor

    overrides = ModeOverrides(
        buy_active_at=tuple([False] * n_slots),
        buy_ceiling_c_per_kwh=None,
        conserve_active_at=tuple([True] * n_slots),
        conserve_floor_c_per_kwh=15.0,
    )
    result = solve_stochastic(
        state=state,
        prices_planning=_prices(imports, exports),
        pv_forecast=None,
        load_profile=_profile(0.0),
        managed_loads=[],
        lp_loads=build_lp_loads(configs=[]),
        battery_config=_battery(),
        mode_overrides=overrides,
    )
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    base = result.forward_trajectory
    # With no PV, any grid_export must come from battery — assert that
    # exporting actually happens above the floor.
    battery_exports = [slot.grid_export_kw for slot in base if slot.grid_export_kw > 0.01]
    assert len(battery_exports) > 0, "LP should discharge battery to grid above floor"


def test_buy_mode_wear_discount_increases_grid_charge() -> None:
    """With buy mode active and a marginal-arbitrage scenario, the
    wear discount should tip the LP into charging more.

    Setup: marginal arbitrage where the spread (5c) is below the full
    wear-cost round-trip (5c) plus efficiency loss but above the
    discounted round-trip (2.5c wear + efficiency). Without the buy-
    mode wear discount, the LP refuses to cycle; with the discount,
    it charges hard in the cheap window.
    """
    # 06:00 UTC == 16:00 Canberra. 2h horizon ends at 18:00 NEM where
    # terminal_soc_floor_pct = 28% — keeps the LP from just discharging
    # to floor and never grid-charging.
    now = datetime(2026, 5, 19, 6, 0, 0, tzinfo=UTC)
    n_slots = 24  # 2h horizon at 5min slots
    imports = [18.0] * 6 + [23.0] * (n_slots - 6)  # cheap-ish then expensive
    exports = [3.0] * n_slots
    state = SystemState(
        timestamp=now,
        soc_pct=30.0,
        battery_power_kw=0.0,
        pv_power_kw=0.0,
        grid_power_kw=0.0,
        house_load_kw=0.0,
        ems_mode=2,
        outdoor_temp_c=20.0,
        occupied=True,
    )
    prices = [
        PriceInterval(
            start=now + SLOT * i,
            end=now + SLOT * (i + 1),
            import_per_kwh=imports[i],
            export_per_kwh=exports[i],
            spot_per_kwh=imports[i] * 0.3,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
        )
        for i in range(n_slots)
    ]
    profile = _profile(2.0)
    battery = _battery()
    lp_loads = build_lp_loads(configs=[])

    # Baseline: no overrides.
    base_result = solve_stochastic(
        state=state,
        prices_planning=prices,
        pv_forecast=None,
        load_profile=profile,
        managed_loads=[],
        lp_loads=lp_loads,
        battery_config=battery,
    )

    # Buy mode active across the cheap window (first 6 slots),
    # ceiling well above the cheap-import price so no ceiling block.
    overrides = ModeOverrides(
        buy_active_at=tuple([True] * 6 + [False] * (n_slots - 6)),
        buy_ceiling_c_per_kwh=20.0,
        conserve_active_at=tuple([False] * n_slots),
        conserve_floor_c_per_kwh=None,
    )
    with_buy = solve_stochastic(
        state=state,
        prices_planning=prices,
        pv_forecast=None,
        load_profile=profile,
        managed_loads=[],
        lp_loads=lp_loads,
        battery_config=battery,
        mode_overrides=overrides,
    )

    base_charge = sum(slot.grid_to_battery_kw for slot in base_result.forward_trajectory[:6])
    buy_charge = sum(slot.grid_to_battery_kw for slot in with_buy.forward_trajectory[:6])
    # Discount should push charging strictly higher in the in-window slots.
    assert buy_charge > base_charge + 1e-3


def test_buy_mode_cutoff_picks_cheapest_slots() -> None:
    """When the SOC cutoff is reachable using only the cheapest in-window
    slots, the LP should leave the more-expensive (but still sub-ceiling)
    slots alone — i.e. the standard cost-min argument holds inside the
    buy-mode wear-discount regime.
    """
    n_window = 24  # 2h buy window
    n_total = 48  # 4h total horizon — expensive tail motivates the charge
    # Window: alternating cheap (5c) / middle (9c), both sub-ceiling 12c.
    # Tail: 25c — drives the LP to bank energy during the cheap window.
    imports = [5.0 if i % 2 == 0 else 9.0 for i in range(n_window)] + [25.0] * (n_total - n_window)
    exports = [3.0] * n_total
    # Start at the floor so the LP is forced to use grid for both
    # house-load and any banking for the expensive tail — that's what
    # makes the cheap/middle selection observable.
    state = _state(soc=10.0)
    overrides = ModeOverrides(
        buy_active_at=tuple([True] * n_window + [False] * (n_total - n_window)),
        buy_ceiling_c_per_kwh=12.0,
        buy_soc_cutoff_pct=30.0,
        conserve_active_at=tuple([False] * n_total),
        conserve_floor_c_per_kwh=None,
    )
    result = solve_stochastic(
        state=state,
        prices_planning=_prices(imports, exports),
        pv_forecast=None,
        load_profile=_profile(2.0),
        managed_loads=[],
        lp_loads=build_lp_loads(configs=[]),
        battery_config=_battery(),
        mode_overrides=overrides,
    )
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    traj = result.forward_trajectory[:n_window]
    cheap_charge = sum(s.grid_to_battery_kw for i, s in enumerate(traj) if i % 2 == 0)
    middle_charge = sum(s.grid_to_battery_kw for i, s in enumerate(traj) if i % 2 == 1)
    # Cheapest slots take essentially all the charging; the 9c slots only
    # get touched if there's not enough cheap capacity (there is here:
    # 12 cheap slots × ~1.9 %/slot ≈ 23 % SOC headroom > the 10 % we need).
    assert cheap_charge > 0, "LP did not charge in cheap slots"
    assert middle_charge < cheap_charge * 0.05, (
        f"middle-priced slots got non-trivial charging; "
        f"cheap={cheap_charge:.3f} kW·slots, middle={middle_charge:.3f}"
    )


def test_buy_mode_charges_without_arbitrage_signal() -> None:
    """Buy mode's lexicographic end-of-window SOC incentive forces the
    LP to charge during the window even when its forecast view sees no
    profitable arbitrage — "I asked for buy, so buy".

    Setup: flat prices across the whole horizon (no future peak,
    nothing to arbitrage into). Without the incentive the LP would
    leave SOC where it started; with it, the LP fills to the cutoff
    using the cheapest sub-ceiling slots.
    """
    n_window = 24  # 2h buy window at 5-min slots
    n_total = 48  # 4h horizon
    # Flat 20c throughout — no future peak, no arb signal. Ceiling 25c
    # so every in-window slot is eligible for grid charging.
    imports = [20.0] * n_total
    exports = [3.0] * n_total
    state = _state(soc=10.0)  # start at floor, well below cutoff
    overrides = ModeOverrides(
        buy_active_at=tuple([True] * n_window + [False] * (n_total - n_window)),
        buy_ceiling_c_per_kwh=25.0,
        buy_soc_cutoff_pct=30.0,
        conserve_active_at=tuple([False] * n_total),
        conserve_floor_c_per_kwh=None,
    )
    result = solve_stochastic(
        state=state,
        prices_planning=_prices(imports, exports),
        pv_forecast=None,
        load_profile=_profile(0.0),  # zero load so charging is the only effect
        managed_loads=[],
        lp_loads=build_lp_loads(configs=[]),
        battery_config=_battery(),
        mode_overrides=overrides,
    )
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    # SOC at the end of the buy window (slot n_window - 1) must reach
    # the cutoff. Allow a tiny tolerance for solver numerics.
    soc_end_window = result.forward_trajectory[n_window - 1].soc_pct_end
    assert soc_end_window >= 30.0 - 1e-3, (
        f"LP failed to reach buy target by end of window: "
        f"soc_end_window={soc_end_window:.2f}, target=30.0"
    )


def test_buy_mode_skips_spikes_above_ceiling() -> None:
    """When some in-window slots exceed the ceiling (spikes), the LP
    must fill using only the sub-ceiling slots — never charging at or
    above the ceiling — and still reach the cutoff if the available
    cheap-slot capacity is enough."""
    n_window = 24
    n_total = 48
    # Alternating cheap (10c) and spike (50c) — ceiling at 30c skips
    # every spike. 12 sub-ceiling slots in the window must still be
    # enough to hit the target.
    imports = [10.0 if i % 2 == 0 else 50.0 for i in range(n_window)] + [20.0] * (
        n_total - n_window
    )
    exports = [3.0] * n_total
    state = _state(soc=10.0)
    overrides = ModeOverrides(
        buy_active_at=tuple([True] * n_window + [False] * (n_total - n_window)),
        buy_ceiling_c_per_kwh=30.0,
        buy_soc_cutoff_pct=25.0,
        conserve_active_at=tuple([False] * n_total),
        conserve_floor_c_per_kwh=None,
    )
    result = solve_stochastic(
        state=state,
        prices_planning=_prices(imports, exports),
        pv_forecast=None,
        load_profile=_profile(0.0),
        managed_loads=[],
        lp_loads=build_lp_loads(configs=[]),
        battery_config=_battery(),
        mode_overrides=overrides,
    )
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    traj = result.forward_trajectory[:n_window]
    spike_charge = sum(s.grid_to_battery_kw for i, s in enumerate(traj) if i % 2 == 1)
    cheap_charge = sum(s.grid_to_battery_kw for i, s in enumerate(traj) if i % 2 == 0)
    # Spike slots must be untouched (hard ceiling constraint).
    assert spike_charge < 1e-3, f"LP charged during spike slots: {spike_charge}"
    # All the charging happens in the cheap slots.
    assert cheap_charge > 0
    # Target must still be reached.
    soc_end_window = traj[-1].soc_pct_end
    assert soc_end_window >= 25.0 - 1e-3


def test_buy_mode_charges_without_cutoff() -> None:
    """SOC cutoff is optional: with it unset, the LP still charges
    aggressively during the buy window (capped only by the battery's
    physical soc_ceiling_pct), picking the cheapest sub-ceiling slots.
    """
    n_window = 24
    n_total = 48
    # Flat 20c throughout — no arb signal. Ceiling 25c so all eligible.
    imports = [20.0] * n_total
    exports = [3.0] * n_total
    state = _state(soc=10.0)
    overrides = ModeOverrides(
        buy_active_at=tuple([True] * n_window + [False] * (n_total - n_window)),
        buy_ceiling_c_per_kwh=25.0,
        buy_soc_cutoff_pct=None,  # ← no cutoff
        conserve_active_at=tuple([False] * n_total),
        conserve_floor_c_per_kwh=None,
    )
    result = solve_stochastic(
        state=state,
        prices_planning=_prices(imports, exports),
        pv_forecast=None,
        load_profile=_profile(0.0),
        managed_loads=[],
        lp_loads=build_lp_loads(configs=[]),
        battery_config=_battery(),
        mode_overrides=overrides,
    )
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    # SOC must have grown materially during the buy window — without a
    # cutoff the LP charges as much as possible, bounded by the battery
    # ceiling (95%) or window duration. From 10% over 2h at 10kW that's
    # roughly 28% of growth before efficiency losses, more than enough
    # to clear a 20% delta threshold here.
    soc_end_window = result.forward_trajectory[n_window - 1].soc_pct_end
    assert soc_end_window >= 30.0, (
        f"LP failed to charge without a cutoff: soc_end_window={soc_end_window:.2f}"
    )


def test_buy_mode_soc_cutoff_caps_charging() -> None:
    """With soc_cutoff_pct set, the LP must not plan SOC past the cutoff
    at any in-window slot end."""
    n_slots = 24
    # Cheap import throughout — without the cutoff the LP would charge
    # up toward the battery's physical ceiling.
    imports = [5.0] * n_slots
    exports = [3.0] * n_slots
    state = _state(soc=40.0)  # well below the cutoff
    overrides = ModeOverrides(
        buy_active_at=tuple([True] * n_slots),
        buy_ceiling_c_per_kwh=20.0,
        buy_soc_cutoff_pct=60.0,
        conserve_active_at=tuple([False] * n_slots),
        conserve_floor_c_per_kwh=None,
    )
    result = solve_stochastic(
        state=state,
        prices_planning=_prices(imports, exports),
        pv_forecast=None,
        load_profile=_profile(2.0),
        managed_loads=[],
        lp_loads=build_lp_loads(configs=[]),
        battery_config=_battery(),
        mode_overrides=overrides,
    )
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    for slot in result.forward_trajectory:
        assert slot.soc_pct_end <= 60.0 + 1e-4, (
            f"slot {slot.slot_start} ended at SOC {slot.soc_pct_end} > cutoff 60.0"
        )
