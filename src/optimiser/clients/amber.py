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
from ..logging_utils import api_call, emit
from ..time_utils import now_utc, parse_iso
from ..types import AmberUsageRow, EventType, PriceForecastLogRow, PriceInterval
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
        # Wall-clock time at which the current rate-limit window is
        # expected to reset. Computed when we parse RateLimit-Reset so
        # we can answer "is the window still open?" without relying on
        # a raw second-count that doesn't tick down on its own.
        self._rate_window_resets_at: datetime | None = None
        # If Amber 429s us, we record when it's safe to try again and
        # skip outbound calls until then. Returning cached data is
        # preferable to hammering the API — retrying just re-triggers
        # the same 5-min bucket and extends the lockout.
        self._rate_limited_until: datetime | None = None
        self._warned_rate_low: bool = False
        # Rising-edge state for the 30-min horizon alert. Emit
        # AMBER_HORIZON_SHORT once when the count crosses below the
        # threshold; emit AMBER_HORIZON_RECOVERED on the way back up.
        # Re-arm only after the recovery edge fires so a single dip
        # doesn't generate a fetch-rate stream of warnings.
        self._horizon_short: bool = False

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

    def current_5min_price(self, t: datetime) -> PriceInterval | None:
        """Return the cached 5-min interval whose [start, end) contains `t`.

        Returns None if we have no cached prices yet, or if cached prices
        are stale enough that none cover `t`. Callers must treat None as
        "no current 5-min price available" and fall back to 30-min data
        or skip the price-conditional logic.

        Why time-based rather than `[0]`: Amber returns `previous + current
        + next`, so `[0]` is one of the previous intervals, not current.
        And after a poll failure, the cached list ages off the wall clock
        — `[0]` then points to a slot deep in the past. Time-based lookup
        is correct in both the fresh and the stale case.
        """
        if not self._last_5min_prices:
            return None
        for p in self._last_5min_prices:
            if p.start <= t < p.end:
                return p
        return None

    async def get_current_prices(self) -> list[PriceInterval]:
        """Fetch 30-min forecast prices (planning horizon)."""
        if self._should_defer_fetch():
            return self._last_prices or []
        prices, log_rows = await self._fetch(
            next_count=self._config.forecast_intervals_30min,
            previous=0,
            resolution=30,
        )
        self._last_prices = prices
        self._last_fetch = now_utc()
        self._last_log_rows_30min = log_rows
        logger.info("Fetched %d 30-min price intervals from Amber", len(prices))
        self._check_horizon_alert(len(prices))
        return prices

    def _check_horizon_alert(self, count: int) -> None:
        """Rising/falling-edge horizon-shrinkage alert.

        Amber's 30-min visible horizon usually sits near 79 intervals
        (~40 h, the AEMO pre-dispatch ceiling). It briefly dips to ~30
        during AEMO's daily refresh; once the new pre-dispatch publishes
        it climbs back to 79. The threshold (`horizon_alert_threshold_
        30min`, default 50) sits between the transient-dip floor and
        the operational ceiling so the daily refresh is silent but
        sustained shrinkage (Amber API change, plan/site mis-config,
        AEMO outage) generates exactly one rising-edge event.
        """
        threshold = self._config.horizon_alert_threshold_30min
        if threshold <= 0:
            return  # alert disabled
        if count < threshold and not self._horizon_short:
            self._horizon_short = True
            logger.warning(
                "Amber 30-min horizon shrunk to %d intervals (< %d threshold) "
                "— LP planning horizon will be capped",
                count,
                threshold,
            )
            emit(
                EventType.AMBER_HORIZON_SHORT,
                {"interval_count": count, "threshold": threshold},
            )
        elif count >= threshold and self._horizon_short:
            self._horizon_short = False
            logger.info(
                "Amber 30-min horizon recovered to %d intervals (>= %d threshold)",
                count,
                threshold,
            )
            emit(
                EventType.AMBER_HORIZON_RECOVERED,
                {"interval_count": count, "threshold": threshold},
            )

    async def get_5min_prices(self) -> list[PriceInterval]:
        """Fetch 5-min resolution prices for the immediate window.

        Returns ~14 intervals: 2 previous + current + ~12 forward.
        Used for acute decisions (spike, neg export, neg import).
        """
        if self._should_defer_fetch():
            return self._last_5min_prices or []
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

    def _should_defer_fetch(self) -> bool:
        """Skip this Amber call if we're in a post-429 cool-down, or if
        the last response's RateLimit-Remaining hit zero and the window
        hasn't rolled over yet. Callers fall back to cached data, which
        `prices_age` / `prices_5min_age` will still flag as stale once
        the cool-down runs long enough to matter to the state machine.
        """
        now = now_utc()
        if self._rate_limited_until is not None and now < self._rate_limited_until:
            return True
        if (
            self._rate_remaining is not None
            and self._rate_remaining <= 0
            and self._rate_window_resets_at is not None
            and now < self._rate_window_resets_at
        ):
            return True
        return False

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
        op = "prices_5min" if resolution == 5 else "prices_30min"

        try:
            async for attempt in amber_retry():
                with attempt, api_call("amber", op) as call:
                    call.extra["resolution"] = resolution
                    resp = await self._client.get(url, params=params)
                    call.set_response(resp)
                    # Rate-limit headroom is on every response (success
                    # and 4xx alike) — attaching it here keeps the
                    # signal on the API_CALL stream without a separate
                    # event. None when headers absent (test mocks).
                    if self._rate_remaining is not None:
                        call.extra["rl_remaining"] = self._rate_remaining
                    if self._rate_limit is not None:
                        call.extra["rl_limit"] = self._rate_limit
                    resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 429 is non-retryable (would re-trigger the same bucket).
            # Parse the rate-limit headers on the error response so we
            # can compute when it's safe to try again, then re-raise so
            # the caller's failure path runs.
            if exc.response is not None and exc.response.status_code == 429:
                self._update_rate_limits(exc.response)
                self._note_rate_limited(exc.response)
            raise

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

        # Amber's feedIn channel is signed from their ledger perspective —
        # negative = revenue to customer, positive = customer pays (solar
        # glut). The LP's internal convention is the customer's:
        # positive = revenue from export. We negate every feedIn-derived
        # value at this boundary so downstream code reads natural
        # "what you get paid per kWh exported" semantics. This applies
        # to feedIn.perKwh AND every field of feedIn.advancedPrice
        # (low/predicted/high). Easy to forget on the advancedPrice
        # branch — covered by `test_feedin_predicted_captured_and_sign_flipped`.
        def _neg(x: float | None) -> float | None:
            return -x if x is not None else None

        for key in sorted(general.keys()):
            gen = general[key]
            fi = feed_in.get(key, {})
            advanced = gen.get("advancedPrice") or {}
            fi_advanced = fi.get("advancedPrice") or {}
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
            export_per_kwh = -fi.get("perKwh", 0)
            export_forecast_predicted = _neg(fi_advanced.get("predicted"))
            export_forecast_low = _neg(fi_advanced.get("low"))
            export_forecast_high = _neg(fi_advanced.get("high"))
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
                    export_forecast_low=export_forecast_low,
                    export_forecast_high=export_forecast_high,
                    export_forecast_predicted=export_forecast_predicted,
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
                    export_forecast_predicted=export_forecast_predicted,
                    export_forecast_low=export_forecast_low,
                    export_forecast_high=export_forecast_high,
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
            if reset is not None:
                self._rate_reset_seconds = int(reset)
                # Project the seconds-from-now header onto a wall-clock
                # timestamp so stale values don't keep us deferred
                # forever — the raw header number doesn't tick down on
                # its own.
                self._rate_window_resets_at = now_utc() + timedelta(
                    seconds=max(self._rate_reset_seconds, 0)
                )
        except (ValueError, TypeError, AttributeError):
            # Header not present / unparseable / headers object not dict-like.
            return

        if self._rate_remaining is None:
            return

        if self._rate_remaining <= _RATE_LIMIT_WARNING_THRESHOLD:
            if not self._warned_rate_low:
                emit(
                    EventType.PRICE_STALE,
                    {
                        "message": "Amber rate limit low",
                        "remaining": self._rate_remaining,
                        "limit": self._rate_limit,
                        "reset_seconds": self._rate_reset_seconds,
                    },
                )
                logger.warning(
                    "Amber rate limit low: %s/%s remaining, resets in %ss",
                    self._rate_remaining,
                    self._rate_limit,
                    self._rate_reset_seconds,
                )
                self._warned_rate_low = True
        else:
            self._warned_rate_low = False

    def _note_rate_limited(self, resp: httpx.Response) -> None:
        """Record a 429 and the wall-clock time it's safe to retry.

        Prefers `Retry-After` (seconds) when present — it's the
        canonical RFC 7231 header and Amber honours it. Falls back to
        `RateLimit-Reset` from the rate-limit draft, and finally to a
        conservative 60 s when neither is parseable.
        """
        headers = getattr(resp, "headers", {}) or {}
        reset_s: int | None = None
        raw_retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
        if raw_retry_after is not None:
            try:
                reset_s = int(raw_retry_after)
            except (ValueError, TypeError):
                pass
        if reset_s is None and self._rate_reset_seconds is not None:
            reset_s = self._rate_reset_seconds
        if reset_s is None or reset_s <= 0:
            reset_s = 60

        self._rate_limited_until = now_utc() + timedelta(seconds=reset_s)
        emit(
            EventType.PRICE_STALE,
            {
                "message": "Amber 429 — deferring fetches",
                "defer_seconds": reset_s,
            },
        )
        logger.warning(
            "Amber returned 429 — skipping fetches for %ss (until %s)",
            reset_s,
            self._rate_limited_until.isoformat(),
        )

    async def get_usage(
        self,
        start_date: str,
        end_date: str,
    ) -> list[dict]:
        """Fetch historical usage data (max 7-day window)."""
        url = f"/sites/{self._config.site_id}/usage"
        params = {"startDate": start_date, "endDate": end_date}
        async for attempt in amber_retry():
            with attempt, api_call("amber", "usage") as call:
                resp = await self._client.get(url, params=params)
                call.set_response(resp)
                resp.raise_for_status()
        return resp.json()

    async def get_usage_intervals(
        self,
        start_date: str,
        end_date: str,
    ) -> list[AmberUsageRow]:
        """Fetch settled per-5-min usage intervals as typed rows.

        Both dates are NEM-day strings (YYYY-MM-DD) and inclusive. Amber
        publishes a day's intervals after the NEM day rolls over (NEM is
        UTC+10, so ~14:00 UTC); requesting today returns []. Window is
        capped at 7 days by Amber.

        Sign convention is preserved as Amber returns it: `cost_cents`
        and `per_kwh_cents` are positive on `general` (you paid) and
        negative on `feedIn` (you earned), so SUM(cost_cents) over a
        day is the net bill in cents.
        """
        raw = await self.get_usage(start_date, end_date)
        rows: list[AmberUsageRow] = []
        for d in raw:
            ts_raw = d.get("startTime")
            if not ts_raw:
                continue
            # Same NEM `+1s` boundary normalisation we apply to the price
            # parser (see `_fetch` decision-log entry): startTime comes
            # back as e.g. "14:00:01Z" and we want it on the wall-clock
            # boundary so downstream joins to telemetry/price logs line up.
            ts = parse_iso(ts_raw).replace(second=0, microsecond=0)
            rows.append(
                AmberUsageRow(
                    ts=ts,
                    nem_date=d.get("date", ""),
                    channel=d.get("channelType", "general"),
                    kwh=float(d.get("kwh", 0.0)),
                    cost_cents=float(d.get("cost", 0.0)),
                    per_kwh_cents=float(d.get("perKwh", 0.0)),
                    spot_per_kwh_cents=(
                        float(d["spotPerKwh"]) if d.get("spotPerKwh") is not None else None
                    ),
                    renewables_pct=(
                        float(d["renewables"]) if d.get("renewables") is not None else None
                    ),
                    descriptor=d.get("descriptor"),
                    spike_status=d.get("spikeStatus"),
                    quality=d.get("quality"),
                )
            )
        return rows
