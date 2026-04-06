"""BOM weather observations client.

Fetches current outdoor temperature from the Bureau of Meteorology
Canberra Airport station (94926). Free, no auth, updates every 30 min.

The JSON feed is "best-effort" — BOM occasionally returns:
- empty `data` arrays (station momentarily offline)
- entries with `null` air_temp (sensor maintenance)
- structurally different responses (rare schema changes)
- 403 with a HTML error page (anti-bot block)

Defensive parsing handles all of these by walking the observations list
to find the first valid `air_temp` and falling back to `_last_temp` on
any failure. A `VALIDATION_WARNING` event is emitted when the structure
is unexpected so an operator can spot a real schema change.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from ..config import WeatherConfig
from ..logging_utils import emit
from ..time_utils import now_utc
from ..types import EventType
from ._retry import DEFAULT_USER_AGENT, bom_retry

logger = logging.getLogger(__name__)


class BOMClient:
    """Async client for BOM weather observations JSON feed."""

    def __init__(self, config: WeatherConfig) -> None:
        self._config = config
        # BOM blocks generic-looking user-agents (returns 403). A polite
        # identifying UA prevents the anti-scraping rule from firing.
        self._client = httpx.AsyncClient(
            timeout=15.0,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        self._last_temp: float | None = None
        self._last_fetch: datetime | None = None

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def data_age(self) -> timedelta | None:
        if self._last_fetch is None:
            return None
        return now_utc() - self._last_fetch

    @property
    def last_temp(self) -> float | None:
        return self._last_temp

    async def get_outdoor_temp(self) -> float | None:
        """Fetch the latest outdoor temperature from BOM.

        BOM JSON structure (expected):
            {"observations": {"data": [{"air_temp": 15.2, ...}, ...]}}

        Returns the first non-null `air_temp` from the observations list
        (most recent first). Falls back to `_last_temp` on any error or
        malformed response.
        """
        try:
            # Retry transient 5xx/network errors. 403 (anti-bot) is NOT
            # retried — that's a policy decision by BOM. The outer
            # try/except catches retry exhaustion and falls back.
            async for attempt in bom_retry():
                with attempt:
                    resp = await self._client.get(self._config.bom_url)
                    resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.exception("Failed to fetch BOM weather data")
            return self._last_temp

        temp = self._parse_temperature(data)
        if temp is not None:
            self._last_temp = temp
            self._last_fetch = now_utc()
            logger.debug("BOM outdoor temp: %.1f°C", self._last_temp)
        return self._last_temp

    def _parse_temperature(self, data: object) -> float | None:
        """Extract the first valid air_temp, or None if structure is wrong.

        Defensive against:
        - data not being a dict
        - missing/wrong-typed `observations` or `data` keys
        - empty observation list
        - all observations having null/missing air_temp
        - air_temp values that aren't numeric
        """
        if not isinstance(data, dict):
            self._warn_malformed("response is not a JSON object", type(data).__name__)
            return None

        observations = data.get("observations")
        if not isinstance(observations, dict):
            self._warn_malformed(
                "missing or malformed 'observations' key",
                type(observations).__name__,
            )
            return None

        records = observations.get("data")
        if not isinstance(records, list):
            self._warn_malformed(
                "missing or malformed 'observations.data' key",
                type(records).__name__,
            )
            return None

        if not records:
            # Not malformed — just no data right now. Common during
            # station maintenance windows.
            logger.info("BOM returned an empty observations list")
            return None

        # Walk records (most recent first) for the first numeric air_temp.
        for entry in records:
            if not isinstance(entry, dict):
                continue
            raw = entry.get("air_temp")
            if raw is None:
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue

        # All entries had null/missing/non-numeric air_temp
        logger.warning(
            "BOM returned %d observations but none had a valid air_temp",
            len(records),
        )
        return None

    def _warn_malformed(self, reason: str, observed_type: str) -> None:
        """Emit a structured warning so a real schema change is visible."""
        emit(
            EventType.VALIDATION_WARNING,
            {
                "client": "bom",
                "message": f"BOM response malformed: {reason}",
                "observed_type": observed_type,
            },
        )
        logger.warning("BOM malformed response: %s (got %s)", reason, observed_type)
