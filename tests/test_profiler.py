"""Tests for load profiler — covers spec §9.9."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from freezegun import freeze_time

from optimiser.config import StorageConfig
from optimiser.profiler import assess_maturity, build_load_profile, smooth_slots
from optimiser.store import TelemetryStore
from optimiser.types import LoadTelemetryRow, TelemetryRow

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

    @freeze_time("2026-01-15")
    def test_managed_load_is_subtracted_from_profile(self) -> None:
        """The LP adds its planned managed-load draw to the energy
        balance, so the profile we hand it must already exclude the
        historical managed-load contribution — otherwise the heat pump
        appears twice. Build a flat 1.5 kW house_load with a parallel
        1.0 kW managed load and assert the profile lands at ~0.5 kW."""
        store = _make_store()
        base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        for day in range(10):
            for interval in range(288):
                ts = base + timedelta(days=day, minutes=interval * 5)
                store.write_telemetry(
                    TelemetryRow(
                        ts=ts,
                        soc_pct=50.0,
                        battery_kw=0.0,
                        pv_kw=0.0,
                        grid_kw=1.5,
                        grid_kw_shelly=1.5,
                        house_load_kw=1.5,
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
                )
                store.write_load_telemetry(
                    LoadTelemetryRow(
                        ts=ts,
                        load_id="hot_water",
                        category="signal_driven",
                        power_kw=1.0,
                        energy_today_kwh=None,
                        cycle_state=None,
                        relay_on=True,
                    )
                )

        ts = datetime(2026, 1, 15, 2, 0, 0, tzinfo=UTC)
        profile = build_load_profile(store, outdoor_temp_c=20.0, occupied=True, timestamp=ts)
        # 1.5 (house) − 1.0 (managed) = 0.5 kW residual baseload, every slot.
        assert all(abs(s - 0.5) < 1e-3 for s in profile.slots), profile.slots


class TestSmoothSlots:
    """Pure-function tests for the boxcar smoother."""

    def test_window_1_is_identity(self) -> None:
        raw = [float(i) for i in range(48)]
        assert smooth_slots(raw, window=1) == raw

    def test_window_3_spreads_single_peak(self) -> None:
        raw = [0.0] * 48
        raw[14] = 6.0
        smoothed = smooth_slots(raw, window=3)
        assert abs(smoothed[13] - 2.0) < 1e-9
        assert abs(smoothed[14] - 2.0) < 1e-9
        assert abs(smoothed[15] - 2.0) < 1e-9
        assert smoothed[0] == 0.0
        assert smoothed[30] == 0.0

    def test_wraps_around_day_boundary(self) -> None:
        """The profile is a circular 24h cycle, so a peak at 00:00 should
        bleed into 23:30 of the previous wrap."""
        raw = [0.0] * 48
        raw[0] = 3.0
        smoothed = smooth_slots(raw, window=3)
        assert abs(smoothed[47] - 1.0) < 1e-9
        assert abs(smoothed[0] - 1.0) < 1e-9
        assert abs(smoothed[1] - 1.0) < 1e-9

    def test_preserves_total_energy(self) -> None:
        """Boxcar redistributes but doesn't create or destroy energy — sum
        over the day is invariant. If smoothing changed the daily total the
        LP's expected baseline would shift depending on where peaks land,
        which would be incoherent."""
        raw = [0.0] * 48
        raw[14] = 6.0
        raw[20] = 3.0
        smoothed = smooth_slots(raw, window=5)
        assert abs(sum(smoothed) - sum(raw)) < 1e-9

    def test_rejects_even_window(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="odd"):
            smooth_slots([0.0] * 48, window=4)


class TestMedianStatistic:
    """`statistic="median"` makes the profile robust to single outlier days."""

    @freeze_time("2026-01-15")
    def test_median_ignores_single_outlier_day(self) -> None:
        """9 days at 1.0 kW + 1 outlier day at 10.0 kW:
        mean ≈ 1.9 kW (lifted), median = 1.0 kW (true typical)."""
        store = _make_store()
        base = DATA_START
        for day in range(10):
            load_kw = 10.0 if day == 5 else 1.0
            for interval in range(288):
                ts = base + timedelta(days=day, minutes=interval * 5)
                store.write_telemetry(
                    TelemetryRow(
                        ts=ts,
                        soc_pct=50.0,
                        battery_kw=0.0,
                        pv_kw=0.0,
                        grid_kw=load_kw,
                        grid_kw_shelly=load_kw,
                        house_load_kw=load_kw,
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
                )

        ts = datetime(2026, 1, 15, 2, 0, 0, tzinfo=UTC)
        profile_mean = build_load_profile(
            store,
            outdoor_temp_c=20.0,
            occupied=True,
            timestamp=ts,
            statistic="mean",
        )
        profile_median = build_load_profile(
            store,
            outdoor_temp_c=20.0,
            occupied=True,
            timestamp=ts,
            statistic="median",
        )

        mid_slot = 20
        assert profile_mean.slots[mid_slot] > 1.5, profile_mean.slots[mid_slot]
        assert abs(profile_median.slots[mid_slot] - 1.0) < 0.05, profile_median.slots[mid_slot]

    @freeze_time("2026-01-15")
    def test_unknown_statistic_rejected(self) -> None:
        import pytest

        store = _make_store()
        _fill_store(store, days=10)
        ts = datetime(2026, 1, 15, 2, 0, 0, tzinfo=UTC)
        with pytest.raises(ValueError, match="statistic"):
            build_load_profile(
                store,
                outdoor_temp_c=20.0,
                occupied=True,
                timestamp=ts,
                statistic="mode",
            )


class TestSmoothingIntegration:
    """build_load_profile applies smoothing after store aggregation."""

    @freeze_time("2026-01-15")
    def test_smoothing_softens_peak(self) -> None:
        """_fill_store builds a daily pattern with morning + evening peaks.
        Smoothing should reduce the peak-slot value while raising neighbours,
        and preserve the daily total."""
        store = _make_store()
        _fill_store(store, days=10)
        ts = datetime(2026, 1, 15, 2, 0, 0, tzinfo=UTC)

        raw = build_load_profile(
            store,
            outdoor_temp_c=20.0,
            occupied=True,
            timestamp=ts,
            smoothing_slots=1,
        )
        smoothed = build_load_profile(
            store,
            outdoor_temp_c=20.0,
            occupied=True,
            timestamp=ts,
            smoothing_slots=3,
        )

        peak_idx = max(range(48), key=lambda i: raw.slots[i])
        assert smoothed.slots[peak_idx] < raw.slots[peak_idx]
        assert abs(sum(smoothed.slots) - sum(raw.slots)) < 1e-6
