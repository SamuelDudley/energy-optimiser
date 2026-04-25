"""Wall-clock-aligned async wake loops.

Replaces sleep-based loops which drift over time. Each wake loop fires
at exact UTC second boundaries (e.g. every minute on :00, every 5 min
on :00/:05/:10) regardless of how long the previous target took.

Multiple independent wake loops run concurrently via asyncio.gather().
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from .logging_utils import emit
from .types import EventType

logger = logging.getLogger(__name__)

UTC = UTC


def next_aligned_wake(
    period_s: int,
    now: datetime | None = None,
    *,
    offset_s: int = 0,
) -> datetime:
    """Compute the next wake time aligned to UTC second boundaries.

    For period_s=60: returns the next minute boundary.
    For period_s=300: returns the next 5-min boundary on the UTC clock.

    `offset_s` shifts every fire by N seconds relative to the natural
    boundary. period_s=300, offset_s=150 fires at :02:30/:07:30/:12:30…
    UTC — useful for landing mid-slot when the data source publishes at
    slot boundaries (e.g. Amber 5-min prices) and we want a few seconds
    of settle before reading.
    """
    if now is None:
        now = datetime.now(UTC)
    epoch_s = int(now.timestamp())
    k = ((epoch_s - offset_s) // period_s) + 1
    next_s = offset_s + k * period_s
    return datetime.fromtimestamp(next_s, UTC)


class WakeLoop:
    """A single wall-clock-aligned wake loop.

    Properties:
    - Aligned to UTC second boundaries (no drift)
    - Independent (slow target doesn't delay other loops)
    - Overrun-safe (skips if target still running)
    - Exception-safe (logs and continues)
    """

    def __init__(
        self,
        name: str,
        period_s: int,
        target: Callable[[], Awaitable[None]],
        *,
        offset_s: int = 0,
    ) -> None:
        self._name = name
        self._period_s = period_s
        self._offset_s = offset_s
        self._target = target
        self._running = False
        self._task_in_flight = False
        self._tasks: set[asyncio.Task] = set()

    @property
    def name(self) -> str:
        return self._name

    async def run(self) -> None:
        """Run the wake loop until stopped. Awaitable forever."""
        self._running = True
        logger.info(
            "Wake loop '%s' started (period=%ds, offset=%ds)",
            self._name, self._period_s, self._offset_s,
        )

        while self._running:
            next_wake = next_aligned_wake(self._period_s, offset_s=self._offset_s)
            delay = (next_wake - datetime.now(UTC)).total_seconds()
            if delay > 0:
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    break

            if not self._running:
                break

            if self._task_in_flight:
                logger.warning(
                    "Wake loop '%s': skipping — previous still running",
                    self._name,
                )
                emit(
                    EventType.TICK_OVERRUN,
                    {
                        "loop": self._name,
                        "scheduled_at": next_wake.isoformat(),
                    },
                )
                continue

            task = asyncio.create_task(self._wrapped(next_wake))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        # Cancel any in-flight tasks on shutdown
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        logger.info("Wake loop '%s' stopped", self._name)

    async def _wrapped(self, scheduled_at: datetime) -> None:
        """Wrap target execution to track in-flight state and catch exceptions."""
        self._task_in_flight = True
        try:
            await self._target()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Wake loop '%s' target failed (scheduled %s)",
                self._name,
                scheduled_at.isoformat(),
            )
        finally:
            self._task_in_flight = False

    def stop(self) -> None:
        """Signal the loop to stop after the current iteration."""
        self._running = False
