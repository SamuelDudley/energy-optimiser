"""Tests for price_forecast_log end-to-end flow.

Covers:
  - AmberClient produces PriceForecastLogRow per interval per fetch
  - drain_log_rows is destructive (no double-logging)
  - Both cadences land in the drain (5-min + 30-min)
  - TelemetryStore.write_price_forecast_log round-trips via DuckDB
  - Write failures are swallowed (observability, not critical path)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from optimiser.clients.amber import AmberClient
from optimiser.config import AmberConfig, StorageConfig
from optimiser.store import TelemetryStore
from optimiser.types import PriceForecastLogRow


@pytest.fixture
def amber_config() -> AmberConfig:
    return AmberConfig(api_key="k", site_id="s")


@pytest.fixture
def store(tmp_path) -> TelemetryStore:
    cfg = StorageConfig(db_path=":memory:", snapshot_dir=str(tmp_path / "snaps"))
    return TelemetryStore(cfg)


def _mock_response(payload: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    resp.headers = {}
    return resp


def _gen(
    start: str,
    *,
    interval_type: str = "ForecastInterval",
    per_kwh: float = 25.0,
    advanced: dict | None = None,
    estimate: bool | None = None,
) -> dict:
    obj = {
        "type": interval_type,
        "duration": 30,
        "channelType": "general",
        "startTime": start,
        "endTime": start.replace(":00:00", ":30:00"),
        "perKwh": per_kwh,
        "spotPerKwh": 6.0,
        "renewables": 45.0,
        "spikeStatus": "none",
        "descriptor": "neutral",
    }
    if advanced is not None:
        obj["advancedPrice"] = advanced
    if estimate is not None:
        obj["estimate"] = estimate
    return obj


def _fi(
    start: str,
    *,
    interval_type: str = "ForecastInterval",
    per_kwh: float = 6.0,
    advanced: dict | None = None,
    estimate: bool | None = None,
) -> dict:
    obj: dict = {
        "type": interval_type,
        "duration": 30,
        "channelType": "feedIn",
        "startTime": start,
        "endTime": start.replace(":00:00", ":30:00"),
        "perKwh": per_kwh,
        "spotPerKwh": 1.5,
        "renewables": 45.0,
        "spikeStatus": "none",
        "descriptor": "neutral",
    }
    if advanced is not None:
        obj["advancedPrice"] = advanced
    if estimate is not None:
        obj["estimate"] = estimate
    return obj


class TestAmberDrainLogRows:
    async def test_empty_drain_before_fetch(
        self,
        amber_config: AmberConfig,
    ) -> None:
        client = AmberClient(amber_config)
        assert client.drain_log_rows() == []

    async def test_30min_fetch_populates_log_rows(
        self,
        amber_config: AmberConfig,
    ) -> None:
        start = "2026-04-15T00:00:00Z"
        payload = [
            _gen(start, advanced={"low": 10, "predicted": 20, "high": 35}),
            _fi(start),
        ]
        client = AmberClient(amber_config)
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=_mock_response(payload))

        await client.get_current_prices()
        rows = client.drain_log_rows()

        assert len(rows) == 1
        r = rows[0]
        assert r.resolution == 30
        assert r.interval_type == "ForecastInterval"
        assert r.forecast_predicted == pytest.approx(20.0)
        assert r.forecast_low == pytest.approx(10.0)
        assert r.forecast_high == pytest.approx(35.0)
        assert r.per_kwh == pytest.approx(25.0)
        # Amber's feedIn.perKwh=+6.0 is a solar-glut penalty in their ledger
        # convention (customer charged). Our internal convention negates at
        # the boundary so positive = revenue — stored here as -6.0.
        assert r.export_per_kwh == pytest.approx(-6.0)

    async def test_feedin_advancedprice_lands_in_log_row(
        self,
        amber_config: AmberConfig,
    ) -> None:
        """advancedPrice on the feedIn channel populates the export-side
        forecast columns on PriceForecastLogRow with the customer-
        perspective sign convention (positive = revenue from export).
        """
        start = "2026-04-15T00:00:00Z"
        payload = [
            _gen(start, advanced={"low": 10, "predicted": 20, "high": 35}),
            _fi(
                start,
                per_kwh=-3.0,
                advanced={"low": -2.0, "predicted": -3.5, "high": -5.0},
            ),
        ]
        client = AmberClient(amber_config)
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=_mock_response(payload))

        await client.get_current_prices()
        rows = client.drain_log_rows()

        assert len(rows) == 1
        r = rows[0]
        # Import side unchanged.
        assert r.forecast_predicted == pytest.approx(20.0)
        # Export side: signs flipped relative to Amber's ledger view.
        assert r.export_per_kwh == pytest.approx(3.0)
        assert r.export_forecast_predicted == pytest.approx(3.5)
        assert r.export_forecast_low == pytest.approx(2.0)
        assert r.export_forecast_high == pytest.approx(5.0)

    async def test_drain_is_destructive(
        self,
        amber_config: AmberConfig,
    ) -> None:
        """Second drain returns empty. No double-logging."""
        start = "2026-04-15T00:00:00Z"
        payload = [_gen(start), _fi(start)]
        client = AmberClient(amber_config)
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=_mock_response(payload))

        await client.get_current_prices()
        first = client.drain_log_rows()
        second = client.drain_log_rows()

        assert len(first) == 1
        assert second == []

    async def test_both_cadences_flow_through(
        self,
        amber_config: AmberConfig,
    ) -> None:
        """5-min and 30-min fetches both go to the drain with the right
        resolution tag."""
        start = "2026-04-15T00:00:00Z"
        payload = [_gen(start), _fi(start)]
        client = AmberClient(amber_config)
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=_mock_response(payload))

        await client.get_5min_prices()
        await client.get_current_prices()
        rows = client.drain_log_rows()

        assert len(rows) == 2
        resolutions = sorted(r.resolution for r in rows)
        assert resolutions == [5, 30]


class TestForecastLogIndexes:
    def test_forecast_log_tables_have_fetched_at_index(self, store: TelemetryStore) -> None:
        """Both re-fetch logs carry a fetched_at index so the dashboard's
        range-scan reduce queries (/dashboard/price_forecast etc.) stay
        bounded as the tables grow unbounded."""
        names = {
            row[0]
            for row in store.connection.execute(
                "SELECT index_name FROM duckdb_indexes()"
            ).fetchall()
        }
        assert "idx_price_forecast_log_fetched_at" in names
        assert "idx_pv_forecast_log_fetched_at" in names


class TestStorePriceForecastLog:
    def test_write_and_round_trip(self, store: TelemetryStore) -> None:
        ts = datetime(2026, 4, 15, 0, 0, tzinfo=UTC)
        rows = [
            PriceForecastLogRow(
                fetched_at=ts,
                resolution=30,
                interval_start=ts,
                interval_end=datetime(2026, 4, 15, 0, 30, tzinfo=UTC),
                interval_type="ForecastInterval",
                per_kwh=25.0,
                export_per_kwh=6.0,
                spot_per_kwh=9.0,
                forecast_predicted=22.0,
                forecast_low=15.0,
                forecast_high=40.0,
                spike_status="none",
                descriptor="neutral",
                is_locked=None,
                renewables_pct=45.0,
                export_forecast_predicted=4.5,
                export_forecast_low=3.0,
                export_forecast_high=6.5,
            ),
        ]
        store.write_price_forecast_log(rows)

        result = store._db.execute(
            "SELECT COUNT(*), AVG(forecast_predicted), MAX(per_kwh), "
            "AVG(export_forecast_predicted), AVG(export_forecast_low), "
            "AVG(export_forecast_high) "
            "FROM price_forecast_log",
        ).fetchone()
        assert result[0] == 1
        assert result[1] == pytest.approx(22.0)
        assert result[2] == pytest.approx(25.0)
        assert result[3] == pytest.approx(4.5)
        assert result[4] == pytest.approx(3.0)
        assert result[5] == pytest.approx(6.5)

    def test_write_with_default_none_export_forecast(
        self,
        store: TelemetryStore,
    ) -> None:
        """Old call sites that don't pass the new export_forecast_*
        kwargs must still work — fields default to None and DuckDB
        stores NULL. This protects forward-compat with any caller that
        constructs PriceForecastLogRow positionally or with an older
        argument set.
        """
        ts = datetime(2026, 4, 15, 0, 0, tzinfo=UTC)
        row = PriceForecastLogRow(
            fetched_at=ts,
            resolution=30,
            interval_start=ts,
            interval_end=datetime(2026, 4, 15, 0, 30, tzinfo=UTC),
            interval_type="ActualInterval",
            per_kwh=25.0,
            export_per_kwh=6.0,
            spot_per_kwh=9.0,
            forecast_predicted=None,
            forecast_low=None,
            forecast_high=None,
            spike_status="none",
            descriptor="neutral",
            is_locked=None,
            renewables_pct=45.0,
        )
        store.write_price_forecast_log([row])
        result = store._db.execute(
            "SELECT export_forecast_predicted, export_forecast_low, "
            "export_forecast_high FROM price_forecast_log"
        ).fetchone()
        assert result == (None, None, None)

    def test_empty_list_noop(self, store: TelemetryStore) -> None:
        store.write_price_forecast_log([])
        count = store._db.execute(
            "SELECT COUNT(*) FROM price_forecast_log",
        ).fetchone()[0]
        assert count == 0

    def test_batch_of_many_rows(self, store: TelemetryStore) -> None:
        """A batch equivalent in size to a real 30-min fetch lands in
        one executemany call."""
        ts = datetime(2026, 4, 15, 0, 0, tzinfo=UTC)
        # 24 intervals = 12h of 30-min data — simpler to index than a
        # full 36h fetch, while still exercising the multi-row path.
        rows = [
            PriceForecastLogRow(
                fetched_at=ts,
                resolution=30,
                interval_start=datetime(2026, 4, 15, i, 0, tzinfo=UTC),
                interval_end=datetime(2026, 4, 15, i, 30, tzinfo=UTC),
                interval_type="ForecastInterval",
                per_kwh=20.0 + i * 0.5,
                export_per_kwh=5.0,
                spot_per_kwh=6.0,
                forecast_predicted=18.0 + i * 0.5,
                forecast_low=10.0,
                forecast_high=30.0,
                spike_status="none",
                descriptor="neutral",
                is_locked=None,
                renewables_pct=40.0,
            )
            for i in range(24)
        ]
        store.write_price_forecast_log(rows)

        count = store._db.execute(
            "SELECT COUNT(*) FROM price_forecast_log",
        ).fetchone()[0]
        assert count == 24

    def test_malformed_row_swallowed(self, store: TelemetryStore) -> None:
        """A bad row must not crash the service. The whole batch is
        dropped (DuckDB rolls back on error), but the tick continues."""
        bad = MagicMock()  # Not a PriceForecastLogRow — will explode on attribute access
        bad.fetched_at = "not a datetime"
        # executemany will fail, exception gets logged and swallowed
        store.write_price_forecast_log([bad])
        # Store should still be usable
        count = store._db.execute(
            "SELECT COUNT(*) FROM price_forecast_log",
        ).fetchone()[0]
        assert count == 0


class TestMultiIntervalSameFetch:
    """The redundancy is the point: the same interval_start appears in
    multiple fetches as the forecast evolves. Test that we capture that."""

    async def test_successive_fetches_of_same_interval(
        self,
        amber_config: AmberConfig,
        store: TelemetryStore,
    ) -> None:
        start = "2026-04-15T12:00:00Z"
        # First fetch: ForecastInterval, predicted=30c
        payload_1 = [
            _gen(
                start,
                interval_type="ForecastInterval",
                advanced={"low": 20, "predicted": 30, "high": 45},
            ),
            _fi(start),
        ]
        # Second fetch, later: still ForecastInterval, revised to 40c
        payload_2 = [
            _gen(
                start,
                interval_type="ForecastInterval",
                advanced={"low": 28, "predicted": 40, "high": 55},
            ),
            _fi(start),
        ]
        # Third fetch: CurrentInterval, estimate=False (locked)
        payload_3 = [
            _gen(start, interval_type="CurrentInterval", per_kwh=38.0, estimate=False),
            _fi(start),
        ]

        client = AmberClient(amber_config)
        client._client = MagicMock()

        for payload in [payload_1, payload_2, payload_3]:
            client._client.get = AsyncMock(return_value=_mock_response(payload))
            await client.get_current_prices()
            store.write_price_forecast_log(client.drain_log_rows())

        # All three entries for the same interval_start
        result = store._db.execute(
            "SELECT COUNT(*), MIN(forecast_predicted), MAX(forecast_predicted) "
            "FROM price_forecast_log WHERE interval_start = ?",
            [datetime(2026, 4, 15, 12, 0, tzinfo=UTC)],
        ).fetchone()
        assert result[0] == 3
        # Predicted evolved from 30 → 40 → None (CurrentInterval has no advancedPrice here)
        assert result[1] == pytest.approx(30.0)
        assert result[2] == pytest.approx(40.0)

        # Locked flag set on the last entry
        locked_count = store._db.execute(
            "SELECT COUNT(*) FROM price_forecast_log WHERE is_locked = TRUE",
        ).fetchone()[0]
        assert locked_count == 1
