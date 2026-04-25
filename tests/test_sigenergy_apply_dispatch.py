"""Tests for `SigenergyController.apply_lp_dispatch` write ordering (S3).

The inverter must never end up in (new mode, stale cap). The apply path
writes the cap register FIRST, then the mode register. If the cap write
fails, the mode write is skipped entirely.
"""

from __future__ import annotations

import pytest

from optimiser.clients.sigenergy import (
    REG_BACKUP_SOC,
    REG_CHARGE_CUTOFF_SOC,
    REG_DISCHARGE_CUTOFF_SOC,
    REG_ESS_MAX_CHARGING_LIMIT,
    REG_ESS_MAX_DISCHARGING_LIMIT,
    REG_REMOTE_EMS_CONTROL_MODE,
    SigenergyController,
)
from optimiser.config import BatteryConfig, SigenergyConfig
from optimiser.lp.dispatch import DispatchKind, LPDispatch
from optimiser.types import RemoteEMSControlMode


def _controller() -> SigenergyController:
    ctrl = SigenergyController(
        SigenergyConfig(host="127.0.0.1"),
        BatteryConfig(),
    )
    ctrl._connected = True
    ctrl._remote_ems_enabled = True  # skip the enable step in tests
    return ctrl


def _charge_dispatch(kw: float = 3.0) -> LPDispatch:
    return LPDispatch(
        mode=RemoteEMSControlMode.COMMAND_CHARGING_GRID_FIRST,
        cap_kw=kw,
        signed_intent_kw=kw,
        kind=DispatchKind.CHARGE,
    )


def _discharge_dispatch(kw: float = 4.0) -> LPDispatch:
    return LPDispatch(
        mode=RemoteEMSControlMode.COMMAND_DISCHARGING_ESS_FIRST,
        cap_kw=kw,
        signed_intent_kw=-kw,
        kind=DispatchKind.DISCHARGE,
    )


def _self_consume_dispatch(target_soc_pct: float | None = None) -> LPDispatch:
    return LPDispatch(
        mode=RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION,
        cap_kw=0.0,
        signed_intent_kw=0.0,
        kind=DispatchKind.SELF_CONSUME,
        target_soc_pct=target_soc_pct,
    )


def _mode2_charge_dispatch(
    target_soc_pct: float = 70.0, signed_intent_kw: float = 3.0
) -> LPDispatch:
    """Adaptive mode-2 PV-charge dispatch (post 2026-04-25). cap_kw is the
    LP rate, used as the Phase-B trim floor; target_soc_pct is advisory
    only."""
    return LPDispatch(
        mode=RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION,
        cap_kw=signed_intent_kw,
        signed_intent_kw=signed_intent_kw,
        kind=DispatchKind.CHARGE,
        target_soc_pct=target_soc_pct,
    )


class _WriteRecorder:
    """Captures ordered calls to the two write helpers so we can assert
    the cap-before-mode invariant."""

    def __init__(
        self, u16_returns: dict[int, bool] | None = None, u32_returns: dict[int, bool] | None = None
    ) -> None:
        self.calls: list[tuple[str, int, int]] = []  # (kind, address, value)
        self._u16_returns = u16_returns or {}
        self._u32_returns = u32_returns or {}

    async def write_u16(self, address: int, value: int) -> bool:
        self.calls.append(("u16", address, value))
        return self._u16_returns.get(address, True)

    async def write_u32(self, address: int, value: int) -> bool:
        self.calls.append(("u32", address, value))
        return self._u32_returns.get(address, True)


def _install_recorder(
    ctrl: SigenergyController,
    monkeypatch: pytest.MonkeyPatch,
    recorder: _WriteRecorder,
) -> None:
    monkeypatch.setattr(ctrl, "_write_u16", recorder.write_u16)
    monkeypatch.setattr(ctrl, "_write_u32", recorder.write_u32)


class TestWriteOrdering:
    async def test_charge_writes_cap_before_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = _controller()
        rec = _WriteRecorder()
        _install_recorder(ctrl, monkeypatch, rec)

        assert await ctrl.apply_lp_dispatch(_charge_dispatch(kw=3.0))

        # Two writes: u32 cap to 40032, then u16 mode to 40031
        assert len(rec.calls) == 2
        assert rec.calls[0] == ("u32", REG_ESS_MAX_CHARGING_LIMIT, 3000)
        assert rec.calls[1] == (
            "u16",
            REG_REMOTE_EMS_CONTROL_MODE,
            RemoteEMSControlMode.COMMAND_CHARGING_GRID_FIRST.value,
        )

    async def test_discharge_writes_cap_before_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = _controller()
        rec = _WriteRecorder()
        _install_recorder(ctrl, monkeypatch, rec)

        assert await ctrl.apply_lp_dispatch(_discharge_dispatch(kw=4.0))

        assert len(rec.calls) == 2
        assert rec.calls[0] == ("u32", REG_ESS_MAX_DISCHARGING_LIMIT, 4000)
        assert rec.calls[1] == (
            "u16",
            REG_REMOTE_EMS_CONTROL_MODE,
            RemoteEMSControlMode.COMMAND_DISCHARGING_ESS_FIRST.value,
        )

    async def test_mode2_idle_writes_cap_zero_then_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Idle dispatch under the post-2026-04-25 design: write
        40032 = 0 (block PV-to-battery charge), then mode = 2. The
        cascade routes surplus to export at the DNSP cap (already
        written by service.py before this call) and discharges battery
        if PV < load. No cutoff (40047) write — that's pinned at the
        startup ceiling."""
        ctrl = _controller()
        rec = _WriteRecorder()
        _install_recorder(ctrl, monkeypatch, rec)

        assert await ctrl.apply_lp_dispatch(_self_consume_dispatch(target_soc_pct=58.5))

        assert len(rec.calls) == 2
        assert rec.calls[0] == ("u32", REG_ESS_MAX_CHARGING_LIMIT, 0)
        assert rec.calls[1] == (
            "u16",
            REG_REMOTE_EMS_CONTROL_MODE,
            RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION.value,
        )
        # No cutoff write
        assert all(addr != REG_CHARGE_CUTOFF_SOC for _, addr, _ in rec.calls)


class TestWriteFailureSafety:
    async def test_cap_failure_aborts_before_mode_write(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The S3 invariant: if cap write fails, we must NOT proceed to
        write mode (that would create the very (new-mode, stale-cap)
        state this ordering prevents)."""
        ctrl = _controller()
        rec = _WriteRecorder(
            u32_returns={REG_ESS_MAX_CHARGING_LIMIT: False},  # cap fails
        )
        _install_recorder(ctrl, monkeypatch, rec)

        result = await ctrl.apply_lp_dispatch(_charge_dispatch())

        assert result is False
        # Only the cap attempt happened — no mode write
        assert len(rec.calls) == 1
        assert rec.calls[0][:2] == ("u32", REG_ESS_MAX_CHARGING_LIMIT)

    async def test_mode2_charge_cap_failure_aborts_before_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mode-2 adaptive charge writes 40032=max FIRST, then mode=2.
        If the 40032 write fails, the mode write must NOT happen — the
        inverter stays in whatever known-good mode the previous tick
        left it in."""
        ctrl = _controller()
        rec = _WriteRecorder(
            u32_returns={REG_ESS_MAX_CHARGING_LIMIT: False},  # cap fails
        )
        _install_recorder(ctrl, monkeypatch, rec)

        result = await ctrl.apply_lp_dispatch(
            _mode2_charge_dispatch(), export_cap_kw=0.0
        )

        assert result is False
        # Only the 40032 attempt happened — no mode write
        assert len(rec.calls) == 1
        assert rec.calls[0][:2] == ("u32", REG_ESS_MAX_CHARGING_LIMIT)

    async def test_mode_failure_after_successful_cap_returns_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cap succeeded, mode failed — still False so the caller
        triggers fallback. The inverter is left in (old mode, new cap),
        which is always safe (see apply_lp_dispatch docstring)."""
        ctrl = _controller()
        rec = _WriteRecorder(
            u16_returns={REG_REMOTE_EMS_CONTROL_MODE: False},  # mode fails
        )
        _install_recorder(ctrl, monkeypatch, rec)

        result = await ctrl.apply_lp_dispatch(_charge_dispatch())

        assert result is False
        # Both writes attempted, cap before mode
        assert len(rec.calls) == 2
        assert rec.calls[0][:2] == ("u32", REG_ESS_MAX_CHARGING_LIMIT)
        assert rec.calls[1][:2] == ("u16", REG_REMOTE_EMS_CONTROL_MODE)


class TestMode2Adaptive:
    """The PV-dominant charge path runs a Phase-A measure / Phase-B trim
    sequence to split surplus PV between the battery and export rather
    than cascade-saturating the battery first. See
    `_apply_mode2_adaptive_charge` and SPEC-ENERGY-01.md §5.4."""

    @staticmethod
    def _state(pv_kw: float, load_kw: float):
        from datetime import UTC, datetime

        from optimiser.types import SystemState

        return SystemState(
            timestamp=datetime.now(UTC),
            soc_pct=50.0,
            battery_power_kw=0.0,
            pv_power_kw=pv_kw,
            grid_power_kw=-(pv_kw - load_kw),
            house_load_kw=load_kw,
            ems_mode=2,
            outdoor_temp_c=None,
            occupied=True,
        )

    async def test_phase_b_trims_surplus_minus_export_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """surplus=8, export_cap=5, headroom=0.5 → trim ≈ 2.5 kW.
        Phase-A writes 40032=max + mode=2; Phase-B writes 40032=trim."""
        ctrl = _controller()
        rec = _WriteRecorder()
        _install_recorder(ctrl, monkeypatch, rec)

        async def _no_sleep(_seconds: float) -> None:
            return None

        async def _read_state():
            return self._state(pv_kw=9.0, load_kw=1.0)  # surplus 8

        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        monkeypatch.setattr(ctrl, "read_state", _read_state)

        assert await ctrl.apply_lp_dispatch(
            _mode2_charge_dispatch(signed_intent_kw=1.0),
            export_cap_kw=5.0,
        )

        # 1) 40032 = max (uncap), 2) mode = 2, 3) 40032 = trim
        assert len(rec.calls) == 3
        assert rec.calls[0][:2] == ("u32", REG_ESS_MAX_CHARGING_LIMIT)
        # Phase-A uncap should be the configured max_dc_charge_kw
        assert rec.calls[0][2] == int(round(ctrl._battery.max_dc_charge_kw * 1000))
        assert rec.calls[1] == (
            "u16",
            REG_REMOTE_EMS_CONTROL_MODE,
            RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION.value,
        )
        # surplus 8 - export 5 - headroom 0.5 = 2.5 kW → 2500
        assert rec.calls[2] == ("u32", REG_ESS_MAX_CHARGING_LIMIT, 2500)

    async def test_phase_b_lp_rate_is_trim_floor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If measured surplus is below LP rate, trim collapses to the
        LP rate — protects against transient PV droop during Phase A."""
        ctrl = _controller()
        rec = _WriteRecorder()
        _install_recorder(ctrl, monkeypatch, rec)

        async def _no_sleep(_seconds: float) -> None:
            return None

        async def _read_state():
            return self._state(pv_kw=2.0, load_kw=1.0)  # surplus 1

        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        monkeypatch.setattr(ctrl, "read_state", _read_state)

        assert await ctrl.apply_lp_dispatch(
            _mode2_charge_dispatch(signed_intent_kw=4.0),  # LP wanted 4 kW
            export_cap_kw=5.0,
        )

        # max(LP_rate=4, max(0, 1-5) - 0.5) = 4 → 4000
        assert rec.calls[2] == ("u32", REG_ESS_MAX_CHARGING_LIMIT, 4000)

    async def test_phase_b_clamped_to_max_dc_charge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Trim never exceeds the physical DC charge limit, even with
        absurdly high measured surplus."""
        ctrl = _controller()
        rec = _WriteRecorder()
        _install_recorder(ctrl, monkeypatch, rec)

        async def _no_sleep(_seconds: float) -> None:
            return None

        async def _read_state():
            return self._state(pv_kw=50.0, load_kw=0.0)  # impossible surplus

        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        monkeypatch.setattr(ctrl, "read_state", _read_state)

        assert await ctrl.apply_lp_dispatch(
            _mode2_charge_dispatch(signed_intent_kw=2.0),
            export_cap_kw=5.0,
        )

        max_raw = int(round(ctrl._battery.max_dc_charge_kw * 1000))
        assert rec.calls[2] == ("u32", REG_ESS_MAX_CHARGING_LIMIT, max_raw)

    async def test_phase_a_telemetry_failure_leaves_uncapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If read_state returns None during Phase A, the path returns
        True with Phase-A state in force (40032=max). Equivalent to the
        fallback's 'uncapped charge' behaviour — safe."""
        ctrl = _controller()
        rec = _WriteRecorder()
        _install_recorder(ctrl, monkeypatch, rec)

        async def _no_sleep(_seconds: float) -> None:
            return None

        async def _read_state():
            return None

        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        monkeypatch.setattr(ctrl, "read_state", _read_state)

        assert await ctrl.apply_lp_dispatch(
            _mode2_charge_dispatch(), export_cap_kw=5.0,
        )

        # Phase A only: 40032=max, mode=2. No Phase-B trim.
        assert len(rec.calls) == 2
        assert rec.calls[0][:2] == ("u32", REG_ESS_MAX_CHARGING_LIMIT)
        assert rec.calls[1][:2] == ("u16", REG_REMOTE_EMS_CONTROL_MODE)


class TestAssertSOCLimits:
    """§4.2: split between startup (all three limits) and periodic
    re-assertion (discharge-side only, skipping 40047 so it doesn't
    fight §3.3's tick-managed charge cutoff)."""

    async def test_startup_writes_all_three_limits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctrl = _controller()
        rec = _WriteRecorder()
        _install_recorder(ctrl, monkeypatch, rec)

        assert await ctrl.assert_battery_soc_limits()
        addresses = [addr for _, addr, _ in rec.calls]
        assert REG_CHARGE_CUTOFF_SOC in addresses
        assert REG_DISCHARGE_CUTOFF_SOC in addresses
        assert REG_BACKUP_SOC in addresses
        assert len(rec.calls) == 3

    async def test_periodic_writes_only_discharge_side(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hourly re-assertion must NOT write 40047 — that register will
        become tick-managed under §3.3, and an hourly overwrite would
        briefly push the charge ceiling back up to 95%."""
        ctrl = _controller()
        rec = _WriteRecorder()
        _install_recorder(ctrl, monkeypatch, rec)

        assert await ctrl.assert_discharge_soc_limits()
        addresses = [addr for _, addr, _ in rec.calls]
        assert REG_CHARGE_CUTOFF_SOC not in addresses, (
            "periodic re-assertion must skip the charge cutoff register"
        )
        assert REG_DISCHARGE_CUTOFF_SOC in addresses
        assert REG_BACKUP_SOC in addresses
        assert len(rec.calls) == 2

    async def test_periodic_propagates_write_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctrl = _controller()
        rec = _WriteRecorder(u16_returns={REG_BACKUP_SOC: False})
        _install_recorder(ctrl, monkeypatch, rec)

        assert await ctrl.assert_discharge_soc_limits() is False
