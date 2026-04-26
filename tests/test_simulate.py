"""Tests for the closed-loop simulator (simulate.py).

The simulator is heavyweight — each step does a real stochastic LP
solve (~150 ms) — so tests use synthetic 1-day snapshot streams
generated in-memory rather than touching disk."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from optimiser.config import BatteryConfig
from optimiser.simulate import (
    ScenarioModifier,
    SimulationResult,
    _physics_step,
    simulate,
)


# ── Physics step ─────────────────────────────────────────────────


class TestPhysicsStep:
    def test_charge_decreases_grid_export(self) -> None:
        """Battery charging from PV reduces grid export."""
        soc_end, gi, ge = _physics_step(
            soc_pct=50.0,
            battery_kw=3.0,        # charging at 3 kW
            pv_actual_kw=5.0,      # plenty of PV
            house_load_kw=1.0,
            battery_config=BatteryConfig(),
            export_limit_kw=5.0,
            slot_hours=5 / 60.0,
        )
        # PV 5kW, house 1kW, charge 3kW → 1kW left over to grid.
        assert ge == pytest.approx(1.0, abs=0.01)
        assert gi == 0.0
        # SOC should rise: 3 kW × 5/60 h × 0.9 eta = 0.225 kWh / 40 kWh = 0.5625%
        assert soc_end == pytest.approx(50.0 + 0.5625, abs=0.01)

    def test_discharge_reduces_grid_import(self) -> None:
        """Battery discharging covers house load + exports surplus."""
        soc_end, gi, ge = _physics_step(
            soc_pct=70.0,
            battery_kw=-6.0,       # discharging at 6 kW
            pv_actual_kw=0.0,      # no PV (evening)
            house_load_kw=1.0,
            battery_config=BatteryConfig(),
            export_limit_kw=5.0,
            slot_hours=5 / 60.0,
        )
        assert gi == 0.0
        # 6 kW discharge - 1 kW house = 5 kW export (= cap)
        assert ge == pytest.approx(5.0, abs=0.01)
        # SOC drops: 6 × 5/60 = 0.5 kWh / 40 = 1.25%
        assert soc_end == pytest.approx(70.0 - 1.25, abs=0.01)

    def test_export_limit_caps_grid_out(self) -> None:
        """Even with massive PV + massive discharge, export is capped."""
        _, _, ge = _physics_step(
            soc_pct=80.0,
            battery_kw=-10.0,
            pv_actual_kw=15.0,
            house_load_kw=1.0,
            battery_config=BatteryConfig(),
            export_limit_kw=5.0,
            slot_hours=5 / 60.0,
        )
        assert ge == 5.0

    def test_soc_clamps_to_physical_range(self) -> None:
        """No SOC drift past [0, 100]%."""
        soc_end, _, _ = _physics_step(
            soc_pct=99.0,
            battery_kw=20.0,        # absurd charge rate
            pv_actual_kw=20.0,
            house_load_kw=0.0,
            battery_config=BatteryConfig(),
            export_limit_kw=5.0,
            slot_hours=5 / 60.0,
        )
        assert soc_end <= 100.0
        soc_end, _, _ = _physics_step(
            soc_pct=1.0,
            battery_kw=-20.0,
            pv_actual_kw=0.0,
            house_load_kw=0.0,
            battery_config=BatteryConfig(),
            export_limit_kw=5.0,
            slot_hours=5 / 60.0,
        )
        assert soc_end >= 0.0


# ── Scenario modifier ────────────────────────────────────────────


def _make_minimal_snapshot_dict(
    ts: datetime, soc: float = 50.0, pv: float = 0.0
) -> dict:
    """Build a snapshot JSON dict suitable for the reconstruction
    path. Matches the schema the production service emits."""
    iso = lambda t: t.isoformat()  # noqa: E731
    intervals = []
    for i in range(48):
        intervals.append(
            {
                "start": iso(ts + timedelta(minutes=30 * i)),
                "end": iso(ts + timedelta(minutes=30 * (i + 1))),
                "import_per_kwh": 20.0,
                "export_per_kwh": 7.0,
                "spot_per_kwh": 5.0,
                "renewables_pct": 40.0,
                "spike_status": "none",
                "descriptor": "neutral",
                "forecast_low": None,
                "forecast_high": None,
                "forecast_predicted": 18.0,
            }
        )
    pv_intervals = [
        {
            "start": iso(ts + timedelta(minutes=30 * i)),
            "end": iso(ts + timedelta(minutes=30 * (i + 1))),
            "pv_estimate_kw": 5.0,
            "pv_estimate10_kw": 3.5,
            "pv_estimate90_kw": 6.5,
        }
        for i in range(48)
    ]
    return {
        "tick_id": f"tick-{ts.isoformat()}",
        "timestamp": iso(ts),
        "version": "test",
        "system_state": {
            "timestamp": iso(ts),
            "soc_pct": soc,
            "battery_power_kw": 0.0,
            "pv_power_kw": pv,
            "grid_power_kw": 1.0,
            "house_load_kw": 1.0,
            "ems_mode": 2,
            "outdoor_temp_c": 20.0,
            "occupied": True,
        },
        "price_forecast": intervals,
        "pv_forecast": pv_intervals,
        "load_profile": {
            "slots": [1.0] * 48,
            "maturity_level": 0,
            "context": "test",
        },
        "managed_loads": [],
        "maturity_level": 0,
        "output": {
            "battery_action": "self_consume",
            "charge_limit_kw": 0.0,
            "discharge_limit_kw": 0.0,
            "target_soc": None,
            "load_commands": [],
            "grid_export_limit_kw": None,
            "reason": "test",
        },
    }


def _write_snapshot_file(
    path: Path,
    n_steps: int,
    *,
    start: datetime,
    interval_min: int = 1,
    soc_at_start: float = 50.0,
    pv_pattern: list[float] | None = None,
) -> Path:
    """Write a synthetic NDJSON.gz snapshot stream. Returns the path.

    `pv_pattern` cycles through values for system_state.pv_power_kw."""
    pv_pattern = pv_pattern or [0.0]
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for i in range(n_steps):
            ts = start + timedelta(minutes=interval_min * i)
            d = _make_minimal_snapshot_dict(
                ts,
                soc=soc_at_start,
                pv=pv_pattern[i % len(pv_pattern)],
            )
            f.write(json.dumps(d) + "\n")
    return path


class TestScenarioModifier:
    def test_identity_preserves_record(self) -> None:
        """Default modifier (no perturbation) leaves the record unchanged."""
        mod = ScenarioModifier()
        ts = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
        from optimiser.replay import _reconstruct_snapshot
        snap = _reconstruct_snapshot(_make_minimal_snapshot_dict(ts, pv=2.5))
        out = mod.apply_to_snapshot(snap)
        assert out.system_state.pv_power_kw == snap.system_state.pv_power_kw
        assert out.price_forecast[0].import_per_kwh == snap.price_forecast[0].import_per_kwh

    def test_pv_actual_multiplier_scales_realised_pv(self) -> None:
        mod = ScenarioModifier(actual_pv_multiplier=0.3)
        ts = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        from optimiser.replay import _reconstruct_snapshot
        snap = _reconstruct_snapshot(_make_minimal_snapshot_dict(ts, pv=10.0))
        out = mod.apply_to_snapshot(snap)
        assert out.system_state.pv_power_kw == pytest.approx(3.0, abs=0.001)

    def test_forecast_multiplier_independent_of_actual(self) -> None:
        """The two multipliers cover different fields — make sure
        they don't bleed into each other."""
        mod = ScenarioModifier(
            pv_forecast_multiplier=2.0, actual_pv_multiplier=0.5
        )
        ts = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        from optimiser.replay import _reconstruct_snapshot
        snap = _reconstruct_snapshot(_make_minimal_snapshot_dict(ts, pv=5.0))
        out = mod.apply_to_snapshot(snap)
        # actual scaled by 0.5
        assert out.system_state.pv_power_kw == pytest.approx(2.5, abs=0.001)
        # forecast scaled by 2.0
        assert out.pv_forecast[0].pv_estimate_kw == pytest.approx(10.0, abs=0.001)


# ── End-to-end simulation ────────────────────────────────────────


class TestEndToEnd:
    def test_simulation_runs_and_produces_steps(self, tmp_path: Path) -> None:
        """Smoke: a small synthetic snapshot stream produces a simulation
        result with the expected number of steps and no solve failures."""
        path = tmp_path / "snap.ndjson.gz"
        start = datetime(2026, 4, 1, 6, 0, tzinfo=UTC)
        _write_snapshot_file(path, n_steps=12, start=start)
        result = simulate(
            snapshots=[path],
            battery_config=BatteryConfig(soc_floor_pct=15.0),
        )
        # Should have stepped 5-min slots across the snapshot window
        # (12 minutes of snapshots = ~3 sim steps at 5 min cadence
        # depending on snapshot/slot alignment).
        assert isinstance(result, SimulationResult)
        assert len(result.steps) >= 1
        assert result.n_solve_failures == 0

    def test_floor_holds_under_simulator(self, tmp_path: Path) -> None:
        """The simulator respects the LP's hard floor — over a multi-
        slot run starting near the floor, SOC never drifts below."""
        path = tmp_path / "snap.ndjson.gz"
        start = datetime(2026, 4, 1, 18, 0, tzinfo=UTC)  # evening
        _write_snapshot_file(
            path, n_steps=120, start=start, soc_at_start=18.0,
            pv_pattern=[0.0],  # no PV (evening)
        )
        result = simulate(
            snapshots=[path],
            battery_config=BatteryConfig(soc_floor_pct=15.0),
        )
        # Floor must hold across the sim
        assert result.min_soc_pct >= 15.0 - 0.5

    def test_modifier_changes_outcome(self, tmp_path: Path) -> None:
        """A scenario modifier that suppresses PV should produce a
        worse cost than the unmodified baseline (less PV → more grid
        import in physics)."""
        path = tmp_path / "snap.ndjson.gz"
        start = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)  # mid-morning, PV up
        _write_snapshot_file(
            path, n_steps=60, start=start, pv_pattern=[5.0, 5.5, 6.0]
        )
        baseline = simulate(
            snapshots=[path],
            battery_config=BatteryConfig(),
            modifier=ScenarioModifier(name="history"),
        )
        bust = simulate(
            snapshots=[path],
            battery_config=BatteryConfig(),
            modifier=ScenarioModifier(name="bust", actual_pv_multiplier=0.2),
        )
        # PV bust → more grid import → higher cost
        assert bust.total_cost_aud > baseline.total_cost_aud

    def test_initial_soc_override_takes_effect(self, tmp_path: Path) -> None:
        """Explicit initial_soc_pct overrides the snapshot's SOC."""
        path = tmp_path / "snap.ndjson.gz"
        start = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
        _write_snapshot_file(
            path, n_steps=12, start=start, soc_at_start=80.0
        )
        result = simulate(
            snapshots=[path],
            battery_config=BatteryConfig(),
            initial_soc_pct=20.0,
        )
        assert result.steps[0].soc_pct_start == pytest.approx(20.0, abs=0.5)
