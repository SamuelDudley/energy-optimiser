"""UniFi occupancy detection via WiFi client presence.

Polls the UniFi controller local API for connected clients and checks
if any tracked phone MAC addresses are present. Includes a grace period
to prevent flapping when phones briefly disconnect.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from ..config import OccupancyConfig
from ..logging_utils import api_call
from ..time_utils import now_utc

logger = logging.getLogger(__name__)


class UniFiOccupancyDetector:
    """Detect occupancy by checking if tracked phones are on WiFi."""

    def __init__(self, config: OccupancyConfig) -> None:
        self._config = config
        self._tracked = {mac.lower() for mac in config.tracked_macs}
        self._client = httpx.AsyncClient(
            verify=False,  # UniFi self-signed certs
            timeout=10.0,
        )
        self._cookie: str | None = None
        self._csrf: str | None = None

        # State
        self._occupied: bool = True  # Assume occupied at start
        self._override: bool | None = None
        self._last_seen: datetime = now_utc()
        self._grace_period = timedelta(minutes=config.away_threshold_min)

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def is_occupied(self) -> bool:
        if self._override is not None:
            return self._override
        return self._occupied

    async def set_override(self, occupied: bool | None) -> None:
        """Manual override. None clears the override."""
        self._override = occupied
        logger.info("Occupancy override set to %s", occupied)

    async def _login(self) -> bool:
        """Authenticate with the UniFi controller."""
        try:
            url = f"https://{self._config.unifi_host}:{self._config.unifi_port}/api/login"
            with api_call("unifi", "login") as call:
                resp = await self._client.post(
                    url,
                    json={
                        "username": self._config.unifi_username,
                        "password": self._config.unifi_password,
                    },
                )
                call.set_response(resp)
                resp.raise_for_status()
            # Session cookie is set automatically by httpx
            self._csrf = resp.headers.get("x-csrf-token")
            return True
        except Exception:
            logger.exception("UniFi login failed")
            return False

    async def poll(self) -> bool:
        """Poll UniFi for connected clients and update occupancy state.

        Returns current occupancy status.
        """
        if self._override is not None:
            return self._override

        if not self._tracked:
            # No phones configured — always occupied
            return True

        try:
            phones_seen = await self._get_connected_phones()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                # Re-authenticate and retry
                if await self._login():
                    phones_seen = await self._get_connected_phones()
                else:
                    return self._occupied
            else:
                logger.warning("UniFi poll failed: %s", e)
                return self._occupied
        except Exception:
            logger.exception("UniFi poll failed")
            return self._occupied

        now = now_utc()

        if phones_seen > 0:
            self._last_seen = now
            if not self._occupied:
                logger.info("Occupancy: arrived (%d phones)", phones_seen)
            self._occupied = True
        else:
            # Grace period before marking unoccupied
            time_since_seen = now - self._last_seen
            if time_since_seen >= self._grace_period:
                if self._occupied:
                    logger.info(
                        "Occupancy: away (no phones for %d min)",
                        int(time_since_seen.total_seconds() / 60),
                    )
                self._occupied = False

        return self._occupied

    async def _get_connected_phones(self) -> int:
        """Query UniFi for connected clients matching tracked MACs."""
        site = self._config.unifi_site
        url = f"https://{self._config.unifi_host}:{self._config.unifi_port}/api/s/{site}/stat/sta"
        headers = {}
        if self._csrf:
            headers["x-csrf-token"] = self._csrf

        with api_call("unifi", "list_clients") as call:
            resp = await self._client.get(url, headers=headers)
            call.set_response(resp)
            resp.raise_for_status()
        data = resp.json()

        clients = data.get("data", [])
        connected_macs = {c.get("mac", "").lower() for c in clients}
        matches = self._tracked & connected_macs
        return len(matches)
