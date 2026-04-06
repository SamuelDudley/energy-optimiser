"""Shared HTTP retry policy for external API clients.

Per-API rate limits and recommended retry behaviour are derived from
provider documentation and community knowledge:

- **Amber Electric**: 50 requests per 5-minute window, per account
  (https://github.com/amberelectric/public-api/discussions/146).
  Returns 429 with draft IETF rate-limit headers when exceeded.
  We never retry 429 — would just re-trigger the limit.
- **Solcast (hobbyist tier)**: 10 requests per day hard quota, AND
  transient 429 "too busy" responses under server load. We retry 5xx
  and network errors aggressively but only retry 429 once with a
  longer wait, since storms can last many minutes.
- **BOM (anonymous JSON feed)**: No documented limit, but 403 is
  returned when the user-agent looks bot-like or when the IP is
  rate-shedded. We retry 5xx/network only; 403 is "go away" and
  retrying just wastes budget.

All policies use exponential backoff to avoid thundering-herd retries.
"""

from __future__ import annotations

import logging

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


def _is_retryable_5xx_or_network(exc: BaseException) -> bool:
    """Retry on transient network errors and 5xx server errors only."""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


def _log_retry(state: RetryCallState) -> None:
    """Log each retry attempt for visibility."""
    if state.outcome and state.outcome.failed:
        exc = state.outcome.exception()
        logger.warning(
            "HTTP retry %d/%d after %s: %s",
            state.attempt_number,
            state.retry_object.stop.max_attempt_number,
            type(exc).__name__,
            exc,
        )


# ── Per-API policies ─────────────────────────────────────────────


def amber_retry() -> AsyncRetrying:
    """Amber: 3 attempts, exponential backoff 2/4/8 seconds.
    Never retries 429 (would re-trigger the rate limit).
    """
    return AsyncRetrying(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=8),
        retry=retry_if_exception(_is_retryable_5xx_or_network),
        reraise=True,
        before_sleep=_log_retry,
    )


def solcast_retry() -> AsyncRetrying:
    """Solcast: 3 attempts, exponential backoff 2/4/8 seconds.

    Retries only 5xx and network errors. 429 is **not** retried because
    the hobbyist tier has a hard 10-calls-per-day quota and we cannot
    distinguish a quota-exhaustion 429 from a transient "too busy" 429
    by response alone. Retrying risks burning further quota. The next
    scheduled poll (~2.4 h later) is the correct fallback — quota
    resets at midnight UTC and load-shedding storms typically clear
    within a poll cycle.

    The client also tracks its own per-day call count and pre-flights
    to avoid spending quota at the boundary (see `SolcastClient`).
    """
    return AsyncRetrying(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=8),
        retry=retry_if_exception(_is_retryable_5xx_or_network),
        reraise=True,
        before_sleep=_log_retry,
    )


def bom_retry() -> AsyncRetrying:
    """BOM: 3 attempts, exponential backoff 2/4/8 seconds.
    Never retries 403 (anti-bot block — retrying makes it worse).
    """
    return AsyncRetrying(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=8),
        retry=retry_if_exception(_is_retryable_5xx_or_network),
        reraise=True,
        before_sleep=_log_retry,
    )


# A polite UA helps the BOM not block us as a generic scraper.
DEFAULT_USER_AGENT = "energy-optimiser/0.1 (+https://github.com/Banksian/energy-optimiser)"
