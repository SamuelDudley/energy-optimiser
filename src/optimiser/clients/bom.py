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
from ..logging_utils import api_call, emit
from ..time_utils import now_utc
from ..types import EventType, WeatherForecastInterval
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
                with attempt, api_call("bom", "observations") as call:
                    resp = await self._client.get(self._config.bom_url)
                    call.set_response(resp)
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

    async def get_hourly_forecast(self) -> list[WeatherForecastInterval]:
        """Fetch BOM's hourly forecast for the configured geohash.

        Returns an empty list if the URL is empty, the fetch fails, or
        the response is malformed. Never raises. Schema is undocumented
        but stable (powers the BOM mobile app); all fields are defensively
        parsed so a shape change produces NULLs rather than exceptions.
        """
        url = getattr(self._config, "bom_forecast_url", "") or ""
        if not url:
            return []
        try:
            async for attempt in bom_retry():
                with attempt, api_call("bom", "hourly_forecast") as call:
                    resp = await self._client.get(url)
                    call.set_response(resp)
                    resp.raise_for_status()
            payload = resp.json()
        except Exception:
            logger.exception("Failed to fetch BOM hourly forecast")
            return []

        return self._parse_hourly_forecast(payload)

    def _parse_hourly_forecast(self, data: object) -> list[WeatherForecastInterval]:
        """Defensively parse BOM's hourly forecast JSON.

        Expected shape (undocumented, observed):
          {"data": [{"time": "ISO8601Z", "temp": float,
                     "temp_feels_like": float, "relative_humidity": int,
                     "rain": {"chance": int,
                              "amount": {"min": float, "max": float}},
                     "wind": {"speed_kilometre": int}, ...}, ...]}
        """
        if not isinstance(data, dict):
            self._warn_malformed("forecast response is not an object", type(data).__name__)
            return []
        records = data.get("data")
        if not isinstance(records, list):
            self._warn_malformed(
                "forecast 'data' key missing or not a list",
                type(records).__name__,
            )
            return []
        out: list[WeatherForecastInterval] = []
        for entry in records:
            if not isinstance(entry, dict):
                continue
            ts_raw = entry.get("time")
            if not isinstance(ts_raw, str):
                continue
            try:
                period_end = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            out.append(
                WeatherForecastInterval(
                    period_end=period_end,
                    temp_c=_maybe_float(entry.get("temp")),
                    apparent_temp_c=_maybe_float(entry.get("temp_feels_like")),
                    humidity_pct=_maybe_float(entry.get("relative_humidity")),
                    rain_chance_pct=_maybe_float(_dig(entry, "rain", "chance")),
                    rain_mm=_rain_amount_mid(entry.get("rain")),
                    wind_kmh=_maybe_float(_dig(entry, "wind", "speed_kilometre")),
                )
            )
        return out


def _maybe_float(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _dig(d: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _rain_amount_mid(rain: object) -> float | None:
    """Return the midpoint of BOM's rain min/max band. BOM reports a
    range when uncertain; the midpoint is a reasonable scalar summary
    for later analysis (a consumer that cares about the band can join
    back to the raw fetch log, if we ever keep one)."""
    amount = _dig(rain, "amount")
    if not isinstance(amount, dict):
        return None
    lo = _maybe_float(amount.get("min"))
    hi = _maybe_float(amount.get("max"))
    if lo is None and hi is None:
        return None
    if lo is None:
        return hi
    if hi is None:
        return lo
    return (lo + hi) / 2.0
