"""/metrics — Prometheus text exposition.

The Metrics registry is updated inline as events occur (tick end, LP
end, dispatch write, state transition). This handler simply renders
the current registry state; it does not query or poll anything.
"""

from __future__ import annotations

import time

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ..probe import SERVICE_PROBE_KEY


async def metrics(request: web.Request) -> web.Response:
    probe = request.app[SERVICE_PROBE_KEY]

    # heartbeat_age is cheap to compute at scrape time and can't be
    # stored inline without going stale between ticks. Every other
    # metric is updated by the tick loop as events occur.
    try:
        age_s = time.time() - probe.heartbeat_path.stat().st_mtime
        probe.metrics.heartbeat_age_s.set(age_s)
    except OSError:
        pass

    output = generate_latest(probe.metrics.registry)
    # CONTENT_TYPE_LATEST includes the Prometheus protocol version
    # suffix ("text/plain; version=0.0.4; charset=utf-8"). Pass it
    # through the headers dict so aiohttp doesn't mangle the suffix.
    return web.Response(body=output, headers={"Content-Type": CONTENT_TYPE_LATEST})
