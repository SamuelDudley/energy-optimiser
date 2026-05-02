"""/daily_spend — daily aggregate of Amber's settled per-5-min usage.

Reads from the `amber_usage` table populated by the daily wake loop in
`service._backfill_amber_usage`. The aggregation is cheap (one row per
day per channel in the source), so we run it on every request rather
than materialising a separate daily table — keeps the persistence path
single-write/single-source-of-truth.

Sign convention matches Amber's: cost_cents is positive on the general
(import) channel and negative on feedIn (export). `net_cost_aud` is
SUM(cost_cents)/100, so a negative number means net revenue for the day.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any

from aiohttp import web

from ..probe import API_CONFIG_KEY, SERVICE_PROBE_KEY

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 60       # ~2 months — covers the dashboard panel
MAX_LIMIT = 366          # one year, capped to bound aggregate cost


def _parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        # Round-trip through `date.fromisoformat` to validate. Pass the
        # raw YYYY-MM-DD straight through to SQL — `nem_date` is stored
        # as VARCHAR (Amber's `date` field), and string compares are
        # well-defined in YYYY-MM-DD form.
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=f"bad date {raw!r}: {exc}") from exc


def _parse_limit(raw: str | None) -> int:
    if not raw:
        return DEFAULT_LIMIT
    try:
        n = int(raw)
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=f"bad limit {raw!r}") from exc
    if n <= 0:
        raise web.HTTPBadRequest(reason="limit must be positive")
    return min(n, MAX_LIMIT)


def _run_query(
    conn,
    since: str | None,
    until: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Synchronous DuckDB query. Run via asyncio.to_thread.

    Single GROUP BY pivots both channels in one pass via SUM(... FILTER):
      - import_kwh / cost = sum where channel='general'
      - export_kwh / revenue = sum where channel='feedIn' (sign-flipped
        to customer convention so positive = revenue)
      - net_cost_aud = SUM(cost_cents)/100 across all channels (Amber's
        sign convention already nets to the bill)

    Volume-weighted prices guard against div-by-zero on days with no
    flow on a channel (NULLIF ⇒ NULL ⇒ JSON null in the response).
    """
    where = []
    params: list[Any] = []
    if since is not None:
        where.append("nem_date >= ?")
        params.append(since)
    if until is not None:
        where.append("nem_date < ?")
        params.append(until)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    sql = f"""
        SELECT
            nem_date,
            -- Net to the bill: positive = paid, negative = earned.
            ROUND(SUM(cost_cents) / 100.0, 4) AS net_cost_aud,
            ROUND(SUM(cost_cents) FILTER (WHERE channel = 'general') / 100.0, 4)
                AS import_cost_aud,
            -- Sign-flip feedIn so it reads as revenue (positive = earned).
            ROUND(-SUM(cost_cents) FILTER (WHERE channel = 'feedIn') / 100.0, 4)
                AS export_revenue_aud,
            ROUND(SUM(kwh) FILTER (WHERE channel = 'general'), 4) AS import_kwh,
            ROUND(SUM(kwh) FILTER (WHERE channel = 'feedIn'), 4) AS export_kwh,
            ROUND(
                SUM(cost_cents) FILTER (WHERE channel = 'general')
                / NULLIF(SUM(kwh) FILTER (WHERE channel = 'general'), 0),
                4
            ) AS import_avg_ckwh,
            ROUND(
                -SUM(cost_cents) FILTER (WHERE channel = 'feedIn')
                / NULLIF(SUM(kwh) FILTER (WHERE channel = 'feedIn'), 0),
                4
            ) AS export_avg_ckwh,
            ROUND(AVG(renewables_pct), 2) AS renewables_avg_pct,
            COUNT(*) AS interval_count,
            COUNT(*) FILTER (WHERE quality = 'billable') AS billable_interval_count
        FROM amber_usage
        {where_sql}
        GROUP BY nem_date
        ORDER BY nem_date DESC
        LIMIT ?
    """  # noqa: S608 — `where_sql` is a fixed-key set, all values parameterised
    params.append(limit)

    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
    finally:
        cur.close()
    return [dict(zip(cols, r, strict=True)) for r in rows]


def _to_jsonable(v: Any) -> Any:
    if isinstance(v, datetime | date):
        return v.isoformat()
    return v


async def daily_spend(request: web.Request) -> web.Response:
    probe = request.app[SERVICE_PROBE_KEY]
    api_cfg = request.app[API_CONFIG_KEY]
    since = _parse_date(request.query.get("since"))
    until = _parse_date(request.query.get("until"))
    limit = _parse_limit(request.query.get("limit"))

    try:
        rows = await asyncio.wait_for(
            asyncio.to_thread(
                _run_query, probe.db_connection, since, until, limit,
            ),
            timeout=api_cfg.query_timeout_s,
        )
    except TimeoutError:
        return web.json_response({"error": "query timed out"}, status=504)
    except Exception as exc:
        logger.exception("daily_spend query failed")
        return web.json_response(
            {"error": "query failed", "detail": str(exc)}, status=500,
        )

    out = [
        {col: _to_jsonable(val) for col, val in row.items()}
        for row in rows
    ]
    return web.json_response({"count": len(out), "rows": out})
