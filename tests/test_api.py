"""Tests for the read-only HTTP API (aiohttp).

Uses aiohttp's in-process test client — no real TCP port is bound. The
handlers read state from a small stub that implements the ServiceProbe
protocol, so these tests don't stand up a Service.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from optimiser.api.auth import make_auth_middleware
from optimiser.api.handlers.discovery import TABLE_DESCRIPTIONS, root, table_schema
from optimiser.api.handlers.health import healthz, readyz
from optimiser.api.handlers.logs import logs as logs_handler
from optimiser.api.handlers.metrics import metrics as metrics_handler
from optimiser.api.handlers.tables import table_rows
from optimiser.api.log_buffer import RingBufferHandler
from optimiser.api.metrics import Metrics
from optimiser.api.probe import API_CONFIG_KEY, SERVICE_PROBE_KEY
from optimiser.config import APIConfig

TOKEN = "test-token-xyz"
_PUBLIC = ("/", "/healthz", "/readyz")


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

    def __post_init__(self) -> None:
        if self.metrics is None:
            self.metrics = Metrics()


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


def _build_app(
    probe: _Probe, api_config: APIConfig | None = None
) -> web.Application:
    app = web.Application(middlewares=[make_auth_middleware(TOKEN, _PUBLIC)])
    app[SERVICE_PROBE_KEY] = probe
    app[API_CONFIG_KEY] = api_config or APIConfig(
        bearer_token_env="EO_API_TOKEN", query_max_limit=100, query_timeout_s=5.0
    )
    app.router.add_get("/", root)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/metrics", metrics_handler)
    app.router.add_get("/logs", logs_handler)
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

    async def test_protected_path_rejects_missing_bearer(
        self, tmp_path: Path
    ) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=duckdb.connect()
        )
        async with await _client(probe) as c:
            r = await c.get("/telemetry/schema")
            assert r.status == 401
            assert r.headers["WWW-Authenticate"].startswith("Bearer ")

    async def test_protected_path_rejects_wrong_bearer(
        self, tmp_path: Path
    ) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=duckdb.connect()
        )
        async with await _client(probe) as c:
            r = await c.get(
                "/telemetry/schema", headers={"Authorization": "Bearer nope"}
            )
            assert r.status == 401

    async def test_protected_path_accepts_correct_bearer(
        self, tmp_path: Path
    ) -> None:
        conn = duckdb.connect()
        conn.execute("CREATE TABLE telemetry (ts TIMESTAMPTZ, soc_pct REAL)")
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn
        )
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

    async def test_root_entries_have_description_and_auth_flag(
        self, tmp_path: Path
    ) -> None:
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
    async def test_returns_columns_for_real_table(
        self, tmp_path: Path
    ) -> None:
        conn = duckdb.connect()
        conn.execute(
            "CREATE TABLE telemetry ("
            "  ts TIMESTAMPTZ NOT NULL,"
            "  soc_pct REAL,"
            "  battery_kw REAL"
            ")"
        )
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn
        )
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
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=duckdb.connect()
        )
        async with await _client(probe) as c:
            r = await c.get("/not_a_table/schema", headers=_auth())
            assert r.status == 404

    async def test_whitelist_blocks_sql_injection_attempt(
        self, tmp_path: Path
    ) -> None:
        """The path ends up in a DESCRIBE literal; whitelist is the only
        defence. An unknown name must 404 before the query runs."""
        conn = duckdb.connect()
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn
        )
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

    async def test_empty_metrics_expose_zero_valued_families(
        self, tmp_path: Path
    ) -> None:
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

        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), metrics=metrics
        )
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

    async def test_heartbeat_age_is_derived_at_scrape_time(
        self, tmp_path: Path
    ) -> None:
        """The handler computes heartbeat_age from file mtime each scrape
        — no need for the tick loop to update it inline."""
        probe = _Probe(heartbeat_path=_fresh_heartbeat(tmp_path))
        async with await _client(probe) as c:
            r = await c.get("/metrics", headers=_auth())
            body = await r.text()
            # Freshly-touched file → small value.
            assert "eo_heartbeat_age_seconds" in body

    async def test_state_machine_multiseries_reflects_current_state(
        self, tmp_path: Path
    ) -> None:
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
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), metrics=metrics
        )
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
        snap = buf.snapshot(
            since=None, until=None, min_level=logging.DEBUG, limit=100
        )
        assert len(snap) == 3

    def test_newest_first_order(self) -> None:
        buf = _seeded_buffer()
        snap = buf.snapshot(
            since=None, until=None, min_level=logging.DEBUG, limit=100
        )
        # Four lines; newest (error) first
        messages = [r["message"] for r in snap]
        assert messages[0] == "an error line"
        assert messages[-1] == "a debug line"

    def test_level_filter_min_inclusive(self) -> None:
        buf = _seeded_buffer()
        snap = buf.snapshot(
            since=None, until=None, min_level=logging.WARNING, limit=100
        )
        levels = {r["level"] for r in snap}
        assert levels == {"WARNING", "ERROR"}

    def test_limit_clamps_output(self) -> None:
        buf = _seeded_buffer()
        snap = buf.snapshot(
            since=None, until=None, min_level=logging.DEBUG, limit=2
        )
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
        snap = buf.snapshot(
            since=None, until=None, min_level=logging.DEBUG, limit=5
        )
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

    async def test_logs_returns_ring_buffer_newest_first(
        self, tmp_path: Path
    ) -> None:
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
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), log_buffer=None
        )
        async with await _client(probe) as c:
            r = await c.get("/logs", headers=_auth())
            assert r.status == 503


def _seed_telemetry(conn: duckdb.DuckDBPyConnection, n: int = 5) -> None:
    """Insert n hourly telemetry rows starting at 2026-01-01T00:00 UTC."""
    conn.execute(
        "CREATE TABLE telemetry ("
        "  ts TIMESTAMPTZ NOT NULL,"
        "  soc_pct REAL,"
        "  battery_kw REAL"
        ")"
    )
    for i in range(n):
        ts = f"2026-01-01 0{i}:00:00+00:00"
        conn.execute(
            "INSERT INTO telemetry VALUES (?, ?, ?)", [ts, 50.0 + i, float(i)]
        )


class TestTableQuery:
    async def test_requires_auth(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        _seed_telemetry(conn, 3)
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn
        )
        async with await _client(probe) as c:
            r = await c.get("/telemetry")
            assert r.status == 401

    async def test_returns_all_rows_when_no_filter(
        self, tmp_path: Path
    ) -> None:
        conn = duckdb.connect()
        _seed_telemetry(conn, 5)
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn
        )
        async with await _client(probe) as c:
            r = await c.get("/telemetry", headers=_auth())
            body = await r.json()
            assert body["table"] == "telemetry"
            assert body["count"] == 5
            # Sorted ASC by ts; first row is hour 0
            assert body["rows"][0]["soc_pct"] == 50.0
            assert body["rows"][-1]["soc_pct"] == 54.0

    async def test_datetime_serialised_as_iso(
        self, tmp_path: Path
    ) -> None:
        conn = duckdb.connect()
        _seed_telemetry(conn, 1)
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn
        )
        async with await _client(probe) as c:
            r = await c.get("/telemetry", headers=_auth())
            body = await r.json()
            ts = body["rows"][0]["ts"]
            # Should round-trip through datetime.fromisoformat without raising
            datetime.fromisoformat(ts)

    async def test_since_filter_excludes_earlier_rows(
        self, tmp_path: Path
    ) -> None:
        conn = duckdb.connect()
        _seed_telemetry(conn, 5)
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn
        )
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
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn
        )
        async with await _client(probe) as c:
            r = await c.get(
                "/telemetry?until=2026-01-01T03:00:00%2B00:00",
                headers=_auth(),
            )
            body = await r.json()
            assert body["count"] == 3  # hours 0, 1, 2 — not 3
            assert body["rows"][-1]["soc_pct"] == 52.0

    async def test_limit_clamped_to_query_max_limit(
        self, tmp_path: Path
    ) -> None:
        conn = duckdb.connect()
        _seed_telemetry(conn, 10)
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn
        )
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
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn
        )
        async with await _client(probe) as c:
            r = await c.get("/telemetry?limit=3", headers=_auth())
            body = await r.json()
            assert body["count"] == 3

    async def test_unknown_table_404(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn
        )
        async with await _client(probe) as c:
            r = await c.get("/not_a_real_table", headers=_auth())
            assert r.status == 404

    async def test_bad_since_400(self, tmp_path: Path) -> None:
        conn = duckdb.connect()
        _seed_telemetry(conn, 1)
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn
        )
        async with await _client(probe) as c:
            r = await c.get("/telemetry?since=garbage", headers=_auth())
            assert r.status == 400

    async def test_paging_via_advancing_since(self, tmp_path: Path) -> None:
        """Replicates the paging convention: ask for limit=2, advance
        `since` past the last row, ask again, etc."""
        conn = duckdb.connect()
        _seed_telemetry(conn, 5)
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path), db_connection=conn
        )
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
