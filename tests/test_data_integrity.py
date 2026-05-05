"""Tests for data-integrity fixes:

- #5: DuckDB write buffer (retry on failure, drop oldest on overflow)
- #6: Shelly energy counter reset detection (preserve energy_today_kwh
  across device reboots)
- #10: Schema-versioned telemetry (exclude pre-fix rows from analytics)
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from freezegun import freeze_time

from optimiser.clients.shelly import ShellyLoadController
from optimiser.config import ManagedLoadConfig, StorageConfig
from optimiser.store import CURRENT_SCHEMA_VERSION, TelemetryStore
from optimiser.types import LoadCategory, LoadCycleState, TelemetryRow

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


# ── Extended inverter telemetry (2026-04) ───────────────────────


class TestExtendedInverterFields:
    """Round-trip tests for the extended observational columns added for
    backtest data coverage (battery thermal, alarms, lifetime counters,
    per-MPPT, grid quality, commanded-mode readback)."""

    def test_extended_fields_round_trip(self) -> None:
        store = _store()
        ts = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        row = TelemetryRow(
            ts=ts,
            soc_pct=50.0,
            battery_kw=0.0,
            pv_kw=0.0,
            grid_kw=1.0,
            grid_kw_shelly=1.0,
            house_load_kw=1.0,
            import_price=20.0,
            export_price=5.0,
            spot_price=6.0,
            renewables_pct=40.0,
            spike_status="none",
            pv_forecast_kw=0.0,
            outdoor_temp_c=20.0,
            occupied=True,
            ems_mode=7,
            planner_action="self_consume",
            planner_reason="test",
            soh_pct=99.2,
            cell_temp_avg_c=22.4,
            cell_temp_max_c=23.1,
            cell_temp_min_c=21.7,
            cell_volt_avg_v=3.35,
            pcs_temp_c=35.5,
            available_charge_kw=8.2,
            available_discharge_kw=9.1,
            running_state=1,
            alarm1=0,
            alarm2=0,
            alarm3=0,
            alarm4=0,
            alarm5=0,
            lifetime_pv_kwh=12345.67,
            lifetime_load_kwh=8765.43,
            lifetime_charge_kwh=4321.0,
            lifetime_discharge_kwh=4100.5,
            lifetime_import_kwh=2000.0,
            lifetime_export_kwh=1500.0,
            mppt1_voltage_v=420.3,
            mppt1_current_a=6.1,
            mppt2_voltage_v=415.7,
            mppt2_current_a=5.9,
            mppt3_voltage_v=None,
            mppt3_current_a=None,
            mppt4_voltage_v=None,
            mppt4_current_a=None,
            grid_freq_hz=50.01,
            phase_a_voltage_v=239.4,
            phase_b_voltage_v=240.1,
            phase_c_voltage_v=238.9,
            remote_ems_mode=2,
        )
        store.write_telemetry(row)

        out = store.connection.execute(
            """SELECT soh_pct, cell_temp_avg_c, available_charge_kw,
                      lifetime_pv_kwh, mppt1_voltage_v, mppt3_voltage_v,
                      grid_freq_hz, remote_ems_mode, alarm1
               FROM telemetry"""
        ).fetchone()
        assert out[0] == pytest.approx(99.2)
        assert out[1] == pytest.approx(22.4)
        assert out[2] == pytest.approx(8.2)
        # Lifetime counters stored as DOUBLE → full precision preserved.
        assert out[3] == pytest.approx(12345.67)
        assert out[4] == pytest.approx(420.3)
        assert out[5] is None
        assert out[6] == pytest.approx(50.01)
        assert out[7] == 2
        assert out[8] == 0

    def test_legacy_row_without_extended_fields(self) -> None:
        """A row constructed with only legacy fields still writes cleanly;
        new columns come back as NULL."""
        store = _store()
        ts = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        store.write_telemetry(_row(ts))  # _row omits every extended field

        out = store.connection.execute(
            "SELECT soh_pct, lifetime_pv_kwh, remote_ems_mode FROM telemetry"
        ).fetchone()
        assert out == (None, None, None)


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

    @freeze_time("2026-01-01 06:00:00", tz_offset=0)
    def test_bidirectional_net_energy(self) -> None:
        """Mains CT exercises both counters; today reads net (imp − exp)."""
        ctrl = ShellyLoadController(_shelly_cfg())
        # Baseline: 500 kWh imported lifetime, 27000 kWh exported lifetime.
        ctrl._track_daily_energy(500.0, 27000.0)
        assert ctrl._energy_today_kwh == 0.0

        # Throughout the day: +1 kWh imp, +5 kWh exp → net −4 kWh.
        ctrl._track_daily_energy(501.0, 27005.0)
        assert ctrl._energy_today_kwh == pytest.approx(-4.0)

        # More export: +0 imp, +3 more exp → net −7 kWh.
        ctrl._track_daily_energy(501.0, 27008.0)
        assert ctrl._energy_today_kwh == pytest.approx(-7.0)

    @freeze_time("2026-01-01 06:00:00", tz_offset=0)
    def test_export_counter_reset_preserves_today(self, capsys) -> None:
        """A reboot that resets the export-side counter shouldn't poison net."""
        ctrl = ShellyLoadController(_shelly_cfg())
        ctrl._track_daily_energy(500.0, 27000.0)
        ctrl._track_daily_energy(501.0, 27005.0)  # net −4

        # Reboot: both counters reset to 0; new session starts.
        ctrl._track_daily_energy(0.0, 0.0)
        # Today preserved across the reboot (no double-counting):
        assert ctrl._energy_today_kwh == pytest.approx(-4.0)

        captured = capsys.readouterr()
        assert "shelly counter reset" in captured.out.lower()

        # Subsequent reads accumulate from the new baselines.
        ctrl._track_daily_energy(0.5, 1.0)  # +0.5 imp, +1.0 exp = −0.5 since reboot
        # Preserved −4 + (−0.5) = −4.5
        assert ctrl._energy_today_kwh == pytest.approx(-4.5)


# ── Relay state-change tracking (SIGNAL_DRIVEN_CONTINUOUS carry-over) ──


class TestRelayStateTracking:
    def test_first_observation_anchors_timestamp(self) -> None:
        ctrl = ShellyLoadController(_shelly_cfg())
        assert ctrl._relay_state_since is None
        with freeze_time("2026-01-01 06:00:00"):
            ctrl._track_relay_state(True)
        assert ctrl._last_relay_state is True
        assert ctrl._relay_state_since == datetime(2026, 1, 1, 6, 0, 0, tzinfo=UTC)

    def test_steady_state_does_not_advance_timestamp(self) -> None:
        ctrl = ShellyLoadController(_shelly_cfg())
        with freeze_time("2026-01-01 06:00:00"):
            ctrl._track_relay_state(True)
            anchor = ctrl._relay_state_since
        with freeze_time("2026-01-01 06:05:00"):
            ctrl._track_relay_state(True)  # still on, no transition
        assert ctrl._relay_state_since == anchor

    def test_transition_advances_timestamp(self) -> None:
        ctrl = ShellyLoadController(_shelly_cfg())
        with freeze_time("2026-01-01 06:00:00"):
            ctrl._track_relay_state(True)
        with freeze_time("2026-01-01 06:30:00"):
            ctrl._track_relay_state(False)  # transition
        assert ctrl._last_relay_state is False
        assert ctrl._relay_state_since == datetime(2026, 1, 1, 6, 30, 0, tzinfo=UTC)


# ── Cycle state for SIGNAL_DRIVEN_CONTINUOUS ──────────────────────


def _continuous_cfg(daily_target_kwh: float = 4.0) -> ManagedLoadConfig:
    return ManagedLoadConfig(
        load_id="hot_water",
        category=LoadCategory.SIGNAL_DRIVEN_CONTINUOUS,
        shelly_host="test",
        has_relay=True,
        daily_target_kwh=daily_target_kwh,
        min_on_slots=12,
        min_off_slots=4,
    )


class TestCycleStateContinuous:
    def test_relay_off_below_target_is_idle(self) -> None:
        ctrl = ShellyLoadController(_continuous_cfg())
        ctrl._energy_today_imp_kwh = 1.5
        ctrl._update_cycle_state_continuous(relay_on=False)
        assert ctrl._cycle_state == LoadCycleState.IDLE

    def test_relay_on_below_target_is_running(self) -> None:
        ctrl = ShellyLoadController(_continuous_cfg())
        ctrl._energy_today_imp_kwh = 1.5
        ctrl._update_cycle_state_continuous(relay_on=True)
        assert ctrl._cycle_state == LoadCycleState.RUNNING

    def test_target_met_latches_complete_today(self) -> None:
        ctrl = ShellyLoadController(_continuous_cfg(daily_target_kwh=4.0))
        ctrl._energy_today_imp_kwh = 4.0
        ctrl._update_cycle_state_continuous(relay_on=True)
        assert ctrl._cycle_state == LoadCycleState.COMPLETE_TODAY

    def test_target_met_overrides_relay_off(self) -> None:
        ctrl = ShellyLoadController(_continuous_cfg(daily_target_kwh=4.0))
        ctrl._energy_today_imp_kwh = 4.2  # past target
        ctrl._update_cycle_state_continuous(relay_on=False)
        assert ctrl._cycle_state == LoadCycleState.COMPLETE_TODAY

    def test_no_target_never_completes(self) -> None:
        # daily_target_kwh=None means no target — only IDLE/RUNNING.
        ctrl = ShellyLoadController(_continuous_cfg())
        ctrl._config = replace(ctrl._config, daily_target_kwh=None)
        ctrl._energy_today_imp_kwh = 999.0
        ctrl._update_cycle_state_continuous(relay_on=True)
        assert ctrl._cycle_state == LoadCycleState.RUNNING

    def test_midnight_reset_clears_complete(self) -> None:
        # COMPLETE_TODAY is latched while energy_today_kwh ≥ target. After
        # `_track_daily_energy` rolls the counter at midnight (back to 0),
        # the next status() call should drop us back to IDLE/RUNNING based
        # on relay state alone.
        ctrl = ShellyLoadController(_continuous_cfg(daily_target_kwh=4.0))
        ctrl._energy_today_imp_kwh = 4.5
        ctrl._update_cycle_state_continuous(relay_on=False)
        assert ctrl._cycle_state == LoadCycleState.COMPLETE_TODAY
        ctrl._energy_today_imp_kwh = 0.0  # midnight reset (handled elsewhere)
        ctrl._update_cycle_state_continuous(relay_on=False)
        assert ctrl._cycle_state == LoadCycleState.IDLE


# ── Time-mode (daily_run_minutes) ─────────────────────────────────


def _time_mode_cfg(daily_run_minutes: int = 240) -> ManagedLoadConfig:
    return ManagedLoadConfig(
        load_id="hot_water",
        category=LoadCategory.SIGNAL_DRIVEN_CONTINUOUS,
        shelly_host="test",
        has_relay=True,
        daily_target_kwh=None,
        daily_run_minutes=daily_run_minutes,
        min_on_slots=48,
        min_off_slots=4,
    )


class TestRelayOnMinutesAccumulator:
    """Right-Riemann integration of relay-on time across status() polls."""

    def test_first_poll_anchors_no_accumulation(self) -> None:
        ctrl = ShellyLoadController(_time_mode_cfg())
        with freeze_time("2026-05-03 02:00:00", tz_offset=0):
            ctrl._track_relay_on_minutes(relay_on=True)
        # Local-day initialisation seeded the anchor; no minutes counted
        # because no prior poll to integrate from.
        assert ctrl._relay_on_minutes_today == 0.0
        assert ctrl._last_status_at is not None

    def test_relay_on_for_one_minute_accumulates_one(self) -> None:
        ctrl = ShellyLoadController(_time_mode_cfg())
        # Two polls 60 s apart, both observing relay-on. Right-Riemann
        # uses the *current* observation, so the second poll's interval
        # contributes 1.0 min to the accumulator.
        with freeze_time("2026-05-03 02:00:00", tz_offset=0):
            ctrl._track_relay_on_minutes(relay_on=True)
        with freeze_time("2026-05-03 02:01:00", tz_offset=0):
            ctrl._track_relay_on_minutes(relay_on=True)
        assert ctrl._relay_on_minutes_today == pytest.approx(1.0, abs=1e-6)

    def test_relay_off_intervals_not_counted(self) -> None:
        ctrl = ShellyLoadController(_time_mode_cfg())
        with freeze_time("2026-05-03 02:00:00", tz_offset=0):
            ctrl._track_relay_on_minutes(relay_on=False)
        with freeze_time("2026-05-03 02:01:00", tz_offset=0):
            ctrl._track_relay_on_minutes(relay_on=False)
        with freeze_time("2026-05-03 02:02:00", tz_offset=0):
            ctrl._track_relay_on_minutes(relay_on=False)
        assert ctrl._relay_on_minutes_today == 0.0

    def test_local_midnight_resets_counter(self) -> None:
        # Canberra is UTC+10/+11. Use a fixed UTC instant just past local
        # midnight to exercise the local-date rollover path.
        ctrl = ShellyLoadController(_time_mode_cfg())
        # Day 1 (local 2026-05-03): accumulate some on-time.
        # 12:00 UTC = 22:00 AEST local 2026-05-03.
        with freeze_time("2026-05-03 12:00:00", tz_offset=0):
            ctrl._track_relay_on_minutes(relay_on=True)
        with freeze_time("2026-05-03 12:30:00", tz_offset=0):
            ctrl._track_relay_on_minutes(relay_on=True)
        assert ctrl._relay_on_minutes_today == pytest.approx(30.0, abs=1e-6)
        # 14:30 UTC = 00:30 AEST local 2026-05-04 — local-date crossed.
        with freeze_time("2026-05-03 14:30:00", tz_offset=0):
            ctrl._track_relay_on_minutes(relay_on=True)
        assert ctrl._relay_on_minutes_today == 0.0

    def test_complete_today_latches_when_minutes_target_met(self) -> None:
        ctrl = ShellyLoadController(_time_mode_cfg(daily_run_minutes=240))
        ctrl._relay_on_minutes_today = 240.0
        ctrl._update_cycle_state_continuous(relay_on=True)
        assert ctrl._cycle_state == LoadCycleState.COMPLETE_TODAY

    def test_complete_today_latches_overrides_relay_off(self) -> None:
        ctrl = ShellyLoadController(_time_mode_cfg(daily_run_minutes=240))
        ctrl._relay_on_minutes_today = 240.0
        ctrl._update_cycle_state_continuous(relay_on=False)
        assert ctrl._cycle_state == LoadCycleState.COMPLETE_TODAY

    def test_below_minutes_target_runs_when_relay_on(self) -> None:
        ctrl = ShellyLoadController(_time_mode_cfg(daily_run_minutes=240))
        ctrl._relay_on_minutes_today = 100.0  # well below target
        ctrl._update_cycle_state_continuous(relay_on=True)
        assert ctrl._cycle_state == LoadCycleState.RUNNING


# ── Config parser-time validation (mode-exclusivity) ──────────────


class TestSignalDrivenConfigValidation:
    """Parser-time enforcement: signal_driven loads need exactly one of
    daily_target_kwh / daily_run_minutes."""

    def test_neither_set_raises(self) -> None:
        from optimiser.config import _validate_signal_driven_target

        with pytest.raises(ValueError, match="exactly one"):
            _validate_signal_driven_target(
                {
                    "load_id": "test_load",
                    "category": "signal_driven",
                }
            )

    def test_both_set_raises(self) -> None:
        from optimiser.config import _validate_signal_driven_target

        with pytest.raises(ValueError, match="not both"):
            _validate_signal_driven_target(
                {
                    "load_id": "test_load",
                    "category": "signal_driven",
                    "daily_target_kwh": 4.0,
                    "daily_run_minutes": 240,
                }
            )

    def test_negative_kwh_raises(self) -> None:
        from optimiser.config import _validate_signal_driven_target

        with pytest.raises(ValueError, match="must be > 0"):
            _validate_signal_driven_target(
                {
                    "load_id": "test_load",
                    "category": "signal_driven",
                    "daily_target_kwh": -1.0,
                }
            )

    def test_zero_minutes_raises(self) -> None:
        from optimiser.config import _validate_signal_driven_target

        with pytest.raises(ValueError, match="positive integer"):
            _validate_signal_driven_target(
                {
                    "load_id": "test_load",
                    "category": "signal_driven",
                    "daily_run_minutes": 0,
                }
            )

    def test_observable_skipped(self) -> None:
        # Observable / shiftable loads don't have either field; validator
        # must early-return for unrelated categories.
        from optimiser.config import _validate_signal_driven_target

        _validate_signal_driven_target(
            {
                "load_id": "mains",
                "category": "observable",
            }
        )  # no exception

    def test_kwh_only_passes(self) -> None:
        from optimiser.config import _validate_signal_driven_target

        _validate_signal_driven_target(
            {
                "load_id": "test_load",
                "category": "signal_driven_continuous",
                "daily_target_kwh": 4.0,
            }
        )

    def test_minutes_only_passes(self) -> None:
        from optimiser.config import _validate_signal_driven_target

        _validate_signal_driven_target(
            {
                "load_id": "test_load",
                "category": "signal_driven_continuous",
                "daily_run_minutes": 240,
            }
        )
