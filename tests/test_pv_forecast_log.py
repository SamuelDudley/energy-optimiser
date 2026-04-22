"""Tests for pv_forecast_log end-to-end flow.

Covers:
  - SolcastClient produces PVForecastLogRow per interval per fetch
  - drain_log_rows is destructive (no double-logging)
  - TelemetryStore.write_pv_forecast_log round-trips via DuckDB
  - read_latest_pv_forecast returns fresh cache, None when stale
  - read_latest_pv_forecast filters out expired intervals
  - seed_cache restores without generating log rows
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from optimiser.clients.solcast import SolcastClient
from optimiser.config import SolcastConfig, StorageConfig
from optimiser.store import TelemetryStore
from optimiser.types import PVForecast, PVForecastLogRow


@pytest.fixture
def solcast_config() -> SolcastConfig:
    return SolcastConfig(api_key="k", resource_id="rid")


@pytest.fixture
def store(tmp_path) -> TelemetryStore:
    cfg = StorageConfig(db_path=":memory:", snapshot_dir=str(tmp_path / "snaps"))
    return TelemetryStore(cfg)


def _mock_response(forecasts: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"forecasts": forecasts}
    resp.raise_for_status.return_value = None
    return resp


def _fc(
    period_end: str,
    *,
    pv50: float = 2.5,
    pv10: float = 1.8,
    pv90: float = 3.1,
) -> dict:
    return {
        "period_end": period_end,
        "period": "PT30M",
        "pv_estimate": pv50,
        "pv_estimate10": pv10,
        "pv_estimate90": pv90,
    }


class TestSolcastDrainLogRows:
    async def test_empty_drain_before_fetch(
        self, solcast_config: SolcastConfig,
    ) -> None:
        client = SolcastClient(solcast_config)
        assert client.drain_log_rows() == []

    async def test_fetch_populates_log_rows(
        self, solcast_config: SolcastConfig,
    ) -> None:
        payload = [
            _fc("2026-04-22T12:00:00.0000000Z", pv50=2.5, pv10=1.8, pv90=3.1),
            _fc("2026-04-22T12:30:00.0000000Z", pv50=3.0, pv10=2.0, pv90=3.8),
        ]
        client = SolcastClient(solcast_config)
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=_mock_response(payload))

        await client.get_forecast()
        rows = client.drain_log_rows()

        assert len(rows) == 2
        r0, r1 = rows
        assert r0.pv_estimate_kw == pytest.approx(2.5)
        assert r0.pv_estimate10_kw == pytest.approx(1.8)
        assert r0.pv_estimate90_kw == pytest.approx(3.1)
        assert r0.actual_kw is None  # populated later by a backfill job
        assert r0.period_end == datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
        assert r1.period_end == datetime(2026, 4, 22, 12, 30, tzinfo=UTC)
        # All rows in a single fetch share the same fetched_at
        assert r0.fetched_at == r1.fetched_at

    async def test_drain_is_destructive(
        self, solcast_config: SolcastConfig,
    ) -> None:
        payload = [_fc("2026-04-22T12:00:00.0000000Z")]
        client = SolcastClient(solcast_config)
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=_mock_response(payload))

        await client.get_forecast()
        first = client.drain_log_rows()
        second = client.drain_log_rows()

        assert len(first) == 1
        assert second == []


class TestStorePVForecastLog:
    def test_write_and_read_back(self, store: TelemetryStore) -> None:
        now = datetime.now(UTC).replace(microsecond=0)
        future = now + timedelta(minutes=30)
        rows = [
            PVForecastLogRow(
                fetched_at=now,
                period_end=future,
                pv_estimate_kw=2.5,
                pv_estimate10_kw=1.8,
                pv_estimate90_kw=3.1,
            ),
        ]
        store.write_pv_forecast_log(rows)

        result = store.read_latest_pv_forecast(max_age_minutes=60)
        assert result is not None
        forecasts, fetched_at = result
        assert fetched_at == now
        assert len(forecasts) == 1
        assert forecasts[0].pv_estimate_kw == pytest.approx(2.5)
        assert forecasts[0].end == future

    def test_read_latest_returns_none_when_stale(
        self, store: TelemetryStore,
    ) -> None:
        old = datetime.now(UTC) - timedelta(hours=2)
        rows = [
            PVForecastLogRow(
                fetched_at=old,
                period_end=old + timedelta(hours=3),  # future, but fetch is stale
                pv_estimate_kw=2.5,
                pv_estimate10_kw=1.8,
                pv_estimate90_kw=3.1,
            ),
        ]
        store.write_pv_forecast_log(rows)
        assert store.read_latest_pv_forecast(max_age_minutes=60) is None

    def test_read_latest_filters_expired_intervals(
        self, store: TelemetryStore,
    ) -> None:
        """A 30-min-old fetch is still "fresh" but some of its early
        intervals have now slipped into the past — drop those."""
        now = datetime.now(UTC)
        fetched = now - timedelta(minutes=30)
        rows = [
            PVForecastLogRow(
                fetched_at=fetched,
                period_end=now - timedelta(minutes=10),  # already past
                pv_estimate_kw=0.0,
                pv_estimate10_kw=0.0,
                pv_estimate90_kw=0.0,
            ),
            PVForecastLogRow(
                fetched_at=fetched,
                period_end=now + timedelta(minutes=20),  # still in the future
                pv_estimate_kw=2.5,
                pv_estimate10_kw=1.8,
                pv_estimate90_kw=3.1,
            ),
        ]
        store.write_pv_forecast_log(rows)

        result = store.read_latest_pv_forecast(max_age_minutes=60)
        assert result is not None
        forecasts, _ = result
        assert len(forecasts) == 1
        assert forecasts[0].pv_estimate_kw == pytest.approx(2.5)

    def test_read_latest_returns_none_when_empty(
        self, store: TelemetryStore,
    ) -> None:
        assert store.read_latest_pv_forecast() is None


class TestActualsBackfill:
    async def test_client_parses_actuals_into_period_end_map(
        self, solcast_config: SolcastConfig,
    ) -> None:
        payload = {
            "estimated_actuals": [
                {"period_end": "2026-04-22T12:00:00.0000000Z", "pv_estimate": 8.2},
                {"period_end": "2026-04-22T12:30:00.0000000Z", "pv_estimate": 7.8},
            ]
        }
        client = SolcastClient(solcast_config)
        client._client = MagicMock()
        resp = MagicMock()
        resp.json.return_value = payload
        resp.raise_for_status.return_value = None
        client._client.get = AsyncMock(return_value=resp)

        result = await client.get_actuals_by_period_end()
        assert result == {
            datetime(2026, 4, 22, 12, 0, tzinfo=UTC): 8.2,
            datetime(2026, 4, 22, 12, 30, tzinfo=UTC): 7.8,
        }

    async def test_client_skips_actuals_when_quota_exhausted(
        self, solcast_config: SolcastConfig,
    ) -> None:
        from optimiser.time_utils import now_utc

        client = SolcastClient(solcast_config)
        # Burn the quota. Also set _quota_date to today — otherwise the
        # first _maybe_reset_quota() call resets _call_count_today to 0
        # (it treats None != today.date() as "new day, reset counter").
        client._call_count_today = 10
        client._quota_date = now_utc().date()
        client._client = MagicMock()
        client._client.get = AsyncMock()
        result = await client.get_actuals_by_period_end()
        assert result == {}
        client._client.get.assert_not_called()

    def test_store_update_touches_rows(self, store: TelemetryStore) -> None:
        now = datetime.now(UTC).replace(microsecond=0)
        pe = now + timedelta(hours=1)
        # Two forecast rows for the same period_end at different fetched_at —
        # both should get the actual_kw backfilled (supports calibration
        # analysis of forecast evolution).
        store.write_pv_forecast_log([
            PVForecastLogRow(
                fetched_at=now - timedelta(hours=2),
                period_end=pe,
                pv_estimate_kw=5.0, pv_estimate10_kw=3.5, pv_estimate90_kw=6.5,
            ),
            PVForecastLogRow(
                fetched_at=now - timedelta(hours=1),
                period_end=pe,
                pv_estimate_kw=6.0, pv_estimate10_kw=4.0, pv_estimate90_kw=7.5,
            ),
        ])
        n = store.update_pv_actuals({pe: 5.5})
        assert n == 2
        # Both rows now carry the actual
        rows = store._db.sql(
            "SELECT actual_kw FROM pv_forecast_log ORDER BY fetched_at"
        ).fetchall()
        assert all(r[0] == pytest.approx(5.5) for r in rows)

    def test_store_update_ignores_unmatched_periods(
        self, store: TelemetryStore,
    ) -> None:
        now = datetime.now(UTC).replace(microsecond=0)
        store.write_pv_forecast_log([
            PVForecastLogRow(
                fetched_at=now,
                period_end=now + timedelta(hours=1),
                pv_estimate_kw=5.0, pv_estimate10_kw=3.5, pv_estimate90_kw=6.5,
            ),
        ])
        # Backfill for a period_end that doesn't exist in the log
        n = store.update_pv_actuals({now + timedelta(days=30): 4.2})
        assert n == 0

    def test_store_update_handles_empty_input(self, store: TelemetryStore) -> None:
        assert store.update_pv_actuals({}) == 0


class TestSeedCache:
    async def test_seed_cache_populates_forecast_without_log_rows(
        self, solcast_config: SolcastConfig,
    ) -> None:
        """The startup restore path must not double-log — seed_cache is
        for restoring, not re-emitting. A subsequent drain should be empty."""
        now = datetime.now(UTC)
        forecasts = [
            PVForecast(
                start=now,
                end=now + timedelta(minutes=30),
                pv_estimate_kw=2.5,
                pv_estimate10_kw=1.8,
                pv_estimate90_kw=3.1,
            ),
        ]
        client = SolcastClient(solcast_config)
        client.seed_cache(forecasts, fetched_at=now)

        assert client.last_forecast == forecasts
        assert client.drain_log_rows() == []
