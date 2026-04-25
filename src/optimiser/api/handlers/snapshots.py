"""/snapshots — range-query historical TickSnapshots via the NDJSON glob.

The snapshot writer drops one .ndjson.gz file per UTC day under
`storage.snapshot_dir`. DuckDB's `read_json_auto` streams compressed
NDJSON natively, so we expose it as a range query keyed on the
snapshot `timestamp` field (same paging convention as /{table}).

Unlike the DuckDB tables, this endpoint uses a small default and cap
for `limit` — each snapshot carries the full LP forward trajectory
(~432 slots × ~8 fields) plus price/PV forecasts, so a 1000-row
response is tens of megabytes of JSON and not useful. Keep pagination
small; the on-disk NDJSON remains the source of truth for bulk work.

Resilience note: the live (today's) .ndjson.gz is an append-mode gzip
with SYNC_FLUSH between writes. Each service restart opens a new
gzip member inside the same file, and an abrupt exit can leave the
trailing member truncated. DuckDB's gzip reader then fails the whole
query. We work around this by querying files one at a time and
skipping (with a `skipped_files` hint in the response) any file whose
gzip stream fails to decode, so a torn live file can't kill a history
query. Use /plan/current for fresh data.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from aiohttp import web

from ..probe import API_CONFIG_KEY, SERVICE_PROBE_KEY

logger = logging.getLogger(__name__)

SNAPSHOTS_DEFAULT_LIMIT = 20
SNAPSHOTS_MAX_LIMIT = 200


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=f"bad timestamp {raw!r}: {exc}") from exc


def _parse_limit(raw: str | None) -> int:
    if not raw:
        return SNAPSHOTS_DEFAULT_LIMIT
    try:
        n = int(raw)
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=f"bad limit {raw!r}") from exc
    if n <= 0:
        raise web.HTTPBadRequest(reason="limit must be positive")
    return min(n, SNAPSHOTS_MAX_LIMIT)


def _to_utc_naive(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(UTC).replace(tzinfo=None)


def _to_jsonable(v: Any) -> Any:
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, timedelta):
        return v.total_seconds()
    if isinstance(v, dict):
        return {k: _to_jsonable(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_to_jsonable(x) for x in v]
    return v


def _query_one_file(
    conn,
    path: str,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> tuple[list[str], list[list[Any]]]:
    # DuckDB auto-inference types `timestamp` as TIMESTAMP on simple
    # snapshots but falls back to VARCHAR on the real file — complex
    # nested objects (433-slot forward trajectory, multi-field nested
    # structs) push the sampler past what it confidently types. The
    # `CAST(... AS TIMESTAMP)` wrapper is a no-op when inferred as
    # TIMESTAMP and a per-row parse when VARCHAR — works in both
    # cases. Caveat: we pass naive UTC on the param side because the
    # resulting TIMESTAMP is naive too.
    where = []
    params: list[Any] = [path]
    if since is not None:
        where.append("CAST(timestamp AS TIMESTAMP) >= ?")
        params.append(_to_utc_naive(since))
    if until is not None:
        where.append("CAST(timestamp AS TIMESTAMP) < ?")
        params.append(_to_utc_naive(until))
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    sql = (
        f"SELECT * FROM read_json_auto(?, format='newline_delimited', "
        f"ignore_errors=true) {where_sql} "
        f"ORDER BY CAST(timestamp AS TIMESTAMP) ASC LIMIT ?"  # noqa: S608
    )
    params.append(limit)

    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
    finally:
        cur.close()
    return cols, [list(r) for r in rows]


def _run_query(
    conn,
    files: list[str],
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> tuple[list[str], list[list[Any]], list[dict[str, str]]]:
    """Query the snapshot files one at a time and merge, skipping files
    whose gzip stream fails to decode (typically the live file during
    a restart window). Returns columns, rows, and a list of skipped
    files with the DuckDB error message for diagnostic surfacing.
    """
    all_rows: list[list[Any]] = []
    skipped: list[dict[str, str]] = []
    cols: list[str] = []

    for path in files:
        try:
            file_cols, file_rows = _query_one_file(
                conn, path, since, until, limit
            )
        except Exception as exc:
            # Per-file skip is intentional: a torn gzip from an abrupt
            # restart (gzip stream error), or a truncated file with no
            # decodable JSON (schema-inference / BinderException), or
            # even a transient read failure shouldn't break a query
            # that can still serve the other files. Surface via
            # skipped_files so the caller can see what was excluded.
            logger.warning("skipping snapshot file %s: %s", path, str(exc)[:120])
            skipped.append({"path": path, "reason": str(exc)[:200]})
            continue
        if not cols and file_cols:
            cols = file_cols
        all_rows.extend(file_rows)

    # Per-file we LIMIT to keep memory bounded, then do a global
    # ORDER BY + LIMIT in Python. timestamp is the first column in
    # the TickSnapshot schema (field order from the dataclass), so
    # we have to look it up by name.
    ts_idx = cols.index("timestamp") if "timestamp" in cols else 0
    all_rows.sort(key=lambda r: r[ts_idx])
    all_rows = all_rows[:limit]

    return cols, all_rows, skipped


async def snapshots(request: web.Request) -> web.Response:
    probe = request.app[SERVICE_PROBE_KEY]
    api_cfg = request.app[API_CONFIG_KEY]
    snapshot_dir = probe.snapshot_dir

    if not snapshot_dir.exists():
        return web.json_response(
            {"snapshot_dir": str(snapshot_dir), "count": 0, "rows": []}
        )

    since = _parse_iso(request.query.get("since"))
    until = _parse_iso(request.query.get("until"))
    limit = _parse_limit(request.query.get("limit"))

    files = sorted(str(p) for p in snapshot_dir.glob("*.ndjson.gz"))
    if not files:
        return web.json_response(
            {"snapshot_dir": str(snapshot_dir), "count": 0, "rows": []}
        )

    try:
        cols, rows, skipped = await asyncio.wait_for(
            asyncio.to_thread(
                _run_query, probe.db_connection, files, since, until, limit
            ),
            timeout=api_cfg.query_timeout_s,
        )
    except TimeoutError:
        return web.json_response({"error": "query timed out"}, status=504)
    except Exception as exc:
        logger.exception("snapshots query failed")
        return web.json_response(
            {"error": "query failed", "detail": str(exc)}, status=500
        )

    out_rows = [
        {col: _to_jsonable(val) for col, val in zip(cols, row, strict=True)}
        for row in rows
    ]
    body: dict[str, Any] = {
        "snapshot_dir": str(snapshot_dir),
        "count": len(out_rows),
        "rows": out_rows,
    }
    if skipped:
        body["skipped_files"] = skipped
    return web.json_response(body)
