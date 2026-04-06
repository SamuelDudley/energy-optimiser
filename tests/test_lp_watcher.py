"""Tests for `VerificationWatcher` — the post-write deviation detector.

Coverage focus: every state combination the watcher might encounter
(no command, command-but-grace, OK, deviation × N, latched-but-probing,
clean × N to clear).

Time-sensitive paths use a fixed `write_timestamp` and patch
`datetime.now` at the watcher's call site so we control "how long since
write" deterministically without sleeping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from optimiser.lp.dispatch import (
    DispatchKind,
    LPDispatch,
    dispatch_from_slot,
)
from optimiser.lp.result import SlotDecision
from optimiser.lp.runtime import (
    CommandedState,
    FallbackReason,
    LPRuntime,
)
from optimiser.lp.watcher import VerificationWatcher
from optimiser.types import EventType, RemoteEMSControlMode

UTC = UTC
NOW = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)


# ── Test scaffolding ─────────────────────────────────────────────


def _slot(*, battery_kw: float = -3.0) -> SlotDecision:
    return SlotDecision(
        slot_start=NOW,
        battery_kw=battery_kw,
        grid_import_kw=0.0,
        grid_export_kw=3.0,
        pv_to_house_kw=0.0,
        pv_to_battery_kw=0.0,
        pv_to_export_kw=0.0,
        soc_pct_end=60.0,
    )


def _make_watcher(
    *,
    measured_kw: float | None = None,
    measured_seq: list[float | None] | None = None,
) -> tuple[VerificationWatcher, LPRuntime, MagicMock]:
    """Build a watcher with a mocked sigenergy.

    `measured_kw`: single value returned on every call.
    `measured_seq`: list of values returned in order (for multi-poll tests).
    """
    runtime = LPRuntime()
    sigenergy = MagicMock()
    if measured_seq is not None:
        sigenergy.read_battery_power_kw = AsyncMock(side_effect=measured_seq)
    else:
        sigenergy.read_battery_power_kw = AsyncMock(return_value=measured_kw)
    sigenergy.set_fallback = AsyncMock(return_value=True)

    watcher = VerificationWatcher(
        runtime=runtime,
        sigenergy=sigenergy,
        shelly_controllers=[],
    )
    return watcher, runtime, sigenergy


async def _record(
    runtime: LPRuntime,
    *,
    battery_kw: float = -3.0,
    write_age_s: float = 60.0,
) -> LPDispatch:
    """Record a dispatch with a controllable age."""
    dispatch = dispatch_from_slot(_slot(battery_kw=battery_kw))
    runtime.commanded = CommandedState(
        dispatch=dispatch,
        write_timestamp=NOW - timedelta(seconds=write_age_s),
    )
    return dispatch


def _patch_now(monkeypatch: pytest.MonkeyPatch, fixed: datetime = NOW) -> None:
    """Pin `datetime.now(timezone.utc)` inside the watcher to a fixed value."""

    class _FixedDT:
        @staticmethod
        def now(tz=None):
            return fixed

    monkeypatch.setattr("optimiser.lp.watcher.datetime", _FixedDT)


# ── Idle paths (no I/O expected) ─────────────────────────────────


class TestWatcherIdle:
    @pytest.mark.asyncio
    async def test_no_commanded_means_no_read(self, monkeypatch) -> None:
        watcher, runtime, sigenergy = _make_watcher(measured_kw=-3.0)
        # No record_command call → commanded is None
        _patch_now(monkeypatch)
        await watcher.poll()
        sigenergy.read_battery_power_kw.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_within_grace_period_no_read(self, monkeypatch) -> None:
        # 10s after write, default grace is 30s — too soon
        watcher, runtime, sigenergy = _make_watcher(measured_kw=-3.0)
        await _record(runtime, write_age_s=10.0)
        _patch_now(monkeypatch)
        await watcher.poll()
        sigenergy.read_battery_power_kw.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_self_consume_dispatch_skips_verification(self, monkeypatch) -> None:
        # Even past grace, SELF_CONSUME has no verification to do.
        watcher, runtime, sigenergy = _make_watcher(measured_kw=2.0)
        # Build a SELF_CONSUME dispatch directly
        dispatch = LPDispatch(
            mode=RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION,
            cap_kw=0.0,
            signed_intent_kw=0.05,
            kind=DispatchKind.SELF_CONSUME,
        )
        runtime.commanded = CommandedState(
            dispatch=dispatch,
            write_timestamp=NOW - timedelta(seconds=60),
        )
        _patch_now(monkeypatch)
        await watcher.poll()
        # It DID read (we don't know it's SELF_CONSUME until after) — but
        # took no action. Verify breaker untouched.
        assert runtime.breaker.consecutive_deviations == 0
        assert not runtime.breaker.latched

    @pytest.mark.asyncio
    async def test_modbus_read_failure_does_not_trigger_fallback(self, monkeypatch) -> None:
        # Read returns None (Modbus glitch). Watcher should silently bail —
        # transient read failures aren't deviations.
        watcher, runtime, sigenergy = _make_watcher(measured_kw=None)
        await _record(runtime, write_age_s=60.0)
        _patch_now(monkeypatch)
        await watcher.poll()
        assert runtime.breaker.consecutive_deviations == 0
        sigenergy.set_fallback.assert_not_awaited()


# ── Clean verification ───────────────────────────────────────────


class TestWatcherClean:
    @pytest.mark.asyncio
    async def test_ok_resets_deviation_counter(self, monkeypatch) -> None:
        watcher, runtime, sigenergy = _make_watcher(measured_kw=-2.5)
        await _record(runtime, battery_kw=-3.0, write_age_s=60.0)
        # Simulate a previous deviation
        runtime.breaker.consecutive_deviations = 2
        _patch_now(monkeypatch)
        await watcher.poll()
        # -2.5 measured against -3.0 cap = OK (sub-cap discharge)
        assert runtime.breaker.consecutive_deviations == 0

    @pytest.mark.asyncio
    async def test_clean_during_normal_op_does_not_clear_breaker(self, monkeypatch) -> None:
        # Not latched → consecutive_clean_probes shouldn't be relevant
        watcher, runtime, sigenergy = _make_watcher(measured_kw=-2.0)
        await _record(runtime, battery_kw=-3.0, write_age_s=60.0)
        _patch_now(monkeypatch)
        await watcher.poll()
        assert runtime.breaker.consecutive_clean_probes == 0
        assert not runtime.breaker.latched


# ── Probe-clears-latch flow ──────────────────────────────────────


class TestWatcherProbeClears:
    @pytest.mark.asyncio
    async def test_clean_probes_below_threshold_keep_latch(self, monkeypatch) -> None:
        # Latched, clean_probe_threshold=3, only 2 clean polls so far
        watcher, runtime, sigenergy = _make_watcher(measured_kw=-2.5)
        await _record(runtime, battery_kw=-3.0, write_age_s=60.0)
        runtime.breaker.latched = True
        runtime.breaker.latched_at = NOW - timedelta(minutes=10)
        runtime.breaker.last_fallback_reason = FallbackReason.LP_TIMEOUT
        runtime.breaker.consecutive_clean_probes = 1
        _patch_now(monkeypatch)
        await watcher.poll()
        # 1 + 1 = 2; threshold is 3 → still latched
        assert runtime.breaker.latched
        assert runtime.breaker.consecutive_clean_probes == 2

    @pytest.mark.asyncio
    async def test_third_clean_probe_clears_latch(self, monkeypatch) -> None:
        watcher, runtime, sigenergy = _make_watcher(measured_kw=-2.5)
        await _record(runtime, battery_kw=-3.0, write_age_s=60.0)
        runtime.breaker.latched = True
        runtime.breaker.latched_at = NOW - timedelta(minutes=10)
        runtime.breaker.last_fallback_reason = FallbackReason.LP_TIMEOUT
        runtime.breaker.consecutive_clean_probes = 2
        _patch_now(monkeypatch)
        with patch("optimiser.lp.watcher.emit") as mock_emit:
            await watcher.poll()
        assert not runtime.breaker.latched
        assert runtime.breaker.consecutive_clean_probes == 0
        assert runtime.breaker.last_fallback_reason == FallbackReason.NONE
        # Expect BREAKER_CLEARED with previous reason
        cleared = [c for c in mock_emit.call_args_list if c.args[0] == EventType.BREAKER_CLEARED]
        assert len(cleared) == 1
        assert cleared[0].args[1]["previous_reason"] == "lp_timeout"


# ── Deviation flow ──────────────────────────────────────────────


class TestWatcherDeviation:
    @pytest.mark.asyncio
    async def test_single_deviation_increments_counter_no_fallback(self, monkeypatch) -> None:
        # measured = +2 while commanded discharge → WRONG_DIRECTION
        watcher, runtime, sigenergy = _make_watcher(measured_kw=2.0)
        await _record(runtime, battery_kw=-3.0, write_age_s=60.0)
        _patch_now(monkeypatch)
        with patch("optimiser.lp.watcher.emit") as mock_emit:
            await watcher.poll()
        assert runtime.breaker.consecutive_deviations == 1
        assert not runtime.breaker.latched
        sigenergy.set_fallback.assert_not_awaited()
        # VERIFY_DEVIATION emitted even on single deviation
        deviations = [
            c for c in mock_emit.call_args_list if c.args[0] == EventType.VERIFY_DEVIATION
        ]
        assert len(deviations) == 1
        assert deviations[0].args[1]["outcome"] == "wrong_direction"
        assert deviations[0].args[1]["consecutive"] == 1

    @pytest.mark.asyncio
    async def test_threshold_deviations_trigger_fallback(self, monkeypatch) -> None:
        # 3 polls all returning wrong direction
        watcher, runtime, sigenergy = _make_watcher(
            measured_seq=[2.0, 2.0, 2.0],
        )
        await _record(runtime, battery_kw=-3.0, write_age_s=60.0)
        _patch_now(monkeypatch)
        with patch("optimiser.lp.watcher.emit"):
            await watcher.poll()
            await watcher.poll()
            await watcher.poll()
        assert runtime.breaker.latched
        assert runtime.breaker.last_fallback_reason == FallbackReason.VERIFY_DEVIATION
        sigenergy.set_fallback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clean_between_deviations_resets_counter(self, monkeypatch) -> None:
        # deviation, clean, deviation → not 2 deviations, just 1
        watcher, runtime, sigenergy = _make_watcher(
            measured_seq=[2.0, -2.5, 2.0],  # bad, ok, bad
        )
        await _record(runtime, battery_kw=-3.0, write_age_s=60.0)
        _patch_now(monkeypatch)
        with patch("optimiser.lp.watcher.emit"):
            await watcher.poll()  # dev 1
            await watcher.poll()  # clean → counter resets to 0
            await watcher.poll()  # dev 1 (not 2)
        assert runtime.breaker.consecutive_deviations == 1
        assert not runtime.breaker.latched

    @pytest.mark.asyncio
    async def test_over_cap_outcome_triggers_fallback(self, monkeypatch) -> None:
        # cap is 3.0 × 1.05 = 3.15 tolerance; -5.0 is way over
        watcher, runtime, sigenergy = _make_watcher(
            measured_seq=[-5.0, -5.0, -5.0],
        )
        await _record(runtime, battery_kw=-3.0, write_age_s=60.0)
        _patch_now(monkeypatch)
        with patch("optimiser.lp.watcher.emit") as mock_emit:
            await watcher.poll()
            await watcher.poll()
            await watcher.poll()
        assert runtime.breaker.latched
        # All three deviations should be over_cap
        outcomes = [
            c.args[1]["outcome"]
            for c in mock_emit.call_args_list
            if c.args[0] == EventType.VERIFY_DEVIATION
        ]
        assert outcomes == ["over_cap", "over_cap", "over_cap"]

    @pytest.mark.asyncio
    async def test_already_latched_does_not_double_trigger(self, monkeypatch) -> None:
        # If already latched, deviation should not call trigger_fallback again.
        # (Caller might ask why we still process at all — answer: we still
        # increment the counter for visibility, but don't re-trigger.)
        watcher, runtime, sigenergy = _make_watcher(measured_kw=2.0)
        await _record(runtime, battery_kw=-3.0, write_age_s=60.0)
        runtime.breaker.latched = True
        runtime.breaker.latched_at = NOW
        runtime.breaker.consecutive_deviations = 5  # already past threshold
        _patch_now(monkeypatch)
        await watcher.poll()
        sigenergy.set_fallback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deviation_resets_clean_probes(self, monkeypatch) -> None:
        # Probe was nearly cleared (2/3 clean), then deviation hits.
        # Clean counter resets so we can't sneak through with 2 clean +
        # 1 deviation + 1 clean = "passed".
        watcher, runtime, sigenergy = _make_watcher(measured_kw=2.0)
        await _record(runtime, battery_kw=-3.0, write_age_s=60.0)
        runtime.breaker.latched = True
        runtime.breaker.latched_at = NOW
        runtime.breaker.consecutive_clean_probes = 2
        _patch_now(monkeypatch)
        with patch("optimiser.lp.watcher.emit"):
            await watcher.poll()
        assert runtime.breaker.consecutive_clean_probes == 0


# ── A3: record_command resets deviation counter ──────────────────


class TestRecordCommandResetsDeviations:
    """A new dispatch must not inherit the previous dispatch's deviation
    count. A half-accumulated counter under an old CHARGE command means
    nothing once we've switched to DISCHARGE. See runtime.record_command
    docstring for rationale."""

    @pytest.mark.asyncio
    async def test_record_resets_deviation_counter(self) -> None:
        runtime = LPRuntime()
        # Simulate: previous dispatch had accumulated 2 consecutive devs
        runtime.breaker.consecutive_deviations = 2

        dispatch = dispatch_from_slot(_slot(battery_kw=-3.0))
        await runtime.record_command(dispatch)

        assert runtime.breaker.consecutive_deviations == 0

    @pytest.mark.asyncio
    async def test_record_preserves_clean_probe_counter(self) -> None:
        """Clean-probe counter must survive record_command — during a
        probe window (latched, re-attempting LP), clean verifications
        must accumulate across multiple ticks to clear the latch."""
        runtime = LPRuntime()
        runtime.breaker.latched = True
        runtime.breaker.consecutive_clean_probes = 2

        dispatch = dispatch_from_slot(_slot(battery_kw=-3.0))
        await runtime.record_command(dispatch)

        assert runtime.breaker.consecutive_clean_probes == 2

    @pytest.mark.asyncio
    async def test_cross_dispatch_deviation_does_not_carry(
        self,
        monkeypatch,
    ) -> None:
        """End-to-end-ish: under CHARGE, counter reaches 2. Then we
        dispatch DISCHARGE and see ONE more deviation. Without the reset
        this would hit threshold (3) immediately; with the reset, we need
        a full 3 consecutive devs against the new dispatch."""
        watcher, runtime, sigenergy = _make_watcher(measured_kw=-5.0)
        # First dispatch: CHARGE — this reads (via read_battery_power_kw)
        # -5kW, which is wrong direction for a charge. Let that accumulate.
        await _record(runtime, battery_kw=3.0, write_age_s=60.0)
        _patch_now(monkeypatch)
        with patch("optimiser.lp.watcher.emit"):
            await watcher.poll()
        assert runtime.breaker.consecutive_deviations == 1
        with patch("optimiser.lp.watcher.emit"):
            await watcher.poll()
        assert runtime.breaker.consecutive_deviations == 2

        # Now we record a NEW dispatch (DISCHARGE). Counter must reset.
        new_dispatch = dispatch_from_slot(_slot(battery_kw=-3.0))
        await runtime.record_command(new_dispatch)
        assert runtime.breaker.consecutive_deviations == 0
