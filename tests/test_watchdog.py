"""Tests for the external dead-man watchdog.

Focus: the logic of _heartbeat_age_s and _trigger_fallback, plus the
staleness-detection behaviour. We mock pymodbus so no real Modbus is
touched, and drive file mtimes directly.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from optimiser.watchdog import (
    REG_REMOTE_EMS_ENABLE,
    _heartbeat_age_s,
    _trigger_fallback,
    run,
)


class TestHeartbeatAge:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _heartbeat_age_s(tmp_path / "nope") is None

    def test_fresh_file_returns_small_age(self, tmp_path: Path) -> None:
        p = tmp_path / "hb"
        p.touch()
        age = _heartbeat_age_s(p)
        assert age is not None
        assert age < 1.0

    def test_old_file_returns_real_age(self, tmp_path: Path) -> None:
        p = tmp_path / "hb"
        p.touch()
        # Rewrite mtime to 120s ago
        old_mtime = time.time() - 120
        import os
        os.utime(p, (old_mtime, old_mtime))
        age = _heartbeat_age_s(p)
        assert age is not None
        assert 119 < age < 121


class TestTriggerFallback:
    async def test_writes_40029_as_zero(self) -> None:
        client = MagicMock()
        result = MagicMock()
        result.isError.return_value = False
        client.write_register = AsyncMock(return_value=result)

        ok = await _trigger_fallback(client, slave_id=247)
        assert ok is True
        client.write_register.assert_awaited_once()
        kwargs = client.write_register.call_args.kwargs
        # The whole safety of this sidecar hinges on these three args.
        assert kwargs["address"] == REG_REMOTE_EMS_ENABLE
        assert kwargs["value"] == 0
        assert kwargs["device_id"] == 247

    async def test_returns_false_when_modbus_write_errors(self) -> None:
        client = MagicMock()
        result = MagicMock()
        result.isError.return_value = True
        client.write_register = AsyncMock(return_value=result)

        ok = await _trigger_fallback(client, slave_id=247)
        assert ok is False

    async def test_returns_false_when_modbus_write_raises(self) -> None:
        client = MagicMock()
        client.write_register = AsyncMock(side_effect=ConnectionError("refused"))

        ok = await _trigger_fallback(client, slave_id=247)
        assert ok is False


class TestRunLoop:
    """End-to-end of the poll loop with pymodbus and timing mocked."""

    async def _run_briefly(
        self,
        heartbeat_path: Path,
        *,
        stale_seconds: float = 60.0,
        poll_seconds: float = 0.01,
        grace_seconds: float = 0.0,
        iterations: int = 3,
    ) -> MagicMock:
        """Start run() and cancel after a few poll iterations. Returns the
        MagicMock patched in for AsyncModbusTcpClient so the caller can
        assert on its write_register calls.
        """
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.isError.return_value = False
        mock_client.write_register = AsyncMock(return_value=mock_result)

        from unittest.mock import patch

        with patch(
            "optimiser.watchdog.AsyncModbusTcpClient",
            return_value=mock_client,
        ):
            task = asyncio.create_task(
                run(
                    heartbeat_path=heartbeat_path,
                    sigenergy_host="127.0.0.1",
                    sigenergy_port=502,
                    slave_id=247,
                    stale_seconds=stale_seconds,
                    poll_seconds=poll_seconds,
                    grace_seconds=grace_seconds,
                )
            )
            # Let the loop run for a few iterations.
            await asyncio.sleep(poll_seconds * iterations + 0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return mock_client

    async def test_fresh_heartbeat_does_not_fire(self, tmp_path: Path) -> None:
        hb = tmp_path / "hb"
        hb.touch()
        client = await self._run_briefly(hb)
        client.write_register.assert_not_awaited()

    async def test_stale_heartbeat_fires_once(self, tmp_path: Path) -> None:
        hb = tmp_path / "hb"
        hb.touch()
        # Make it 10 minutes old
        old = time.time() - 600
        import os
        os.utime(hb, (old, old))

        client = await self._run_briefly(
            hb, stale_seconds=1.0, poll_seconds=0.01, iterations=5
        )
        # Must fire exactly once — repeat stale polls are suppressed until
        # the heartbeat recovers.
        assert client.write_register.await_count == 1

    async def test_recovery_re_arms(self, tmp_path: Path) -> None:
        """After firing, if the heartbeat comes back fresh, the watchdog
        re-arms so a second failure can fire again."""
        hb = tmp_path / "hb"
        hb.touch()
        import os

        # Start stale
        old = time.time() - 600
        os.utime(hb, (old, old))

        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.isError.return_value = False
        mock_client.write_register = AsyncMock(return_value=mock_result)

        from unittest.mock import patch

        with patch(
            "optimiser.watchdog.AsyncModbusTcpClient",
            return_value=mock_client,
        ):
            task = asyncio.create_task(
                run(
                    heartbeat_path=hb,
                    sigenergy_host="127.0.0.1",
                    sigenergy_port=502,
                    slave_id=247,
                    stale_seconds=1.0,
                    poll_seconds=0.02,
                    grace_seconds=0.0,
                )
            )
            await asyncio.sleep(0.06)  # fires once
            # Refresh heartbeat (simulate service recovery)
            hb.touch()
            await asyncio.sleep(0.06)  # watchdog re-arms
            # Go stale again
            os.utime(hb, (old, old))
            await asyncio.sleep(0.06)  # fires second time
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert mock_client.write_register.await_count == 2

    async def test_missing_file_within_grace_does_not_fire(
        self, tmp_path: Path
    ) -> None:
        hb = tmp_path / "nope"  # does not exist
        client = await self._run_briefly(
            hb,
            stale_seconds=1.0,
            poll_seconds=0.01,
            grace_seconds=10.0,  # generous grace
            iterations=3,
        )
        client.write_register.assert_not_awaited()

    async def test_missing_file_past_grace_fires(self, tmp_path: Path) -> None:
        hb = tmp_path / "nope"  # does not exist
        client = await self._run_briefly(
            hb,
            stale_seconds=1.0,
            poll_seconds=0.01,
            grace_seconds=0.0,  # no grace
            iterations=3,
        )
        # Fires when no heartbeat ever appears past grace window
        assert client.write_register.await_count >= 1


class TestServiceTouchesHeartbeat:
    """The main service side: verify _touch_heartbeat updates the file
    mtime and survives path failures without raising."""

    def test_touch_creates_and_updates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from optimiser.service import Service

        hb = tmp_path / "subdir" / "hb"
        monkeypatch.setenv("EO_HEARTBEAT_PATH", str(hb))

        svc = Service.__new__(Service)
        assert not hb.exists()
        svc._touch_heartbeat()
        assert hb.exists()

        first_mtime = hb.stat().st_mtime
        time.sleep(0.01)
        svc._touch_heartbeat()
        second_mtime = hb.stat().st_mtime
        assert second_mtime >= first_mtime

    def test_touch_swallows_permission_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from optimiser.service import Service

        # A path the test runner definitely can't write to. Service.touch
        # must log and continue — the watchdog itself will detect staleness.
        monkeypatch.setenv("EO_HEARTBEAT_PATH", "/proc/1/heartbeat")
        svc = Service.__new__(Service)
        # Must not raise
        svc._touch_heartbeat()
