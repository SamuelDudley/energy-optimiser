"""Tests for data-integrity fixes:

- #5: DuckDB write buffer (retry on failure, drop oldest on overflow)
- #6: Shelly energy counter reset detection (preserve energy_today_kwh
  across device reboots)
- #10: Schema-versioned telemetry (exclude pre-fix rows from analytics)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from freezegun import freeze_time

from optimiser.clients.shelly import ShellyLoadController
from optimiser.config import ManagedLoadConfig, StorageConfig
from optimiser.store import CURRENT_SCHEMA_VERSION, TelemetryStore
from optimiser.types import LoadCategory, TelemetryRow

UTC = UTC


# ── Helpers ──────────────────────────────────────────────────────


def _store() -> TelemetryStore:
    return TelemetryStore(StorageConfig(db_path=":memory:", snapshot_dir="/tmp/x"))


def _row(ts: datetime, house_load: float | None = 1.5) -> TelemetryRow:
    return TelemetryRow(
        ts=ts,
        soc_pct=50.0,
        battery_kw=0.0,
        pv_kw=0.0,
        grid_kw=house_load,
        grid_kw_shelly=house_load,
        house_load_kw=house_load,
        import_price=20.0,
        export_price=5.0,
        spot_price=6.0,
        renewables_pct=40.0,
        spike_status="none",
        pv_forecast_kw=0.0,
        outdoor_temp_c=20.0,
        occupied=True,
        ems_mode=2,
        planner_action="self_consume",
        planner_reason="test",
    )


def _shelly_cfg() -> ManagedLoadConfig:
    return ManagedLoadConfig(
        load_id="hot_water",
        category=LoadCategory.SIGNAL_DRIVEN,
        shelly_host="test",
        has_relay=True,
        daily_target_kwh=4.0,
    )


# ── #10: schema_version filtering ────────────────────────────────


class TestSchemaVersion:
    def test_new_writes_carry_current_version(self) -> None:
        store = _store()
        ts = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        store.write_telemetry(_row(ts))

        result = store.connection.execute("SELECT schema_version FROM telemetry").fetchone()
        assert result[0] == CURRENT_SCHEMA_VERSION

    def test_legacy_rows_excluded_from_p95(self) -> None:
        """Pre-fix rows (schema_version IS NULL) are excluded from analytics."""
        store = _store()
        ts = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

        # Insert a legacy row directly (no schema_version)
        store.connection.execute(
            """INSERT INTO telemetry (ts, house_load_kw, occupied)
               VALUES (?, ?, true)""",
            [ts, 99.0],  # huge value — would dominate P95 if included
        )

        # Insert a current-schema row via the store
        for i in range(10):
            store.write_telemetry(
                _row(
                    ts + timedelta(minutes=i),
                    house_load=1.0,
                )
            )

        p95 = store.get_rolling_p95(days=365, as_of=ts + timedelta(days=1))
        assert p95 is not None
        assert p95 < 5.0, f"legacy 99.0 row leaked into P95: {p95}"

    def test_legacy_rows_excluded_from_load_profile(self) -> None:
        store = _store()
        ts = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

        # Lots of legacy rows that would otherwise satisfy min_samples
        for i in range(100):
            store.connection.execute(
                """INSERT INTO telemetry (ts, house_load_kw, occupied,
                                          outdoor_temp_c)
                   VALUES (?, ?, true, ?)""",
                [ts + timedelta(minutes=i * 5), 99.0, 20.0],
            )

        # Profile query should return None — no current-schema samples
        result = store.get_load_profile_slots(
            occupied=True,
            as_of=ts + timedelta(days=1),
        )
        assert result is None

    def test_migration_adds_column_to_legacy_table(self) -> None:
        """A pre-existing table without schema_version gets the column added."""
        store = _store()
        # Drop & recreate without schema_version to simulate legacy
        store.connection.execute("DROP TABLE telemetry")
        store.connection.execute("""
            CREATE TABLE telemetry (
                ts TIMESTAMPTZ NOT NULL,
                soc_pct REAL,
                house_load_kw REAL,
                occupied BOOLEAN
            )
        """)
        # Re-run migrations — should add the column
        store._init_tables()
        cols = store.connection.execute("PRAGMA table_info(telemetry)").fetchall()
        col_names = [c[1] for c in cols]
        assert "schema_version" in col_names


# ── #5: DuckDB write buffer ──────────────────────────────────────


class TestWriteBuffer:
    def test_normal_writes_drain_immediately(self) -> None:
        store = _store()
        ts = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        for i in range(5):
            store.write_telemetry(_row(ts + timedelta(minutes=i)))

        pending_t, pending_l = store.pending_count
        assert pending_t == 0
        assert pending_l == 0

        count = store.connection.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
        assert count == 5

    def test_failed_write_retains_in_buffer(self) -> None:
        store = _store()
        ts = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

        # Force the underlying write to raise
        with patch.object(store, "_do_write_telemetry", side_effect=RuntimeError("disk full")):
            store.write_telemetry(_row(ts))
            store.write_telemetry(_row(ts + timedelta(minutes=1)))

        pending_t, _ = store.pending_count
        assert pending_t == 2

        # Recovery: writes succeed on next call
        store.write_telemetry(_row(ts + timedelta(minutes=2)))
        pending_t, _ = store.pending_count
        assert pending_t == 0

        count = store.connection.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
        assert count == 3

    def test_buffer_overflow_drops_oldest_and_emits(self, capsys) -> None:
        store = TelemetryStore(
            StorageConfig(db_path=":memory:", snapshot_dir="/tmp/x"),
            max_buffer=10,
        )
        ts = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

        with patch.object(store, "_do_write_telemetry", side_effect=RuntimeError("disk full")):
            for i in range(15):
                store.write_telemetry(_row(ts + timedelta(minutes=i)))

        pending_t, _ = store.pending_count
        assert pending_t == 10  # capped at max_buffer

        captured = capsys.readouterr()
        assert "validation_reject" in captured.out.lower()
        assert "overflow" in captured.out.lower()


# ── #6: Shelly counter reset ─────────────────────────────────────


class TestShellyCounterReset:
    @freeze_time("2026-01-01 06:00:00", tz_offset=0)
    def test_normal_progression(self) -> None:
        ctrl = ShellyLoadController(_shelly_cfg())
        # Day starts: total = 100 kWh
        ctrl._track_daily_energy(100.0)
        assert ctrl._energy_today_kwh == 0.0

        # Mid-day: counter at 102.5 → 2.5 kWh today
        ctrl._track_daily_energy(102.5)
        assert ctrl._energy_today_kwh == pytest.approx(2.5)

    @freeze_time("2026-01-01 06:00:00", tz_offset=0)
    def test_counter_reset_preserves_today(self, capsys) -> None:
        ctrl = ShellyLoadController(_shelly_cfg())
        ctrl._track_daily_energy(100.0)  # baseline
        ctrl._track_daily_energy(102.5)  # 2.5 kWh today

        # Shelly reboots — counter back to 0, then ticks up
        ctrl._track_daily_energy(0.5)

        # Today's accumulator should still reflect the 2.5 kWh delivered
        # before the reboot (no negative jump, no double-counting).
        assert ctrl._energy_today_kwh == pytest.approx(2.5)

        captured = capsys.readouterr()
        assert "shelly counter reset" in captured.out.lower()

        # Subsequent reads accumulate from the new baseline
        ctrl._track_daily_energy(1.0)
        # 0.5 → 1.0 = +0.5 since reset, plus the preserved 2.5 = 3.0
        assert ctrl._energy_today_kwh == pytest.approx(3.0)

    @freeze_time("2026-01-01 06:00:00", tz_offset=0)
    def test_tiny_backward_jitter_not_treated_as_reset(self) -> None:
        """A backward jump under the threshold is not a reset."""
        ctrl = ShellyLoadController(_shelly_cfg())
        ctrl._track_daily_energy(100.0)
        ctrl._track_daily_energy(102.5)  # +2.5
        # 50 Wh backward — below the 100 Wh threshold
        ctrl._track_daily_energy(102.45)
        # Treated as normal compute path: 102.45 - 100.0 = 2.45 kWh
        assert ctrl._energy_today_kwh == pytest.approx(2.45)

    def test_midnight_rollover_resets_baseline(self) -> None:
        ctrl = ShellyLoadController(_shelly_cfg())
        with freeze_time("2026-01-01 06:00:00"):
            ctrl._track_daily_energy(100.0)
            ctrl._track_daily_energy(103.0)  # 3 kWh today

        # Cross midnight (UTC)
        with freeze_time("2026-01-02 00:00:01"):
            ctrl._track_daily_energy(103.5)
            assert ctrl._energy_today_kwh == 0.0

            ctrl._track_daily_energy(104.0)
            assert ctrl._energy_today_kwh == pytest.approx(0.5)
