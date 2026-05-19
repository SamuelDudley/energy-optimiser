"""/dashboard/stream — Server-Sent Events feed of TickSnapshots.

Replaces the dashboard's per-15-second poll of `/plan/current` with a
push: the Service publishes each new TickSnapshot to a broadcaster, and
this handler drains a per-client queue out as `text/event-stream`.

On connect, the most recent snapshot (if any) is sent immediately so the
client renders without waiting up to 60 s for the next tick. After that,
the loop alternates between draining the subscriber queue and sending a
heartbeat comment if no snapshot arrives within ``_HEARTBEAT_S`` — the
heartbeat is what proxies and the browser use to notice a dead
connection. Auth is the same bearer header as every other endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from aiohttp import web

from ..probe import SERVICE_PROBE_KEY

logger = logging.getLogger(__name__)

# Send a comment line every N seconds when no snapshot arrives. Picks
# up dead connections faster than the OS TCP keepalive, and primes any
# intermediate proxy that buffers until the first byte.
_HEARTBEAT_S = 15.0


def _jsonable(obj: Any) -> Any:
    """Same shape as plan.py's serialiser — datetimes → ISO, dataclasses
    → dicts, fallthrough → str(). Kept inline to avoid cross-handler
    imports of private helpers."""
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    return str(obj)


def _format_event(event: str, payload: dict[str, Any]) -> bytes:
    data = json.dumps(payload, default=_jsonable)
    return f"event: {event}\ndata: {data}\n\n".encode()


async def dashboard_stream(request: web.Request) -> web.StreamResponse:
    probe = request.app[SERVICE_PROBE_KEY]
    broadcaster = probe.snapshot_broadcaster

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Tell nginx (if anyone fronts this) not to buffer.
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    # Subscribe first, then snapshot — this way a tick that lands
    # between the two can only cause a duplicate send, never a miss.
    queue = broadcaster.subscribe()
    try:
        # Initial bootstrap so the client renders immediately.
        snap = probe.last_snapshot
        if snap is not None:
            await response.write(_format_event("snapshot", asdict(snap)))

        while True:
            try:
                snapshot = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_S)
            except TimeoutError:
                # Comment line — clients ignore the data, but the bytes
                # keep the connection live. `:` prefix is the SSE
                # comment syntax.
                await response.write(b": keepalive\n\n")
                continue
            await response.write(_format_event("snapshot", asdict(snapshot)))
    except (ConnectionResetError, asyncio.CancelledError):
        # Client went away (closed tab, lost network). Normal — just
        # unwind cleanly.
        pass
    except Exception:
        logger.exception("dashboard SSE stream error")
    finally:
        broadcaster.unsubscribe(queue)

    return response
