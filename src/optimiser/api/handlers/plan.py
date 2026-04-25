"""/plan/current — the most recent LP plan, as written to NDJSON.

Returns the in-memory `TickSnapshot` the tick loop just produced,
serialised to JSON with the same shape as one line of a .ndjson.gz
file. That covers the common "what is the planner doing right now"
question without needing a shell into the container.

503 while the service is still on its first tick (no snapshot yet).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from aiohttp import web

from ..probe import SERVICE_PROBE_KEY


def _jsonable(obj: Any) -> Any:
    """Serialiser matching logging_utils._serialise. Datetimes → ISO
    strings; any unexpected non-dataclass object falls back to str(),
    which keeps us from crashing a diagnostic endpoint on a type we
    haven't taught it about."""
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    return str(obj)


async def plan_current(request: web.Request) -> web.Response:
    probe = request.app[SERVICE_PROBE_KEY]
    snapshot = probe.last_snapshot
    if snapshot is None:
        return web.json_response(
            {"error": "no plan yet", "detail": "service has not completed a tick"},
            status=503,
        )
    return web.json_response(asdict(snapshot), dumps=_dumps)


def _dumps(obj: Any) -> str:
    """aiohttp's json_response accepts a `dumps` callable. We need a
    custom one because asdict() produces a plain dict, but nested
    datetimes / enums inside lists aren't unwrapped by asdict itself."""
    import json

    return json.dumps(obj, default=_jsonable)
