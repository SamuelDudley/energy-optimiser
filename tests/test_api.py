"""Tests for the read-only HTTP API (aiohttp).

Uses aiohttp's in-process test client — no real TCP port is bound. The
handlers read state from a small stub that implements the ServiceProbe
protocol, so these tests don't stand up a Service.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from optimiser.api.auth import make_auth_middleware
from optimiser.api.handlers.dashboard import (
    dashboard_config,
    dashboard_index,
    dashboard_static,
)
from optimiser.api.handlers.discovery import TABLE_DESCRIPTIONS, root, table_schema
from optimiser.api.handlers.health import healthz, readyz
from optimiser.api.handlers.logs import logs as logs_handler
from optimiser.api.handlers.metrics import metrics as metrics_handler
from optimiser.api.handlers.plan import plan_current
from optimiser.api.handlers.snapshots import snapshots as snapshots_handler
from optimiser.api.handlers.tables import table_rows
from optimiser.api.log_buffer import RingBufferHandler
from optimiser.api.metrics import Metrics
from optimiser.api.probe import API_CONFIG_KEY, SERVICE_PROBE_KEY
from optimiser.api.server import _favicon
from optimiser.config import APIConfig, BatteryConfig
from optimiser.modes import ModeManager

TOKEN = "test-token-xyz"
_PUBLIC = (
    "/",
    "/healthz",
    "/readyz",
    "/favicon.ico",
    "/dashboard",
    "/dashboard/static/dashboard.css",
    "/dashboard/static/dashboard.js",
)


@dataclass
class _Probe:
    heartbeat_path: Path
    # ServiceState values are lowercase (StrEnum + auto()).
    service_state: str = "active"
    sigenergy_connected: bool = True
    version: str = "0.2.0-test"
    db_connection: duckdb.DuckDBPyConnection | None = None
    metrics: Metrics | None = None
    log_buffer: RingBufferHandler | None = None
    last_snapshot: object = None  # Actual type: TickSnapshot | None
    snapshot_dir: Path | None = None
    battery_config: BatteryConfig | None = None
    managed_load_configs: list = field(default_factory=list)
    mode_manager: ModeManager | None = None

    def __post_init__(self) -> None:
        if self.metrics is None:
            self.metrics = Metrics()
        if self.snapshot_dir is None:
            # Default to a path that doesn't exist — /snapshots treats
            # this as an empty result, which is what most tests want.
            self.snapshot_dir = Path("/nonexistent-snapshot-dir")
        if self.battery_config is None:
            self.battery_config = BatteryConfig()
        if self.mode_manager is None:
            # Most tests don't care about modes; default to an empty
            # manager backed by a path that won't exist (load is a no-op).
            self.mode_manager = ModeManager(self.heartbeat_path.parent / "active_modes.json")


def _fresh_heartbeat(tmp_path: Path) -> Path:
    p = tmp_path / "heartbeat"
    p.touch()
    return p


def _stale_heartbeat(tmp_path: Path) -> Path:
    p = tmp_path / "heartbeat"
    p.touch()
    # Nudge mtime 5 minutes into the past.
    old = time.time() - 300
    import os

    os.utime(p, (old, old))
    return p


def _build_app(probe: _Probe, api_config: APIConfig | None = None) -> web.Application:
    app = web.Application(middlewares=[make_auth_middleware(TOKEN, _PUBLIC)])
    app[SERVICE_PROBE_KEY] = probe
    app[API_CONFIG_KEY] = api_config or APIConfig(
        bearer_token_env="EO_API_TOKEN", query_max_limit=100, query_timeout_s=5.0
    )
    app.router.add_get("/", root)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/favicon.ico", _favicon)
    app.router.add_get("/metrics", metrics_handler)
    app.router.add_get("/logs", logs_handler)
    app.router.add_get("/plan/current", plan_current)
    app.router.add_get("/snapshots", snapshots_handler)
    app.router.add_get("/dashboard", dashboard_index)
    app.router.add_get("/dashboard/static/{filename}", dashboard_static)
    app.router.add_get("/dashboard/config", dashboard_config)
    app.router.add_get("/{table}/schema", table_schema)
    app.router.add_get("/{table}", table_rows)
    return app


async def _client(probe: _Probe) -> TestClient:
    server = TestServer(_build_app(probe))
    client = TestClient(server)
    await client.start_server()
    return client


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class TestAuth:
    async def test_public_paths_need_no_token(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            for path in ("/", "/healthz", "/readyz"):
                r = await c.get(path)
                assert r.status in (200, 503), (path, r.status)

    async def test_favicon_returns_204_without_auth(self, tmp_path: Path) -> None:
        # Browsers auto-request /favicon.ico on every page load. The
        # route returns 204 directly so it doesn't fall through to the
        # /{table} catch-all and spam the events log with auth denials.
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/favicon.ico")
            assert r.status == 204

    async def test_protected_path_rejects_missing_bearer(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=duckdb.connect())
        async with await _client(probe) as c:
            r = await c.get("/telemetry/schema")
            assert r.status == 401
            assert r.headers["WWW-Authenticate"].startswith("Bearer ")

    async def test_protected_path_rejects_wrong_bearer(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=duckdb.connect())
        async with await _client(probe) as c:
            r = await c.get("/telemetry/schema", headers={"Authorization": "Bearer nope"})
            assert r.status == 401

    async def test_protected_path_accepts_correct_bearer(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        conn.execute("CREATE TABLE telemetry (ts TIMESTAMPTZ, soc_pct REAL)")
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn)
        async with await _client(probe) as c:
            r = await c.get("/telemetry/schema", headers=_auth())
            assert r.status == 200


class TestHealthz:
    async def test_fresh_heartbeat_is_ok(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/healthz")
            assert r.status == 200
            body = await r.json()
            assert body["ok"] is True
            assert body["heartbeat_age_s"] < 10

    async def test_stale_heartbeat_is_503(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_stale_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/healthz")
            assert r.status == 503
            body = await r.json()
            assert body["ok"] is False

    async def test_missing_heartbeat_is_503(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=tmp_path / "does_not_exist")
        async with await _client(probe) as c:
            r = await c.get("/healthz")
            assert r.status == 503


class TestReadyz:
    async def test_active_and_connected_is_ok(self, tmp_path: Path) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            service_state="active",
            sigenergy_connected=True,
        )
        async with await _client(probe) as c:
            r = await c.get("/readyz")
            assert r.status == 200
            body = await r.json()
            assert body["ok"] is True

    async def test_fallback_is_503(self, tmp_path: Path) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            service_state="fallback",
            sigenergy_connected=True,
        )
        async with await _client(probe) as c:
            r = await c.get("/readyz")
            assert r.status == 503

    async def test_disconnected_is_503(self, tmp_path: Path) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            service_state="active",
            sigenergy_connected=False,
        )
        async with await _client(probe) as c:
            r = await c.get("/readyz")
            assert r.status == 503

    async def test_active_no_price_is_ready(self, tmp_path: Path) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            service_state="active_no_price",
            sigenergy_connected=True,
        )
        async with await _client(probe) as c:
            r = await c.get("/readyz")
            assert r.status == 200


class TestIndex:
    async def test_root_lists_all_endpoints(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/")
            assert r.status == 200
            body = await r.json()
            assert body["service"] == "energy-optimiser"
            assert body["version"] == "0.2.0-test"
            paths = {e["path"] for e in body["endpoints"]}
            assert {"/", "/healthz", "/readyz", "/metrics", "/logs"} <= paths
            for table in TABLE_DESCRIPTIONS:
                assert f"/{table}" in paths

    async def test_root_entries_have_description_and_auth_flag(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/")
            body = await r.json()
            for entry in body["endpoints"]:
                assert "path" in entry
                assert "method" in entry
                assert "auth" in entry
                assert "description" in entry and entry["description"]


class TestTableSchema:
    async def test_returns_columns_for_real_table(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        conn.execute(
            "CREATE TABLE telemetry (  ts TIMESTAMPTZ NOT NULL,  soc_pct REAL,  battery_kw REAL)"
        )
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn)
        async with await _client(probe) as c:
            r = await c.get("/telemetry/schema", headers=_auth())
            assert r.status == 200
            body = await r.json()
            assert body["table"] == "telemetry"
            names = [col["name"] for col in body["columns"]]
            assert names == ["ts", "soc_pct", "battery_kw"]
            ts_col = next(c for c in body["columns"] if c["name"] == "ts")
            assert ts_col["nullable"] is False
            assert body["description"]  # non-empty

    async def test_unknown_table_404s(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=duckdb.connect())
        async with await _client(probe) as c:
            r = await c.get("/not_a_table/schema", headers=_auth())
            assert r.status == 404

    async def test_whitelist_blocks_sql_injection_attempt(self, tmp_path: Path) -> None:
        """The path ends up in a DESCRIBE literal; whitelist is the only
        defence. An unknown name must 404 before the query runs."""
        conn = duckdb.connect()
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn)
        async with await _client(probe) as c:
            # URL-encoded semicolon + DROP would be a real attack shape.
            r = await c.get("/telemetry;%20DROP%20TABLE/schema", headers=_auth())
            assert r.status == 404


class TestMetrics:
    async def test_metrics_requires_auth(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/metrics")
            assert r.status == 401

    async def test_empty_metrics_expose_zero_valued_families(self, tmp_path: Path) -> None:
        """A brand-new registry renders zero-valued counters and HELP/TYPE
        lines for everything we declared. Proves the handler wires up."""
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/metrics", headers=_auth())
            assert r.status == 200
            assert r.headers["Content-Type"].startswith("text/plain")
            body = await r.text()
            # Sanity: every metric name we declared should appear as a
            # HELP line, whether or not a sample exists yet.
            for name in (
                "eo_battery_soc_pct",
                "eo_battery_power_kw",
                "eo_pv_power_kw",
                "eo_house_load_kw",
                "eo_grid_power_kw",
                "eo_lp_solves_total",
                "eo_dispatch_writes_total",
                "eo_circuit_breaker_trips_total",
                "eo_lp_solve_duration_ms",
                "eo_tick_duration_ms",
            ):
                assert f"# HELP {name}" in body, name

    async def test_recorded_values_show_up(self, tmp_path: Path) -> None:
        metrics = Metrics()
        metrics.soc_pct.set(62.5)
        metrics.record_lp_solve("optimal", 1234.5)
        metrics.record_lp_solve("optimal", 800.0)
        metrics.record_dispatch_write(True)
        metrics.record_dispatch_write(False)
        metrics.record_circuit_breaker_trip("lp_timeout")

        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), metrics=metrics)
        async with await _client(probe) as c:
            r = await c.get("/metrics", headers=_auth())
            body = await r.text()
            assert "eo_battery_soc_pct 62.5" in body
            assert 'eo_lp_solves_total{status="optimal"} 2.0' in body
            assert 'eo_dispatch_writes_total{result="success"} 1.0' in body
            assert 'eo_dispatch_writes_total{result="failure"} 1.0' in body
            assert 'eo_circuit_breaker_trips_total{reason="lp_timeout"} 1.0' in body
            # Histogram bucket lines
            assert "eo_lp_solve_duration_ms_bucket" in body
            assert "eo_lp_solve_duration_ms_count 2.0" in body

    async def test_heartbeat_age_is_derived_at_scrape_time(self, tmp_path: Path) -> None:
        """The handler computes heartbeat_age from file mtime each scrape
        — no need for the tick loop to update it inline."""
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/metrics", headers=_auth())
            body = await r.text()
            # Freshly-touched file → small value.
            assert "eo_heartbeat_age_seconds" in body

    async def test_state_machine_multiseries_reflects_current_state(self, tmp_path: Path) -> None:
        metrics = Metrics()
        # Mock a SystemState with the fields record_live_state reads.
        from unittest.mock import MagicMock

        state = MagicMock()
        state.soc_pct = 70.0
        state.battery_power_kw = 1.5
        state.pv_power_kw = 3.2
        state.house_load_kw = 0.8
        state.grid_power_kw = 0.1
        metrics.record_live_state(
            state=state,
            current_import_price=20.0,
            current_export_price=5.0,
            sigenergy_connected=True,
            service_state="active",
            circuit_breaker_open=False,
            heartbeat_age_s=None,
        )
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), metrics=metrics)
        async with await _client(probe) as c:
            r = await c.get("/metrics", headers=_auth())
            body = await r.text()
            assert 'eo_state_machine_state{state="active"} 1.0' in body
            assert 'eo_state_machine_state{state="fallback"} 0.0' in body


def _seeded_buffer(capacity: int = 50) -> RingBufferHandler:
    buf = RingBufferHandler(capacity)
    lg = logging.getLogger(f"test.buffer.{id(buf)}")
    lg.addHandler(buf)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False  # keep pytest capture out of our buffer
    lg.debug("a debug line")
    lg.info("an info line")
    lg.warning("a warning line")
    lg.error("an error line")
    lg.removeHandler(buf)
    return buf


class TestRingBuffer:
    """Unit tests for RingBufferHandler (no HTTP)."""

    def test_bounded_capacity(self) -> None:
        buf = RingBufferHandler(3)
        lg = logging.getLogger(f"test.bounded.{id(buf)}")
        lg.addHandler(buf)
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        for i in range(10):
            lg.info("line %d", i)
        snap = buf.snapshot(since=None, until=None, min_level=logging.DEBUG, limit=100)
        assert len(snap) == 3

    def test_newest_first_order(self) -> None:
        buf = _seeded_buffer()
        snap = buf.snapshot(since=None, until=None, min_level=logging.DEBUG, limit=100)
        # Four lines; newest (error) first
        messages = [r["message"] for r in snap]
        assert messages[0] == "an error line"
        assert messages[-1] == "a debug line"

    def test_level_filter_min_inclusive(self) -> None:
        buf = _seeded_buffer()
        snap = buf.snapshot(since=None, until=None, min_level=logging.WARNING, limit=100)
        levels = {r["level"] for r in snap}
        assert levels == {"WARNING", "ERROR"}

    def test_limit_clamps_output(self) -> None:
        buf = _seeded_buffer()
        snap = buf.snapshot(since=None, until=None, min_level=logging.DEBUG, limit=2)
        assert len(snap) == 2

    def test_captures_exc_info(self) -> None:
        buf = RingBufferHandler(10)
        lg = logging.getLogger(f"test.exc.{id(buf)}")
        lg.addHandler(buf)
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            lg.exception("something exploded")
        snap = buf.snapshot(since=None, until=None, min_level=logging.DEBUG, limit=5)
        assert len(snap) == 1
        # The LogRecord machinery should have captured exc_info into the
        # formatted string.
        assert snap[0].get("exc_info") is not None


class TestLogs:
    async def test_logs_requires_auth(self, tmp_path: Path) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            log_buffer=_seeded_buffer(),
        )
        async with await _client(probe) as c:
            r = await c.get("/logs")
            assert r.status == 401

    async def test_logs_returns_ring_buffer_newest_first(self, tmp_path: Path) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            log_buffer=_seeded_buffer(),
        )
        async with await _client(probe) as c:
            r = await c.get("/logs", headers=_auth())
            assert r.status == 200
            body = await r.json()
            assert body["count"] == 4
            assert body["records"][0]["message"] == "an error line"
            assert body["records"][-1]["message"] == "a debug line"

    async def test_logs_filters_by_level(self, tmp_path: Path) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            log_buffer=_seeded_buffer(),
        )
        async with await _client(probe) as c:
            r = await c.get("/logs?level=WARNING", headers=_auth())
            body = await r.json()
            assert body["count"] == 2
            assert {r["level"] for r in body["records"]} == {"WARNING", "ERROR"}

    async def test_logs_respects_limit(self, tmp_path: Path) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            log_buffer=_seeded_buffer(),
        )
        async with await _client(probe) as c:
            r = await c.get("/logs?limit=1", headers=_auth())
            body = await r.json()
            assert body["count"] == 1

    async def test_bad_level_returns_400(self, tmp_path: Path) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            log_buffer=_seeded_buffer(),
        )
        async with await _client(probe) as c:
            r = await c.get("/logs?level=NOPE", headers=_auth())
            assert r.status == 400

    async def test_bad_since_returns_400(self, tmp_path: Path) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            log_buffer=_seeded_buffer(),
        )
        async with await _client(probe) as c:
            r = await c.get("/logs?since=not-a-timestamp", headers=_auth())
            assert r.status == 400

    async def test_bad_limit_returns_400(self, tmp_path: Path) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            log_buffer=_seeded_buffer(),
        )
        async with await _client(probe) as c:
            r = await c.get("/logs?limit=-5", headers=_auth())
            assert r.status == 400

    async def test_missing_buffer_returns_503(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), log_buffer=None)
        async with await _client(probe) as c:
            r = await c.get("/logs", headers=_auth())
            assert r.status == 503


def _seed_telemetry(conn: duckdb.DuckDBPyConnection, n: int = 5) -> None:
    """Insert n hourly telemetry rows starting at 2026-01-01T00:00 UTC."""
    conn.execute(
        "CREATE TABLE telemetry (  ts TIMESTAMPTZ NOT NULL,  soc_pct REAL,  battery_kw REAL)"
    )
    for i in range(n):
        ts = f"2026-01-01 0{i}:00:00+00:00"
        conn.execute("INSERT INTO telemetry VALUES (?, ?, ?)", [ts, 50.0 + i, float(i)])


class TestTableQuery:
    async def test_requires_auth(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        _seed_telemetry(conn, 3)
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn)
        async with await _client(probe) as c:
            r = await c.get("/telemetry")
            assert r.status == 401

    async def test_returns_all_rows_when_no_filter(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        _seed_telemetry(conn, 5)
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn)
        async with await _client(probe) as c:
            r = await c.get("/telemetry", headers=_auth())
            body = await r.json()
            assert body["table"] == "telemetry"
            assert body["count"] == 5
            # Sorted ASC by ts; first row is hour 0
            assert body["rows"][0]["soc_pct"] == 50.0
            assert body["rows"][-1]["soc_pct"] == 54.0

    async def test_datetime_serialised_as_iso(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        _seed_telemetry(conn, 1)
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn)
        async with await _client(probe) as c:
            r = await c.get("/telemetry", headers=_auth())
            body = await r.json()
            ts = body["rows"][0]["ts"]
            # Should round-trip through datetime.fromisoformat without raising
            datetime.fromisoformat(ts)

    async def test_since_filter_excludes_earlier_rows(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        _seed_telemetry(conn, 5)
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn)
        async with await _client(probe) as c:
            # `+` in query strings is a space — encode as %2B.
            r = await c.get(
                "/telemetry?since=2026-01-01T03:00:00%2B00:00",
                headers=_auth(),
            )
            body = await r.json()
            assert body["count"] == 2  # hours 3 and 4
            assert body["rows"][0]["soc_pct"] == 53.0

    async def test_until_is_exclusive(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        _seed_telemetry(conn, 5)
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn)
        async with await _client(probe) as c:
            r = await c.get(
                "/telemetry?until=2026-01-01T03:00:00%2B00:00",
                headers=_auth(),
            )
            body = await r.json()
            assert body["count"] == 3  # hours 0, 1, 2 — not 3
            assert body["rows"][-1]["soc_pct"] == 52.0

    async def test_limit_clamped_to_query_max_limit(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        _seed_telemetry(conn, 10)
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn)
        # APIConfig in _build_app has query_max_limit=100, so a request
        # for limit=99999 should clamp.
        async with await _client(probe) as c:
            r = await c.get("/telemetry?limit=99999", headers=_auth())
            body = await r.json()
            # Capped to query_max_limit (100), so we get all 10 rows
            assert body["count"] == 10

    async def test_limit_three_returns_three(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        _seed_telemetry(conn, 10)
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn)
        async with await _client(probe) as c:
            r = await c.get("/telemetry?limit=3", headers=_auth())
            body = await r.json()
            assert body["count"] == 3

    async def test_unknown_table_404(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn)
        async with await _client(probe) as c:
            r = await c.get("/not_a_real_table", headers=_auth())
            assert r.status == 404

    async def test_bad_since_400(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        _seed_telemetry(conn, 1)
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn)
        async with await _client(probe) as c:
            r = await c.get("/telemetry?since=garbage", headers=_auth())
            assert r.status == 400

    async def test_paging_via_advancing_since(self, tmp_path: Path) -> None:
        """Replicates the paging convention: ask for limit=2, advance
        `since` past the last row, ask again, etc."""
        conn = duckdb.connect()
        _seed_telemetry(conn, 5)
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn)
        async with await _client(probe) as c:
            seen = []
            since = "2026-01-01T00:00:00+00:00"
            for _ in range(5):
                # Encode the `+` so it doesn't get treated as a space.
                r = await c.get(
                    f"/telemetry?since={since.replace('+', '%2B')}&limit=2",
                    headers=_auth(),
                )
                body = await r.json()
                if body["count"] == 0:
                    break
                seen.extend(body["rows"])
                last_ts = datetime.fromisoformat(body["rows"][-1]["ts"])
                since = (last_ts + timedelta(microseconds=1)).isoformat()
            assert len(seen) == 5
            assert [r["soc_pct"] for r in seen] == [50.0, 51.0, 52.0, 53.0, 54.0]


class TestForecastLogTableQuery:
    """Regression: forecast log tables have no `ts` column — they key on
    `fetched_at`. The range handler must use the per-table time column."""

    @staticmethod
    def _seed_pv(conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute(
            "CREATE TABLE pv_forecast_log ("
            "  fetched_at TIMESTAMPTZ NOT NULL,"
            "  period_end TIMESTAMPTZ NOT NULL,"
            "  pv_estimate_kw REAL"
            ")"
        )
        for i in range(4):
            conn.execute(
                "INSERT INTO pv_forecast_log VALUES (?, ?, ?)",
                [
                    f"2026-01-01 0{i}:00:00+00:00",
                    f"2026-01-01 0{i}:30:00+00:00",
                    float(i),
                ],
            )

    @staticmethod
    def _seed_price(conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute(
            "CREATE TABLE price_forecast_log ("
            "  fetched_at TIMESTAMPTZ NOT NULL,"
            "  resolution INTEGER NOT NULL,"
            "  interval_start TIMESTAMPTZ NOT NULL,"
            "  per_kwh REAL"
            ")"
        )
        for i in range(3):
            conn.execute(
                "INSERT INTO price_forecast_log VALUES (?, ?, ?, ?)",
                [
                    f"2026-01-01 0{i}:00:00+00:00",
                    5,
                    f"2026-01-01 0{i}:05:00+00:00",
                    10.0 + i,
                ],
            )

    @staticmethod
    def _seed_weather(conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute(
            "CREATE TABLE weather_forecast_log ("
            "  fetched_at TIMESTAMPTZ NOT NULL,"
            "  period_end TIMESTAMPTZ NOT NULL,"
            "  temp_c REAL"
            ")"
        )
        for i in range(3):
            conn.execute(
                "INSERT INTO weather_forecast_log VALUES (?, ?, ?)",
                [
                    f"2026-01-01 0{i}:00:00+00:00",
                    f"2026-01-01 0{i}:59:00+00:00",
                    20.0 + i,
                ],
            )

    async def test_pv_forecast_log_range_query(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        self._seed_pv(conn)
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn)
        async with await _client(probe) as c:
            r = await c.get("/pv_forecast_log", headers=_auth())
            assert r.status == 200
            body = await r.json()
            assert body["count"] == 4
            assert [row["pv_estimate_kw"] for row in body["rows"]] == [0.0, 1.0, 2.0, 3.0]

            r = await c.get(
                "/pv_forecast_log?since=2026-01-01T02:00:00%2B00:00",
                headers=_auth(),
            )
            body = await r.json()
            assert body["count"] == 2
            assert body["rows"][0]["pv_estimate_kw"] == 2.0

    async def test_price_forecast_log_range_query(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        self._seed_price(conn)
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn)
        async with await _client(probe) as c:
            r = await c.get(
                "/price_forecast_log?until=2026-01-01T02:00:00%2B00:00",
                headers=_auth(),
            )
            assert r.status == 200
            body = await r.json()
            assert body["count"] == 2
            assert body["rows"][-1]["per_kwh"] == 11.0

    async def test_weather_forecast_log_range_query(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        self._seed_weather(conn)
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn)
        async with await _client(probe) as c:
            r = await c.get("/weather_forecast_log?limit=2", headers=_auth())
            assert r.status == 200
            body = await r.json()
            assert body["count"] == 2
            assert [row["temp_c"] for row in body["rows"]] == [20.0, 21.0]


def _make_snapshot(timestamp: datetime, soc_pct: float = 50.0, action: str = "self_consume"):
    """Build a minimal TickSnapshot with one forward slot for tests."""
    from optimiser.lp.result import LPSolution, SlotDecision, SolveStatus
    from optimiser.types import (
        BatteryAction,
        LoadProfile,
        PlannerOutput,
        SystemState,
        TickSnapshot,
    )

    state = SystemState(
        timestamp=timestamp,
        soc_pct=soc_pct,
        battery_power_kw=0.0,
        pv_power_kw=1.5,
        grid_power_kw=0.3,
        house_load_kw=1.8,
        ems_mode=2,
        outdoor_temp_c=18.0,
        occupied=True,
    )
    slot0 = SlotDecision(
        slot_start=timestamp,
        battery_kw=-2.0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        pv_to_house_kw=1.5,
        pv_to_battery_kw=0.0,
        pv_to_export_kw=0.0,
        soc_pct_end=soc_pct - 0.5,
        grid_to_battery_kw=0.0,
        load_kw={},
    )
    solution = LPSolution(
        status=SolveStatus.OPTIMAL,
        slot_0=slot0,
        forward_trajectory=[slot0],
        load_commands=[],
        grid_export_limit_kw=None,
        expected_total_cost_cents=-12.5,
        solve_time_ms=42.0,
        reason="test plan",
    )
    output = PlannerOutput(
        battery_action=BatteryAction(action),
        charge_limit_kw=0.0,
        discharge_limit_kw=10.0,
        target_soc=soc_pct,
        load_commands=[],
        grid_export_limit_kw=None,
        reason="test",
    )
    profile = LoadProfile(slots=[0.0] * 48, maturity_level=0, context="test")
    return TickSnapshot(
        tick_id="test-tick",
        timestamp=timestamp,
        version="0.0.0-test",
        system_state=state,
        price_forecast=[],
        pv_forecast=None,
        load_profile=profile,
        managed_loads=[],
        maturity_level=0,
        output=output,
        lp_solution=solution,
        lp_dispatch=None,
    )


class TestPlanCurrent:
    async def test_requires_auth(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/plan/current")
            assert r.status == 401

    async def test_503_before_first_tick(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), last_snapshot=None)
        async with await _client(probe) as c:
            r = await c.get("/plan/current", headers=_auth())
            assert r.status == 503
            body = await r.json()
            assert "error" in body

    async def test_returns_snapshot_after_tick(self, tmp_path: Path) -> None:
        ts = datetime.fromisoformat("2026-04-24T10:00:00+00:00")
        snapshot = _make_snapshot(ts, soc_pct=72.5, action="discharge_ess")
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), last_snapshot=snapshot)
        async with await _client(probe) as c:
            r = await c.get("/plan/current", headers=_auth())
            assert r.status == 200
            body = await r.json()
            assert body["tick_id"] == "test-tick"
            assert body["system_state"]["soc_pct"] == 72.5
            assert body["lp_solution"]["status"] == "optimal"
            assert len(body["lp_solution"]["forward_trajectory"]) == 1
            assert body["lp_solution"]["forward_trajectory"][0]["battery_kw"] == -2.0
            assert body["output"]["battery_action"] == "discharge_ess"


class TestSnapshots:
    """The /snapshots endpoint reads the NDJSON glob via DuckDB. The
    files on disk are newline-delimited JSON, one TickSnapshot per line
    — same format `SnapshotWriter` writes live."""

    async def test_requires_auth(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/snapshots")
            assert r.status == 401

    async def test_empty_when_dir_missing(self, tmp_path: Path) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            snapshot_dir=tmp_path / "no-such-dir",
            db_connection=duckdb.connect(),
        )
        async with await _client(probe) as c:
            r = await c.get("/snapshots", headers=_auth())
            assert r.status == 200
            body = await r.json()
            assert body["count"] == 0
            assert body["rows"] == []

    async def test_empty_when_dir_exists_but_has_no_files(self, tmp_path: Path) -> None:
        snap_dir = tmp_path / "snapshots"
        snap_dir.mkdir()
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            snapshot_dir=snap_dir,
            db_connection=duckdb.connect(),
        )
        async with await _client(probe) as c:
            r = await c.get("/snapshots", headers=_auth())
            assert r.status == 200
            body = await r.json()
            assert body["count"] == 0

    async def test_reads_snapshots_from_ndjson(self, tmp_path: Path) -> None:
        # DuckDB's read_json_auto accepts uncompressed NDJSON via the
        # same code path; tests use .ndjson (plain) to skip the gzip
        # round-trip. The handler's glob is '*.ndjson.gz', so we point
        # it directly at a *.ndjson file by parking a subclass — easier
        # to use a plain file and adjust the glob for the test.
        from dataclasses import asdict

        from optimiser.logging_utils import _serialise

        snap_dir = tmp_path / "snapshots"
        snap_dir.mkdir()
        ts1 = datetime.fromisoformat("2026-04-24T09:00:00+00:00")
        ts2 = datetime.fromisoformat("2026-04-24T09:01:00+00:00")
        ts3 = datetime.fromisoformat("2026-04-24T09:02:00+00:00")
        snaps = [
            _make_snapshot(ts1, soc_pct=60.0),
            _make_snapshot(ts2, soc_pct=61.0),
            _make_snapshot(ts3, soc_pct=62.0),
        ]
        # Write as plain .ndjson.gz via gzip — matches production.
        import gzip

        with gzip.open(snap_dir / "2026-04-24.ndjson.gz", "wt", encoding="utf-8") as f:
            for s in snaps:
                f.write(json.dumps(asdict(s), default=_serialise) + "\n")

        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            snapshot_dir=snap_dir,
            db_connection=duckdb.connect(),
        )
        async with await _client(probe) as c:
            r = await c.get("/snapshots", headers=_auth())
            assert r.status == 200
            body = await r.json()
            assert body["count"] == 3
            assert [row["system_state"]["soc_pct"] for row in body["rows"]] == [
                60.0,
                61.0,
                62.0,
            ]

            # since filter
            r = await c.get(
                "/snapshots?since=2026-04-24T09:01:30%2B00:00",
                headers=_auth(),
            )
            body = await r.json()
            assert body["count"] == 1
            assert body["rows"][0]["system_state"]["soc_pct"] == 62.0

            # limit
            r = await c.get("/snapshots?limit=2", headers=_auth())
            body = await r.json()
            assert body["count"] == 2

    async def test_bad_limit_400(self, tmp_path: Path) -> None:
        snap_dir = tmp_path / "snapshots"
        snap_dir.mkdir()
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            snapshot_dir=snap_dir,
            db_connection=duckdb.connect(),
        )
        async with await _client(probe) as c:
            r = await c.get("/snapshots?limit=garbage", headers=_auth())
            assert r.status == 400

    async def test_skips_corrupt_gzip(self, tmp_path: Path) -> None:
        """A torn/truncated .ndjson.gz (e.g. from an abrupt service
        exit mid-flush) must not poison the whole query — the handler
        skips the bad file and reports it under skipped_files."""
        import gzip
        from dataclasses import asdict

        from optimiser.logging_utils import _serialise

        snap_dir = tmp_path / "snapshots"
        snap_dir.mkdir()
        base = datetime.fromisoformat("2026-04-24T00:00:00+00:00")
        snaps = [_make_snapshot(base + timedelta(seconds=i)) for i in range(3)]

        # File 1: clean
        with gzip.open(snap_dir / "2026-04-23.ndjson.gz", "wt", encoding="utf-8") as f:
            for s in snaps:
                f.write(json.dumps(asdict(s), default=_serialise) + "\n")

        # File 2: truncated gzip stream — only a partial gzip magic,
        # exactly what an abrupt mid-flush append leaves on disk.
        (snap_dir / "2026-04-24.ndjson.gz").write_bytes(b"\x1f\x8b\x08\x00garbage")

        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            snapshot_dir=snap_dir,
            db_connection=duckdb.connect(),
        )
        async with await _client(probe) as c:
            r = await c.get("/snapshots", headers=_auth())
            assert r.status == 200
            body = await r.json()
            assert body["count"] == 3  # from the clean file
            assert "skipped_files" in body
            assert len(body["skipped_files"]) == 1
            assert "2026-04-24" in body["skipped_files"][0]["path"]

    async def test_limit_capped(self, tmp_path: Path) -> None:
        """Default cap is 200, regardless of what the caller asks for."""
        import gzip
        from dataclasses import asdict

        from optimiser.logging_utils import _serialise

        snap_dir = tmp_path / "snapshots"
        snap_dir.mkdir()
        base = datetime.fromisoformat("2026-04-24T00:00:00+00:00")
        snaps = [_make_snapshot(base + timedelta(seconds=i)) for i in range(5)]
        with gzip.open(snap_dir / "2026-04-24.ndjson.gz", "wt", encoding="utf-8") as f:
            for s in snaps:
                f.write(json.dumps(asdict(s), default=_serialise) + "\n")

        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            snapshot_dir=snap_dir,
            db_connection=duckdb.connect(),
        )
        async with await _client(probe) as c:
            # Request 999; cap is 200, seed has 5 → returns 5.
            r = await c.get("/snapshots?limit=999", headers=_auth())
            body = await r.json()
            assert body["count"] == 5


class TestDashboard:
    """Coverage for the operator dashboard handler.

    The HTML / CSS / JS files are checked for presence and correct
    content-types. /dashboard/config is gated by bearer auth and surfaces
    BatteryConfig fields the SOC panel needs (soc_floor_pct etc.).
    """

    async def test_dashboard_index_serves_html(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/dashboard")
            assert r.status == 200
            assert r.content_type == "text/html"
            body = await r.text()
            assert "<title>" in body
            assert "ts-figure" in body  # the time-series figure container

    async def test_dashboard_index_is_public(self, tmp_path: Path) -> None:
        # The HTML page itself contains no data — must work without a token.
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/dashboard")
            assert r.status == 200

    async def test_dashboard_static_serves_js(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/dashboard/static/dashboard.js")
            assert r.status == 200
            assert r.content_type == "application/javascript"
            body = await r.text()
            assert "Plotly" in body  # we reference Plotly.* throughout

    async def test_dashboard_static_serves_css(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/dashboard/static/dashboard.css")
            assert r.status == 200
            assert r.content_type == "text/css"

    async def test_dashboard_static_blocks_unknown_filename(self, tmp_path: Path) -> None:
        # Layered defence: only the explicit dashboard.css / .js entries
        # are in the public-paths whitelist, so the auth middleware
        # rejects any other path *before* the handler runs (401). The
        # handler's own _STATIC_FILES whitelist is the second layer
        # (would return 404 if reached). Either way, no leak.
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/dashboard/static/secret.txt")
            assert r.status in (401, 404), r.status
            # With a token, the handler's whitelist must still reject.
            r = await c.get("/dashboard/static/secret.txt", headers=_auth())
            assert r.status == 404, r.status

    async def test_dashboard_static_blocks_path_traversal(self, tmp_path: Path) -> None:
        # An attempted ../etc/passwd must not return passwd contents,
        # whether blocked by auth (401), the handler whitelist (404), or
        # request normalisation (400). With a valid token the handler's
        # whitelist is what enforces this.
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/dashboard/static/..%2F..%2Fetc%2Fpasswd", headers=_auth())
            assert r.status in (400, 404), r.status
            if r.status != 400:
                body = await r.text()
                assert "root:" not in body  # /etc/passwd content sentinel

    async def test_dashboard_config_requires_token(self, tmp_path: Path) -> None:
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/dashboard/config")
            assert r.status == 401

    async def test_dashboard_config_returns_battery_fields(self, tmp_path: Path) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            battery_config=BatteryConfig(soc_floor_pct=22.5, capacity_kwh=40.0),
        )
        async with await _client(probe) as c:
            r = await c.get("/dashboard/config", headers=_auth())
            assert r.status == 200
            body = await r.json()
            assert body["battery"]["soc_floor_pct"] == 22.5
            assert body["battery"]["capacity_kwh"] == 40.0
            # SOC panel needs all of these to draw axes / floor line.
            for k in ("max_ac_charge_kw", "max_dc_charge_kw", "max_discharge_kw"):
                assert k in body["battery"]

    async def test_dashboard_config_includes_empty_active_modes(self, tmp_path: Path) -> None:
        # No modes activated → key is present and empty (lets the dashboard
        # distinguish "no override" from "field missing / unknown").
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/dashboard/config", headers=_auth())
            assert r.status == 200
            body = await r.json()
            assert body["active_modes"] == []

    async def test_dashboard_config_includes_active_modes(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime, timedelta

        from optimiser.modes import ActiveMode

        mgr = ModeManager(tmp_path / "active_modes.json")
        end_at = datetime.now(UTC) + timedelta(hours=2)
        mgr.activate(
            ActiveMode(
                kind="buy",
                end_at=end_at,
                params={"ceiling_c_per_kwh": 12.0},
                activated_at=datetime.now(UTC),
                source="user",
            )
        )
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path), mode_manager=mgr)
        async with await _client(probe) as c:
            r = await c.get("/dashboard/config", headers=_auth())
            assert r.status == 200
            body = await r.json()
            kinds = [m["kind"] for m in body["active_modes"]]
            assert kinds == ["buy"]
            assert body["active_modes"][0]["params"] == {"ceiling_c_per_kwh": 12.0}

    async def test_static_dir_env_override(self, tmp_path: Path, monkeypatch) -> None:
        """Setting EO_DASHBOARD_STATIC_DIR makes the handler read from a
        different directory — used by docker-compose so dev edits to
        HTML/CSS/JS don't require rebuilding the image."""
        # Drop a sentinel CSS file in a tmp dir; point the handler at it
        # via the env var; reload the module so _STATIC_DIR re-resolves.
        import importlib

        from optimiser.api.handlers import dashboard as dashboard_mod

        custom = tmp_path / "static-override"
        custom.mkdir()
        (custom / "dashboard.css").write_text("/* sentinel */")
        (custom / "dashboard.html").write_text("<html><body>OVR</body></html>")
        # The JS must exist even if we don't fetch it (the index page
        # references it but we're only testing static-asset routing).
        (custom / "dashboard.js").write_text("// sentinel")

        monkeypatch.setenv("EO_DASHBOARD_STATIC_DIR", str(custom))
        importlib.reload(dashboard_mod)

        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        # Build the app with the *reloaded* handler references.
        app = web.Application(middlewares=[make_auth_middleware(TOKEN, _PUBLIC)])
        app[SERVICE_PROBE_KEY] = probe
        app[API_CONFIG_KEY] = APIConfig(
            bearer_token_env="EO_API_TOKEN", query_max_limit=100, query_timeout_s=5.0
        )
        app.router.add_get("/dashboard", dashboard_mod.dashboard_index)
        app.router.add_get("/dashboard/static/{filename}", dashboard_mod.dashboard_static)
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/dashboard")
            body = await r.text()
            assert "OVR" in body, body
            r = await c.get("/dashboard/static/dashboard.css")
            assert "sentinel" in await r.text()

        # Restore the un-overridden module so other tests see the real assets.
        monkeypatch.delenv("EO_DASHBOARD_STATIC_DIR", raising=False)
        importlib.reload(dashboard_mod)
