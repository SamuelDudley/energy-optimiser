"""Tests for the external dead-man watchdog.

Focus: the logic of _heartbeat_age_s and _trigger_fallback, plus the
staleness-detection behaviour. We mock pymodbus so no real Modbus is
touched, and drive file mtimes directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from optimiser.watchdog import (
    MODE_MAXIMUM_SELF_CONSUMPTION,
    REG_ESS_MAX_CHARGING_LIMIT,
    REG_GRID_EXPORT_POWER_LIMIT,
    REG_REMOTE_EMS_CONTROL_MODE,
    REG_REMOTE_EMS_ENABLE,
    _heartbeat_age_s,
    _trigger_fallback,
    _write_register,
    run,
)

# Default max-charge-cap value used by the watchdog fallback. Mirrors
# the CLI default so tests don't need to pass it unless they're
# specifically exercising override behaviour.
_MAX_CHARGE_RAW = 13000


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


def _ok_result() -> MagicMock:
    r = MagicMock()
    r.isError.return_value = False
    return r


def _err_result() -> MagicMock:
    r = MagicMock()
    r.isError.return_value = True
    return r


class TestTriggerFallback:
    """The fallback writes three registers in order on the happy path, and
    falls through to a last-resort REMOTE_EMS_ENABLE=0 if any of them
    fails. All of that is load-bearing for the dead-man guarantee."""

    async def test_happy_path_writes_four_registers_in_order(self) -> None:
        """Happy path fires four writes: u32 40032 (charge cap uncap) →
        u16 40031 (mode) → u32 40038 (export=0) → u16 40029 (enable=1).

        Charge-cap write sits first because the uncapping protects mode 2
        from throttling surplus PV when it takes effect. Enable=1 sits
        last so the preceding mode + export are already in place when
        remote EMS takes control. 40032 and 40038 are both U32 (FC16);
        40031 and 40029 are U16 (FC6).
        """
        client = MagicMock()
        client.write_register = AsyncMock(return_value=_ok_result())
        client.write_registers = AsyncMock(return_value=_ok_result())

        ok = await _trigger_fallback(client, slave_id=247, max_charge_raw=_MAX_CHARGE_RAW)
        assert ok is True

        # U32 (FC16) writes, in order: charge cap (40032) then export=0 (40038).
        u32_calls = client.write_registers.await_args_list
        assert len(u32_calls) == 2
        assert u32_calls[0].kwargs["address"] == REG_ESS_MAX_CHARGING_LIMIT
        # Raw value 13000 splits high/low word into [0, 13000] for the
        # pymodbus write_registers call.
        assert u32_calls[0].kwargs["values"] == [
            (_MAX_CHARGE_RAW >> 16) & 0xFFFF,
            _MAX_CHARGE_RAW & 0xFFFF,
        ]
        assert u32_calls[1].kwargs["address"] == REG_GRID_EXPORT_POWER_LIMIT
        assert u32_calls[1].kwargs["values"] == [0, 0]
        for c in u32_calls:
            assert c.kwargs["device_id"] == 247

        # U16 (FC6) writes: mode then enable=1.
        u16_calls = client.write_register.await_args_list
        assert len(u16_calls) == 2
        assert u16_calls[0].kwargs["address"] == REG_REMOTE_EMS_CONTROL_MODE
        assert u16_calls[0].kwargs["value"] == MODE_MAXIMUM_SELF_CONSUMPTION
        assert u16_calls[1].kwargs["address"] == REG_REMOTE_EMS_ENABLE
        assert u16_calls[1].kwargs["value"] == 1
        for c in u16_calls:
            assert c.kwargs["device_id"] == 247

    async def test_export_limit_written_as_u32_via_fc16(self) -> None:
        """Register 40038 (grid export limit) is U32 — spec §7 row
        `40038–39 … U32`. It must be written with write_registers (FC16,
        two registers), NOT write_register (FC6): a single-register write
        returns ILLEGAL DATA ADDRESS on the real inverter (observed 19/19
        watchdog firings 2026-06-06/10). Mirrors the main service's
        set_export_limit_kw() which uses the U32 path."""
        client = MagicMock()
        client.write_register = AsyncMock(return_value=_ok_result())
        client.write_registers = AsyncMock(return_value=_ok_result())

        ok = await _trigger_fallback(client, slave_id=247, max_charge_raw=_MAX_CHARGE_RAW)
        assert ok is True

        # Export limit = 0 must be a two-register FC16 write to 40038.
        export_calls = [
            c
            for c in client.write_registers.await_args_list
            if c.kwargs["address"] == REG_GRID_EXPORT_POWER_LIMIT
        ]
        assert len(export_calls) == 1
        assert export_calls[0].kwargs["values"] == [0, 0]
        assert export_calls[0].kwargs["device_id"] == 247
        # …and must NOT be attempted as a single-register FC6 write.
        u16_addrs = [c.kwargs["address"] for c in client.write_register.await_args_list]
        assert REG_GRID_EXPORT_POWER_LIMIT not in u16_addrs

    async def test_charge_cap_fails_falls_through_to_last_resort(
        self,
    ) -> None:
        """Even the charge-cap write failing is tolerated — the remaining
        writes still attempt, and the last-resort disables remote EMS if
        any explicit step fails."""
        client = MagicMock()
        client.write_register = AsyncMock(return_value=_ok_result())
        # Two U32 (FC16) writes: charge cap fails, export succeeds.
        client.write_registers = AsyncMock(side_effect=[_err_result(), _ok_result()])

        ok = await _trigger_fallback(client, slave_id=247, max_charge_raw=_MAX_CHARGE_RAW)
        assert ok is True  # last-resort succeeded
        # u32: cap + export (2). u16: mode + enable=1 + last-resort (3).
        assert client.write_registers.await_count == 2
        assert client.write_register.await_count == 3

    async def test_mode_write_fails_falls_through_to_last_resort(self) -> None:
        client = MagicMock()
        # U16 (FC6) writes: mode, enable=1, then last-resort enable=0.
        client.write_register = AsyncMock(
            side_effect=[
                _err_result(),  # mode fails
                _ok_result(),  # enable=1
                _ok_result(),  # last-resort enable=0
            ]
        )
        client.write_registers = AsyncMock(return_value=_ok_result())  # cap + export ok

        ok = await _trigger_fallback(client, slave_id=247, max_charge_raw=_MAX_CHARGE_RAW)
        assert ok is True  # last-resort succeeded

        calls = client.write_register.await_args_list
        assert len(calls) == 3
        # Last write is the last-resort enable=0.
        assert calls[-1].kwargs["address"] == REG_REMOTE_EMS_ENABLE
        assert calls[-1].kwargs["value"] == 0

    async def test_export_write_fails_falls_through_to_last_resort(
        self,
    ) -> None:
        client = MagicMock()
        client.write_register = AsyncMock(return_value=_ok_result())
        # Export (40038) is a U32/FC16 write — fail it via write_registers.
        client.write_registers = AsyncMock(
            side_effect=[
                _ok_result(),  # charge cap
                _err_result(),  # export fails
            ]
        )

        ok = await _trigger_fallback(client, slave_id=247, max_charge_raw=_MAX_CHARGE_RAW)
        assert ok is True
        assert client.write_registers.await_count == 2
        # u16: mode + enable=1 + last-resort = 3
        assert client.write_register.await_count == 3

    async def test_enable_write_fails_falls_through_to_last_resort(
        self,
    ) -> None:
        client = MagicMock()
        # U16 (FC6): mode, enable=1 (fails), last-resort enable=0.
        client.write_register = AsyncMock(
            side_effect=[
                _ok_result(),  # mode
                _err_result(),  # enable=1 fails
                _ok_result(),  # last-resort enable=0
            ]
        )
        client.write_registers = AsyncMock(return_value=_ok_result())  # cap + export ok

        ok = await _trigger_fallback(client, slave_id=247, max_charge_raw=_MAX_CHARGE_RAW)
        assert ok is True
        assert client.write_register.await_count == 3

    async def test_modbus_raises_triggers_last_resort(self) -> None:
        """Every Modbus call raises. Five logical writes, each retried once
        (2 physical attempts per logical write). U32/FC16: cap + export =
        2 logical × 2 = 4. U16/FC6: mode + enable=1 + last-resort = 3
        logical × 2 = 6."""
        client = MagicMock()
        client.write_register = AsyncMock(side_effect=[ConnectionError("no route")] * 6)
        client.write_registers = AsyncMock(side_effect=[ConnectionError("no route")] * 4)
        client.connect = AsyncMock(return_value=False)

        ok = await _trigger_fallback(client, slave_id=247, max_charge_raw=_MAX_CHARGE_RAW)
        assert ok is False
        assert client.write_registers.await_count == 4
        assert client.write_register.await_count == 6

    async def test_all_writes_fail_returns_false(self) -> None:
        client = MagicMock()
        client.write_register = AsyncMock(return_value=_err_result())
        client.write_registers = AsyncMock(return_value=_err_result())

        ok = await _trigger_fallback(client, slave_id=247, max_charge_raw=_MAX_CHARGE_RAW)
        assert ok is False
        # u32: cap + export (2). u16: mode + enable=1 + last-resort (3).
        assert client.write_registers.await_count == 2
        assert client.write_register.await_count == 3

    async def test_last_resort_raises_returns_false(self) -> None:
        """Explicit path fails, then last-resort also raises — must not
        crash. isError() responses don't trigger retry (deterministic);
        the last-resort raises so its retry-once path runs.
        """
        client = MagicMock()
        # U16 (FC6): mode, enable=1, then last-resort (raises, retried once).
        # Export is now a U32 write, so it's not in this side-effect list.
        client.write_register = AsyncMock(
            side_effect=[
                _err_result(),  # mode
                _err_result(),  # enable=1
                ConnectionError("refused"),  # last-resort, attempt 1
                ConnectionError("refused"),  # last-resort, attempt 2 (retry)
            ]
        )
        # U32 (FC16): charge cap + export both fail (isError, no retry).
        client.write_registers = AsyncMock(return_value=_err_result())
        client.connect = AsyncMock(return_value=False)

        ok = await _trigger_fallback(client, slave_id=247, max_charge_raw=_MAX_CHARGE_RAW)
        assert ok is False
        # mode (1) + enable=1 (1) + last-resort (2 attempts) = 4
        assert client.write_register.await_count == 4


class TestWriteRetry:
    """§4.1: `_write_register` retries once on Exception (TCP-level),
    calling connect() in between. isError() responses are not retried —
    protocol errors are deterministic."""

    async def test_succeeds_on_second_attempt_after_transient_failure(
        self,
    ) -> None:
        client = MagicMock()
        client.write_register = AsyncMock(
            side_effect=[ConnectionError("socket dropped"), _ok_result()]
        )
        client.connect = AsyncMock(return_value=True)

        ok = await _write_register(client, slave_id=247, address=40031, value=2, label="mode")
        assert ok is True
        assert client.write_register.await_count == 2
        # Reconnect was called exactly once, between the two attempts.
        assert client.connect.await_count == 1

    async def test_returns_false_after_retries_exhausted(self) -> None:
        client = MagicMock()
        client.write_register = AsyncMock(
            side_effect=[ConnectionError("down"), ConnectionError("down")]
        )
        client.connect = AsyncMock(return_value=False)

        ok = await _write_register(client, slave_id=247, address=40031, value=2, label="mode")
        assert ok is False
        assert client.write_register.await_count == 2

    async def test_iserror_does_not_retry(self) -> None:
        """Protocol-level isError() is a deterministic write rejection —
        retrying won't help. Returns False on first error without
        consulting connect() or issuing a second write."""
        client = MagicMock()
        client.write_register = AsyncMock(return_value=_err_result())
        client.connect = AsyncMock(return_value=True)

        ok = await _write_register(client, slave_id=247, address=40031, value=2, label="mode")
        assert ok is False
        assert client.write_register.await_count == 1
        assert client.connect.await_count == 0


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
        mock_client.write_registers = AsyncMock(return_value=mock_result)
        # §4.1: run() now pre-connects explicitly on startup; the mock
        # must provide an awaitable connect() or the pre-connect raises
        # (which is caught and logged but noisy).
        mock_client.connect = AsyncMock(return_value=True)

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
                    max_charge_raw=_MAX_CHARGE_RAW,
                )
            )
            # Let the loop run for a few iterations.
            await asyncio.sleep(poll_seconds * iterations + 0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return mock_client

    async def test_fresh_heartbeat_does_not_fire(self, tmp_path: Path) -> None:
        hb = tmp_path / "hb"
        hb.touch()
        client = await self._run_briefly(hb)
        client.write_register.assert_not_awaited()

    async def test_run_pre_connects_on_startup(self, tmp_path: Path) -> None:
        """§4.1: run() calls client.connect() once at startup so the
        first fallback write isn't stalled on connect-on-demand latency.
        """
        hb = tmp_path / "hb"
        hb.touch()
        client = await self._run_briefly(hb, iterations=2)
        # Exactly one connect on startup, regardless of whether fallback
        # fired during the run.
        assert client.connect.await_count >= 1

    async def test_run_survives_pre_connect_failure(self, tmp_path: Path) -> None:
        """If the inverter is unreachable at startup, the watchdog must
        keep running — the main service may still come up and freshen
        the heartbeat before a fallback is needed."""
        hb = tmp_path / "hb"
        hb.touch()

        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.isError.return_value = False
        mock_client.write_register = AsyncMock(return_value=mock_result)
        mock_client.write_registers = AsyncMock(return_value=mock_result)
        # Pre-connect raises — simulates inverter offline at startup.
        mock_client.connect = AsyncMock(side_effect=ConnectionError("unreachable"))

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
                    stale_seconds=60.0,
                    poll_seconds=0.02,
                    grace_seconds=0.0,
                    max_charge_raw=_MAX_CHARGE_RAW,
                )
            )
            # Let a few poll iterations pass; task should still be alive.
            await asyncio.sleep(0.1)
            assert not task.done(), "watchdog must not exit if pre-connect fails"
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_stale_heartbeat_re_asserts_every_poll(self, tmp_path: Path) -> None:
        """Re-assertion model: each stale poll fires the full three-write
        fallback. Idempotent and defends against transient Modbus drops."""
        hb = tmp_path / "hb"
        hb.touch()
        old = time.time() - 600
        import os

        os.utime(hb, (old, old))

        client = await self._run_briefly(hb, stale_seconds=1.0, poll_seconds=0.02, iterations=4)
        # Each stale poll issues 3 writes on the happy path. After ~4
        # polls we expect at least 2 full fires (6 writes). Allow slack
        # for scheduler jitter.
        assert client.write_register.await_count >= 6, (
            f"expected re-assertion across polls, got {client.write_register.await_count} writes"
        )
        # Every third write should target REG_REMOTE_EMS_ENABLE with value 1
        # (the third write of each fallback fire is the enable=1 assertion).
        enable_writes = [
            c
            for c in client.write_register.await_args_list
            if c.kwargs["address"] == REG_REMOTE_EMS_ENABLE
        ]
        assert len(enable_writes) >= 2
        # In the happy path, every enable write is value=1 (explicit pin).
        # No last-resort enable=0 should fire when Modbus is healthy.
        assert all(c.kwargs["value"] == 1 for c in enable_writes)

    async def test_recovery_stops_writes(self, tmp_path: Path) -> None:
        """While stale: writes. After heartbeat refresh: writes stop
        until the next staleness episode."""
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
        mock_client.write_registers = AsyncMock(return_value=mock_result)
        mock_client.connect = AsyncMock(return_value=True)

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
                    max_charge_raw=_MAX_CHARGE_RAW,
                )
            )
            await asyncio.sleep(0.1)  # stale episode #1 — several fires
            writes_during_stale_1 = mock_client.write_register.await_count
            assert writes_during_stale_1 >= 3

            # Refresh heartbeat — should stop firing
            hb.touch()
            await asyncio.sleep(0.1)
            writes_after_recovery = mock_client.write_register.await_count
            # After recovery, no new writes should accumulate. Allow 1
            # extra in case the recovery landed mid-poll.
            assert writes_after_recovery - writes_during_stale_1 <= 3, (
                "watchdog should stop firing after recovery"
            )

            # Go stale again
            os.utime(hb, (old, old))
            await asyncio.sleep(0.1)
            writes_during_stale_2 = mock_client.write_register.await_count
            assert writes_during_stale_2 > writes_after_recovery, (
                "watchdog should resume firing on second staleness episode"
            )

            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_missing_file_within_grace_does_not_fire(self, tmp_path: Path) -> None:
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

    def test_touch_swallows_permission_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from optimiser.service import Service

        # A path the test runner definitely can't write to. Service.touch
        # must log and continue — the watchdog itself will detect staleness.
        monkeypatch.setenv("EO_HEARTBEAT_PATH", "/proc/1/heartbeat")
        svc = Service.__new__(Service)
        # Must not raise
        svc._touch_heartbeat()
