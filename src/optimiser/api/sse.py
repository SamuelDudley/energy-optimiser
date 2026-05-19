"""Snapshot fan-out for the dashboard's `/dashboard/stream` SSE endpoint.

The Service publishes each new TickSnapshot here; one in-memory bounded
queue per connected client drains it. Bounded + drop-oldest because a
slow client must never backpressure the tick loop, and a snapshot is
tick-fresh — if the client missed the previous one, the next one (60 s
later) carries everything that matters.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..types import TickSnapshot

logger = logging.getLogger(__name__)


class SnapshotBroadcaster:
    # Per-subscriber queue depth. Two slots is enough to absorb a slow
    # consumer that's mid-render when the next tick fires; beyond that
    # we'd rather drop than queue stale data.
    _QUEUE_MAXSIZE = 2

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[TickSnapshot]] = set()

    def subscribe(self) -> asyncio.Queue[TickSnapshot]:
        q: asyncio.Queue[TickSnapshot] = asyncio.Queue(maxsize=self._QUEUE_MAXSIZE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[TickSnapshot]) -> None:
        self._subscribers.discard(q)

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, snapshot: TickSnapshot) -> None:
        """Fan out to every subscriber. Non-blocking: on a full queue,
        drop the oldest entry and enqueue the new one — clients always
        see the freshest snapshot, never a stale backlog."""
        for q in self._subscribers:
            try:
                q.put_nowait(snapshot)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(snapshot)
                except asyncio.QueueFull:
                    # Lost the race against another publisher; the client
                    # will pick up the next tick. Don't log per-tick.
                    pass
