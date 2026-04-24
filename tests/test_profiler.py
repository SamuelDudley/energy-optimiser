"""Tests for load profiler — covers spec §9.9."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from freezegun import freeze_time

from optimiser.config import StorageConfig
from optimiser.profiler import assess_maturity, build_load_profile
from optimiser.store import TelemetryStore
from optimiser.types import TelemetryRow

UTC = UTC
# All test data starts from this date
DATA_START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _make_store() -> TelemetryStore:
    """Create an in-memory DuckDB store for testing."""
    config = StorageConfig(
        db_path=":memory:",
        snapshot_dir="/tmp/test-snapshots",
    )
    return TelemetryStore(config)


def _fill_store(store: TelemetryStore, days: int, temp: float = 20.0) -> None:
    """Fill store with synthetic telemetry data."""
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    for day in range(days):
        for interval in range(288):  # 5-min intervals per day
            ts = base + timedelta(days=day, minutes=interval * 5)
            hour = (ts.hour + 10) % 24  # approximate local hour
            # Simulate daily load pattern
            if 7 <= hour <= 9 or 17 <= hour <= 21:
                load = 3.5 + (hour - 17) * 0.2 if hour >= 17 else 2.5
            else:
                load = 0.8

            store.write_telemetry(
                TelemetryRow(
                    ts=ts,
                    soc_pct=50.0,
                    battery_kw=0.0,
                    pv_kw=0.0,
                    grid_kw=load,
                    grid_kw_shelly=load,
                    house_load_kw=load,
                    import_price=20.0,
                    export_price=5.0,
                    spot_price=6.0,
                    renewables_pct=40.0,
                    spike_status="none",
                    pv_forecast_kw=0.0,
                    outdoor_temp_c=temp,
                    occupied=True,
                    ems_mode=2,
                    planner_action="self_consume",
                    planner_reason="test",
                )
            )


class TestMaturityLevels:
    def test_empty_db_is_l0(self) -> None:
        store = _make_store()
        assert assess_maturity(store) == 0

    def test_one_week_is_l1(self) -> None:
        store = _make_store()
        _fill_store(store, days=10)
        assert assess_maturity(store) == 1

    def test_one_month_is_l2(self) -> None:
        store = _make_store()
        _fill_store(store, days=35)
        assert assess_maturity(store) == 2


class TestBuildLoadProfile:
    def test_cold_start_returns_default(self) -> None:
        store = _make_store()
        profile = build_load_profile(store, occupied=True)
        assert profile.maturity_level == 0
        assert profile.context == "L0 default"
        assert len(profile.slots) == 48
        assert all(s == 1.0 for s in profile.slots)

    def test_cold_start_unoccupied(self) -> None:
        store = _make_store()
        profile = build_load_profile(store, occupied=False)
        assert all(s == 0.5 for s in profile.slots)

    @freeze_time("2026-01-15")
    def test_with_data_returns_profile(self) -> None:
        store = _make_store()
        _fill_store(store, days=10)
        ts = datetime(2026, 1, 15, 2, 0, 0, tzinfo=UTC)
        profile = build_load_profile(store, outdoor_temp_c=20.0, occupied=True, timestamp=ts)
        assert profile.maturity_level >= 1
        assert len(profile.slots) == 48
        # Should show some variation (not all same value)
        assert max(profile.slots) > min(profile.slots)

    @freeze_time("2026-01-15")
    def test_fallback_when_bucket_empty(self) -> None:
        store = _make_store()
        # Fill with mild temps only
        _fill_store(store, days=10, temp=20.0)
        # Request cold profile — should fall back to broader bucket
        ts = datetime(2026, 1, 15, 2, 0, 0, tzinfo=UTC)
        profile = build_load_profile(store, outdoor_temp_c=5.0, occupied=True, timestamp=ts)
        assert profile.maturity_level >= 0
        assert len(profile.slots) == 48
