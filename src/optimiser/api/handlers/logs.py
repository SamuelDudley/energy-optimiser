"""/logs — read recent records from the in-memory ring buffer.

Ring buffer is authoritative for the "last N minutes" use case. For
the full history operators use Docker's log driver or the
RotatingFileHandler file on disk — this endpoint is tuned for fast
programmatic tailing, not comprehensive retrieval.
"""

from __future__ import annotations

import logging
from datetime import datetime

from aiohttp import web

from ..probe import SERVICE_PROBE_KEY

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise web.HTTPBadRequest(reason=f"bad timestamp {raw!r}: {exc}") from exc


def _parse_level(raw: str | None) -> int:
    if not raw:
        return logging.DEBUG
    name = raw.upper()
    level = logging.getLevelName(name)
    if not isinstance(level, int):
        raise web.HTTPBadRequest(reason=f"unknown level {raw!r}")
    return level


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


async def logs(request: web.Request) -> web.Response:
    probe = request.app[SERVICE_PROBE_KEY]
    buf = probe.log_buffer  # RingBufferHandler
    if buf is None:
        return web.json_response(
            {"error": "log buffer not attached"}, status=503
        )

    since = _parse_iso(request.query.get("since"))
    until = _parse_iso(request.query.get("until"))
    level = _parse_level(request.query.get("level"))
    limit = _parse_limit(request.query.get("limit"))

    records = buf.snapshot(since=since, until=until, min_level=level, limit=limit)
    return web.json_response({"count": len(records), "records": records})
