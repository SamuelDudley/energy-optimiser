"""Tests for data validation — covers spec §9.8."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from optimiser.types import TelemetryRow
from optimiser.validation import validate_telemetry

UTC = UTC
NOW = datetime(2026, 4, 3, 7, 0, 0, tzinfo=UTC)


def _make_row(**overrides) -> TelemetryRow:
    defaults = dict(
        ts=NOW,
        soc_pct=50.0,
        battery_kw=0.0,
        pv_kw=3.0,
        grid_kw=1.0,
        grid_kw_shelly=1.1,
        house_load_kw=4.0,
        import_price=20.0,
        export_price=5.0,
        spot_price=6.0,
        renewables_pct=40.0,
        spike_status="none",
        pv_forecast_kw=3.5,
        outdoor_temp_c=20.0,
        occupied=True,
        ems_mode=2,
        planner_action="self_consume",
        planner_reason="test",
    )
    defaults.update(overrides)
    return TelemetryRow(**defaults)


class TestGridSensorOffline:
    def test_nulls_grid_and_load(self) -> None:
        row = _make_row()
        corrected, result = validate_telemetry(
            row,
            grid_sensor_online=False,
            bom_data_age=None,
            rolling_p95=None,
        )
        assert "grid_kw" in result.rejected_fields
        assert "house_load_kw" in result.rejected_fields
        assert corrected.grid_kw is None
        assert corrected.house_load_kw is None

    def test_passes_when_online(self) -> None:
        row = _make_row()
        _, result = validate_telemetry(
            row,
            grid_sensor_online=True,
            bom_data_age=None,
            rolling_p95=None,
        )
        assert "grid_kw" not in result.rejected_fields


class TestNegativeHouseLoad:
    def test_rejects_negative_load(self) -> None:
        row = _make_row(house_load_kw=-1.5)
        corrected, result = validate_telemetry(
            row,
            grid_sensor_online=True,
            bom_data_age=None,
            rolling_p95=None,
        )
        assert "house_load_kw" in result.rejected_fields
        assert corrected.house_load_kw is None

    def test_allows_small_negative(self) -> None:
        """Small negative (-0.05) is within tolerance."""
        row = _make_row(house_load_kw=-0.05)
        _, result = validate_telemetry(
            row,
            grid_sensor_online=True,
            bom_data_age=None,
            rolling_p95=None,
        )
        assert "house_load_kw" not in result.rejected_fields


class TestSOCBounds:
    def test_rejects_soc_over_100(self) -> None:
        row = _make_row(soc_pct=105.0)
        corrected, result = validate_telemetry(
            row,
            grid_sensor_online=True,
            bom_data_age=None,
            rolling_p95=None,
        )
        assert "soc_pct" in result.rejected_fields
        assert corrected.soc_pct is None

    def test_rejects_negative_soc(self) -> None:
        row = _make_row(soc_pct=-5.0)
        _, result = validate_telemetry(
            row,
            grid_sensor_online=True,
            bom_data_age=None,
            rolling_p95=None,
        )
        assert "soc_pct" in result.rejected_fields


class TestStaleBOM:
    def test_rejects_stale_temp(self) -> None:
        row = _make_row(outdoor_temp_c=25.0)
        corrected, result = validate_telemetry(
            row,
            grid_sensor_online=True,
            bom_data_age=timedelta(hours=3),
            rolling_p95=None,
        )
        assert "outdoor_temp_c" in result.rejected_fields
        assert corrected.outdoor_temp_c is None

    def test_accepts_fresh_temp(self) -> None:
        row = _make_row(outdoor_temp_c=25.0)
        _, result = validate_telemetry(
            row,
            grid_sensor_online=True,
            bom_data_age=timedelta(minutes=15),
            rolling_p95=None,
        )
        assert "outdoor_temp_c" not in result.rejected_fields


class TestOutlierDetection:
    def test_flags_outlier(self) -> None:
        row = _make_row(house_load_kw=25.0)
        _, result = validate_telemetry(
            row,
            grid_sensor_online=True,
            bom_data_age=None,
            rolling_p95=5.0,  # 25 > 3×5
        )
        assert any("outlier" in w.lower() for w in result.warnings)
        # Outliers are flagged but NOT rejected
        assert "house_load_kw" not in result.rejected_fields


class TestMainsCTCrossValidation:
    def test_flags_divergence(self) -> None:
        row = _make_row(grid_kw=3.0, grid_kw_shelly=1.5)
        _, result = validate_telemetry(
            row,
            grid_sensor_online=True,
            bom_data_age=None,
            rolling_p95=None,
            grid_kw_shelly=1.5,
        )
        assert any("divergence" in w.lower() for w in result.warnings)

    def test_no_flag_within_tolerance(self) -> None:
        row = _make_row(grid_kw=3.0, grid_kw_shelly=3.2)
        _, result = validate_telemetry(
            row,
            grid_sensor_online=True,
            bom_data_age=None,
            rolling_p95=None,
            grid_kw_shelly=3.2,
        )
        assert not any("divergence" in w.lower() for w in result.warnings)
