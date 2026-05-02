"""/ops/* — read-only ops dashboard endpoints.

Aggregates operator-facing health signals from two sources:

  * **Tick snapshots** (.ndjson.gz under ``storage.snapshot_dir``) for
    LP solve metrics. ``lp_solution.solve_time_ms`` and ``status`` are
    on every snapshot, so a histogram + status mix come straight from
    the existing snapshot pipeline — no event-log dependency.
  * **Event log** (events-YYYY-MM-DD.ndjson under ``storage.event_log_dir``)
    for everything else: API_CALL, MODBUS_READ_BATCH, MODBUS_WRITE,
    MODBUS_ERROR, MODBUS_RECONNECTED, FALLBACK_TRIGGERED,
    VERIFY_DEVIATION, BREAKER_LATCHED, STATE_TRANSITION, etc.

Pattern, copied from the rest of the API:
  * Synchronous DuckDB query in a private ``_run_*`` function.
  * Public handler dispatches via ``asyncio.to_thread`` wrapped in
    ``asyncio.wait_for(timeout=api_cfg.query_timeout_s)``.
  * In-memory TTL cache (``_OpsCache``) keyed on ``(endpoint, window_h)``
    so the JS dashboard can poll every 30 s without re-scanning the
    event log on every poll. NDJSON aggregation is the expensive bit
    and the data is slow-changing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import web

from ..probe import API_CONFIG_KEY, SERVICE_PROBE_KEY

logger = logging.getLogger(__name__)

# Default lookback windows. The dashboard usually wants "the last few
# hours" — long enough to see a trend, short enough to render quickly.
_DEFAULT_WINDOW_HOURS = 6
_MAX_WINDOW_HOURS = 168  # 7 days

# TTL on the per-endpoint result cache. 30s aligns with the dashboard
# polling cadence so a typical poll is a dict lookup, not a glob+scan.
_CACHE_TTL_S = 30.0


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def _parse_window_hours(raw: str | None) -> int:
    if not raw:
        return _DEFAULT_WINDOW_HOURS
    try:
        n = int(raw)
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=f"bad window_h {raw!r}") from exc
    if n <= 0:
        raise web.HTTPBadRequest(reason="window_h must be positive")
    return min(n, _MAX_WINDOW_HOURS)


def _utc_naive(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(UTC).replace(tzinfo=None)


def _snapshot_files(snapshot_dir, since_naive: datetime | None = None) -> list[str]:
    """All daily snapshot files (newest first). Read one at a time so
    a torn gzip stream in today's live file (which the snapshot writer
    appends to as multi-member gzip) doesn't kill the whole query —
    same file-skip pattern as ``api/handlers/snapshots.py``.

    When ``since_naive`` is provided, files dated *strictly before* the
    UTC date one day prior are skipped — the snapshot file
    ``YYYY-MM-DD.ndjson.gz`` covers that whole UTC day, so we keep one
    extra day of buffer to handle the rotation boundary cleanly. This
    matters because each daily file is ~15-18 MB and DuckDB cannot
    push the timestamp predicate into the gzipped JSON read; without
    pre-filtering, a window_h=1 query still scans every historical
    file in the directory.
    """
    paths = sorted(snapshot_dir.glob("*.ndjson.gz"), reverse=True)
    if since_naive is None:
        return [str(p) for p in paths]
    cutoff_date = (since_naive - timedelta(days=1)).date()
    out: list[str] = []
    for p in paths:
        # Filename stem is the UTC date the file rotated for; older
        # filenames cannot contain in-window snapshots.
        try:
            stem = p.name.split(".", 1)[0]
            file_date = datetime.fromisoformat(stem).date()
        except ValueError:
            # Unrecognised naming — keep it rather than silently drop
            out.append(str(p))
            continue
        if file_date >= cutoff_date:
            out.append(str(p))
    return out


def _event_glob(event_log_dir) -> str:
    """Glob expression for event log files (plain .ndjson)."""
    return str(event_log_dir / "events-*.ndjson")


# ─────────────────────────────────────────────────────────────────
# In-memory TTL cache
# ─────────────────────────────────────────────────────────────────


@dataclass
class _CacheEntry:
    expires_at: float
    payload: dict[str, Any]


class _OpsCache:
    """Process-local cache shared across all /ops endpoints.

    Keys are ``(endpoint, window_h)``. Concurrent dashboard refreshes
    inside the TTL collapse to one DuckDB scan; staleness is capped at
    ``_CACHE_TTL_S`` so the data never lags by more than 30s.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int], _CacheEntry] = {}

    def get(self, key: tuple[str, int]) -> dict[str, Any] | None:
        entry = self._entries.get(key)
        if entry is None or entry.expires_at < time.monotonic():
            return None
        return entry.payload

    def put(self, key: tuple[str, int], payload: dict[str, Any]) -> None:
        self._entries[key] = _CacheEntry(
            expires_at=time.monotonic() + _CACHE_TTL_S,
            payload=payload,
        )


_cache = _OpsCache()


# ─────────────────────────────────────────────────────────────────
# /ops/solve — LP solve time series + histogram + status mix
# ─────────────────────────────────────────────────────────────────


def _run_solve(
    conn,
    snapshot_files: list[str],
    since_naive: datetime,
) -> dict[str, Any]:
    """Aggregate LP solve metrics from snapshots in the window.

    Reads each daily .ndjson.gz file in isolation and skips any whose
    gzip stream fails to decode — matches the per-file-skip behaviour
    of ``api/handlers/snapshots.py`` so a torn live file (abrupt restart
    can leave the trailing gzip member truncated) doesn't kill the
    query.

    Returns three series:
      * series  — per-tick (timestamp, ms, status) for the line chart
      * histogram — ms-bucket counts (0-100ms, 100-250, 250-500, 500-1s,
        1-2s, 2-5s, 5-10s, >10s) for the density panel
      * status_counts — {OPTIMAL: n, TIMEOUT: n, ...} for the status mix
    """
    # `read_ndjson_objects` + `json_extract` is ~4x faster than
    # `read_json_auto` here. The TickSnapshot row is huge (433-slot
    # forward trajectory + nested forecasts); auto-infer parses the
    # whole row even though we only need three scalar paths. Treating
    # each line as opaque JSON and extracting just those paths skips
    # the heavy parsing entirely. Measured 1257ms → 339ms on a 10MB
    # day file (757 ticks).
    sql = """
    WITH s AS (
      SELECT
        CAST(json_extract_string(j, '$.timestamp') AS TIMESTAMP)        AS ts,
        CAST(json_extract(j, '$.lp_solution.solve_time_ms') AS DOUBLE)  AS ms,
        json_extract_string(j, '$.lp_solution.status')                  AS status
      FROM read_ndjson_objects(?) AS t(j)
    )
    SELECT ts, ms, status FROM s
    WHERE ms IS NOT NULL AND ts >= ?
    ORDER BY ts ASC
    """
    rows: list[tuple[Any, Any, Any]] = []
    skipped: list[dict[str, str]] = []
    for path in snapshot_files:
        cur = conn.cursor()
        try:
            try:
                cur.execute(sql, [path, since_naive])
                rows.extend(cur.fetchall())
            except Exception as exc:
                # Torn gzip / truncated last member — skip this file
                # rather than fail the whole window.
                logger.warning("ops/solve skipping %s: %s", path, str(exc)[:120])
                skipped.append({"path": path, "reason": str(exc)[:200]})
                continue
        finally:
            cur.close()

    series: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    bucket_edges = [100, 250, 500, 1000, 2000, 5000, 10000]
    bucket_labels = [
        "0-100ms",
        "100-250ms",
        "250-500ms",
        "500ms-1s",
        "1-2s",
        "2-5s",
        "5-10s",
        ">10s",
    ]
    bucket_counts = [0] * len(bucket_labels)

    for ts, ms, status in rows:
        if ms is None:
            continue
        ms_f = float(ms)
        series.append(
            {
                "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "ms": ms_f,
                "status": str(status) if status is not None else None,
            }
        )
        if status is not None:
            key = str(status)
            status_counts[key] = status_counts.get(key, 0) + 1
        idx = len(bucket_edges)
        for i, edge in enumerate(bucket_edges):
            if ms_f < edge:
                idx = i
                break
        bucket_counts[idx] += 1

    histogram = [
        {"bucket": label, "count": count}
        for label, count in zip(bucket_labels, bucket_counts, strict=True)
    ]
    out: dict[str, Any] = {
        "count": len(series),
        "series": series,
        "histogram": histogram,
        "status_counts": status_counts,
    }
    if skipped:
        out["skipped_files"] = skipped
    return out


async def ops_solve(request: web.Request) -> web.Response:
    """LP solve performance: time series, histogram, status mix."""
    probe = request.app[SERVICE_PROBE_KEY]
    api_cfg = request.app[API_CONFIG_KEY]
    window_h = _parse_window_hours(request.query.get("window_h"))

    cached = _cache.get(("solve", window_h))
    if cached is not None:
        return web.json_response(cached)

    snapshot_dir = probe.snapshot_dir
    if not snapshot_dir.exists():
        empty = {"count": 0, "series": [], "histogram": [], "status_counts": {}}
        return web.json_response(empty)

    since = _utc_naive(datetime.now(UTC) - timedelta(hours=window_h))
    files = _snapshot_files(snapshot_dir, since_naive=since)
    if not files:
        return web.json_response(
            {
                "count": 0,
                "series": [],
                "histogram": [],
                "status_counts": {},
                "window_h": window_h,
            }
        )
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_run_solve, probe.db_connection, files, since),
            timeout=api_cfg.query_timeout_s,
        )
    except TimeoutError:
        return web.json_response({"error": "query timed out"}, status=504)
    except Exception as exc:
        logger.exception("/ops/solve query failed")
        return web.json_response({"error": "query failed", "detail": str(exc)}, status=500)

    result["window_h"] = window_h
    _cache.put(("solve", window_h), result)
    return web.json_response(result)


# ─────────────────────────────────────────────────────────────────
# /ops/api_health — per-client req rate, error rate, p95 latency
# ─────────────────────────────────────────────────────────────────


def _run_api_health(
    conn,
    event_glob: str,
    since_naive: datetime,
) -> dict[str, Any]:
    """Aggregate API_CALL events by client across the window."""
    sql = """
    WITH e AS (
      SELECT
        CAST(ts AS TIMESTAMP)         AS ts,
        data.client                   AS client,
        data.op                       AS op,
        CAST(data.http_status AS INT) AS http_status,
        CAST(data.ms AS DOUBLE)       AS ms,
        CAST(data.ok AS BOOLEAN)      AS ok
      FROM read_json_auto(?, format='newline_delimited', ignore_errors=true)
      WHERE event = 'api_call'
        AND CAST(ts AS TIMESTAMP) >= ?
    )
    SELECT
      client,
      COUNT(*)                                                AS calls,
      COUNT(*) FILTER (WHERE ok = false)                      AS errors,
      QUANTILE_CONT(ms, 0.5)                                  AS p50_ms,
      QUANTILE_CONT(ms, 0.95)                                 AS p95_ms,
      MAX(ms)                                                 AS max_ms,
      MAX(ts)                                                 AS last_call_ts
    FROM e
    GROUP BY client
    ORDER BY client
    """
    cur = conn.cursor()
    try:
        cur.execute(sql, [event_glob, since_naive])
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
    finally:
        cur.close()

    clients: list[dict[str, Any]] = []
    for r in rows:
        rec = dict(zip(cols, r, strict=True))
        if rec.get("last_call_ts") is not None and hasattr(rec["last_call_ts"], "isoformat"):
            rec["last_call_ts"] = rec["last_call_ts"].isoformat()
        clients.append(rec)
    return {"clients": clients}


async def ops_api_health(request: web.Request) -> web.Response:
    probe = request.app[SERVICE_PROBE_KEY]
    api_cfg = request.app[API_CONFIG_KEY]
    window_h = _parse_window_hours(request.query.get("window_h"))

    cached = _cache.get(("api_health", window_h))
    if cached is not None:
        return web.json_response(cached)

    event_log_dir = probe.event_log_dir
    if not event_log_dir.exists():
        return web.json_response({"clients": [], "window_h": window_h})

    since = _utc_naive(datetime.now(UTC) - timedelta(hours=window_h))
    glob = _event_glob(event_log_dir)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_run_api_health, probe.db_connection, glob, since),
            timeout=api_cfg.query_timeout_s,
        )
    except TimeoutError:
        return web.json_response({"error": "query timed out"}, status=504)
    except Exception as exc:
        logger.exception("/ops/api_health query failed")
        return web.json_response({"error": "query failed", "detail": str(exc)}, status=500)

    result["window_h"] = window_h
    _cache.put(("api_health", window_h), result)
    return web.json_response(result)


# ─────────────────────────────────────────────────────────────────
# /ops/modbus — read latency, write success/error, reconnect rate,
#               watcher mismatches
# ─────────────────────────────────────────────────────────────────


def _run_modbus(
    conn,
    event_glob: str,
    since_naive: datetime,
) -> dict[str, Any]:
    """Aggregate Modbus health from event log.

    Six numbers operators care about:
      * read_batch p50/p95/max ms
      * total reads, total read errors (sum across batches)
      * write success vs error (overall + per-register table)
      * reconnect attempts in window
      * verify deviations (watcher caught the inverter ignoring us)
      * fallback triggers + breaker latches in window
    """
    cur = conn.cursor()

    read_sql = """
    WITH e AS (
      SELECT
        CAST(data.ms AS DOUBLE)         AS ms,
        CAST(data.reg_count AS INT)     AS reg_count,
        CAST(data.err_count AS INT)     AS err_count,
        CAST(data.reconnected AS BOOLEAN) AS reconnected,
        CAST(data.grid_sensor_ok AS BOOLEAN) AS grid_sensor_ok
      FROM read_json_auto(?, format='newline_delimited', ignore_errors=true)
      WHERE event = 'modbus_read_batch'
        AND CAST(ts AS TIMESTAMP) >= ?
    )
    SELECT
      COUNT(*)                          AS batches,
      QUANTILE_CONT(ms, 0.5)            AS p50_ms,
      QUANTILE_CONT(ms, 0.95)           AS p95_ms,
      MAX(ms)                           AS max_ms,
      SUM(reg_count)                    AS total_reads,
      SUM(err_count)                    AS total_read_errors,
      SUM(CASE WHEN reconnected THEN 1 ELSE 0 END) AS reconnect_ticks,
      SUM(CASE WHEN grid_sensor_ok THEN 0 ELSE 1 END) AS grid_sensor_offline_ticks
    FROM e
    """
    try:
        cur.execute(read_sql, [event_glob, since_naive])
        cols = [c[0] for c in cur.description]
        row = cur.fetchone()
    finally:
        pass
    reads = dict(zip(cols, row, strict=True)) if row is not None else {}

    write_sql = """
    SELECT
      event,
      CAST(data.register AS INT) AS register,
      COUNT(*)                   AS n,
      QUANTILE_CONT(CAST(data.ms AS DOUBLE), 0.95) AS p95_ms
    FROM read_json_auto(?, format='newline_delimited', ignore_errors=true)
    WHERE event IN ('modbus_write', 'modbus_error')
      AND CAST(ts AS TIMESTAMP) >= ?
    GROUP BY event, register
    ORDER BY register, event
    """
    cur2 = conn.cursor()
    try:
        cur2.execute(write_sql, [event_glob, since_naive])
        wcols = [c[0] for c in cur2.description]
        wrows = cur2.fetchall()
    finally:
        cur2.close()
    writes = [dict(zip(wcols, r, strict=True)) for r in wrows]

    incidents_sql = """
    SELECT event, COUNT(*) AS n
    FROM read_json_auto(?, format='newline_delimited', ignore_errors=true)
    WHERE event IN (
        'modbus_reconnected',
        'verify_deviation',
        'fallback_triggered',
        'breaker_latched',
        'breaker_cleared'
    )
      AND CAST(ts AS TIMESTAMP) >= ?
    GROUP BY event
    ORDER BY event
    """
    cur3 = conn.cursor()
    try:
        cur3.execute(incidents_sql, [event_glob, since_naive])
        irows = cur3.fetchall()
    finally:
        cur3.close()
    incidents = {str(event): int(n) for event, n in irows}

    cur.close()

    return {
        "reads": reads,
        "writes": writes,
        "incidents": incidents,
    }


async def ops_modbus(request: web.Request) -> web.Response:
    probe = request.app[SERVICE_PROBE_KEY]
    api_cfg = request.app[API_CONFIG_KEY]
    window_h = _parse_window_hours(request.query.get("window_h"))

    cached = _cache.get(("modbus", window_h))
    if cached is not None:
        return web.json_response(cached)

    event_log_dir = probe.event_log_dir
    if not event_log_dir.exists():
        return web.json_response({"reads": {}, "writes": [], "incidents": {}, "window_h": window_h})

    since = _utc_naive(datetime.now(UTC) - timedelta(hours=window_h))
    glob = _event_glob(event_log_dir)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_run_modbus, probe.db_connection, glob, since),
            timeout=api_cfg.query_timeout_s,
        )
    except TimeoutError:
        return web.json_response({"error": "query timed out"}, status=504)
    except Exception as exc:
        logger.exception("/ops/modbus query failed")
        return web.json_response({"error": "query failed", "detail": str(exc)}, status=500)

    result["window_h"] = window_h
    _cache.put(("modbus", window_h), result)
    return web.json_response(result)


# ─────────────────────────────────────────────────────────────────
# /ops/state — state machine transitions + fallback timeline
# ─────────────────────────────────────────────────────────────────


def _run_state(
    conn,
    event_glob: str,
    since_naive: datetime,
) -> dict[str, Any]:
    sql = """
    SELECT
      CAST(ts AS TIMESTAMP) AS ts,
      event,
      data
    FROM read_json_auto(?, format='newline_delimited', ignore_errors=true)
    WHERE event IN (
        'state_transition',
        'fallback_triggered',
        'breaker_latched',
        'breaker_cleared',
        'breaker_probe',
        'export_blocked_stale_price'
    )
      AND CAST(ts AS TIMESTAMP) >= ?
    ORDER BY ts ASC
    """
    cur = conn.cursor()
    try:
        cur.execute(sql, [event_glob, since_naive])
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
    finally:
        cur.close()

    out_rows = []
    for r in rows:
        rec = dict(zip(cols, r, strict=True))
        if hasattr(rec.get("ts"), "isoformat"):
            rec["ts"] = rec["ts"].isoformat()
        out_rows.append(rec)
    return {"events": out_rows, "count": len(out_rows)}


async def ops_state(request: web.Request) -> web.Response:
    probe = request.app[SERVICE_PROBE_KEY]
    api_cfg = request.app[API_CONFIG_KEY]
    window_h = _parse_window_hours(request.query.get("window_h"))

    cached = _cache.get(("state", window_h))
    if cached is not None:
        return web.json_response(cached)

    event_log_dir = probe.event_log_dir
    if not event_log_dir.exists():
        return web.json_response({"events": [], "count": 0, "window_h": window_h})

    since = _utc_naive(datetime.now(UTC) - timedelta(hours=window_h))
    glob = _event_glob(event_log_dir)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_run_state, probe.db_connection, glob, since),
            timeout=api_cfg.query_timeout_s,
        )
    except TimeoutError:
        return web.json_response({"error": "query timed out"}, status=504)
    except Exception as exc:
        logger.exception("/ops/state query failed")
        return web.json_response({"error": "query failed", "detail": str(exc)}, status=500)

    result["window_h"] = window_h
    _cache.put(("state", window_h), result)
    return web.json_response(result)
