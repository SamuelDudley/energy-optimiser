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


def _mode2_charge_dispatch(target_soc_pct: float = 70.0) -> LPDispatch:
    """A §3.3 PV-charge dispatch: mode 2, no cap, cutoff carries intent."""
    return LPDispatch(
        mode=RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION,
        cap_kw=0.0,
        signed_intent_kw=3.0,
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

    async def test_self_consume_without_target_soc_writes_cap_then_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Defensive path: a mode-2 dispatch missing target_soc_pct (e.g.
        # constructed by tests, or hypothetically by a code path that
        # doesn't go through dispatch_from_slot) still writes 40032
        # (uncap the charge leg) and the mode register; it skips the
        # cutoff write. Production dispatches always carry target_soc_pct
        # under §3.3 — see TestMode2Idle below.
        ctrl = _controller()
        rec = _WriteRecorder()
        _install_recorder(ctrl, monkeypatch, rec)

        assert await ctrl.apply_lp_dispatch(_self_consume_dispatch())

        assert len(rec.calls) == 2
        assert rec.calls[0][:2] == ("u32", REG_ESS_MAX_CHARGING_LIMIT)
        assert rec.calls[1] == (
            "u16",
            REG_REMOTE_EMS_CONTROL_MODE,
            RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION.value,
        )

    async def test_mode2_charge_writes_cap_and_cutoff_before_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The §3.3 PV-charge path writes three registers in order:
        (1) reg 40032 = max_dc_charge_kw so the battery leg can accept
        PV at the physical DC max (otherwise a stale small value from a
        prior mode-3 tick throttles charge and surplus PV curtails —
        verified on hardware 2026-04-24), (2) reg 40047 (cutoff SOC) to
        bound charging by SOC, (3) reg 40031 (mode). All aux writes
        must precede the mode write so a half-failed apply leaves the
        inverter in its prior known state."""
        ctrl = _controller()
        rec = _WriteRecorder()
        _install_recorder(ctrl, monkeypatch, rec)

        assert await ctrl.apply_lp_dispatch(_mode2_charge_dispatch(target_soc_pct=72.0))

        assert len(rec.calls) == 3
        assert rec.calls[0][:2] == ("u32", REG_ESS_MAX_CHARGING_LIMIT)
        assert rec.calls[1] == ("u16", REG_CHARGE_CUTOFF_SOC, 720)  # 72.0% × 10
        assert rec.calls[2] == (
            "u16",
            REG_REMOTE_EMS_CONTROL_MODE,
            RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION.value,
        )

    async def test_mode2_idle_writes_cap_cutoff_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Idle (kind=SELF_CONSUME) under §3.3 writes the same three
        registers as mode-2 charge: 40032 uncapped, 40047 at current+
        buffer, then mode. Re-asserting 40032 every tick keeps stale
        small values from a previous mode-3 dispatch from throttling
        the battery the next time PV is available."""
        ctrl = _controller()
        rec = _WriteRecorder()
        _install_recorder(ctrl, monkeypatch, rec)

        assert await ctrl.apply_lp_dispatch(_self_consume_dispatch(target_soc_pct=58.5))

        assert len(rec.calls) == 3
        assert rec.calls[0][:2] == ("u32", REG_ESS_MAX_CHARGING_LIMIT)
        assert rec.calls[1] == ("u16", REG_CHARGE_CUTOFF_SOC, 585)
        assert rec.calls[2][:2] == ("u16", REG_REMOTE_EMS_CONTROL_MODE)


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

    async def test_mode2_cutoff_failure_aborts_before_mode_write(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Symmetric with cap-failure-aborts: if 40047 (cutoff) write
        fails on a mode-2 dispatch, we must NOT proceed to write mode.
        The cap write to 40032 (which precedes cutoff) does fire; the
        mode register is left unchanged so the inverter stays in
        whatever known-good mode the previous tick left it in."""
        ctrl = _controller()
        rec = _WriteRecorder(
            u16_returns={REG_CHARGE_CUTOFF_SOC: False},  # cutoff fails
        )
        _install_recorder(ctrl, monkeypatch, rec)

        result = await ctrl.apply_lp_dispatch(_mode2_charge_dispatch())

        assert result is False
        assert len(rec.calls) == 2
        assert rec.calls[0][:2] == ("u32", REG_ESS_MAX_CHARGING_LIMIT)
        assert rec.calls[1][:2] == ("u16", REG_CHARGE_CUTOFF_SOC)

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
