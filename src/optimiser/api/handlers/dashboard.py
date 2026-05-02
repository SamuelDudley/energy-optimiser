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

import os
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

from ..probe import SERVICE_PROBE_KEY


def _resolve_static_dir() -> Path:
    """Static dir from env override (dev mount) or package default."""
    override = os.environ.get("EO_DASHBOARD_STATIC_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "static"


_STATIC_DIR = _resolve_static_dir()

_STATIC_FILES: dict[str, str] = {
    "dashboard.css": "text/css",
    "dashboard.js": "application/javascript",
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
    Threading these through TickSnapshot for every tick would be
    wasteful — they change at config-reload time, not per tick.
    """
    probe = request.app[SERVICE_PROBE_KEY]
    return web.json_response(
        {
            "battery": asdict(probe.battery_config),
            "version": probe.version,
        }
    )
