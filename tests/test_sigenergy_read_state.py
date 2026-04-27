"""Tests for `SigenergyController.read_state` null-over-wrong behaviour.

Covers S2 from the pre-deployment review:
  - Grid sensor offline (status != 1)  → grid_power_kw and house_load_kw both None
  - Derivation comes out absurdly negative (possible sign-convention error)
      → house_load_kw nulled, grid_power_kw preserved
  - Happy path (sensor online, sensible derivation) → both populated

Mocks at the helper-method level (`_read_input_u16`, `_read_input_s32`) so
we don't have to fabricate a pymodbus result object shape.
"""

from __future__ import annotations

import pytest

from optimiser.clients.sigenergy import (
    REG_EMS_WORK_MODE,
    REG_GRID_ACTIVE_POWER,
    REG_GRID_SENSOR_STATUS,
    REG_PLANT_ESS_POWER,
    REG_PLANT_ESS_SOC,
    REG_PLANT_PV_POWER,
    SigenergyController,
)
from optimiser.config import BatteryConfig, SigenergyConfig


def _controller() -> SigenergyController:
    """Build a controller with a real pymodbus client we never call. Mark
    it connected so `read_state` proceeds past its early return."""
    ctrl = SigenergyController(
        SigenergyConfig(host="127.0.0.1"),
        BatteryConfig(),
    )
    ctrl._connected = True
    return ctrl


def _patch_reads(
    ctrl: SigenergyController,
    monkeypatch: pytest.MonkeyPatch,
    *,
    u16: dict[int, int | None],
    s32: dict[int, float | None],
) -> None:
    """Patch the two read helpers to look up scripted values by register
    address. Unscripted addresses return None (simulates a read failure)."""

    async def _u16(address: int) -> int | None:
        return u16.get(address)

    async def _s32(address: int) -> float | None:
        return s32.get(address)

    monkeypatch.setattr(ctrl, "_read_input_u16", _u16)
    monkeypatch.setattr(ctrl, "_read_input_s32", _s32)


class TestReadStateHappyPath:
    async def test_populates_both_when_sensor_online(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = _controller()
        _patch_reads(
            ctrl,
            monkeypatch,
            u16={
                REG_EMS_WORK_MODE: 2,
                REG_GRID_SENSOR_STATUS: 1,  # online
                REG_PLANT_ESS_SOC: 500,  # 50.0 %
            },
            s32={
                REG_GRID_ACTIVE_POWER: 1.2,  # 1.2 kW import
                REG_PLANT_ESS_POWER: 0.0,
                REG_PLANT_PV_POWER: 3.0,
            },
        )
        state = await ctrl.read_state()
        assert state is not None
        assert state.grid_power_kw == pytest.approx(1.2)
        # house = pv (3) + grid (1.2) - battery (0) = 4.2
        assert state.house_load_kw == pytest.approx(4.2)
        assert state.soc_pct == pytest.approx(50.0)


class TestReadStateGridSensorOffline:
    async def test_nulls_grid_and_house_when_status_not_online(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = _controller()
        _patch_reads(
            ctrl,
            monkeypatch,
            u16={
                REG_EMS_WORK_MODE: 2,
                REG_GRID_SENSOR_STATUS: 0,  # offline
                REG_PLANT_ESS_SOC: 500,
            },
            s32={
                REG_GRID_ACTIVE_POWER: 99.9,  # garbage — must NOT propagate
                REG_PLANT_ESS_POWER: 0.0,
                REG_PLANT_PV_POWER: 3.0,
            },
        )
        state = await ctrl.read_state()
        assert state is not None
        assert state.grid_power_kw is None
        assert state.house_load_kw is None
        # Battery and PV are independent registers — still valid.
        assert state.battery_power_kw == pytest.approx(0.0)
        assert state.pv_power_kw == pytest.approx(3.0)
        assert ctrl.grid_sensor_online is False


class TestReadStateAbsurdDerivation:
    async def test_nulls_house_load_when_derivation_very_negative(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Simulate a sign-convention error — e.g. grid_kw comes in as -4
        when we're importing 4 kW. Derivation = pv (0) + grid (-4) -
        battery (0) = -4 kW. Must null house_load, keep grid (it's what
        the register gave us; downstream validation can flag divergence
        vs the Shelly mains CT)."""
        ctrl = _controller()
        _patch_reads(
            ctrl,
            monkeypatch,
            u16={
                REG_EMS_WORK_MODE: 2,
                REG_GRID_SENSOR_STATUS: 1,  # online but values are suspect
                REG_PLANT_ESS_SOC: 500,
            },
            s32={
                REG_GRID_ACTIVE_POWER: -4.0,
                REG_PLANT_ESS_POWER: 0.0,
                REG_PLANT_PV_POWER: 0.0,
            },
        )
        state = await ctrl.read_state()
        assert state is not None
        assert state.house_load_kw is None  # absurd → nulled
        assert state.grid_power_kw == pytest.approx(-4.0)  # preserved raw

    async def test_small_negative_within_noise_accepted(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Measurement noise on the order of 10–50 W can push a truly-zero
        house load slightly negative. We accept down to −0.1 kW."""
        ctrl = _controller()
        _patch_reads(
            ctrl,
            monkeypatch,
            u16={
                REG_EMS_WORK_MODE: 2,
                REG_GRID_SENSOR_STATUS: 1,
                REG_PLANT_ESS_SOC: 500,
            },
            s32={
                REG_GRID_ACTIVE_POWER: -0.03,
                REG_PLANT_ESS_POWER: 0.0,
                REG_PLANT_PV_POWER: 0.0,
            },
        )
        state = await ctrl.read_state()
        assert state is not None
        # -0.03 is within the -0.1 noise floor, so preserved as-is.
        assert state.house_load_kw == pytest.approx(-0.03)


class TestReadStateMissingRegisters:
    async def test_returns_none_when_any_critical_register_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Existing contract: if SOC / grid / battery / PV can't be read,
        read_state returns None. The S2 change must not regress this."""
        ctrl = _controller()
        _patch_reads(
            ctrl,
            monkeypatch,
            u16={
                REG_EMS_WORK_MODE: 2,
                REG_GRID_SENSOR_STATUS: 1,
                REG_PLANT_ESS_SOC: None,  # failed read
            },
            s32={
                REG_GRID_ACTIVE_POWER: 1.0,
                REG_PLANT_ESS_POWER: 0.0,
                REG_PLANT_PV_POWER: 0.0,
            },
        )
        state = await ctrl.read_state()
        assert state is None


# ── Event dedup on persistent faults ─────────────────────────────


class TestReadStateEventDedup:
    """A persistent fault (grid sensor offline, or recurring absurd
    derivation) must not spam VALIDATION_WARNING every tick. Emit on the
    rising edge; re-arm after the condition clears."""

    async def test_grid_sensor_offline_warns_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = _controller()
        _patch_reads(
            ctrl,
            monkeypatch,
            u16={
                REG_EMS_WORK_MODE: 2,
                REG_GRID_SENSOR_STATUS: 0,  # persistently offline
                REG_PLANT_ESS_SOC: 500,
            },
            s32={
                REG_GRID_ACTIVE_POWER: 0.0,
                REG_PLANT_ESS_POWER: 0.0,
                REG_PLANT_PV_POWER: 0.0,
            },
        )
        events: list[tuple] = []
        monkeypatch.setattr(
            "optimiser.clients.sigenergy.emit",
            lambda evt_type, payload: events.append((evt_type, payload)),
        )

        # Three consecutive reads with the fault present
        for _ in range(3):
            await ctrl.read_state()

        warnings = [e for e in events if "grid" in str(e[1].get("message", "")).lower()]
        assert len(warnings) == 1, (
            f"expected 1 grid-sensor warning across 3 ticks, got {len(warnings)}"
        )

    async def test_re_arms_after_recovery(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sensor goes offline → warn. Sensor recovers → silent. Sensor
        offline again → warn second time."""
        ctrl = _controller()
        status_seq = iter([0, 0, 1, 1, 0, 0])  # offline → online → offline

        async def _u16(address: int) -> int | None:
            if address == REG_GRID_SENSOR_STATUS:
                return next(status_seq)
            return {
                REG_EMS_WORK_MODE: 2,
                REG_PLANT_ESS_SOC: 500,
            }.get(address)

        async def _s32(address: int) -> float | None:
            return {
                REG_GRID_ACTIVE_POWER: 0.0,
                REG_PLANT_ESS_POWER: 0.0,
                REG_PLANT_PV_POWER: 0.0,
            }.get(address)

        monkeypatch.setattr(ctrl, "_read_input_u16", _u16)
        monkeypatch.setattr(ctrl, "_read_input_s32", _s32)

        events: list[tuple] = []
        monkeypatch.setattr(
            "optimiser.clients.sigenergy.emit",
            lambda evt_type, payload: events.append((evt_type, payload)),
        )

        # Six reads: 2 offline, 2 online, 2 offline-again
        for _ in range(6):
            await ctrl.read_state()

        warnings = [e for e in events if "grid" in str(e[1].get("message", "")).lower()]
        # Expect 2 warnings: one for each offline episode
        assert len(warnings) == 2

    async def test_absurd_derivation_warns_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = _controller()
        _patch_reads(
            ctrl,
            monkeypatch,
            u16={
                REG_EMS_WORK_MODE: 2,
                REG_GRID_SENSOR_STATUS: 1,  # online, but derivation is bad
                REG_PLANT_ESS_SOC: 500,
            },
            s32={
                REG_GRID_ACTIVE_POWER: -4.0,  # sign convention suspicion
                REG_PLANT_ESS_POWER: 0.0,
                REG_PLANT_PV_POWER: 0.0,
            },
        )
        events: list[tuple] = []
        monkeypatch.setattr(
            "optimiser.clients.sigenergy.emit",
            lambda evt_type, payload: events.append((evt_type, payload)),
        )

        for _ in range(3):
            await ctrl.read_state()

        warnings = [e for e in events if "derived" in str(e[1].get("message", "")).lower()]
        assert len(warnings) == 1


# ── Sticky-disconnect recovery ────────────────────────────────────


class TestReadStateReconnectAfterDrop:
    """Wi-Fi outage / inverter reboot scenarios. A prior `_read_input_*`
    raised ConnectionException and flipped `_connected = False`. Without
    an explicit re-arm path the flag stays False forever and `read_state`
    short-circuits on every subsequent tick — the heartbeat goes stale,
    the watchdog keeps firing, and the inverter sits idle even after the
    network has recovered. The fix attempts a reconnect inside `read_state`
    and re-arms via `_mark_connected` on any successful read or write."""

    async def test_reconnects_and_returns_state_when_link_recovers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = SigenergyController(
            SigenergyConfig(host="127.0.0.1"),
            BatteryConfig(),
        )
        ctrl._connected = False  # simulate prior drop

        # pymodbus client.connect() succeeds on the next tick
        async def _fake_connect() -> bool:
            return True

        monkeypatch.setattr(ctrl._client, "connect", _fake_connect)
        _patch_reads(
            ctrl,
            monkeypatch,
            u16={
                REG_EMS_WORK_MODE: 2,
                REG_GRID_SENSOR_STATUS: 1,
                REG_PLANT_ESS_SOC: 500,
            },
            s32={
                REG_GRID_ACTIVE_POWER: 1.0,
                REG_PLANT_ESS_POWER: 0.0,
                REG_PLANT_PV_POWER: 2.0,
            },
        )
        events: list[tuple] = []
        monkeypatch.setattr(
            "optimiser.clients.sigenergy.emit",
            lambda evt_type, payload: events.append((evt_type, payload)),
        )

        state = await ctrl.read_state()
        assert state is not None
        assert ctrl.connected is True
        # MODBUS_RECONNECTED emitted on the rising edge.
        from optimiser.types import EventType as ET
        recon = [e for e in events if e[0] == ET.MODBUS_RECONNECTED]
        assert len(recon) == 1

    async def test_returns_none_when_reconnect_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = SigenergyController(
            SigenergyConfig(host="127.0.0.1"),
            BatteryConfig(),
        )
        ctrl._connected = False

        async def _fake_connect() -> bool:
            return False  # inverter still unreachable

        monkeypatch.setattr(ctrl._client, "connect", _fake_connect)

        state = await ctrl.read_state()
        assert state is None
        assert ctrl.connected is False

    async def test_reconnect_failure_quiet_and_state_stays_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sustained outages must not flood the log with stack traces.
        `_attempt_reconnect` logs a one-line WARNING; the existing read
        path's exception traceback already captured the original drop."""
        ctrl = SigenergyController(
            SigenergyConfig(host="127.0.0.1"),
            BatteryConfig(),
        )
        ctrl._connected = False

        async def _raises(*_a, **_k):
            raise OSError("host unreachable")

        monkeypatch.setattr(ctrl._client, "connect", _raises)

        state = await ctrl.read_state()
        assert state is None
        assert ctrl.connected is False


class TestWriteRecoversConnectedFlag:
    """While `read_state` is short-circuiting, the fallback path keeps
    issuing writes (`set_fallback`). When those writes start succeeding
    again — pymodbus's underlying socket has recovered — `_connected`
    must flip back to True so the next tick's `read_state` proceeds
    without waiting for the explicit reconnect attempt."""

    async def test_successful_u16_write_marks_connected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = SigenergyController(
            SigenergyConfig(host="127.0.0.1"),
            BatteryConfig(),
        )
        ctrl._connected = False

        class _OkResult:
            def isError(self) -> bool:
                return False

        async def _fake_write(**_kwargs):
            return _OkResult()

        monkeypatch.setattr(ctrl._client, "write_register", _fake_write)
        events: list[tuple] = []
        monkeypatch.setattr(
            "optimiser.clients.sigenergy.emit",
            lambda evt_type, payload: events.append((evt_type, payload)),
        )

        ok = await ctrl._write_u16(40031, 2)
        assert ok is True
        assert ctrl.connected is True

        from optimiser.types import EventType as ET
        recon = [e for e in events if e[0] == ET.MODBUS_RECONNECTED]
        assert len(recon) == 1, "Rising-edge recovery should emit one event"

    async def test_successful_u32_write_marks_connected(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = SigenergyController(
            SigenergyConfig(host="127.0.0.1"),
            BatteryConfig(),
        )
        ctrl._connected = False

        class _OkResult:
            def isError(self) -> bool:
                return False

        async def _fake_writes(**_kwargs):
            return _OkResult()

        monkeypatch.setattr(ctrl._client, "write_registers", _fake_writes)

        ok = await ctrl._write_u32(40032, 5000)
        assert ok is True
        assert ctrl.connected is True

    async def test_steady_state_writes_dont_re_emit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When already connected, repeated successful writes must NOT
        re-emit MODBUS_RECONNECTED — only the rising edge counts."""
        ctrl = SigenergyController(
            SigenergyConfig(host="127.0.0.1"),
            BatteryConfig(),
        )
        ctrl._connected = True  # already healthy

        class _OkResult:
            def isError(self) -> bool:
                return False

        async def _fake_write(**_kwargs):
            return _OkResult()

        monkeypatch.setattr(ctrl._client, "write_register", _fake_write)
        events: list[tuple] = []
        monkeypatch.setattr(
            "optimiser.clients.sigenergy.emit",
            lambda evt_type, payload: events.append((evt_type, payload)),
        )

        for _ in range(5):
            await ctrl._write_u16(40031, 2)

        from optimiser.types import EventType as ET
        recon = [e for e in events if e[0] == ET.MODBUS_RECONNECTED]
        assert len(recon) == 0
