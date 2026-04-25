"""API self-description endpoints.

- GET /  — unauthenticated endpoint index. Lets an agent bootstrap the
  surface without needing a token: auth requirements, parameters,
  and a pointer to the per-table schema endpoint.
- GET /{table}/schema — authenticated. Runs DuckDB DESCRIBE on the
  named table, returning column name / type / nullable.

Table-name validation is a whitelist to guard against SQL injection
via the URL path; the query itself also parameterises where possible,
but DESCRIBE takes a literal identifier, so whitelisting is the only
safe way.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from ..probe import SERVICE_PROBE_KEY
from .tables import TABLE_TIME_COLUMNS

# Table → one-line description, surfaced by GET /{table}/schema so agents
# have a reason to reach for a given table. Keep these short; agents can
# follow up with the /schema response for column detail.
TABLE_DESCRIPTIONS: dict[str, str] = {
    "telemetry": (
        "5-minute inverter telemetry. One row per telemetry write: SOC, "
        "battery/PV/grid/load kW, Amber prices at the time, planner "
        "action, plus extended observational fields (SOH, cell temps, "
        "lifetime counters, MPPT strings, AC phases)."
    ),
    "load_telemetry": (
        "Per-managed-load 5-minute samples: instantaneous power, "
        "energy-today, cycle state, relay state. One row per configured "
        "managed load per telemetry tick."
    ),
    "pv_forecast_log": (
        "Solcast rooftop PV forecast intervals with P10/P50/P90 scenarios "
        "and (backfilled after the fact) actual production. Used for "
        "forecast-vs-actual analysis and replay."
    ),
    "price_forecast_log": (
        "Amber Electric import/export/spot price intervals. Rolling "
        "forecast plus a few past intervals so replay has full context."
    ),
    "weather_forecast_log": (
        "BOM hourly forecast intervals (temperature, precipitation "
        "probability, cloud, humidity). Used for load/HVAC heuristics."
    ),
}

# Endpoint catalogue. Handwritten from the handler list. Keep in sync
# when new endpoints are added.
def _endpoints_index() -> list[dict[str, Any]]:
    table_entries: list[dict[str, Any]] = []
    for name in TABLE_DESCRIPTIONS:
        time_col = TABLE_TIME_COLUMNS[name]
        table_entries.append(
            {
                "path": f"/{name}",
                "method": "GET",
                "auth": True,
                "description": (
                    f"Range-query {name!r} rows. Returns JSON array sorted "
                    f"ascending by {time_col}. Page by advancing `since` to "
                    f"the last row's {time_col} plus one microsecond."
                ),
                "params": {
                    "since": (
                        f"ISO 8601 timestamp (inclusive, filters on "
                        f"{time_col}) — required if until unset"
                    ),
                    "until": (
                        f"ISO 8601 timestamp (exclusive, filters on "
                        f"{time_col}) — optional"
                    ),
                    "limit": "int ≤ query_max_limit (default 1000)",
                },
                "schema": f"/{name}/schema",
            }
        )

    return [
        {
            "path": "/",
            "method": "GET",
            "auth": False,
            "description": "This endpoint index. No auth required for bootstrap.",
        },
        {
            "path": "/healthz",
            "method": "GET",
            "auth": False,
            "description": (
                "Liveness probe. 200 if the tick-loop heartbeat file was "
                "touched within the last 60 s; 503 otherwise."
            ),
        },
        {
            "path": "/readyz",
            "method": "GET",
            "auth": False,
            "description": (
                "Readiness probe. 200 if the state machine is ACTIVE or "
                "ACTIVE_NO_PRICE and the Sigenergy inverter is connected; "
                "503 otherwise."
            ),
        },
        {
            "path": "/metrics",
            "method": "GET",
            "auth": True,
            "format": "prometheus-text",
            "description": (
                "Prometheus exposition of live gauges, event counters, and "
                "solve/tick duration histograms."
            ),
        },
        {
            "path": "/logs",
            "method": "GET",
            "auth": True,
            "description": (
                "Tail of the in-memory log ring buffer. Newest-first JSON "
                "array. Authoritative log file is written to disk; use "
                "`docker logs` or the rotated log file for full history."
            ),
            "params": {
                "since": "ISO 8601 timestamp (inclusive) — optional",
                "until": "ISO 8601 timestamp (exclusive) — optional",
                "level": "DEBUG|INFO|WARNING|ERROR|CRITICAL — minimum level",
                "limit": "int ≤ 1000 (default 200)",
            },
        },
        {
            "path": "/plan/current",
            "method": "GET",
            "auth": True,
            "description": (
                "Most recent TickSnapshot in memory — the full LP plan "
                "(forward slot trajectory, dispatch, prices, forecasts) "
                "as produced by the tick loop. 503 until the first tick "
                "completes. Same shape as one line of a snapshot .ndjson.gz."
            ),
        },
        {
            "path": "/snapshots",
            "method": "GET",
            "auth": True,
            "description": (
                "Range-query historical TickSnapshots from the NDJSON "
                "snapshot glob via DuckDB. Small default/cap limit — "
                "each row carries the full LP forward trajectory and is "
                "large. Returns JSON array sorted ascending by "
                "`timestamp`. Page by advancing `since` to the last "
                "row's timestamp plus one microsecond."
            ),
            "params": {
                "since": "ISO 8601 timestamp (inclusive, filters on timestamp) — optional",
                "until": "ISO 8601 timestamp (exclusive, filters on timestamp) — optional",
                "limit": "int ≤ 200 (default 20)",
            },
        },
        *table_entries,
    ]


async def root(request: web.Request) -> web.Response:
    probe = request.app[SERVICE_PROBE_KEY]
    return web.json_response(
        {
            "service": "energy-optimiser",
            "version": probe.version,
            "auth": "Bearer token required except where auth=false",
            "endpoints": _endpoints_index(),
        }
    )


async def table_schema(request: web.Request) -> web.Response:
    table = request.match_info["table"]
    if table not in TABLE_DESCRIPTIONS:
        return web.json_response(
            {"error": "unknown table", "table": table}, status=404
        )

    probe = request.app[SERVICE_PROBE_KEY]
    # DESCRIBE is a fast, metadata-only query — safe to run on the live
    # connection without the async wrapper used for data queries.
    try:
        rows = probe.db_connection.execute(f"DESCRIBE {table}").fetchall()
    except Exception as exc:
        return web.json_response(
            {"error": "describe failed", "detail": str(exc)}, status=500
        )

    # DuckDB DESCRIBE returns (column_name, column_type, null, key, default, extra).
    columns = [
        {
            "name": r[0],
            "type": r[1],
            "nullable": str(r[2]).upper() == "YES",
        }
        for r in rows
    ]
    return web.json_response(
        {
            "table": table,
            "description": TABLE_DESCRIPTIONS[table],
            "columns": columns,
        }
    )
