"""Tests for wall-clock-aligned wake loops — covers spec §9.13."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from optimiser.wake_loop import WakeLoop, next_aligned_wake

UTC = UTC


class TestNextAlignedWake:
    def test_60s_aligns_to_minute(self) -> None:
        now = datetime(2026, 4, 12, 10, 30, 42, 500_000, tzinfo=UTC)
        wake = next_aligned_wake(60, now)
        assert wake == datetime(2026, 4, 12, 10, 31, 0, tzinfo=UTC)

    def test_300s_aligns_to_5min(self) -> None:
        now = datetime(2026, 4, 12, 10, 32, 15, tzinfo=UTC)
        wake = next_aligned_wake(300, now)
        assert wake == datetime(2026, 4, 12, 10, 35, 0, tzinfo=UTC)

    def test_already_on_boundary_jumps_forward(self) -> None:
        # Exactly on boundary should still go to NEXT boundary
        now = datetime(2026, 4, 12, 10, 30, 0, tzinfo=UTC)
        wake = next_aligned_wake(60, now)
        assert wake == datetime(2026, 4, 12, 10, 31, 0, tzinfo=UTC)

    def test_1800s_aligns_to_30min(self) -> None:
        now = datetime(2026, 4, 12, 10, 17, 0, tzinfo=UTC)
        wake = next_aligned_wake(1800, now)
        assert wake == datetime(2026, 4, 12, 10, 30, 0, tzinfo=UTC)


class TestWakeLoopExecution:
    async def test_target_runs_and_loop_continues_on_exception(self) -> None:
        call_count = 0

        async def target() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first call fails")

        # Use a very short period and stop after a few iterations
        loop = WakeLoop("test", period_s=1, target=target)

        async def stop_after() -> None:
            await asyncio.sleep(2.5)
            loop.stop()

        await asyncio.gather(loop.run(), stop_after())

        # Should have called target at least twice despite the exception
        assert call_count >= 2

    async def test_overrun_skipped(self) -> None:
        executions = 0
        in_progress = 0
        max_concurrent = 0

        async def slow_target() -> None:
            nonlocal executions, in_progress, max_concurrent
            in_progress += 1
            max_concurrent = max(max_concurrent, in_progress)
            executions += 1
            await asyncio.sleep(2.5)  # Longer than period
            in_progress -= 1

        loop = WakeLoop("slow", period_s=1, target=slow_target)

        async def stop_after() -> None:
            await asyncio.sleep(4.0)
            loop.stop()

        await asyncio.gather(loop.run(), stop_after())

        # Even though wake fires every 1s, slow target prevents overlap
        assert max_concurrent == 1

    async def test_stop_terminates_loop(self) -> None:
        ran = False

        async def target() -> None:
            nonlocal ran
            ran = True

        loop = WakeLoop("stop_test", period_s=1, target=target)

        async def stop_quickly() -> None:
            await asyncio.sleep(0.1)
            loop.stop()

        # Should complete without hanging
        await asyncio.wait_for(
            asyncio.gather(loop.run(), stop_quickly()),
            timeout=5.0,
        )
