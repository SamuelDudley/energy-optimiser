"""Tests for replay's snapshot deserialization helpers.

The replay engine reads NDJSON snapshots written by past versions of
the service. Adding fields to PriceInterval must not break replay over
old snapshots — `_reconstruct_price_interval` falls back to None for
any missing key, and the LP's resolver then falls back to the raw
point price. That guarantees an old-snapshot replay reproduces the
deployed-at-the-time decision exactly.
"""

from __future__ import annotations

from optimiser.replay import _reconstruct_price_interval


def _legacy_dict() -> dict:
    """Snapshot row as written *before* the export-side advancedPrice
    fields existed. Has the import-side forecast_* keys but nothing on
    the export side."""
    return {
        "start": "2026-04-15T00:00:00+00:00",
        "end": "2026-04-15T00:30:00+00:00",
        "import_per_kwh": 25.0,
        "export_per_kwh": 6.0,
        "spot_per_kwh": 9.0,
        "renewables_pct": 45.0,
        "spike_status": "none",
        "descriptor": "neutral",
        "forecast_low": 15.0,
        "forecast_high": 40.0,
        "forecast_predicted": 22.0,
    }


def test_reconstruct_handles_missing_export_forecast_fields() -> None:
    p = _reconstruct_price_interval(_legacy_dict())
    # Import side intact.
    assert p.forecast_predicted == 22.0
    assert p.forecast_low == 15.0
    assert p.forecast_high == 40.0
    # Export-side advancedPrice fields not in the dict — must default
    # to None, leaving the LP's resolver to fall back to export_per_kwh.
    assert p.export_forecast_predicted is None
    assert p.export_forecast_low is None
    assert p.export_forecast_high is None


def test_reconstruct_populates_export_forecast_when_present() -> None:
    d = _legacy_dict()
    d["export_forecast_predicted"] = 4.5
    d["export_forecast_low"] = 3.0
    d["export_forecast_high"] = 6.5
    p = _reconstruct_price_interval(d)
    assert p.export_forecast_predicted == 4.5
    assert p.export_forecast_low == 3.0
    assert p.export_forecast_high == 6.5
