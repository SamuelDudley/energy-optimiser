"""Shared mutable state between the LP planning tick and the verification watcher.

Both the tick loop and the watcher loop read/write this object. The tick loop
records what was commanded; the watcher loop reads the commanded value, polls
the inverter, and may trigger fallback. The watcher must not race with a
mid-write tick (commanded would be stale relative to inverter state) — hence
the asyncio.Lock.

Circuit breaker pattern: `latched=True` means we've fallen back and are
waiting in cooldown before probing. Returns to LP control after N consecutive
clean verifications.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum, auto

from .dispatch import LPDispatch


class FallbackReason(StrEnum):
    LP_TIMEOUT = auto()
    LP_INFEASIBLE = auto()
    LP_ERROR = auto()
    LP_BUILD_FAILED = auto()
    VERIFY_DEVIATION = auto()
    SOLVER_UNAVAILABLE = auto()
    NONE = auto()


@dataclass
class CommandedState:
    """What the LP last commanded the inverter to do.

    Stores the full dispatch (mode + cap + signed intent) so the watcher
    can verify direction-and-cap without re-deriving from raw register
    state. Reset to None when not in LP control (during latch). The
    watcher only verifies when this is populated AND
    `now − write_timestamp > grace`.
    """

    dispatch: LPDispatch
    write_timestamp: datetime  # UTC — when we sent the Modbus write


@dataclass
class CircuitBreaker:
    """Latched-with-probe state machine for verification failures.

    Lifecycle:
      OK → (verify deviation × N) → LATCHED
      LATCHED → (cooldown elapses) → PROBING
      PROBING → (clean verify × M) → OK
      PROBING → (verify deviation) → LATCHED (with extended cooldown)
    """

    latched: bool = False
    latched_at: datetime | None = None
    cooldown: timedelta = timedelta(minutes=5)
    consecutive_deviations: int = 0
    consecutive_clean_probes: int = 0
    deviation_threshold_count: int = 3  # 3 × 10s polls = 30s
    clean_probe_threshold: int = 3  # need 3 clean verifications to clear
    last_fallback_reason: FallbackReason = FallbackReason.NONE

    def is_in_cooldown(self, now: datetime) -> bool:
        if not self.latched or self.latched_at is None:
            return False
        return (now - self.latched_at) < self.cooldown

    def can_probe(self, now: datetime) -> bool:
        """True when we're latched but cooldown has elapsed — try LP again."""
        return self.latched and not self.is_in_cooldown(now)


@dataclass
class LPRuntime:
    """Shared mutable state. Always acquire `lock` before reading or writing."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    commanded: CommandedState | None = None
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)

    # Verification thresholds (per agreed design):
    grace_period: timedelta = timedelta(seconds=30)
    deviation_floor_kw: float = 0.3  # 300W — see dispatch.verify_battery_response

    async def record_command(self, dispatch: LPDispatch) -> None:
        """Tick loop calls this after a successful Modbus write.

        Resets `consecutive_deviations` because deviations accumulated
        against the previous dispatch don't necessarily reflect the
        inverter's response to this new one — e.g. two "wrong direction"
        polls under a CHARGE command are meaningless if we've now
        commanded DISCHARGE. The grace period combined with this reset
        means every dispatch gets a fresh verification window.

        `consecutive_clean_probes` is deliberately NOT reset: during a
        probe (breaker latched, re-attempting LP control), we need
        clean verifications to accumulate across multiple ticks to clear
        the latch, even if the specific dispatch changes each tick.
        """
        async with self.lock:
            self.commanded = CommandedState(
                dispatch=dispatch,
                write_timestamp=datetime.now(UTC),
            )
            self.breaker.consecutive_deviations = 0

    async def latch(self, reason: FallbackReason) -> None:
        """Enter latched state. Clears the commanded value so the watcher idles."""
        async with self.lock:
            self.breaker.latched = True
            self.breaker.latched_at = datetime.now(UTC)
            self.breaker.last_fallback_reason = reason
            self.breaker.consecutive_deviations = 0
            self.breaker.consecutive_clean_probes = 0
            self.commanded = None  # don't verify while latched

    async def clear_latch(self) -> None:
        """Probe succeeded N times in a row — return to normal LP control."""
        async with self.lock:
            self.breaker.latched = False
            self.breaker.latched_at = None
            self.breaker.consecutive_clean_probes = 0
            self.breaker.last_fallback_reason = FallbackReason.NONE
