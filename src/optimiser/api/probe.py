"""Shared probe protocol + aiohttp AppKey.

Broken out so handlers can import it without cycling through
`api.server`, which imports from the handler modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import duckdb
from aiohttp import web

from ..config import APIConfig, BatteryConfig, ManagedLoadConfig
from ..types import TickSnapshot
from .log_buffer import RingBufferHandler
from .metrics import Metrics

if TYPE_CHECKING:
    from datetime import datetime

    from ..modes import ModeManager
    from ..types import PriceInterval


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
    # Most recent TickSnapshot — None until the first tick completes.
    # Powers /plan/current so it can avoid re-reading the NDJSON file.
    last_snapshot: TickSnapshot | None
    # Directory where NDJSON snapshots live (daily .ndjson.gz). Powers
    # /snapshots range queries via DuckDB read_json over a glob.
    snapshot_dir: Path
    # Directory where the daily event log lives (events-YYYY-MM-DD.ndjson).
    # Powers /ops/* range queries via DuckDB read_json over a glob.
    event_log_dir: Path
    # Battery configuration (capacity, SOC floor, charge/discharge caps).
    # Exposed for /dashboard/config so the SOC panel can draw the floor
    # line at the actual configured value rather than a hardcoded default.
    battery_config: BatteryConfig
    # Managed-load configs. Exposed for /dashboard/config so the load
    # cards can render the right target unit (kWh vs minutes) and
    # progress fraction without rebuilding state per tick.
    managed_load_configs: list[ManagedLoadConfig]

    @property
    def mode_manager(self) -> ModeManager: ...

    def amber_price_window(self, end_at: datetime) -> list[PriceInterval]: ...


# Typed app key — handlers retrieve the probe via
# `request.app[SERVICE_PROBE_KEY]` instead of a bare string, which
# gives static typing and silences aiohttp's NotAppKeyWarning.
SERVICE_PROBE_KEY: web.AppKey[ServiceProbe] = web.AppKey("service_probe", ServiceProbe)

# Table-query handler reads the limit cap + timeout from this. Kept
# off ServiceProbe because it's the API server's own config, not
# something the Service computes.
API_CONFIG_KEY: web.AppKey[APIConfig] = web.AppKey("api_config", APIConfig)
