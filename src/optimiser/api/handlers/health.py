"""Liveness and readiness probes. Unauthenticated.

- /healthz — the service is alive. Based on the heartbeat file's age,
  the same signal the external watchdog sidecar watches. Passes if
  heartbeat mtime is less than HEARTBEAT_STALE_S ago.
- /readyz — the service is ready to run the control loop. Requires
  state machine ACTIVE / ACTIVE_NO_PRICE *and* a connected inverter.
  FALLBACK / DEGRADED / INITIALISE all fail ready.
"""

from __future__ import annotations

import time
from pathlib import Path

from aiohttp import web

from ..probe import SERVICE_PROBE_KEY

HEARTBEAT_STALE_S = 60
READY_STATES = frozenset({"ACTIVE", "ACTIVE_NO_PRICE"})


async def healthz(request: web.Request) -> web.Response:
    probe = request.app[SERVICE_PROBE_KEY]
    path: Path = probe.heartbeat_path
    try:
        age_s = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return web.json_response(
            {"ok": False, "reason": "heartbeat file missing"}, status=503
        )
    except OSError as exc:
        return web.json_response(
            {"ok": False, "reason": f"heartbeat stat failed: {exc}"}, status=503
        )

    if age_s > HEARTBEAT_STALE_S:
        return web.json_response(
            {"ok": False, "heartbeat_age_s": round(age_s, 1)}, status=503
        )
    return web.json_response({"ok": True, "heartbeat_age_s": round(age_s, 1)})


async def readyz(request: web.Request) -> web.Response:
    probe = request.app[SERVICE_PROBE_KEY]
    state = probe.service_state
    connected = probe.sigenergy_connected
    ok = state in READY_STATES and connected
    body = {
        "ok": ok,
        "state": state,
        "sigenergy_connected": connected,
    }
    return web.json_response(body, status=200 if ok else 503)
