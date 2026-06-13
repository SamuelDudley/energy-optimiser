"""/dashboard — interactive operator dashboard.

Serves a single-page HTML app + its JS/CSS assets out of
``src/optimiser/api/static/``. The HTML page itself is unauthenticated
(it carries no data); the in-page JS prompts for the bearer token on
first load and stashes it in localStorage, then uses it to fetch
``/plan/current``, ``/telemetry``, ``/dashboard/config``, etc.

Static-asset whitelist: only files explicitly listed in ``_STATIC_FILES``
are served, both to limit the path-traversal surface and to keep adding
a new asset an intentional code change.

Dev workflow: set ``EO_DASHBOARD_STATIC_DIR`` to a host-mounted directory
inside the container (see ``docker-compose.yml``) so HTML / CSS / JS
edits hot-reload without rebuilding the image. Files are read from disk
on every request; in production this is a few-kB synchronous read per
poll, and the dashboard is typically open by one operator at a time.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from ..probe import API_CONFIG_KEY, SERVICE_PROBE_KEY
from .tables import _parse_iso, _to_jsonable

logger = logging.getLogger(__name__)


def _resolve_static_dir() -> Path:
    """Static dir from env override (dev mount) or package default."""
    override = os.environ.get("EO_DASHBOARD_STATIC_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "static"


_STATIC_DIR = _resolve_static_dir()

_STATIC_FILES: dict[str, str] = {
    "dashboard.css": "text/css",
    "chart-utils.js": "application/javascript",
    "dashboard.js": "application/javascript",
    "ops.js": "application/javascript",
}


async def dashboard_index(request: web.Request) -> web.Response:
    """Serve the dashboard HTML page. Public — no data in the markup."""
    html_path = _STATIC_DIR / "dashboard.html"
    try:
        body = html_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return web.Response(status=500, text="dashboard.html missing")
    return web.Response(text=body, content_type="text/html")


async def dashboard_static(request: web.Request) -> web.Response:
    """Serve a whitelisted static asset (CSS / JS). Public."""
    name = request.match_info["filename"]
    content_type = _STATIC_FILES.get(name)
    if content_type is None:
        return web.Response(status=404, text="not found")
    path = _STATIC_DIR / name
    try:
        body = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return web.Response(status=404, text="not found")
    return web.Response(text=body, content_type=content_type)


async def dashboard_config(request: web.Request) -> web.Response:
    """Authed bundle of config the dashboard needs but isn't in TickSnapshot.

    The LP's hard SOC floor (``battery.soc_floor_pct``) drives the floor
    line on the SOC panel; rate limits constrain a few axis ranges.
    Managed-load knobs let the load cards render the right target unit
    (kWh vs minutes) and progress fraction. Threading these through
    TickSnapshot for every tick would be wasteful — they change at
    config-reload time, not per tick.
    """
    probe = request.app[SERVICE_PROBE_KEY]
    managed_loads = [
        {
            "load_id": cfg.load_id,
            "category": cfg.category.value,
            "draw_kw": cfg.draw_kw,
            "daily_target_kwh": cfg.daily_target_kwh,
            "daily_run_minutes": cfg.daily_run_minutes,
            "deadline_hour_local": cfg.deadline_hour_local,
        }
        for cfg in probe.managed_load_configs
    ]
    now = datetime.now(UTC)
    active_modes = [m.to_dict() for m in probe.mode_manager.active(now)]
    return web.json_response(
        {
            "battery": asdict(probe.battery_config),
            "managed_loads": managed_loads,
            "active_modes": active_modes,
            "version": probe.version,
        }
    )


# The forecast-log tables are re-written in full on every upstream poll
# (Amber every 60s, Solcast on each fetch), so a 24h window holds ~14.5k
# raw price rows that collapse to a few hundred distinct intervals. These
# two endpoints push that reduction server-side — keeping only the latest
# forecast per interval — so the dashboard fetches the rendered view
# (~hundreds of rows) instead of the raw re-fetch log (~MBs). The SQL
# mirrors what dashboard.js previously did client-side in
# bucketLatestPriceForecast / bucketLatestPVForecast.
#
# {where} is injected by _run_reduce_query with a parameterised fetched_at
# range (the only filter the dashboard sends).
_PRICE_FORECAST_REDUCE_SQL = """
WITH windowed AS (
    SELECT * FROM price_forecast_log {where}
),
latest_per_res AS (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY interval_start, resolution
            ORDER BY fetched_at DESC
        ) AS _rn
    FROM windowed
    WHERE forecast_predicted IS NOT NULL OR forecast_low IS NOT NULL
),
best_res AS (
    SELECT * EXCLUDE (_rn),
        ROW_NUMBER() OVER (
            PARTITION BY interval_start
            ORDER BY resolution ASC
        ) AS _rn2
    FROM latest_per_res
    WHERE _rn = 1
)
SELECT * EXCLUDE (_rn2)
FROM best_res
WHERE _rn2 = 1
ORDER BY interval_start ASC
"""

_PV_FORECAST_REDUCE_SQL = """
WITH windowed AS (
    SELECT * FROM pv_forecast_log {where}
),
latest AS (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY period_end
            ORDER BY fetched_at DESC
        ) AS _rn
    FROM windowed
)
SELECT * EXCLUDE (_rn)
FROM latest
WHERE _rn = 1
ORDER BY period_end ASC
"""


def _run_reduce_query(
    conn, sql_template: str, since: datetime | None, until: datetime | None
) -> tuple[list[str], list[list[Any]]]:
    """Synchronous core for the reduce endpoints. Called via
    asyncio.to_thread on a fresh cursor so the tick loop's connection
    isn't blocked by the read (same pattern as tables._run_query)."""
    where: list[str] = []
    params: list[Any] = []
    if since is not None:
        where.append("fetched_at >= ?")
        params.append(since)
    if until is not None:
        where.append("fetched_at < ?")
        params.append(until)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    sql = sql_template.format(where=where_sql)

    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
    finally:
        cur.close()
    return cols, [list(r) for r in rows]


async def _reduce_handler(request: web.Request, table: str, sql_template: str) -> web.Response:
    probe = request.app[SERVICE_PROBE_KEY]
    api_cfg = request.app[API_CONFIG_KEY]
    since = _parse_iso(request.query.get("since"))
    until = _parse_iso(request.query.get("until"))

    try:
        cols, rows = await asyncio.wait_for(
            asyncio.to_thread(_run_reduce_query, probe.db_connection, sql_template, since, until),
            timeout=api_cfg.query_timeout_s,
        )
    except TimeoutError:
        return web.json_response({"error": "query timed out", "table": table}, status=504)
    except Exception as exc:
        logger.exception("forecast reduce query failed: %s", table)
        return web.json_response({"error": "query failed", "detail": str(exc)}, status=500)

    out_rows = [
        {col: _to_jsonable(val) for col, val in zip(cols, row, strict=True)} for row in rows
    ]
    return web.json_response({"table": table, "count": len(out_rows), "rows": out_rows})


async def price_forecast(request: web.Request) -> web.Response:
    """Reduced price-forecast view: latest forecast per interval, best
    resolution (5-min beats 30-min). Replaces a ~14.5k-row/24h raw fetch."""
    return await _reduce_handler(request, "price_forecast_log", _PRICE_FORECAST_REDUCE_SQL)


async def pv_forecast(request: web.Request) -> web.Response:
    """Reduced PV-forecast view: latest forecast per period_end."""
    return await _reduce_handler(request, "pv_forecast_log", _PV_FORECAST_REDUCE_SQL)
