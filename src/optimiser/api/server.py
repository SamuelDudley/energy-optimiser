"""Read-only HTTP API server (aiohttp).

Wiring:
- `APIServer` owns the aiohttp Application and its TCPSite.
- `start()` and `stop()` are awaited by `Service.start()` / `Service.stop()`.
- Handlers reach live state via `request.app["service_probe"]`, a
  minimal Protocol the Service satisfies. This keeps the API package
  from taking a hard dependency on Service internals.
"""

from __future__ import annotations

import logging

from aiohttp import web

from ..config import APIConfig
from .auth import load_token, make_auth_middleware
from .handlers.discovery import root, table_schema
from .handlers.health import healthz, readyz
from .handlers.metrics import metrics as metrics_handler
from .probe import SERVICE_PROBE_KEY, ServiceProbe

logger = logging.getLogger(__name__)

# Endpoints that skip bearer-token auth. Liveness probes and the
# self-describing index are open so operators and agents can bootstrap
# without a token.
_PUBLIC_PATHS = ("/", "/healthz", "/readyz")


class APIServer:
    def __init__(self, config: APIConfig, probe: ServiceProbe) -> None:
        self._config = config
        self._probe = probe
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        if not self._config.enabled:
            logger.info("API server disabled in config")
            return

        # Fail closed if token is missing — surfaces a misconfiguration
        # at startup rather than quietly shipping an open API.
        token = load_token(self._config.bearer_token_env)

        app = web.Application(
            middlewares=[make_auth_middleware(token, _PUBLIC_PATHS)]
        )
        app[SERVICE_PROBE_KEY] = self._probe

        app.router.add_get("/", root)
        app.router.add_get("/healthz", healthz)
        app.router.add_get("/readyz", readyz)
        app.router.add_get("/metrics", metrics_handler)
        app.router.add_get("/{table}/schema", table_schema)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner, host=self._config.host, port=self._config.port
        )
        await self._site.start()
        logger.info(
            "API server listening on %s:%d", self._config.host, self._config.port
        )

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
