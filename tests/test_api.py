"""Tests for the read-only HTTP API (aiohttp).

Uses aiohttp's in-process test client — no real TCP port is bound. The
handlers read state from a small stub that implements the ServiceProbe
protocol, so these tests don't stand up a Service.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import duckdb
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from optimiser.api.auth import make_auth_middleware
from optimiser.api.handlers.discovery import TABLE_DESCRIPTIONS, root, table_schema
from optimiser.api.handlers.health import healthz, readyz
from optimiser.api.probe import SERVICE_PROBE_KEY

TOKEN = "test-token-xyz"
_PUBLIC = ("/", "/healthz", "/readyz")


@dataclass
class _Probe:
    heartbeat_path: Path
    service_state: str = "ACTIVE"
    sigenergy_connected: bool = True
    version: str = "0.2.0-test"
    db_connection: duckdb.DuckDBPyConnection | None = None


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


def _build_app(probe: _Probe) -> web.Application:
    app = web.Application(middlewares=[make_auth_middleware(TOKEN, _PUBLIC)])
    app[SERVICE_PROBE_KEY] = probe
    app.router.add_get("/", root)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/{table}/schema", table_schema)
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
            service_state="ACTIVE",
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
            service_state="FALLBACK",
            sigenergy_connected=True,
        )
        async with await _client(probe) as c:
            r = await c.get("/readyz")
            assert r.status == 503

    async def test_disconnected_is_503(self, tmp_path: Path) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            service_state="ACTIVE",
            sigenergy_connected=False,
        )
        async with await _client(probe) as c:
            r = await c.get("/readyz")
            assert r.status == 503

    async def test_active_no_price_is_ready(self, tmp_path: Path) -> None:
        probe = _Probe(
            heartbeat_path=_fresh_heartbeat(tmp_path),
            service_state="ACTIVE_NO_PRICE",
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
