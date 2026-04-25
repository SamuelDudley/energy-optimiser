"""/{table} — range-query the DuckDB telemetry tables.

Shared handler for all five tables the service writes. Each request:
1. Validates the table name against a whitelist (defence-in-depth
   alongside the aiohttp route matcher).
2. Parses `since` / `until` / `limit` query params.
3. Runs a parameterised SELECT in a worker thread (DuckDB queries are
   synchronous; we use `conn.cursor()` so the tick loop's connection
   isn't blocked by a long read).
4. Serialises the result rows into JSON-safe dicts (datetimes → ISO
   strings).

Paging convention: rows are returned ascending by the table's time
column (see TABLE_TIME_COLUMNS), up to `limit`. If you get exactly
`limit` rows back, advance `since` to the last row's timestamp plus
1 microsecond for the next page.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

from aiohttp import web

from ..probe import API_CONFIG_KEY, SERVICE_PROBE_KEY

logger = logging.getLogger(__name__)

# Per-table time column used for since/until filtering and ORDER BY.
# Telemetry tables use `ts` (write-time); forecast logs use `fetched_at`
# (fetch-time), which is the append-order column for deterministic paging.
# Also acts as the whitelist of queryable tables — mirrors entries in
# `handlers.discovery.TABLE_DESCRIPTIONS`, kept in sync by convention.
TABLE_TIME_COLUMNS: dict[str, str] = {
    "telemetry": "ts",
    "load_telemetry": "ts",
    "pv_forecast_log": "fetched_at",
    "price_forecast_log": "fetched_at",
    "weather_forecast_log": "fetched_at",
}
QUERYABLE_TABLES = frozenset(TABLE_TIME_COLUMNS)

DEFAULT_LIMIT = 1000


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=f"bad timestamp {raw!r}: {exc}") from exc


def _parse_limit(raw: str | None, cap: int) -> int:
    if not raw:
        return min(DEFAULT_LIMIT, cap)
    try:
        n = int(raw)
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=f"bad limit {raw!r}") from exc
    if n <= 0:
        raise web.HTTPBadRequest(reason="limit must be positive")
    return min(n, cap)


def _to_jsonable(v: Any) -> Any:
    """Convert DuckDB-native types to JSON-compatible ones."""
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, timedelta):
        return v.total_seconds()
    return v


def _run_query(
    conn, table: str, since: datetime | None, until: datetime | None, limit: int
) -> tuple[list[str], list[list[Any]]]:
    """Synchronous core. Called via asyncio.to_thread.

    Uses `conn.cursor()` so this runs on a separate DuckDB query
    handle and doesn't serialise behind the tick loop's writes.
    """
    time_col = TABLE_TIME_COLUMNS[table]
    where = []
    params: list[Any] = []
    if since is not None:
        where.append(f"{time_col} >= ?")
        params.append(since)
    if until is not None:
        where.append(f"{time_col} < ?")
        params.append(until)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    # `table` and `time_col` are taken from the whitelist above — safe to
    # interpolate. Filter values are still parameterised.
    sql = f"SELECT * FROM {table} {where_sql} ORDER BY {time_col} ASC LIMIT ?"  # noqa: S608 — interpolation is whitelisted
    params.append(limit)

    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
    finally:
        cur.close()
    return cols, [list(r) for r in rows]


async def table_rows(request: web.Request) -> web.Response:
    table = request.match_info["table"]
    if table not in QUERYABLE_TABLES:
        return web.json_response(
            {"error": "unknown table", "table": table}, status=404
        )

    probe = request.app[SERVICE_PROBE_KEY]
    api_cfg = request.app[API_CONFIG_KEY]
    since = _parse_iso(request.query.get("since"))
    until = _parse_iso(request.query.get("until"))
    limit = _parse_limit(request.query.get("limit"), cap=api_cfg.query_max_limit)

    try:
        cols, rows = await asyncio.wait_for(
            asyncio.to_thread(
                _run_query, probe.db_connection, table, since, until, limit
            ),
            timeout=api_cfg.query_timeout_s,
        )
    except TimeoutError:
        return web.json_response(
            {"error": "query timed out", "table": table}, status=504
        )
    except Exception as exc:
        logger.exception("table query failed: %s", table)
        return web.json_response(
            {"error": "query failed", "detail": str(exc)}, status=500
        )

    out_rows = [
        {col: _to_jsonable(val) for col, val in zip(cols, row, strict=True)}
        for row in rows
    ]
    return web.json_response({"table": table, "count": len(out_rows), "rows": out_rows})
