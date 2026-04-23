"""Bearer-token middleware.

The expected token is read from an environment variable named in
APIConfig.bearer_token_env. If the env var is unset or empty at server
start, the server refuses to start — failing closed is safer than
quietly shipping an open API.
"""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Awaitable, Callable, Iterable

from aiohttp import web

logger = logging.getLogger(__name__)

_HEADER = "Authorization"
_SCHEME = "Bearer "

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def load_token(env_var: str) -> str:
    token = os.environ.get(env_var, "").strip()
    if not token:
        raise RuntimeError(
            f"API bearer token env var {env_var!r} is unset or empty — "
            "refusing to start the HTTP server open"
        )
    return token


def make_auth_middleware(
    token: str, public_paths: Iterable[str]
) -> web.middleware:
    """Return a middleware that requires `Authorization: Bearer <token>`
    on every request except those whose path is in `public_paths`."""
    public = frozenset(public_paths)

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.path in public:
            return await handler(request)

        supplied = request.headers.get(_HEADER, "")
        if not supplied.startswith(_SCHEME):
            return _deny(request, "missing bearer")
        # hmac.compare_digest is constant-time
        if not hmac.compare_digest(supplied[len(_SCHEME) :], token):
            return _deny(request, "bad bearer")
        return await handler(request)

    return middleware


def _deny(request: web.Request, reason: str) -> web.Response:
    logger.warning(
        "api_auth_denied path=%s remote=%s reason=%s",
        request.path,
        request.remote,
        reason,
    )
    return web.Response(
        status=401,
        text='{"error": "unauthorized"}',
        content_type="application/json",
        headers={"WWW-Authenticate": 'Bearer realm="energy-optimiser"'},
    )
