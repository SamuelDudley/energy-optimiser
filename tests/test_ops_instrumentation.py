"""Coverage for the ops-dashboard instrumentation hooks.

These events feed the /ops backend handlers — every assertion here is a
contract with the dashboard's read-side. If you change a payload field,
update the corresponding handler in `optimiser/api/handlers/ops.py` (or
its tests) at the same time.

Covered:
  * `api_call` context manager — success / non-2xx / exception paths
  * `MODBUS_READ_BATCH` emission from `SigenergyController.read_state`
    (happy path, reconnect-failed early return)
  * `ms` latency attached to `MODBUS_WRITE` / `MODBUS_ERROR`
  * `attempts` + `ms` attached to `MODBUS_RECONNECTED`
"""

from __future__ import annotations

from unittest.mock import patch

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
from optimiser.logging_utils import api_call
from optimiser.types import EventType

# ─────────────────────────────────────────────────────────────────
# api_call helper
# ─────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status_code = status
        self.is_success = 200 <= status < 300


class TestApiCallHelper:
    def test_success_path_emits_ok_true(self) -> None:
        with patch("optimiser.logging_utils.emit") as mock_emit:
            with api_call("amber", "prices_5min") as call:
                call.set_response(_FakeResponse(200))
        assert mock_emit.call_count == 1
        evt, payload = mock_emit.call_args.args
        assert evt == EventType.API_CALL
        assert payload["client"] == "amber"
        assert payload["op"] == "prices_5min"
        assert payload["http_status"] == 200
        assert payload["ok"] is True
        assert payload["ms"] >= 0
        # No exception → no `extra` block (unless caller put something there)
        assert "extra" not in payload

    def test_non_2xx_records_ok_false(self) -> None:
        with patch("optimiser.logging_utils.emit") as mock_emit:
            with api_call("amber", "prices_30min") as call:
                call.set_response(_FakeResponse(429))
        evt, payload = mock_emit.call_args.args
        assert evt == EventType.API_CALL
        assert payload["http_status"] == 429
        assert payload["ok"] is False

    def test_exception_records_class_name_and_reraises(self) -> None:
        with patch("optimiser.logging_utils.emit") as mock_emit, pytest.raises(ValueError):
            with api_call("shelly", "switch_set") as call:
                call.extra["device_id"] = "hw_hp"
                raise ValueError("boom")
        evt, payload = mock_emit.call_args.args
        assert evt == EventType.API_CALL
        assert payload["ok"] is False
        assert payload["http_status"] is None  # set_response never ran
        assert payload["extra"]["exception"] == "ValueError"
        assert payload["extra"]["device_id"] == "hw_hp"

    def test_extra_dict_propagates_to_payload(self) -> None:
        with patch("optimiser.logging_utils.emit") as mock_emit:
            with api_call("amber", "prices_5min") as call:
                call.set_response(_FakeResponse(200))
                call.extra["rl_remaining"] = 42
        payload = mock_emit.call_args.args[1]
        assert payload["extra"]["rl_remaining"] == 42


# ─────────────────────────────────────────────────────────────────
# MODBUS_READ_BATCH emission
# ─────────────────────────────────────────────────────────────────


def _controller() -> SigenergyController:
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
    """Patch helpers but preserve the `_reads_total` / `_reads_failed`
    bumps that the real helpers do, so the batch event reflects the
    real call count rather than zero."""

    async def _u16(address: int) -> int | None:
        ctrl._reads_total += 1
        v = u16.get(address)
        if v is None:
            ctrl._reads_failed += 1
        return v

    async def _s32(address: int) -> float | None:
        ctrl._reads_total += 1
        v = s32.get(address)
        if v is None:
            ctrl._reads_failed += 1
        return v

    monkeypatch.setattr(ctrl, "_read_input_u16", _u16)
    monkeypatch.setattr(ctrl, "_read_input_s32", _s32)


def _drain_batch_events(mock_emit) -> list[dict]:
    """Return the payload dicts of every MODBUS_READ_BATCH call."""
    return [
        call.args[1]
        for call in mock_emit.call_args_list
        if call.args and call.args[0] == EventType.MODBUS_READ_BATCH
    ]


class TestModbusReadBatch:
    async def test_emits_on_happy_path_with_real_register_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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
                REG_GRID_ACTIVE_POWER: 1.2,
                REG_PLANT_ESS_POWER: 0.0,
                REG_PLANT_PV_POWER: 3.0,
            },
        )

        # Stub the extended telemetry helper so we don't have to mock 30+
        # registers — the batch counts are dominated by it but the test
        # only needs to confirm the event fires with sensible fields.
        async def _empty_extended() -> dict:
            return {}

        monkeypatch.setattr(ctrl, "_read_extended_telemetry", _empty_extended)

        with patch("optimiser.clients.sigenergy.emit") as mock_emit:
            state = await ctrl.read_state()

        assert state is not None
        batches = _drain_batch_events(mock_emit)
        assert len(batches) == 1
        b = batches[0]
        # Six core reads happened (ems_mode, grid_sensor, grid_kw,
        # soc, battery_kw, pv_kw) and zero failed.
        assert b["reg_count"] == 6
        assert b["err_count"] == 0
        assert b["reconnected"] is False
        assert b["grid_sensor_ok"] is True
        assert b["ms"] >= 0

    async def test_emits_on_reconnect_failure_early_return(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = _controller()
        ctrl._connected = False  # force reconnect path

        async def _failed_reconnect() -> bool:
            return False

        monkeypatch.setattr(ctrl, "_attempt_reconnect", _failed_reconnect)

        with patch("optimiser.clients.sigenergy.emit") as mock_emit:
            state = await ctrl.read_state()

        # Early return → state is None but the batch event still fires
        # so the dashboard sees the failure mode (zero reads, no reconnect).
        assert state is None
        batches = _drain_batch_events(mock_emit)
        assert len(batches) == 1
        b = batches[0]
        assert b["reg_count"] == 0
        assert b["err_count"] == 0
        assert b["reconnected"] is False  # the attempt failed
        # First-call: _grid_sensor_status defaults to 0 → grid_sensor_ok False
        assert b["grid_sensor_ok"] is False


# ─────────────────────────────────────────────────────────────────
# Latency on MODBUS_WRITE / MODBUS_ERROR
# ─────────────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, error: bool) -> None:
        self._error = error

    def isError(self) -> bool:
        return self._error

    def __str__(self) -> str:
        return "MockError" if self._error else "MockOk"


class TestModbusWriteLatency:
    async def test_write_u16_success_carries_ms(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = _controller()

        async def _ok(*_a, **_kw):
            return _FakeResult(error=False)

        monkeypatch.setattr(ctrl._client, "write_register", _ok)

        with patch("optimiser.clients.sigenergy.emit") as mock_emit:
            ok = await ctrl._write_u16(40031, 2)

        assert ok is True
        write_events = [
            c.args[1]
            for c in mock_emit.call_args_list
            if c.args and c.args[0] == EventType.MODBUS_WRITE
        ]
        assert len(write_events) == 1
        payload = write_events[0]
        assert payload["register"] == 40031
        assert payload["value"] == 2
        assert "ms" in payload and payload["ms"] >= 0

    async def test_write_u16_isError_carries_ms_on_error_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = _controller()

        async def _err(*_a, **_kw):
            return _FakeResult(error=True)

        monkeypatch.setattr(ctrl._client, "write_register", _err)

        with patch("optimiser.clients.sigenergy.emit") as mock_emit:
            ok = await ctrl._write_u16(40031, 2)

        assert ok is False
        err_events = [
            c.args[1]
            for c in mock_emit.call_args_list
            if c.args and c.args[0] == EventType.MODBUS_ERROR
        ]
        assert len(err_events) == 1
        assert "ms" in err_events[0]


# ─────────────────────────────────────────────────────────────────
# MODBUS_RECONNECTED metadata
# ─────────────────────────────────────────────────────────────────


class TestReconnectMetadata:
    async def test_attempts_and_ms_on_rising_edge(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ctrl = _controller()
        ctrl._connected = False

        async def _connect() -> bool:
            return True

        monkeypatch.setattr(ctrl._client, "connect", _connect)

        with patch("optimiser.clients.sigenergy.emit") as mock_emit:
            ok = await ctrl._attempt_reconnect()

        assert ok is True
        rec_events = [
            c.args[1]
            for c in mock_emit.call_args_list
            if c.args and c.args[0] == EventType.MODBUS_RECONNECTED
        ]
        assert len(rec_events) == 1
        payload = rec_events[0]
        assert payload["attempts"] == 1
        assert payload["ms"] is not None
        assert payload["ms"] >= 0
