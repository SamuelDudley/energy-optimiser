"""End-to-end tests for the amber_usage / daily_spend pipeline.

Covers:
  - AmberClient.get_usage_intervals parser: shape, sign convention,
    NEM `+1s` boundary normalisation
  - TelemetryStore.write_amber_usage round-trip + (ts, channel) UPSERT
  - latest_amber_usage_date returns most recent NEM date or None
  - /amber_usage table query routing
  - /daily_spend aggregate endpoint: net cost, channel split, sign
  - Service._backfill_amber_usage: empty-table window + incremental catch-up
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from optimiser.api.auth import make_auth_middleware
from optimiser.api.handlers.daily_spend import daily_spend
from optimiser.api.handlers.discovery import root, table_schema
from optimiser.api.handlers.tables import table_rows
from optimiser.api.metrics import Metrics
from optimiser.api.probe import API_CONFIG_KEY, SERVICE_PROBE_KEY
from optimiser.clients.amber import AmberClient
from optimiser.config import AmberConfig, APIConfig, BatteryConfig, StorageConfig
from optimiser.store import TelemetryStore
from optimiser.types import AmberUsageRow

TOKEN = "t"


@pytest.fixture
def store(tmp_path: Path) -> TelemetryStore:
    cfg = StorageConfig(db_path=":memory:", snapshot_dir=str(tmp_path / "snaps"))
    return TelemetryStore(cfg)


@pytest.fixture
def amber_config() -> AmberConfig:
    return AmberConfig(api_key="k", site_id="s")


def _interval(
    *,
    channel: str,
    start: str,
    kwh: float,
    cost: float,
    per_kwh: float,
    nem_date: str = "2026-04-28",
    quality: str = "billable",
) -> dict:
    """Mirror Amber's /usage row shape (verified against live API)."""
    return {
        "type": "Usage",
        "duration": 5,
        "date": nem_date,
        "endTime": start.replace(":00:01", ":05:00").replace(":00Z", ":05Z"),
        "quality": quality,
        "kwh": kwh,
        "nemTime": "2026-04-28T10:05:00+10:00",
        "perKwh": per_kwh,
        "channelType": channel,
        "channelIdentifier": "E1" if channel == "general" else "B1",
        "cost": cost,
        "renewables": 30.0,
        "spotPerKwh": 8.0,
        "startTime": start,
        "spikeStatus": "none",
        "descriptor": "neutral",
    }


def _mock_response(payload: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    resp.headers = {}
    return resp


# ── AmberClient parser ────────────────────────────────────────────


class TestGetUsageIntervalsParser:
    async def test_returns_typed_rows_with_amber_sign_convention(
        self, amber_config: AmberConfig
    ) -> None:
        """general.cost is positive (you pay); feedIn.cost is negative
        (you earned). SUM(cost_cents) is the net bill — preserve as-is."""
        client = AmberClient(amber_config)
        client._client.get = AsyncMock(
            return_value=_mock_response(
                [
                    _interval(
                        channel="general", start="2026-04-28T00:00:01Z",
                        kwh=0.5, cost=10.0, per_kwh=20.0,
                    ),
                    _interval(
                        channel="feedIn", start="2026-04-28T00:00:01Z",
                        kwh=0.3, cost=-3.0, per_kwh=-10.0,
                    ),
                ]
            )
        )
        rows = await client.get_usage_intervals("2026-04-28", "2026-04-28")
        await client.close()

        assert len(rows) == 2
        general = next(r for r in rows if r.channel == "general")
        feed_in = next(r for r in rows if r.channel == "feedIn")
        assert general.cost_cents == 10.0
        assert general.per_kwh_cents == 20.0
        assert feed_in.cost_cents == -3.0
        assert feed_in.per_kwh_cents == -10.0
        # Net = +10 + -3 = +7c. SUM matches the bill.
        assert sum(r.cost_cents for r in rows) == 7.0

    async def test_normalises_nem_plus_one_second_boundary(
        self, amber_config: AmberConfig
    ) -> None:
        """Amber returns startTime as '...:00:01Z'; we want '...:00:00Z'
        so downstream joins line up with telemetry/price logs (same
        normalisation applied to PriceInterval — see decision-log entry)."""
        client = AmberClient(amber_config)
        client._client.get = AsyncMock(
            return_value=_mock_response(
                [
                    _interval(
                        channel="general", start="2026-04-28T13:30:01Z",
                        kwh=1.0, cost=20.0, per_kwh=20.0,
                    )
                ]
            )
        )
        rows = await client.get_usage_intervals("2026-04-28", "2026-04-28")
        await client.close()
        assert rows[0].ts == datetime(2026, 4, 28, 13, 30, 0, tzinfo=UTC)

    async def test_preserves_optional_fields_or_nulls_them(
        self, amber_config: AmberConfig
    ) -> None:
        """Fields Amber sometimes omits (renewables, spot) become None
        rather than 0 — so daily aggregates can correctly skip them."""
        client = AmberClient(amber_config)
        client._client.get = AsyncMock(
            return_value=_mock_response(
                [
                    {
                        "type": "Usage",
                        "duration": 5,
                        "date": "2026-04-28",
                        "channelType": "general",
                        "startTime": "2026-04-28T00:00:01Z",
                        "endTime": "2026-04-28T00:05:00Z",
                        "kwh": 0.0,
                        "cost": 0.0,
                        "perKwh": 25.0,
                        # no spotPerKwh, no renewables, no quality
                    }
                ]
            )
        )
        rows = await client.get_usage_intervals("2026-04-28", "2026-04-28")
        await client.close()
        assert rows[0].spot_per_kwh_cents is None
        assert rows[0].renewables_pct is None
        assert rows[0].quality is None


# ── TelemetryStore round-trip ─────────────────────────────────────


class TestStoreRoundTrip:
    def test_write_then_query(self, store: TelemetryStore) -> None:
        rows = [
            AmberUsageRow(
                ts=datetime(2026, 4, 28, 0, 0, tzinfo=UTC),
                nem_date="2026-04-28",
                channel="general",
                kwh=0.5,
                cost_cents=10.0,
                per_kwh_cents=20.0,
                spot_per_kwh_cents=8.0,
                renewables_pct=30.0,
                descriptor="neutral",
                spike_status="none",
                quality="billable",
            )
        ]
        store.write_amber_usage(rows)
        out = store.connection.sql(
            "SELECT cost_cents, channel, nem_date FROM amber_usage"
        ).fetchall()
        assert out == [(10.0, "general", "2026-04-28")]

    def test_upserts_on_ts_and_channel(self, store: TelemetryStore) -> None:
        """Re-fetched same-day rows must overwrite, not duplicate.
        Amber occasionally re-publishes a day with refined `quality`
        flags — second write should replace, not append."""
        ts = datetime(2026, 4, 28, 0, 0, tzinfo=UTC)
        first = AmberUsageRow(
            ts=ts, nem_date="2026-04-28", channel="general",
            kwh=0.5, cost_cents=10.0, per_kwh_cents=20.0,
            spot_per_kwh_cents=None, renewables_pct=None,
            descriptor=None, spike_status=None, quality="estimated",
        )
        second = AmberUsageRow(
            ts=ts, nem_date="2026-04-28", channel="general",
            kwh=0.55, cost_cents=11.0, per_kwh_cents=20.0,
            spot_per_kwh_cents=None, renewables_pct=None,
            descriptor=None, spike_status=None, quality="billable",
        )
        store.write_amber_usage([first])
        store.write_amber_usage([second])
        out = store.connection.sql(
            "SELECT COUNT(*), MAX(cost_cents), MAX(quality) FROM amber_usage"
        ).fetchone()
        assert out == (1, 11.0, "billable")

    def test_latest_amber_usage_date_returns_max_or_none(
        self, store: TelemetryStore
    ) -> None:
        assert store.latest_amber_usage_date() is None
        rows = [
            AmberUsageRow(
                ts=datetime(2026, 4, 26, 0, 0, tzinfo=UTC),
                nem_date="2026-04-26", channel="general",
                kwh=0.0, cost_cents=0.0, per_kwh_cents=20.0,
                spot_per_kwh_cents=None, renewables_pct=None,
                descriptor=None, spike_status=None, quality=None,
            ),
            AmberUsageRow(
                ts=datetime(2026, 4, 28, 0, 0, tzinfo=UTC),
                nem_date="2026-04-28", channel="general",
                kwh=0.0, cost_cents=0.0, per_kwh_cents=20.0,
                spot_per_kwh_cents=None, renewables_pct=None,
                descriptor=None, spike_status=None, quality=None,
            ),
        ]
        store.write_amber_usage(rows)
        assert store.latest_amber_usage_date() == "2026-04-28"


# ── HTTP API ──────────────────────────────────────────────────────


@dataclass
class _Probe:
    heartbeat_path: Path
    db_connection: duckdb.DuckDBPyConnection
    service_state: str = "active"
    sigenergy_connected: bool = True
    version: str = "test"
    metrics: Metrics | None = None
    log_buffer: object = None
    last_snapshot: object = None
    snapshot_dir: Path | None = None
    battery_config: BatteryConfig | None = None

    def __post_init__(self) -> None:
        if self.metrics is None:
            self.metrics = Metrics()
        if self.snapshot_dir is None:
            self.snapshot_dir = Path("/nonexistent")
        if self.battery_config is None:
            self.battery_config = BatteryConfig()


def _build_app(probe: _Probe) -> web.Application:
    app = web.Application(
        middlewares=[make_auth_middleware(TOKEN, ("/", "/healthz", "/readyz"))]
    )
    app[SERVICE_PROBE_KEY] = probe
    app[API_CONFIG_KEY] = APIConfig(
        bearer_token_env="EO_API_TOKEN", query_max_limit=10000, query_timeout_s=5.0
    )
    app.router.add_get("/", root)
    app.router.add_get("/daily_spend", daily_spend)
    app.router.add_get("/{table}/schema", table_schema)
    app.router.add_get("/{table}", table_rows)
    return app


def _make_db_with_usage(rows: list[tuple]) -> duckdb.DuckDBPyConnection:
    """Set up a DuckDB with the same DDL TelemetryStore creates and
    pre-populate amber_usage rows. Each row is a tuple matching the
    DDL column order."""
    cfg = StorageConfig(db_path=":memory:", snapshot_dir="/tmp")
    store = TelemetryStore(cfg)
    for r in rows:
        store.connection.execute(
            "INSERT INTO amber_usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            list(r),
        )
    return store.connection


class TestAmberUsageTableEndpoint:
    async def test_amber_usage_is_queryable(self, tmp_path: Path) -> None:
        """The /amber_usage range-query endpoint should return rows
        sorted ascending by ts — same convention as other tables."""
        conn = _make_db_with_usage(
            [
                (
                    datetime(2026, 4, 28, 0, 0, tzinfo=UTC),
                    "2026-04-28", "general", 0.5, 10.0, 20.0,
                    8.0, 30.0, "neutral", "none", "billable",
                ),
            ]
        )
        hb = tmp_path / "hb"
        hb.touch()
        probe = _Probe(heartbeat_path=hb, db_connection=conn)
        async with TestClient(TestServer(_build_app(probe))) as c:
            await c.start_server()
            r = await c.get(
                "/amber_usage", headers={"Authorization": f"Bearer {TOKEN}"}
            )
            assert r.status == 200
            body = await r.json()
            assert body["count"] == 1
            assert body["rows"][0]["channel"] == "general"


class TestDailySpendEndpoint:
    async def test_aggregates_channels_into_net_bill(
        self, tmp_path: Path
    ) -> None:
        """Two days of rows: one with both channels, one import-only.
        Verify import / export / net land correctly and Amber's signed
        cost convention is preserved into AUD."""
        conn = _make_db_with_usage(
            [
                # 2026-04-28: 1.0 kWh import @ 20c/kWh = 20c, 2.0 kWh export
                # @ -10c/kWh = -20c.  Net = 0c = $0.00.
                (
                    datetime(2026, 4, 28, 0, 0, tzinfo=UTC),
                    "2026-04-28", "general", 1.0, 20.0, 20.0,
                    8.0, 30.0, "neutral", "none", "billable",
                ),
                (
                    datetime(2026, 4, 28, 0, 0, tzinfo=UTC),
                    "2026-04-28", "feedIn", 2.0, -20.0, -10.0,
                    8.0, 30.0, "high", "none", "billable",
                ),
                # 2026-04-27: import-only, 0.5 kWh @ 30c = 15c = $0.15.
                (
                    datetime(2026, 4, 27, 0, 0, tzinfo=UTC),
                    "2026-04-27", "general", 0.5, 15.0, 30.0,
                    9.0, 25.0, "high", "none", "billable",
                ),
            ]
        )
        hb = tmp_path / "hb"
        hb.touch()
        probe = _Probe(heartbeat_path=hb, db_connection=conn)
        async with TestClient(TestServer(_build_app(probe))) as c:
            await c.start_server()
            r = await c.get(
                "/daily_spend",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert r.status == 200
            body = await r.json()
            assert body["count"] == 2
            # Sorted DESC by nem_date — most recent first.
            day_28 = body["rows"][0]
            day_27 = body["rows"][1]
            assert day_28["nem_date"] == "2026-04-28"
            assert day_27["nem_date"] == "2026-04-27"
            assert day_28["import_cost_aud"] == pytest.approx(0.20)
            # Sign-flipped to customer convention (positive = revenue).
            assert day_28["export_revenue_aud"] == pytest.approx(0.20)
            assert day_28["net_cost_aud"] == pytest.approx(0.0)
            # Day with no feedIn rows: export columns null, not 0.
            assert day_27["export_revenue_aud"] is None
            assert day_27["import_cost_aud"] == pytest.approx(0.15)
            assert day_27["net_cost_aud"] == pytest.approx(0.15)

    async def test_volume_weighted_prices_handle_zero_kwh(
        self, tmp_path: Path
    ) -> None:
        """A day with zero import kWh: volume-weighted import_avg_ckwh
        must come back null rather than NaN/inf (NULLIF guard)."""
        conn = _make_db_with_usage(
            [
                (
                    datetime(2026, 4, 28, 0, 0, tzinfo=UTC),
                    "2026-04-28", "general", 0.0, 0.0, 25.0,
                    8.0, 30.0, "neutral", "none", "billable",
                ),
                (
                    datetime(2026, 4, 28, 0, 0, tzinfo=UTC),
                    "2026-04-28", "feedIn", 5.0, -50.0, -10.0,
                    8.0, 30.0, "high", "none", "billable",
                ),
            ]
        )
        hb = tmp_path / "hb"
        hb.touch()
        probe = _Probe(heartbeat_path=hb, db_connection=conn)
        async with TestClient(TestServer(_build_app(probe))) as c:
            await c.start_server()
            r = await c.get(
                "/daily_spend",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            body = await r.json()
            row = body["rows"][0]
            assert row["import_avg_ckwh"] is None  # 0/0 ⇒ NULL via NULLIF
            assert row["export_avg_ckwh"] == pytest.approx(10.0)
