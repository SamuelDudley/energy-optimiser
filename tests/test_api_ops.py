"""Tests for the /ops/* endpoints.

Each endpoint:
  * Returns an empty shape when its data source dir is missing
  * Reads NDJSON via DuckDB read_json_auto and aggregates correctly
  * Caches results inside the per-endpoint TTL (covered by direct
    handler test rather than HTTP roundtrip — same code path)
"""

from __future__ import annotations

import gzip
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from optimiser.api.handlers.ops import (
    ops_api_health,
    ops_modbus,
    ops_solve,
    ops_state,
)
from optimiser.api.probe import API_CONFIG_KEY, SERVICE_PROBE_KEY
from optimiser.config import APIConfig, BatteryConfig
from optimiser.logging_utils import _serialise

TOKEN = "test-token"


@dataclass
class _Probe:
    """Lightweight stub of the ServiceProbe protocol for ops tests."""

    snapshot_dir: Path
    event_log_dir: Path
    db_connection: duckdb.DuckDBPyConnection
    battery_config: BatteryConfig | None = None
    # The following are required by Protocol but not exercised by /ops handlers
    heartbeat_path: Path | None = None
    service_state: str = "active"
    sigenergy_connected: bool = True
    version: str = "ops-test"
    metrics: object | None = None
    log_buffer: object | None = None
    last_snapshot: object = None

    def __post_init__(self) -> None:
        if self.battery_config is None:
            self.battery_config = BatteryConfig()


def _build_app(probe: _Probe) -> web.Application:
    app = web.Application()
    app[SERVICE_PROBE_KEY] = probe
    app[API_CONFIG_KEY] = APIConfig(
        bearer_token_env="EO_API_TOKEN",
        query_max_limit=100,
        query_timeout_s=5.0,
    )
    app.router.add_get("/ops/solve", ops_solve)
    app.router.add_get("/ops/api_health", ops_api_health)
    app.router.add_get("/ops/modbus", ops_modbus)
    app.router.add_get("/ops/state", ops_state)
    return app


async def _client(probe: _Probe) -> TestClient:
    server = TestServer(_build_app(probe))
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """The ops cache is module-global; clear it between tests so an
    empty-dir test doesn't poison a populated-dir test that follows."""
    from optimiser.api.handlers.ops import _cache

    _cache._entries.clear()
    yield
    _cache._entries.clear()


def _fresh_probe(tmp_path: Path, *, with_dirs: bool = True) -> _Probe:
    snap_dir = tmp_path / "snapshots"
    evt_dir = tmp_path / "events"
    if with_dirs:
        snap_dir.mkdir()
        evt_dir.mkdir()
    return _Probe(
        snapshot_dir=snap_dir,
        event_log_dir=evt_dir,
        db_connection=duckdb.connect(),
    )


def _write_event_log(evt_dir: Path, ts: datetime, lines: list[dict]) -> None:
    """Write event records to the daily file matching ``ts.date()``."""
    path = evt_dir / f"events-{ts.date().isoformat()}.ndjson"
    with path.open("a", encoding="utf-8") as f:
        for record in lines:
            f.write(json.dumps(record, default=_serialise) + "\n")


# ─────────────────────────────────────────────────────────────────
# /ops/solve
# ─────────────────────────────────────────────────────────────────


def _minimal_snapshot(ts: datetime, solve_ms: float, status: str = "optimal") -> dict:
    """Hand-rolled snapshot dict with only the fields /ops/solve reads.

    Avoids hauling in the full TickSnapshot fixture from test_api.py
    so this file stays self-contained.
    """
    return {
        "timestamp": ts.isoformat(),
        "lp_solution": {
            "solve_time_ms": solve_ms,
            "status": status,
        },
    }


def _write_snapshots(snap_dir: Path, ts: datetime, snaps: list[dict]) -> None:
    """Append (don't overwrite) so test cases can write multiple batches
    per UTC date file without clobbering the first."""
    path = snap_dir / f"{ts.date().isoformat()}.ndjson.gz"
    # gzip "at" appends a new gzip member — same multi-member format the
    # production SnapshotWriter produces. DuckDB reads them transparently.
    with gzip.open(path, "at", encoding="utf-8") as f:
        for s in snaps:
            f.write(json.dumps(s) + "\n")


class TestOpsSolve:
    async def test_empty_when_snapshot_dir_missing(self, tmp_path: Path) -> None:
        probe = _fresh_probe(tmp_path, with_dirs=False)
        async with await _client(probe) as c:
            r = await c.get("/ops/solve")
            assert r.status == 200
            body = await r.json()
            assert body["count"] == 0
            assert body["series"] == []

    async def test_aggregates_solve_ms_into_series_and_histogram(self, tmp_path: Path) -> None:
        probe = _fresh_probe(tmp_path)
        now = datetime.now(UTC).replace(microsecond=0)
        snaps = [
            _minimal_snapshot(now - timedelta(minutes=4), 50.0, "optimal"),
            _minimal_snapshot(now - timedelta(minutes=3), 200.0, "optimal"),
            _minimal_snapshot(now - timedelta(minutes=2), 1500.0, "feasible"),
            _minimal_snapshot(now - timedelta(minutes=1), 6000.0, "timeout"),
        ]
        _write_snapshots(probe.snapshot_dir, now, snaps)

        async with await _client(probe) as c:
            r = await c.get("/ops/solve?window_h=1")
            assert r.status == 200
            body = await r.json()

        assert body["count"] == 4
        assert {row["status"] for row in body["series"]} == {
            "optimal",
            "feasible",
            "timeout",
        }

        # Bucket sanity: 50 → 0-100ms, 200 → 100-250ms,
        # 1500 → 1-2s, 6000 → 5-10s.
        bucket_map = {b["bucket"]: b["count"] for b in body["histogram"]}
        assert bucket_map["0-100ms"] == 1
        assert bucket_map["100-250ms"] == 1
        assert bucket_map["1-2s"] == 1
        assert bucket_map["5-10s"] == 1

        assert body["status_counts"] == {
            "optimal": 2,
            "feasible": 1,
            "timeout": 1,
        }

    async def test_window_filters_old_snapshots(self, tmp_path: Path) -> None:
        probe = _fresh_probe(tmp_path)
        now = datetime.now(UTC).replace(microsecond=0)
        # 36 h ago guarantees a different UTC date file (>24 h gap)
        # regardless of when in the day this test happens to run.
        old = now - timedelta(hours=36)
        snaps_today = [_minimal_snapshot(now, 100.0)]
        snaps_old = [_minimal_snapshot(old, 999.0)]
        _write_snapshots(probe.snapshot_dir, now, snaps_today)
        _write_snapshots(probe.snapshot_dir, old, snaps_old)

        async with await _client(probe) as c:
            r = await c.get("/ops/solve?window_h=1")
            assert r.status == 200
            body = await r.json()

        # Only the in-window snapshot is included
        assert body["count"] == 1
        assert body["series"][0]["ms"] == 100.0


# ─────────────────────────────────────────────────────────────────
# /ops/api_health
# ─────────────────────────────────────────────────────────────────


def _api_call(ts: datetime, client: str, op: str, status: int, ms: float) -> dict:
    return {
        "ts": ts.isoformat(),
        "event": "api_call",
        "data": {
            "client": client,
            "op": op,
            "http_status": status,
            "ms": ms,
            "ok": 200 <= status < 300,
        },
    }


class TestOpsApiHealth:
    async def test_empty_when_event_dir_missing(self, tmp_path: Path) -> None:
        probe = _fresh_probe(tmp_path, with_dirs=False)
        async with await _client(probe) as c:
            r = await c.get("/ops/api_health")
            assert r.status == 200
            body = await r.json()
            assert body["clients"] == []

    async def test_groups_by_client_and_computes_percentiles(self, tmp_path: Path) -> None:
        probe = _fresh_probe(tmp_path)
        now = datetime.now(UTC).replace(microsecond=0)
        events = [
            _api_call(now - timedelta(minutes=5), "amber", "prices_5min", 200, 100.0),
            _api_call(now - timedelta(minutes=4), "amber", "prices_5min", 200, 150.0),
            _api_call(now - timedelta(minutes=3), "amber", "prices_5min", 429, 50.0),
            _api_call(now - timedelta(minutes=2), "shelly", "em1_status", 200, 30.0),
            _api_call(now - timedelta(minutes=1), "shelly", "em1_status", 200, 40.0),
        ]
        _write_event_log(probe.event_log_dir, now, events)

        async with await _client(probe) as c:
            r = await c.get("/ops/api_health?window_h=1")
            assert r.status == 200
            body = await r.json()

        clients = {c["client"]: c for c in body["clients"]}
        assert set(clients) == {"amber", "shelly"}
        assert clients["amber"]["calls"] == 3
        assert clients["amber"]["errors"] == 1  # the 429
        assert clients["shelly"]["calls"] == 2
        assert clients["shelly"]["errors"] == 0
        # Percentiles sane (DuckDB uses linear interpolation; we just
        # check ranges to avoid pinning to interpolation specifics).
        assert 30.0 <= clients["shelly"]["p50_ms"] <= 40.0


# ─────────────────────────────────────────────────────────────────
# /ops/modbus
# ─────────────────────────────────────────────────────────────────


def _modbus_read_batch(
    ts: datetime,
    ms: float,
    reg_count: int,
    err_count: int,
    reconnected: bool = False,
    grid_sensor_ok: bool = True,
) -> dict:
    return {
        "ts": ts.isoformat(),
        "event": "modbus_read_batch",
        "data": {
            "ms": ms,
            "reg_count": reg_count,
            "err_count": err_count,
            "reconnected": reconnected,
            "grid_sensor_ok": grid_sensor_ok,
        },
    }


def _modbus_write(ts: datetime, register: int, ms: float, error: bool = False) -> dict:
    return {
        "ts": ts.isoformat(),
        "event": "modbus_error" if error else "modbus_write",
        "data": {"register": register, "value": 1, "ms": ms},
    }


class TestOpsModbus:
    async def test_aggregates_reads_and_writes(self, tmp_path: Path) -> None:
        probe = _fresh_probe(tmp_path)
        now = datetime.now(UTC).replace(microsecond=0)
        events = [
            _modbus_read_batch(now - timedelta(minutes=5), 120.0, 40, 0),
            _modbus_read_batch(now - timedelta(minutes=4), 150.0, 40, 1),
            _modbus_read_batch(
                now - timedelta(minutes=3),
                500.0,
                40,
                3,
                reconnected=True,
                grid_sensor_ok=False,
            ),
            _modbus_write(now - timedelta(minutes=2), 40031, 25.0),
            _modbus_write(now - timedelta(minutes=2), 40031, 30.0),
            _modbus_write(now - timedelta(minutes=1), 40032, 40.0, error=True),
            {
                "ts": (now - timedelta(minutes=2, seconds=30)).isoformat(),
                "event": "verify_deviation",
                "data": {},
            },
            {
                "ts": (now - timedelta(minutes=2)).isoformat(),
                "event": "modbus_reconnected",
                "data": {"attempts": 1, "ms": 12.0},
            },
        ]
        _write_event_log(probe.event_log_dir, now, events)

        async with await _client(probe) as c:
            r = await c.get("/ops/modbus?window_h=1")
            assert r.status == 200
            body = await r.json()

        reads = body["reads"]
        assert reads["batches"] == 3
        assert reads["total_reads"] == 120
        assert reads["total_read_errors"] == 4
        assert reads["reconnect_ticks"] == 1
        assert reads["grid_sensor_offline_ticks"] == 1
        # p95 should be the slow one (or near it).
        assert reads["p95_ms"] >= 150.0

        # Writes table grouped by (register, event).
        writes_by_key = {(w["register"], w["event"]): w["n"] for w in body["writes"]}
        assert writes_by_key[(40031, "modbus_write")] == 2
        assert writes_by_key[(40032, "modbus_error")] == 1

        # Incidents counter.
        assert body["incidents"]["verify_deviation"] == 1
        assert body["incidents"]["modbus_reconnected"] == 1


# ─────────────────────────────────────────────────────────────────
# /ops/state
# ─────────────────────────────────────────────────────────────────


class TestOpsState:
    async def test_returns_state_machine_and_fallback_events(self, tmp_path: Path) -> None:
        probe = _fresh_probe(tmp_path)
        now = datetime.now(UTC).replace(microsecond=0)
        events = [
            {
                "ts": (now - timedelta(minutes=10)).isoformat(),
                "event": "state_transition",
                "data": {"from": "active", "to": "degraded"},
            },
            {
                "ts": (now - timedelta(minutes=8)).isoformat(),
                "event": "fallback_triggered",
                "data": {"reason": "verify_deviation"},
            },
            {
                "ts": (now - timedelta(minutes=5)).isoformat(),
                "event": "breaker_latched",
                "data": {},
            },
            {
                "ts": (now - timedelta(minutes=3)).isoformat(),
                "event": "breaker_cleared",
                "data": {},
            },
            # Should be excluded — different event type
            {
                "ts": (now - timedelta(minutes=2)).isoformat(),
                "event": "tick_complete",
                "data": {},
            },
        ]
        _write_event_log(probe.event_log_dir, now, events)

        async with await _client(probe) as c:
            r = await c.get("/ops/state?window_h=1")
            assert r.status == 200
            body = await r.json()

        assert body["count"] == 4
        types = [e["event"] for e in body["events"]]
        assert types == [
            "state_transition",
            "fallback_triggered",
            "breaker_latched",
            "breaker_cleared",
        ]
