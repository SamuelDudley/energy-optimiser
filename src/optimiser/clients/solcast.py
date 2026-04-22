"""Solcast rooftop PV forecast client.

Free hobbyist tier: **10 API calls per day**, resets at midnight UTC.
This is a hard quota — exceeding it returns 429 with no quota until
the next reset. Forecasts are 30-min intervals with P10/P50/P90
estimates in kW.

Quota management strategy:
- Track successful calls per UTC day (`_call_count_today`).
- Pre-flight every fetch: if `count >= max_calls_per_day - safety_buffer`,
  refuse to call and return cached data with a warning event.
- Don't retry 429s — the next scheduled poll is the correct fallback
  (poll interval defaults to 86400/10 = 8640 s).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import httpx

from ..config import SolcastConfig
from ..logging_utils import emit
from ..time_utils import now_utc, parse_iso
from ..types import EventType, PVForecast, PVForecastLogRow
from ._retry import solcast_retry

logger = logging.getLogger(__name__)


class SolcastClient:
    """Async client for the Solcast rooftop API."""

    def __init__(self, config: SolcastConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=30.0,
        )
        self._last_forecast: list[PVForecast] | None = None
        self._last_fetch: datetime | None = None
        self._pending_log_rows: list[PVForecastLogRow] = []
        # Quota tracker (resets at UTC midnight). Counts successful
        # responses only — 4xx/5xx don't count against quota.
        self._call_count_today: int = 0
        self._quota_date: date | None = None

    async def close(self) -> None:
        await self._client.aclose()

    def drain_log_rows(self) -> list[PVForecastLogRow]:
        """Drain pending pv_forecast_log rows from the last successful
        fetch. Destructive — the internal buffer is emptied so
        double-logging can't occur. Returns [] if no new fetches have
        happened since the last drain."""
        out = self._pending_log_rows
        self._pending_log_rows = []
        return out

    def seed_cache(self, forecast: list[PVForecast], fetched_at: datetime) -> None:
        """Populate the in-memory cache from an external source (typically
        the DuckDB log on startup) so the first tick has PV data without
        spending an API call. No log rows are generated — we're not
        re-fetching, just restoring."""
        self._last_forecast = forecast
        self._last_fetch = fetched_at

    @property
    def forecast_age(self) -> timedelta | None:
        if self._last_fetch is None:
            return None
        return now_utc() - self._last_fetch

    @property
    def last_forecast(self) -> list[PVForecast] | None:
        return self._last_forecast

    @property
    def calls_today(self) -> int:
        """Diagnostics: successful Solcast calls so far this UTC day."""
        self._maybe_reset_quota()
        return self._call_count_today

    def _maybe_reset_quota(self) -> None:
        """Reset call counter at UTC day rollover."""
        today = now_utc().date()
        if self._quota_date != today:
            self._quota_date = today
            self._call_count_today = 0

    def _quota_remaining(self) -> int:
        """Calls left today before hitting the safety buffer."""
        self._maybe_reset_quota()
        return self._config.max_calls_per_day - self._config.safety_buffer - self._call_count_today

    async def get_forecast(self) -> list[PVForecast]:
        """Fetch rooftop PV production forecast.

        Response format:
        {
            "forecasts": [
                {
                    "pv_estimate": 0.59,      # kW
                    "pv_estimate10": 0.44,     # kW P10 (pessimistic)
                    "pv_estimate90": 0.69,     # kW P90 (optimistic)
                    "period_end": "2024-10-12T09:30:00.0000000Z",
                    "period": "PT30M"
                }
            ]
        }
        """
        url = f"/rooftop_sites/{self._config.resource_id}/forecasts"
        params = {
            "format": "json",
            "hours": self._config.forecast_hours,
        }

        # Pre-flight: refuse to spend our last quota on a routine poll.
        # Returns the cached forecast if any, else an empty list.
        remaining = self._quota_remaining()
        if remaining <= 0:
            emit(
                EventType.PRICE_STALE,
                {  # closest existing event type
                    "client": "solcast",
                    "message": (
                        f"Solcast daily quota exhausted "
                        f"({self._call_count_today}/{self._config.max_calls_per_day}); "
                        f"using cached forecast until midnight UTC reset"
                    ),
                    "calls_today": self._call_count_today,
                    "quota": self._config.max_calls_per_day,
                },
            )
            return self._last_forecast or []

        # Hobbyist tier: hard 10 calls/day. We retry only 5xx/network —
        # 429 is not retried (could be quota-exhausted or load-shedding;
        # either way, next scheduled poll is the right fallback).
        async for attempt in solcast_retry():
            with attempt:
                resp = await self._client.get(url, params=params)
                resp.raise_for_status()
        # Successful response — count against quota
        self._call_count_today += 1
        data = resp.json()

        fetched_at = now_utc()
        forecasts: list[PVForecast] = []
        log_rows: list[PVForecastLogRow] = []
        for item in data.get("forecasts", []):
            period_end = parse_iso(item["period_end"])
            # Period is typically PT30M — compute start from end
            period_str = item.get("period", "PT30M")
            minutes = 30  # default
            if period_str == "PT30M":
                minutes = 30
            elif period_str == "PT5M":
                minutes = 5
            period_start = period_end - timedelta(minutes=minutes)

            pv50 = float(item.get("pv_estimate", 0))
            pv10 = float(item.get("pv_estimate10", 0))
            pv90 = float(item.get("pv_estimate90", 0))

            forecasts.append(
                PVForecast(
                    start=period_start,
                    end=period_end,
                    pv_estimate_kw=pv50,
                    pv_estimate10_kw=pv10,
                    pv_estimate90_kw=pv90,
                )
            )
            log_rows.append(
                PVForecastLogRow(
                    fetched_at=fetched_at,
                    period_end=period_end,
                    pv_estimate_kw=pv50,
                    pv_estimate10_kw=pv10,
                    pv_estimate90_kw=pv90,
                )
            )

        self._last_forecast = forecasts
        self._last_fetch = fetched_at
        self._pending_log_rows = log_rows
        logger.info("Fetched %d PV forecast intervals from Solcast", len(forecasts))
        return forecasts

    async def get_estimated_actuals(self) -> list[dict]:
        """Fetch estimated actuals for forecast accuracy tracking.

        Counts against the same daily quota as `get_forecast` — this
        endpoint shares the per-site call budget. Returns [] if quota
        is exhausted rather than spending our last call.
        """
        if self._quota_remaining() <= 0:
            logger.info("Skipping Solcast estimated_actuals — quota exhausted")
            return []

        url = f"/rooftop_sites/{self._config.resource_id}/estimated_actuals"
        params = {"format": "json"}
        async for attempt in solcast_retry():
            with attempt:
                resp = await self._client.get(url, params=params)
                resp.raise_for_status()
        self._call_count_today += 1
        return resp.json().get("estimated_actuals", [])

    async def get_actuals_by_period_end(self) -> dict[datetime, float]:
        """Fetch satellite-derived actuals and return a
        `{period_end (UTC) → kW}` mapping.

        Built on top of `get_estimated_actuals`; same quota cost. Used by
        the nightly backfill to populate `pv_forecast_log.actual_kw` so
        analysts can compute `forecast − actual` or `actual − measured`
        (the second is the curtailment/waste signal).

        Returns `{}` on quota exhaustion or empty responses — callers
        should treat that as "no update this cycle", not an error.
        """
        raw = await self.get_estimated_actuals()
        out: dict[datetime, float] = {}
        for item in raw:
            period_end_str = item.get("period_end")
            if period_end_str is None:
                continue
            try:
                period_end = parse_iso(period_end_str)
                kw = float(item.get("pv_estimate", 0))
            except (TypeError, ValueError):
                continue
            out[period_end] = kw
        return out
