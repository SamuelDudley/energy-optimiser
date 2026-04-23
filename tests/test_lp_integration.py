"""Tests for LP integration scaffolding: dispatch mapping, verification,
runtime state, and fallback contract.

Service-loop and watcher integration tests come in a follow-up session
once the wiring is in place.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from optimiser.config import BatteryConfig
from optimiser.lp.dispatch import (
    DEADBAND_KW,
    DeviationKind,
    DispatchKind,
    verify_battery_response,
)
from optimiser.lp.dispatch import dispatch_from_slot as _dispatch_from_slot
from optimiser.lp.fallback import trigger_fallback
from optimiser.lp.result import SlotDecision
from optimiser.lp.runtime import (
    CircuitBreaker,
    FallbackReason,
    LPRuntime,
)
from optimiser.types import RemoteEMSControlMode

UTC = UTC
NOW = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)

# Default battery config for dispatch tests — 10 kW discharge, 10 kW AC
# charge, 13 kW DC charge, matching production values.
_BAT = BatteryConfig()


def dispatch_from_slot(slot: SlotDecision, measured_pv_kw: float | None = None) -> object:
    """Test helper: injects the default BatteryConfig so test bodies stay
    focused on the (slot → dispatch) mapping."""
    return _dispatch_from_slot(slot, _BAT, measured_pv_kw=measured_pv_kw)


def _slot(
    *,
    battery_kw: float = 0.0,
    pv_to_battery_kw: float = 0.0,
    pv_to_house_kw: float = 0.0,
    pv_to_export_kw: float = 0.0,
    grid_import_kw: float = 0.0,
    grid_export_kw: float = 0.0,
    soc_pct_end: float = 50.0,
) -> SlotDecision:
    """Test factory — defaults to a do-nothing slot."""
    return SlotDecision(
        slot_start=NOW,
        battery_kw=battery_kw,
        grid_import_kw=grid_import_kw,
        grid_export_kw=grid_export_kw,
        pv_to_house_kw=pv_to_house_kw,
        pv_to_battery_kw=pv_to_battery_kw,
        pv_to_export_kw=pv_to_export_kw,
        soc_pct_end=soc_pct_end,
    )


# ── Dispatch mapping ─────────────────────────────────────────────


class TestDispatchFromSlot:
    """The mapping from LP's signed battery_kw to (mode, cap)."""

    def test_deadband_maps_to_self_consume(self) -> None:
        # Below 100W either side → SELF_CONSUME, no cap
        for kw in (0.0, 0.05, -0.05, DEADBAND_KW - 0.001):
            d = dispatch_from_slot(_slot(battery_kw=kw))
            assert d.mode == RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION
            assert d.kind == DispatchKind.SELF_CONSUME
            assert d.cap_kw == 0.0
            assert d.signed_intent_kw == kw

    def test_charge_grid_dominant_picks_mode_3(self) -> None:
        # 5kW charge with only 1kW from PV → 4kW from grid → grid-dominant
        d = dispatch_from_slot(_slot(battery_kw=5.0, pv_to_battery_kw=1.0))
        assert d.mode == RemoteEMSControlMode.COMMAND_CHARGING_GRID_FIRST
        assert d.kind == DispatchKind.CHARGE
        assert d.cap_kw == 5.0  # total intended charge rate
        assert d.signed_intent_kw == 5.0

    def test_charge_pv_dominant_picks_mode_4(self) -> None:
        # 5kW charge with 4kW from PV → 1kW from grid → PV-dominant
        d = dispatch_from_slot(_slot(battery_kw=5.0, pv_to_battery_kw=4.0))
        assert d.mode == RemoteEMSControlMode.COMMAND_CHARGING_PV_FIRST
        assert d.cap_kw == 5.0

    def test_charge_pv_only_picks_mode_4(self) -> None:
        # All charge from PV (e.g. midday surplus) → PV-first
        d = dispatch_from_slot(_slot(battery_kw=3.0, pv_to_battery_kw=3.0))
        assert d.mode == RemoteEMSControlMode.COMMAND_CHARGING_PV_FIRST

    def test_charge_grid_only_picks_mode_3(self) -> None:
        # No PV available (e.g. overnight cheap charging) → grid-first
        d = dispatch_from_slot(_slot(battery_kw=5.0, pv_to_battery_kw=0.0))
        assert d.mode == RemoteEMSControlMode.COMMAND_CHARGING_GRID_FIRST

    def test_charge_equal_split_prefers_pv(self) -> None:
        # Tie-breaker: PV-first when contributions are equal (free energy)
        d = dispatch_from_slot(_slot(battery_kw=4.0, pv_to_battery_kw=2.0))
        assert d.mode == RemoteEMSControlMode.COMMAND_CHARGING_PV_FIRST

    def test_discharge_with_pv_producing_picks_mode_5(self) -> None:
        # PV is producing → use mode 5 (DISCHARGING_PV_FIRST) so the inverter
        # load-follows with PV first, battery topping up. Mode 6 would zero
        # the PV (verified on hardware, see SIGENERGY-MODES.md).
        d = dispatch_from_slot(_slot(battery_kw=-3.0, pv_to_house_kw=1.5))
        assert d.mode == RemoteEMSControlMode.COMMAND_DISCHARGING_PV_FIRST
        assert d.kind == DispatchKind.DISCHARGE
        assert d.cap_kw == _BAT.max_discharge_kw
        assert d.signed_intent_kw == -3.0

    def test_evening_discharge_no_pv_picks_mode_6(self) -> None:
        # No PV → mode 6 (DISCHARGING_ESS_FIRST). Mode 5 would just idle
        # the battery here since it prefers PV.
        d = dispatch_from_slot(_slot(battery_kw=-5.0, pv_to_house_kw=0.0))
        assert d.mode == RemoteEMSControlMode.COMMAND_DISCHARGING_ESS_FIRST
        assert d.cap_kw == _BAT.max_discharge_kw

    def test_discharge_live_pv_overrides_lp_plan(self) -> None:
        # Live PV reading trumps the LP's planned PV flows (which may be
        # based on a stale/pessimistic forecast).
        d = dispatch_from_slot(
            _slot(battery_kw=-2.0, pv_to_house_kw=0.0),
            measured_pv_kw=3.5,
        )
        assert d.mode == RemoteEMSControlMode.COMMAND_DISCHARGING_PV_FIRST

    def test_discharge_live_pv_zero_picks_mode_6_even_if_plan_has_pv(self) -> None:
        # Mirror: LP planned PV but live is zero (cloud event mid-slot).
        d = dispatch_from_slot(
            _slot(battery_kw=-2.0, pv_to_house_kw=1.5),
            measured_pv_kw=0.05,
        )
        assert d.mode == RemoteEMSControlMode.COMMAND_DISCHARGING_ESS_FIRST

    def test_discharge_cap_covers_transient_loads(self) -> None:
        # Core behaviour: the LP picks -2 kW (its expected load) but the
        # dispatch cap is the physical max so sudden loads above forecast
        # (kettle, AC, oven) get covered from battery instead of grid.
        d = dispatch_from_slot(_slot(battery_kw=-2.0))
        assert d.cap_kw == _BAT.max_discharge_kw
        assert d.signed_intent_kw == -2.0
        # Kettle spike → house load 5 kW → inverter load-follows at 5 kW.
        # Well under physical cap → no deviation.
        assert verify_battery_response(d, measured_kw=-5.0) == DeviationKind.OK


# ── Verification ─────────────────────────────────────────────────


class TestVerifyBatteryResponse:
    def test_self_consume_never_verified(self) -> None:
        # No assertion to check in deadband mode
        d = dispatch_from_slot(_slot(battery_kw=0.05))
        assert verify_battery_response(d, measured_kw=0.0) == DeviationKind.NOT_VERIFIED
        assert verify_battery_response(d, measured_kw=10.0) == DeviationKind.NOT_VERIFIED
        assert verify_battery_response(d, measured_kw=-10.0) == DeviationKind.NOT_VERIFIED

    def test_charge_at_cap_is_ok(self) -> None:
        d = dispatch_from_slot(_slot(battery_kw=5.0, pv_to_battery_kw=4.0))
        # Inverter charging at exactly cap → OK
        assert verify_battery_response(d, measured_kw=5.0) == DeviationKind.OK

    def test_charge_under_cap_is_ok(self) -> None:
        # Inverter charging at less than cap (e.g. PV not enough, grid
        # constrained) is acceptable — not all our headroom must be used.
        d = dispatch_from_slot(_slot(battery_kw=5.0, pv_to_battery_kw=4.0))
        assert verify_battery_response(d, measured_kw=2.0) == DeviationKind.OK
        assert verify_battery_response(d, measured_kw=0.0) == DeviationKind.OK

    def test_charge_with_small_negative_within_floor_is_ok(self) -> None:
        # Measurement noise near zero — 200W discharge while we asked
        # for charge isn't a real deviation, just sensor jitter.
        d = dispatch_from_slot(_slot(battery_kw=3.0, pv_to_battery_kw=2.0))
        assert verify_battery_response(d, measured_kw=-0.2) == DeviationKind.OK

    def test_charge_actually_discharging_is_wrong_direction(self) -> None:
        d = dispatch_from_slot(_slot(battery_kw=5.0, pv_to_battery_kw=0.0))
        # Inverter discharging at 2kW when we asked for charge
        assert verify_battery_response(d, measured_kw=-2.0) == DeviationKind.WRONG_DIRECTION

    def test_charge_well_over_cap_is_over_cap(self) -> None:
        d = dispatch_from_slot(_slot(battery_kw=3.0, pv_to_battery_kw=2.0))
        # 5% tolerance: 3.0 × 1.05 = 3.15 — above that is OVER_CAP
        assert verify_battery_response(d, measured_kw=3.1) == DeviationKind.OK
        assert verify_battery_response(d, measured_kw=4.0) == DeviationKind.OVER_CAP

    def test_discharge_at_cap_is_ok(self) -> None:
        d = dispatch_from_slot(_slot(battery_kw=-5.0))
        # measured −5kW (discharging at cap)
        assert verify_battery_response(d, measured_kw=-5.0) == DeviationKind.OK

    def test_discharge_under_cap_is_ok(self) -> None:
        # PV may have absorbed house load; battery does less. OK.
        d = dispatch_from_slot(_slot(battery_kw=-5.0))
        assert verify_battery_response(d, measured_kw=-1.0) == DeviationKind.OK
        assert verify_battery_response(d, measured_kw=0.0) == DeviationKind.OK

    def test_discharge_actually_charging_is_wrong_direction(self) -> None:
        d = dispatch_from_slot(_slot(battery_kw=-3.0))
        assert verify_battery_response(d, measured_kw=2.0) == DeviationKind.WRONG_DIRECTION

    def test_discharge_over_cap_is_over_cap(self) -> None:
        # Cap is max_discharge_kw (10 kW) regardless of LP intent; OVER_CAP
        # now only fires on a hardware-level overshoot past the physical
        # limit. 10 × 1.05 = 10.5 tolerance.
        d = dispatch_from_slot(_slot(battery_kw=-3.0))
        assert verify_battery_response(d, measured_kw=-10.0) == DeviationKind.OK
        assert verify_battery_response(d, measured_kw=-10.4) == DeviationKind.OK
        assert verify_battery_response(d, measured_kw=-11.0) == DeviationKind.OVER_CAP


# ── Runtime state ────────────────────────────────────────────────


class TestLPRuntime:
    @pytest.mark.asyncio
    async def test_record_command_stores_dispatch(self) -> None:
        rt = LPRuntime()
        d = dispatch_from_slot(_slot(battery_kw=-3.5))
        await rt.record_command(d)
        assert rt.commanded is not None
        assert rt.commanded.dispatch is d

    @pytest.mark.asyncio
    async def test_latch_clears_commanded(self) -> None:
        rt = LPRuntime()
        await rt.record_command(dispatch_from_slot(_slot(battery_kw=2.0)))
        await rt.latch(FallbackReason.LP_TIMEOUT)
        assert rt.commanded is None
        assert rt.breaker.latched
        assert rt.breaker.last_fallback_reason == FallbackReason.LP_TIMEOUT

    @pytest.mark.asyncio
    async def test_clear_latch_resets_breaker(self) -> None:
        rt = LPRuntime()
        await rt.latch(FallbackReason.LP_TIMEOUT)
        await rt.clear_latch()
        assert not rt.breaker.latched
        assert rt.breaker.last_fallback_reason == FallbackReason.NONE


# ── Circuit breaker ──────────────────────────────────────────────


class TestCircuitBreaker:
    def test_not_latched_initially(self) -> None:
        cb = CircuitBreaker()
        assert not cb.latched
        assert not cb.is_in_cooldown(NOW)
        assert not cb.can_probe(NOW)

    def test_latched_in_cooldown(self) -> None:
        cb = CircuitBreaker(
            latched=True,
            latched_at=NOW,
            cooldown=timedelta(minutes=5),
        )
        # 1 min later — still in cooldown
        future = NOW + timedelta(minutes=1)
        assert cb.is_in_cooldown(future)
        assert not cb.can_probe(future)

    def test_latched_past_cooldown_can_probe(self) -> None:
        cb = CircuitBreaker(
            latched=True,
            latched_at=NOW,
            cooldown=timedelta(minutes=5),
        )
        future = NOW + timedelta(minutes=5, seconds=1)
        assert not cb.is_in_cooldown(future)
        assert cb.can_probe(future)


# ── Fallback writer ──────────────────────────────────────────────


class TestFallback:
    @pytest.mark.asyncio
    async def test_sets_self_consume_then_relays(self) -> None:
        """Mode-2 first, then relays. No setpoint to clear with the
        load-following dispatch path — the cap registers are mode-gated."""
        sig = MagicMock()
        sig.set_fallback = AsyncMock(return_value=True)
        # Critical: do NOT expect clear_continuous_power to be called
        sig.clear_continuous_power = AsyncMock(
            side_effect=AssertionError(
                "clear_continuous_power should not be called in mode 3/4/6 path"
            )
        )

        shelly_a = MagicMock(load_id="hot_water")
        shelly_a.set_relay = AsyncMock()
        shelly_b = MagicMock(load_id="aircon")
        shelly_b.set_relay = AsyncMock()

        result = await trigger_fallback(
            sig,
            [shelly_a, shelly_b],
            FallbackReason.LP_TIMEOUT,
            commanded_kw=-2.0,
            measured_kw=0.1,
        )
        assert result.set_self_consume
        assert "hot_water" in result.relays_opened
        assert "aircon" in result.relays_opened
        sig.set_fallback.assert_awaited_once()
        shelly_a.set_relay.assert_awaited_once_with(False)
        shelly_b.set_relay.assert_awaited_once_with(False)
        sig.clear_continuous_power.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_continues_after_partial_failures(self) -> None:
        """If set_fallback raises, relay-off must still execute. Fallback
        is best-effort, not all-or-nothing."""
        sig = MagicMock()
        sig.set_fallback = AsyncMock(side_effect=RuntimeError("modbus down"))

        shelly = MagicMock(load_id="hot_water")
        shelly.set_relay = AsyncMock()

        result = await trigger_fallback(
            sig,
            [shelly],
            FallbackReason.LP_ERROR,
        )
        assert not result.set_self_consume  # exception swallowed
        assert result.relays_opened == ["hot_water"]
        shelly.set_relay.assert_awaited_once_with(False)

    @pytest.mark.asyncio
    async def test_relay_failure_doesnt_skip_remaining_relays(self) -> None:
        sig = MagicMock()
        sig.set_fallback = AsyncMock(return_value=True)

        bad = MagicMock(load_id="hot_water")
        bad.set_relay = AsyncMock(side_effect=RuntimeError("relay timeout"))
        good = MagicMock(load_id="aircon")
        good.set_relay = AsyncMock()

        result = await trigger_fallback(
            sig,
            [bad, good],
            FallbackReason.VERIFY_DEVIATION,
        )
        # bad failed → not in relays_opened; good still attempted
        assert "hot_water" not in result.relays_opened
        assert "aircon" in result.relays_opened
        good.set_relay.assert_awaited_once_with(False)
