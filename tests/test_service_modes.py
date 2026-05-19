"""Service-level wiring of the ModeManager."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from optimiser.modes import ActiveMode, ModeManager, ModeOverrides
from optimiser.service import Service

# Synthetic "now" sits far in the future so the ModeManager's load-time
# wall-clock check (datetime.now(UTC)) doesn't treat NOW + Nh as past.
NOW = datetime(2099, 5, 19, 4, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_run_lp_forwards_overrides(tmp_path, monkeypatch) -> None:
    """_run_lp pulls overrides from the manager and forwards them to solve_stochastic."""
    from optimiser.lp.result import LPSolution, SlotDecision, SolveStatus
    from optimiser.types import LoadProfile, PriceInterval, SystemState

    captured = {}

    def fake_solve(**kwargs):
        captured["mode_overrides"] = kwargs.get("mode_overrides")
        slot_0 = SlotDecision(
            slot_start=NOW,
            battery_kw=0.0,
            grid_import_kw=0.0,
            grid_export_kw=0.0,
            pv_to_house_kw=0.0,
            pv_to_battery_kw=0.0,
            pv_to_export_kw=0.0,
            soc_pct_end=50.0,
            grid_to_battery_kw=0.0,
        )
        return LPSolution(
            status=SolveStatus.OPTIMAL,
            slot_0=slot_0,
            forward_trajectory=[slot_0],
            load_commands=[],
            grid_export_limit_kw=None,
            expected_total_cost_cents=0.0,
            solve_time_ms=10.0,
            reason="test",
        )

    async def fake_to_thread(func, **kwargs):
        # solve_stochastic is normally sync inside to_thread; here we
        # short-circuit and call our fake directly.
        return func(**kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)
    monkeypatch.setattr("optimiser.service.solve_stochastic", fake_solve)
    # dispatch_from_slot would touch real battery_config; mock it.
    monkeypatch.setattr("optimiser.service.dispatch_from_slot", lambda *a, **k: MagicMock())

    svc = Service.__new__(Service)
    svc._config = MagicMock()
    svc._config.planner.parsed_price_scenario_mode = None
    svc._config.planner.lp_scenario_weights = None
    svc._config.planner.lp_wear_cost_per_kwh = None
    svc._config.planner.lp_terminal_floor_override_pct = None
    svc._config.planner.lp_wall_clock_timeout_s = 30.0
    svc._config.battery = MagicMock()
    svc._lp_loads = []
    svc._metrics = MagicMock()
    svc._mode_manager = ModeManager(tmp_path / "active_modes.json")
    svc._mode_manager.activate(
        ActiveMode(
            kind="buy",
            end_at=NOW + timedelta(hours=2),
            params={"ceiling_c_per_kwh": 12.0},
            activated_at=NOW,
            source="dashboard",
        )
    )

    state = SystemState(
        timestamp=NOW,
        soc_pct=50.0,
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
            start=NOW + timedelta(minutes=5 * i),
            end=NOW + timedelta(minutes=5 * (i + 1)),
            import_per_kwh=8.0,
            export_per_kwh=3.0,
            spot_per_kwh=2.4,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
        )
        for i in range(24)
    ]
    profile = LoadProfile(slots=[2.0] * 48, maturity_level=0, context="test")

    await svc._run_lp(
        state=state,
        prices_planning=prices,
        pv_forecast=None,
        load_profile=profile,
        managed_loads=[],
    )

    overrides = captured["mode_overrides"]
    assert isinstance(overrides, ModeOverrides)
    assert overrides.buy_ceiling_c_per_kwh == 12.0
    assert overrides.any_buy_active()
