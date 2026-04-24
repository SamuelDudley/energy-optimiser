"""Shared probe protocol + aiohttp AppKey.

Broken out so handlers can import it without cycling through
`api.server`, which imports from the handler modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

import duckdb
from aiohttp import web

from ..config import APIConfig
from .log_buffer import RingBufferHandler
from .metrics import Metrics


@runtime_checkable
class ServiceProbe(Protocol):
    """Minimal surface the API handlers need from the Service.

    Kept intentionally tiny: each attribute is a live read of something
    the Service already computes. No callbacks, no blocking calls.
    """

    version: str
    heartbeat_path: Path
    service_state: str  # ServiceState.value
    sigenergy_connected: bool
    db_connection: duckdb.DuckDBPyConnection
    metrics: Metrics
    log_buffer: RingBufferHandler | None


# Typed app key — handlers retrieve the probe via
# `request.app[SERVICE_PROBE_KEY]` instead of a bare string, which
# gives static typing and silences aiohttp's NotAppKeyWarning.
SERVICE_PROBE_KEY: web.AppKey[ServiceProbe] = web.AppKey(
    "service_probe", ServiceProbe
)

# Table-query handler reads the limit cap + timeout from this. Kept
# off ServiceProbe because it's the API server's own config, not
# something the Service computes.
API_CONFIG_KEY: web.AppKey[APIConfig] = web.AppKey("api_config", APIConfig)
