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


def test_conserve_mode_blocks_battery_export_below_floor() -> None:
    """Slot 2 has ep=5c, floor=15c → battery cannot contribute to export at slot 2."""
    n_slots = 12
    imports = [25.0] * n_slots
    # Low export at slot 2 only.
    exports = [20.0, 20.0, 5.0] + [20.0] * (n_slots - 3)
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
        pv_forecast=None,
        load_profile=_profile(2.0),
        managed_loads=[],
        lp_loads=build_lp_loads(configs=[]),
        battery_config=_battery(),
        mode_overrides=overrides,
    )
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    base = result.forward_trajectory
    # Sub-floor slot must export only PV (which is zero here since no
    # PV forecast → 0).
    assert base[2].grid_export_kw <= base[2].pv_to_export_kw + 1e-6


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
