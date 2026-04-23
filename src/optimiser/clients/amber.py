"""Amber Electric API client.

Polls /prices/current for current + forecast prices across general and
feedIn channels. Tracks the rate-limit headers Amber emits on every
response (RFC draft draft-ietf-httpapi-ratelimit-headers) so we can
pre-flight refuse calls that would push us over the documented 50/5-min
limit rather than wait for a 429.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from ..config import AmberConfig
from ..logging_utils import emit
from ..time_utils import now_utc, parse_iso
from ..types import EventType, PriceForecastLogRow, PriceInterval
from ._retry import amber_retry

logger = logging.getLogger(__name__)

BASE_URL = "https://api.amber.com.au/v1"

# Below this many remaining calls we start warning loudly. Amber's window
# is 5 minutes, and we consume roughly 6 calls/minute during normal ticks
# (60s 5-min poll + 5-min 30-min poll). 5 is comfortable headroom.
_RATE_LIMIT_WARNING_THRESHOLD = 5


class AmberClient:
    """Async client for the Amber Electric API."""

    def __init__(self, config: AmberConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=30.0,
        )
        self._last_prices: list[PriceInterval] | None = None
        self._last_fetch: datetime | None = None
        self._last_5min_prices: list[PriceInterval] | None = None
        self._last_5min_fetch: datetime | None = None
        # Log rows from the last fetch of each cadence. The service
        # drains these to DuckDB; we don't persist here so the client
        # stays free of a store dependency.
        self._last_log_rows_30min: list[PriceForecastLogRow] = []
        self._last_log_rows_5min: list[PriceForecastLogRow] = []
        # Rate limit headers from last successful response.
        self._rate_limit: int | None = None
        self._rate_remaining: int | None = None
        self._rate_reset_seconds: int | None = None
        self._warned_rate_low: bool = False

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def rate_limit_remaining(self) -> int | None:
        """Calls remaining in the current Amber rate-limit window.

        `None` if we haven't made a successful call yet (no header to parse).
        Updated on every non-error response.
        """
        return self._rate_remaining

    @property
    def rate_limit_reset_seconds(self) -> int | None:
        """Seconds until the current rate-limit window resets, or None."""
        return self._rate_reset_seconds

    @property
    def prices_age(self) -> timedelta | None:
        if self._last_fetch is None:
            return None
        return now_utc() - self._last_fetch

    @property
    def last_prices(self) -> list[PriceInterval] | None:
        return self._last_prices

    @property
    def prices_5min_age(self) -> timedelta | None:
        if self._last_5min_fetch is None:
            return None
        return now_utc() - self._last_5min_fetch

    @property
    def last_5min_prices(self) -> list[PriceInterval] | None:
        return self._last_5min_prices

    async def get_current_prices(self) -> list[PriceInterval]:
        """Fetch 30-min forecast prices (planning horizon)."""
        prices, log_rows = await self._fetch(
            next_count=self._config.forecast_intervals_30min,
            previous=0,
            resolution=30,
        )
        self._last_prices = prices
        self._last_fetch = now_utc()
        self._last_log_rows_30min = log_rows
        logger.info("Fetched %d 30-min price intervals from Amber", len(prices))
        return prices

    async def get_5min_prices(self) -> list[PriceInterval]:
        """Fetch 5-min resolution prices for the immediate window.

        Returns ~14 intervals: 2 previous + current + ~12 forward.
        Used for acute decisions (spike, neg export, neg import).
        """
        prices, log_rows = await self._fetch(
            next_count=self._config.forecast_intervals_5min,
            previous=self._config.previous_intervals_5min,
            resolution=5,
        )
        self._last_5min_prices = prices
        self._last_5min_fetch = now_utc()
        self._last_log_rows_5min = log_rows
        logger.info("Fetched %d 5-min price intervals from Amber", len(prices))
        return prices

    def drain_log_rows(self) -> list[PriceForecastLogRow]:
        """Drain pending price_forecast_log rows from both cadences.

        The service calls this once per tick after price fetches complete
        and persists the result to DuckDB. Returns [] if no new fetches
        have happened since the last drain. Draining is destructive —
        the internal buffers are emptied so double-logging can't occur.
        """
        out = self._last_log_rows_30min + self._last_log_rows_5min
        self._last_log_rows_30min = []
        self._last_log_rows_5min = []
        return out

    async def _fetch(
        self,
        next_count: int,
        previous: int,
        resolution: int,
    ) -> tuple[list[PriceInterval], list[PriceForecastLogRow]]:
        """Internal: fetch and merge general+feedIn channels.

        Returns both the domain PriceInterval list (LP-consumable) and a
        PriceForecastLogRow list (observability, for DuckDB). The two
        stay decoupled: LP never touches log rows, analytics never
        touches PriceIntervals.

        Retries on 5xx and network errors only (see `_retry.amber_retry`).
        429 is never retried — Amber's 50/5min limit would just re-trigger.
        Parses RateLimit-* response headers so callers can pre-flight.
        """
        url = f"/sites/{self._config.site_id}/prices/current"
        params = {"next": next_count, "previous": previous, "resolution": resolution}

        async for attempt in amber_retry():
            with attempt:
                resp = await self._client.get(url, params=params)
                resp.raise_for_status()

        self._update_rate_limits(resp)
        data = resp.json()

        general: dict[str, dict] = {}
        feed_in: dict[str, dict] = {}
        for interval in data:
            key = interval["startTime"]
            ch_type = interval.get("channelType", "general")
            if ch_type == "general":
                general[key] = interval
            elif ch_type == "feedIn":
                feed_in[key] = interval

        fetched_at = now_utc()
        prices: list[PriceInterval] = []
        log_rows: list[PriceForecastLogRow] = []
        for key in sorted(general.keys()):
            gen = general[key]
            fi = feed_in.get(key, {})
            advanced = gen.get("advancedPrice") or {}
            interval_type = gen.get("type")
            # CurrentInterval carries an `estimate` flag. False means the
            # final 30-min price has been locked in for that interval
            # (last ~5 min of the window). Actual/Forecast don't carry
            # this — leave is_locked as None for those.
            is_locked: bool | None = None
            if interval_type == "CurrentInterval" and "estimate" in gen:
                is_locked = not gen["estimate"]
            # Amber follows a NEM convention where startTime is offset by +1s
            # (e.g. "10:30:01") and endTime is the wall-clock boundary of the
            # current NEM settlement period (e.g. "11:00:00"), producing a
            # 1-second gap between consecutive intervals. The LP's slot grid
            # lands on exact boundaries, so every top-of-half-hour slot falls
            # into the gap. Normalise start back to the wall-clock boundary
            # so intervals are contiguous [start, end).
            start = parse_iso(gen["startTime"]).replace(second=0, microsecond=0)
            end = parse_iso(gen["endTime"]).replace(second=0, microsecond=0)
            per_kwh = gen.get("perKwh", 0)
            # Amber's feedIn.perKwh is signed from their ledger perspective:
            # negative = revenue to customer (feed-in tariff paid), positive =
            # cost to customer (solar-glut penalty). The LP's internal
            # convention is the customer's: positive = revenue from export.
            # Negate at the boundary so downstream code reads a natural
            # "what you get paid per kWh exported" (positive in normal hours,
            # negative only during curtailment events).
            export_per_kwh = -fi.get("perKwh", 0)
            spot = gen.get("spotPerKwh", 0)
            renewables = gen.get("renewables", 0)
            spike = gen.get("spikeStatus", "none")
            descriptor = gen.get("descriptor", "neutral")
            prices.append(
                PriceInterval(
                    start=start,
                    end=end,
                    import_per_kwh=per_kwh,
                    export_per_kwh=export_per_kwh,
                    spot_per_kwh=spot,
                    renewables_pct=renewables,
                    spike_status=spike,
                    descriptor=descriptor,
                    forecast_low=advanced.get("low"),
                    forecast_high=advanced.get("high"),
                    forecast_predicted=advanced.get("predicted"),
                    is_locked=is_locked,
                )
            )
            log_rows.append(
                PriceForecastLogRow(
                    fetched_at=fetched_at,
                    resolution=resolution,
                    interval_start=start,
                    interval_end=end,
                    interval_type=interval_type,
                    per_kwh=per_kwh,
                    export_per_kwh=export_per_kwh,
                    spot_per_kwh=spot,
                    forecast_predicted=advanced.get("predicted"),
                    forecast_low=advanced.get("low"),
                    forecast_high=advanced.get("high"),
                    spike_status=spike,
                    descriptor=descriptor,
                    is_locked=is_locked,
                    renewables_pct=renewables,
                )
            )
        return prices, log_rows

    def _update_rate_limits(self, resp: httpx.Response) -> None:
        """Parse RateLimit-* headers and maintain rising-edge warning state.

        Emits a `PRICE_STALE` event (reused as a generic "provider
        constraint reached" signal) when remaining drops below a
        threshold. Re-arms when remaining climbs back above.

        Defensive against response objects that don't expose headers
        (test mocks, etc.).
        """
        headers = getattr(resp, "headers", None)
        if headers is None:
            return
        try:
            limit = headers.get("RateLimit-Limit")
            remaining = headers.get("RateLimit-Remaining")
            reset = headers.get("RateLimit-Reset")
            self._rate_limit = int(limit) if limit is not None else self._rate_limit
            self._rate_remaining = int(remaining) if remaining is not None else self._rate_remaining
            self._rate_reset_seconds = int(reset) if reset is not None else self._rate_reset_seconds
        except (ValueError, TypeError, AttributeError):
            # Header not present / unparseable / headers object not dict-like.
            return

        if self._rate_remaining is None:
            return

        if self._rate_remaining <= _RATE_LIMIT_WARNING_THRESHOLD:
            if not self._warned_rate_low:
                emit(EventType.PRICE_STALE, {
                    "message": "Amber rate limit low",
                    "remaining": self._rate_remaining,
                    "limit": self._rate_limit,
                    "reset_seconds": self._rate_reset_seconds,
                })
                logger.warning(
                    "Amber rate limit low: %s/%s remaining, resets in %ss",
                    self._rate_remaining, self._rate_limit, self._rate_reset_seconds,
                )
                self._warned_rate_low = True
        else:
            self._warned_rate_low = False

    async def get_usage(
        self,
        start_date: str,
        end_date: str,
    ) -> list[dict]:
        """Fetch historical usage data (max 7-day window)."""
        url = f"/sites/{self._config.site_id}/usage"
        params = {"startDate": start_date, "endDate": end_date}
        async for attempt in amber_retry():
            with attempt:
                resp = await self._client.get(url, params=params)
                resp.raise_for_status()
        return resp.json()
