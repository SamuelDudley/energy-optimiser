"""Verification watcher: polls the inverter's actual battery power and
checks it matches what the LP commanded.

Runs as a separate `WakeLoop` at 10s cadence, independent of the 60s tick
loop. The watcher is the only thing that ever clears a latched breaker —
the tick loop's "probe" path just re-arms LP control and waits for the
watcher to confirm the inverter is following us cleanly.

Design points:
  - **Lock discipline**: hold `_lp_runtime.lock` only for short reads/writes
    of state. Modbus I/O and fallback calls happen outside the lock so
    they don't block the tick loop.
  - **Grace period**: skip verification for the first 30s after a write.
    The inverter ramps battery power over a few seconds; checking too soon
    produces false positives.
  - **Latched-but-no-command**: if the breaker is latched and there's no
    `commanded` (the normal latched state), the watcher idles. It only
    becomes active again after the tick loop probes (records a command)
    post-cooldown.
  - **Sub-cap is OK**: `verify_battery_response` returns OK when the
    inverter draws less than the cap. Only WRONG_DIRECTION and OVER_CAP
    increment the deviation counter.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ..clients.shelly import ShellyLoadController
from ..clients.sigenergy import SigenergyController
from ..logging_utils import emit
from ..types import EventType
from .dispatch import DeviationKind, verify_battery_response
from .fallback import trigger_fallback
from .runtime import FallbackReason, LPRuntime

logger = logging.getLogger(__name__)


# Default poll cadence. Combined with `deviation_threshold_count=3` in the
# breaker, a sustained deviation triggers fallback in ~30s — fast enough
# to limit damage from a misbehaving inverter without being so jittery
# that one transient measurement noise spike causes a fallback.
WATCHER_PERIOD_S: int = 10


class VerificationWatcher:
    """Polls battery power and verifies against the commanded dispatch.

    Stateless across calls — all state lives in `LPRuntime` so the tick
    loop and watcher share a single source of truth (no parallel counters
    that could drift).
    """

    def __init__(
        self,
        runtime: LPRuntime,
        sigenergy: SigenergyController,
        shelly_controllers: list[ShellyLoadController],
    ) -> None:
        self._runtime = runtime
        self._sigenergy = sigenergy
        self._shelly_controllers = shelly_controllers

    async def poll(self) -> None:
        """Single watcher iteration. Wired into a `WakeLoop` for periodic
        execution; safe to call directly in tests."""
        # 1. Snapshot the runtime state under the lock. We don't hold the
        # lock through the I/O — that would gate the tick loop on Modbus
        # latency. Worst case: the snapshot is stale by the time we use
        # it, which means we either skip a verification (commanded was
        # set after we snapshotted) or verify against a fresh-but-stale
        # dispatch. Both outcomes are safer than holding the lock.
        async with self._runtime.lock:
            commanded = self._runtime.commanded
            breaker_latched = self._runtime.breaker.latched

        # 2. Decide whether to verify at all.
        if commanded is None:
            # No active command — either we've never commanded, or the
            # breaker is latched and waiting to be probed.
            return

        now = datetime.now(UTC)
        if (now - commanded.write_timestamp) < self._runtime.grace_period:
            # Too soon — the inverter is still ramping to the new setpoint.
            return

        # 3. Read actual battery power. Failed reads don't trigger fallback
        # on their own; they're typically transient Modbus glitches and
        # will resolve on the next poll. The state machine handles
        # sustained Modbus failure separately.
        measured = await self._sigenergy.read_battery_power_kw()
        if measured is None:
            return

        # 4. Verify and dispatch the result.
        outcome = verify_battery_response(
            commanded.dispatch,
            measured_kw=measured,
            deviation_floor_kw=self._runtime.deviation_floor_kw,
        )

        if outcome == DeviationKind.NOT_VERIFIED:
            # SELF_CONSUME mode — no assertion to check.
            return

        if outcome == DeviationKind.OK:
            await self._on_clean_verification(breaker_latched)
            return

        # WRONG_DIRECTION or OVER_CAP
        await self._on_deviation(
            outcome=outcome,
            commanded_kw=commanded.dispatch.signed_intent_kw,
            measured_kw=measured,
        )

    # ── Outcome handlers ─────────────────────────────────────────

    async def _on_clean_verification(self, breaker_latched: bool) -> None:
        """The inverter is following us. Reset the deviation counter; if
        we're in a probe window (latched but actively re-commanding),
        increment the clean-probe counter and clear the latch when we hit
        the threshold."""
        async with self._runtime.lock:
            breaker = self._runtime.breaker
            breaker.consecutive_deviations = 0

            if not breaker_latched:
                # Normal operation — nothing more to do.
                return

            # Probe-active path: count this clean verification toward
            # clearing. Once we have N in a row, drop the latch.
            breaker.consecutive_clean_probes += 1
            if breaker.consecutive_clean_probes >= breaker.clean_probe_threshold:
                # Inline clear (we're already holding the lock; can't call
                # `runtime.clear_latch()` because it would try to re-acquire).
                breaker.latched = False
                breaker.latched_at = None
                breaker.consecutive_clean_probes = 0
                last_reason = breaker.last_fallback_reason
                breaker.last_fallback_reason = FallbackReason.NONE
                emit(
                    EventType.BREAKER_CLEARED,
                    {
                        "previous_reason": last_reason.value,
                    },
                )
                logger.info(
                    "Breaker cleared after %d clean probe verifications",
                    breaker.clean_probe_threshold,
                )

    async def _on_deviation(
        self,
        *,
        outcome: DeviationKind,
        commanded_kw: float,
        measured_kw: float,
    ) -> None:
        """Increment the deviation counter; if we hit the threshold,
        trigger fallback and latch the breaker.

        We always emit `VERIFY_DEVIATION` for visibility, even on a single
        deviation that won't trigger fallback — operators want to see early
        signs of inverter misbehaviour before it escalates."""
        async with self._runtime.lock:
            breaker = self._runtime.breaker
            breaker.consecutive_deviations += 1
            count = breaker.consecutive_deviations
            threshold = breaker.deviation_threshold_count
            should_trigger = count >= threshold and not breaker.latched
            breaker.consecutive_clean_probes = 0  # any deviation resets probe progress

        emit(
            EventType.VERIFY_DEVIATION,
            {
                "outcome": outcome.value,
                "commanded_kw": commanded_kw,
                "measured_kw": measured_kw,
                "consecutive": count,
                "threshold": threshold,
            },
        )

        if not should_trigger:
            return

        logger.warning(
            "Verification deviation threshold reached (%d × %s) — falling back",
            count,
            outcome.value,
        )
        # Fallback I/O outside the lock. The latch acquires the lock
        # internally, which is fine since we've already released.
        await trigger_fallback(
            self._sigenergy,
            self._shelly_controllers,
            FallbackReason.VERIFY_DEVIATION,
            commanded_kw=commanded_kw,
            measured_kw=measured_kw,
            # Watcher fires because the inverter is not tracking commands.
            # Until we regain confidence in control, curtail all export —
            # the priority is stopping unintended grid flows, not revenue.
            block_export=True,
            extra_context={"outcome": outcome.value},
        )
        await self._runtime.latch(FallbackReason.VERIFY_DEVIATION)
        emit(
            EventType.BREAKER_LATCHED,
            {
                "reason": FallbackReason.VERIFY_DEVIATION.value,
                "outcome": outcome.value,
            },
        )
