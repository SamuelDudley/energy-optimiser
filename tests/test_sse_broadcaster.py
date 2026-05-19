"""Unit tests for SnapshotBroadcaster — the fan-out behind /dashboard/stream.

The broadcaster lives in the tick loop's hot path: a slow client must
never backpressure publish(), and lost snapshots are preferable to
queued-stale ones. These tests pin both invariants.
"""

from __future__ import annotations

import asyncio

import pytest

from optimiser.api.sse import SnapshotBroadcaster


@pytest.mark.asyncio
async def test_subscribe_publish_receive():
    bc = SnapshotBroadcaster()
    q = bc.subscribe()
    bc.publish("snap-a")
    assert await asyncio.wait_for(q.get(), timeout=0.1) == "snap-a"


@pytest.mark.asyncio
async def test_publish_fans_out_to_all_subscribers():
    bc = SnapshotBroadcaster()
    q1 = bc.subscribe()
    q2 = bc.subscribe()
    bc.publish("snap-a")
    assert await asyncio.wait_for(q1.get(), timeout=0.1) == "snap-a"
    assert await asyncio.wait_for(q2.get(), timeout=0.1) == "snap-a"


@pytest.mark.asyncio
async def test_overflow_drops_oldest_keeps_newest():
    """A slow consumer must not block publish() and must always end up
    with the freshest snapshot once it drains. Queue cap is 2."""
    bc = SnapshotBroadcaster()
    q = bc.subscribe()
    for i in range(5):
        bc.publish(f"snap-{i}")
    # Queue holds at most 2 items — the freshest two.
    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    assert drained[-1] == "snap-4", drained
    assert len(drained) == 2


@pytest.mark.asyncio
async def test_publish_is_nonblocking_under_overflow():
    bc = SnapshotBroadcaster()
    bc.subscribe()
    # publish() must not raise nor block, even if every subscriber's
    # queue is already full. Run a tight loop and assert it returns
    # synchronously each time.
    for i in range(1000):
        bc.publish(i)


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery():
    bc = SnapshotBroadcaster()
    q = bc.subscribe()
    bc.publish("a")
    assert await asyncio.wait_for(q.get(), timeout=0.1) == "a"
    bc.unsubscribe(q)
    assert bc.subscriber_count() == 0
    bc.publish("b")
    # Queue should be empty — unsubscribe pulled us out of the fan-out.
    assert q.empty()


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_a_noop():
    bc = SnapshotBroadcaster()
    bc.publish("orphan")  # must not raise
    assert bc.subscriber_count() == 0
