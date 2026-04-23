"""Sigenergy hybrid inverter Modbus TCP controller.

Reads system state and writes EMS control registers. Uses pymodbus
for async TCP communication.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pymodbus.client import AsyncModbusTcpClient

from ..config import BatteryConfig, SigenergyConfig
from ..logging_utils import emit
from ..time_utils import now_utc
from ..types import (
    BATTERY_ACTION_TO_EMS_MODE,
    BatteryAction,
    EventType,
    PlannerOutput,
    RemoteEMSControlMode,
    SystemState,
)

if TYPE_CHECKING:
    from ..lp.dispatch import LPDispatch

logger = logging.getLogger(__name__)


# ── Register Addresses ───────────────────────────────────────────
# All addresses verified against the Sigenergy HA integration's
# modbusregisterdefinitions.py. Plant-level registers (3003x range)
# aggregate across all inverters and are preferred over per-inverter
# registers (3059x range).

# Read (input registers, 30xxx)
REG_EMS_WORK_MODE = 30003  # plant_ems_work_mode, U16
REG_GRID_SENSOR_STATUS = 30004  # plant_grid_sensor_status, U16
REG_GRID_ACTIVE_POWER = 30005  # plant_grid_sensor_active_power, S32 gain=1000 kW
REG_PLANT_ESS_SOC = 30014  # plant_ess_soc, U16 gain=10 %
REG_PLANT_PV_POWER = 30035  # plant_sigen_photovoltaic_power, S32 gain=1000 kW
REG_PLANT_ESS_POWER = 30037  # plant_ess_power, S32 gain=1000 kW (>0 charging)

# Write (holding registers, 40xxx)
REG_REMOTE_EMS_ENABLE = 40029  # U16: 0=disabled, 1=enabled
# REG_PLANT_ACTIVE_POWER_CMD = 40001  # S32 gain=1000 kW
#
# Continuous active-power setpoint, takes effect in mode 0 (PCS_REMOTE_
# CONTROL). DELIBERATELY NOT USED: mode 0 holds whatever value we last
# wrote and doesn't react to actual house load. With residential load
# variability (kettles, ovens, aircon cycles), every transient leaks
# as unintended grid import or export — order ~$300/year wasted on
# load-tracking errors. We use load-following modes 3/4/6 instead, with
# the inverter handling sub-second load response within our magnitude
# cap. See `lp/dispatch.py` for the mapping logic.
REG_PLANT_ACTIVE_POWER_CMD = 40001  # documented but not in the write path
REG_REMOTE_EMS_CONTROL_MODE = 40031  # U16: RemoteEMSControlMode
REG_ESS_MAX_CHARGING_LIMIT = 40032  # U32, gain=1000, kW
REG_ESS_MAX_DISCHARGING_LIMIT = 40034  # U32, gain=1000, kW
REG_GRID_EXPORT_LIMIT = 40038  # U32, gain=1000, kW
REG_BACKUP_SOC = 40046  # U16, gain=10, % — backup reserve for blackouts
REG_CHARGE_CUTOFF_SOC = 40047  # U16, gain=10, %
REG_DISCHARGE_CUTOFF_SOC = 40048  # U16, gain=10, %

# Modbus function codes: input registers use FC=4, holding use FC=3/6/16
# pymodbus read_input_registers for 30xxx, read_holding_registers for 40xxx
# Offset: Modbus addresses are 0-based, so 30003 → address=30003 for input regs


class SigenergyController:
    """Async Modbus TCP controller for Sigenergy inverter."""

    def __init__(
        self,
        config: SigenergyConfig,
        battery_config: BatteryConfig,
    ) -> None:
        self._config = config
        self._battery = battery_config
        self._client = AsyncModbusTcpClient(
            host=config.host,
            port=config.port,
        )
        self._connected = False
        self._remote_ems_enabled = False
        # One-shot flags: only emit VALIDATION_WARNING on the rising edge
        # of each condition. Re-emit after the condition has cleared and
        # re-occurred. Prevents 1440 duplicate events/day from a persistent
        # grid-sensor fault.
        self._warned_grid_sensor_offline = False
        self._warned_absurd_derivation = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def grid_sensor_online(self) -> bool:
        return self._grid_sensor_status == 1

    async def connect(self) -> bool:
        """Connect to the inverter via Modbus TCP."""
        try:
            connected = await self._client.connect()
            self._connected = connected
            if connected:
                logger.info(
                    "Connected to Sigenergy at %s:%d",
                    self._config.host,
                    self._config.port,
                )
            return connected
        except Exception:
            logger.exception("Modbus connection failed")
            self._connected = False
            return False

    async def disconnect(self) -> None:
        self._client.close()
        self._connected = False

    # ── Read Methods ─────────────────────────────────────────────

    async def _read_input_u16(self, address: int) -> int | None:
        """Read a single U16 input register."""
        try:
            # Sigenergy uses absolute addressing
            result = await self._client.read_input_registers(
                address=address,
                count=1,
                device_id=self._config.slave_id,
            )
            if result.isError():
                logger.warning("Modbus read error at %d: %s", address, result)
                return None
            return result.registers[0]
        except Exception:
            logger.exception("Modbus read failed at %d", address)
            self._connected = False
            return None

    async def _read_input_s32(self, address: int) -> float | None:
        """Read an S32 input register pair (gain=1000 → kW)."""
        try:
            result = await self._client.read_input_registers(
                address=address,
                count=2,
                device_id=self._config.slave_id,
            )
            if result.isError():
                logger.warning("Modbus read error at %d: %s", address, result)
                return None
            # Combine two U16 into S32 (big-endian)
            raw = (result.registers[0] << 16) | result.registers[1]
            # Convert to signed
            if raw >= 0x80000000:
                raw -= 0x100000000
            return raw / 1000.0
        except Exception:
            logger.exception("Modbus read failed at %d", address)
            self._connected = False
            return None

    async def _read_holding_u16(self, address: int) -> int | None:
        """Read a single U16 holding register."""
        try:
            result = await self._client.read_holding_registers(
                address=address,
                count=1,
                device_id=self._config.slave_id,
            )
            if result.isError():
                return None
            return result.registers[0]
        except Exception:
            logger.exception("Modbus holding read failed at %d", address)
            self._connected = False
            return None

    async def read_state(
        self,
        outdoor_temp_c: float | None = None,
        occupied: bool = True,
    ) -> SystemState | None:
        """Read current inverter/battery/grid state."""
        if not self._connected:
            return None

        try:
            ems_mode = await self._read_input_u16(REG_EMS_WORK_MODE)
            self._grid_sensor_status = await self._read_input_u16(REG_GRID_SENSOR_STATUS) or 0
            grid_kw = await self._read_input_s32(REG_GRID_ACTIVE_POWER)
            soc_raw = await self._read_input_u16(REG_PLANT_ESS_SOC)
            battery_kw = await self._read_input_s32(REG_PLANT_ESS_POWER)
            pv_kw = await self._read_input_s32(REG_PLANT_PV_POWER)

            if soc_raw is None or grid_kw is None or battery_kw is None or pv_kw is None:
                return None

            soc_pct = soc_raw / 10.0

            # Null-over-wrong policy (see CLAUDE.md). Two paths produce a
            # nulled house_load / grid reading:
            #
            #  1. Grid sensor explicitly offline (status ≠ 1): the `grid_kw`
            #     register holds whatever the last valid reading was (or
            #     stale garbage). We can't derive house load without a
            #     trusted grid reading, and we don't want to poison the
            #     load profile with a guess.
            #
            #  2. Derivation is absurd (house_load < deadband negative):
            #     either a sign-convention error somewhere in the register
            #     chain (see KNOWN-ISSUES #3 — verify on first deploy) or
            #     a transient bad read from one of the three registers.
            #     Either way, null over wrong.
            grid_power_kw: float | None = grid_kw
            house_load_kw: float | None
            if self._grid_sensor_status != 1:
                grid_power_kw = None
                house_load_kw = None
                if not self._warned_grid_sensor_offline:
                    emit(
                        EventType.VALIDATION_WARNING,
                        {
                            "message": "Grid sensor offline (status != 1) — grid/house_load nulled",
                            "grid_sensor_status": self._grid_sensor_status,
                        },
                    )
                    self._warned_grid_sensor_offline = True
            else:
                # Condition cleared — re-arm for next transition
                self._warned_grid_sensor_offline = False
                # Energy balance: pv + grid_import = house_load + battery_charge
                # Therefore: house_load = pv + grid - battery
                derived = pv_kw + grid_kw - battery_kw
                if derived < -0.1:
                    if not self._warned_absurd_derivation:
                        emit(
                            EventType.VALIDATION_WARNING,
                            {
                                "message": (
                                    "Derived house_load is negative — suspect sign "
                                    "convention error or bad read; nulled"
                                ),
                                "derived_house_load_kw": derived,
                                "pv_kw": pv_kw,
                                "grid_kw": grid_kw,
                                "battery_kw": battery_kw,
                            },
                        )
                        self._warned_absurd_derivation = True
                    house_load_kw = None
                else:
                    self._warned_absurd_derivation = False
                    house_load_kw = derived

            return SystemState(
                timestamp=now_utc(),
                soc_pct=soc_pct,
                battery_power_kw=battery_kw,
                pv_power_kw=pv_kw,
                grid_power_kw=grid_power_kw,
                house_load_kw=house_load_kw,
                ems_mode=ems_mode or 0,
                outdoor_temp_c=outdoor_temp_c,
                occupied=occupied,
            )
        except Exception:
            logger.exception("Failed to read system state")
            self._connected = False
            return None

    # ── Write Methods ────────────────────────────────────────────

    async def _write_u16(self, address: int, value: int) -> bool:
        """Write a single U16 holding register."""
        try:
            result = await self._client.write_register(
                address=address,
                value=value,
                device_id=self._config.slave_id,
            )
            if result.isError():
                logger.warning("Modbus write error at %d: %s", address, result)
                emit(
                    EventType.MODBUS_ERROR,
                    {
                        "register": address,
                        "value": value,
                        "error": str(result),
                    },
                )
                return False
            emit(EventType.MODBUS_WRITE, {"register": address, "value": value})
            return True
        except Exception:
            logger.exception("Modbus write failed at %d", address)
            emit(EventType.MODBUS_ERROR, {"register": address, "value": value})
            self._connected = False
            return False

    async def _write_u32(self, address: int, value: int) -> bool:
        """Write a U32 as two consecutive holding registers."""
        try:
            hi = (value >> 16) & 0xFFFF
            lo = value & 0xFFFF
            result = await self._client.write_registers(
                address=address,
                values=[hi, lo],
                device_id=self._config.slave_id,
            )
            if result.isError():
                logger.warning("Modbus write error at %d: %s", address, result)
                emit(EventType.MODBUS_ERROR, {"register": address, "value": value})
                return False
            emit(EventType.MODBUS_WRITE, {"register": address, "value": value})
            return True
        except Exception:
            logger.exception("Modbus write failed at %d", address)
            emit(EventType.MODBUS_ERROR, {"register": address, "value": value})
            self._connected = False
            return False

    async def enable_remote_ems(self) -> bool:
        """Enable Remote EMS control mode."""
        ok = await self._write_u16(REG_REMOTE_EMS_ENABLE, 1)
        if ok:
            self._remote_ems_enabled = True
            logger.info("Remote EMS enabled")
        return ok

    async def disable_remote_ems(self) -> bool:
        """Disable Remote EMS — inverter reverts to local control."""
        ok = await self._write_u16(REG_REMOTE_EMS_ENABLE, 0)
        if ok:
            self._remote_ems_enabled = False
            logger.info("Remote EMS disabled")
        return ok

    async def assert_battery_soc_limits(self) -> bool:
        """Write hardware SOC limits from `BatteryConfig`.

        These three registers are honoured by the inverter regardless of
        EMS mode — which means they're the only way to stop mode 2 (or
        any other local mode) from charging past the ceiling or
        discharging past the floor. Writes are idempotent, so calling
        this from startup and periodically (watchdog-style) is safe.

        - 40046 backup SOC: reserve held for blackouts (never discharged
          to below this when grid is up).
        - 40047 charge cutoff SOC: hard upper bound on charging.
        - 40048 discharge cutoff SOC: hard lower bound on on-grid
          discharge (also a safety stop).
        """
        ceiling_raw = int(self._battery.soc_ceiling_pct * 10)
        floor_raw = int(self._battery.soc_floor_pct * 10)
        backup_raw = int(self._battery.backup_soc_pct * 10)
        logger.info(
            "Asserting battery SOC limits: ceiling=%.1f%% floor=%.1f%% backup=%.1f%%",
            self._battery.soc_ceiling_pct,
            self._battery.soc_floor_pct,
            self._battery.backup_soc_pct,
        )
        ok = True
        ok &= await self._write_u16(REG_CHARGE_CUTOFF_SOC, ceiling_raw)
        ok &= await self._write_u16(REG_DISCHARGE_CUTOFF_SOC, floor_raw)
        ok &= await self._write_u16(REG_BACKUP_SOC, backup_raw)
        return ok

    async def apply(self, command: PlannerOutput, tick_id: str | None = None) -> bool:
        """Apply a planner output to the inverter registers."""
        ems_mode = BATTERY_ACTION_TO_EMS_MODE[command.battery_action]

        # Ensure Remote EMS is enabled
        if not self._remote_ems_enabled:
            if not await self.enable_remote_ems():
                return False

        ok = True

        # Set control mode
        ok &= await self._write_u16(REG_REMOTE_EMS_CONTROL_MODE, ems_mode.value)

        # Set charge/discharge limits (gain=1000 → multiply kW by 1000)
        charge_raw = int(command.charge_limit_kw * 1000)
        discharge_raw = int(command.discharge_limit_kw * 1000)
        ok &= await self._write_u32(REG_ESS_MAX_CHARGING_LIMIT, charge_raw)
        ok &= await self._write_u32(REG_ESS_MAX_DISCHARGING_LIMIT, discharge_raw)

        # Set SOC cutoffs
        soc_ceiling = (
            int(command.target_soc * 10)
            if command.battery_action
            in (
                BatteryAction.CHARGE_GRID,
                BatteryAction.CHARGE_PV,
            )
            else int(self._battery.soc_ceiling_pct * 10)
        )

        soc_floor = int(self._battery.soc_floor_pct * 10)

        ok &= await self._write_u16(REG_CHARGE_CUTOFF_SOC, soc_ceiling)
        ok &= await self._write_u16(REG_DISCHARGE_CUTOFF_SOC, soc_floor)

        if ok:
            logger.info(
                "Applied: %s, charge=%.1fkW, discharge=%.1fkW, target_soc=%.0f%%",
                command.battery_action.value,
                command.charge_limit_kw,
                command.discharge_limit_kw,
                command.target_soc,
            )

        return ok

    async def set_export_limit_kw(self, limit_kw: float) -> bool:
        """Set grid export limit (register 40038). 0 = block all export."""
        raw = int(limit_kw * 1000)
        logger.info("Setting grid export limit to %.1f kW", limit_kw)
        return await self._write_u32(REG_GRID_EXPORT_LIMIT, raw)

    async def set_fallback(
        self,
        export_price_ckwh: float | None = None,
        *,
        block_export: bool = False,
    ) -> bool:
        """Set Maximum Self Consumption mode + price-aware export cap.

        Writes both registers. The LP's last command may have left the export
        limit pinned to 0 (during a charge slot) or at DNSP max; either way
        the fallback should re-assert an explicit safe value so it doesn't
        inherit a stale command.

        Export cap selection:
        - `block_export=True` → 0 kW. Used by the watcher on verify
          deviation: we've lost control of the inverter, so don't push
          power to grid until trust is restored.
        - `export_price_ckwh < 0` (we would pay to export) → 0 kW (curtail).
        - Otherwise (price ≥ 0 or price unknown) → `battery.export_limit_kw`
          (DNSP max). Price-unknown defaulting to DNSP is the revenue-
          maximising choice for the common case; the edge case where we
          lose money during a price-negative window with no price cache is
          bounded by the watchdog (90 s) or the next LP tick (60 s).
        """
        if block_export:
            export_cap_kw = 0.0
            export_reason = "block_export (verify deviation)"
        elif export_price_ckwh is not None and export_price_ckwh < 0:
            export_cap_kw = 0.0
            export_reason = f"price={export_price_ckwh:.2f}c/kWh"
        else:
            export_cap_kw = self._battery.export_limit_kw
            export_reason = (
                f"price={export_price_ckwh:.2f}c/kWh"
                if export_price_ckwh is not None else "price=unknown"
            )
        logger.info(
            "Setting fallback: Maximum Self Consumption + export=%.1fkW (%s)",
            export_cap_kw, export_reason,
        )
        if not self._remote_ems_enabled:
            if not await self.enable_remote_ems():
                return False
        mode_ok = await self._write_u16(
            REG_REMOTE_EMS_CONTROL_MODE,
            RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION.value,
        )
        export_ok = await self._write_u32(
            REG_GRID_EXPORT_LIMIT,
            int(export_cap_kw * 1000),
        )
        return mode_ok and export_ok

    # ── LP dispatch path ──────────────────────────────────────────
    #
    # The LP outputs a continuous signed `battery_kw` for slot 0 (+ charge,
    # − discharge). We map that to one of the inverter's load-following
    # modes (3/4/6) plus a magnitude cap, rather than mode 0 + a fixed
    # plant-level setpoint. Reason: mode 0 holds whatever number we last
    # wrote and doesn't react to actual house load — every load transient
    # leaks as unintended grid import or export. Modes 3/4/6 let the
    # inverter handle real-time load following within our magnitude cap;
    # the LP supplies intent, the inverter supplies sub-second response.
    #
    # Mode mapping:
    #   |battery_kw| < deadband → mode 2 (SELF_CONSUME), inverter idles
    #   battery_kw > 0, mostly grid-fed → mode 3 (CHARGE_GRID_FIRST), cap 40032
    #   battery_kw > 0, mostly PV-fed   → mode 4 (CHARGE_PV_FIRST),   cap 40032
    #   battery_kw < 0                  → mode 6 (DISCHARGE_ESS_FIRST), cap 40034
    # Mode 5 (DISCHARGE_PV_FIRST) is intentionally not used: it lets the
    # inverter skip battery discharge entirely if PV happens to cover house
    # load, which is the opposite of the LP's intent when it asks to
    # discharge.

    async def apply_lp_dispatch(self, dispatch: LPDispatch) -> bool:
        """Apply the LP's slot-0 decision via mode + cap registers.

        **Write order: cap first, then mode.** If any write fails partway,
        the inverter must never be in (new mode, stale cap).

        - Cap write fails → mode still unchanged, so the half-written cap
          isn't in force yet. Fallback will overwrite mode to SELF_CONSUME,
          which doesn't consult the cap registers.
        - Cap succeeds, mode write fails → cap has been updated but mode
          is still the old one. The old mode either ignores the cap we
          touched (e.g. was SELF_CONSUME, or opposite direction), or was
          the same direction we're now updating — either way, never
          "new direction with stale cap", which was the S3 hazard.

        Mode write is still unconditional every tick (even when unchanged)
        as a defensive re-assertion against the inverter quietly reverting
        mode after a comms drop.

        Returns True if all writes succeeded. On False the caller must
        trigger fallback — partial application is never safe.
        """
        if not self._remote_ems_enabled:
            if not await self.enable_remote_ems():
                return False

        # Write the relevant cap FIRST. If it fails, return early WITHOUT
        # touching the mode register — writing mode on top of a failed cap
        # write would leave the inverter in (new direction, stale cap),
        # which is the very state this ordering is designed to prevent.
        if dispatch.mode in (
            RemoteEMSControlMode.COMMAND_CHARGING_GRID_FIRST,
            RemoteEMSControlMode.COMMAND_CHARGING_PV_FIRST,
        ):
            cap_raw = max(0, int(round(dispatch.cap_kw * 1000)))
            if not await self._write_u32(REG_ESS_MAX_CHARGING_LIMIT, cap_raw):
                return False
        elif dispatch.mode == RemoteEMSControlMode.COMMAND_DISCHARGING_ESS_FIRST:
            cap_raw = max(0, int(round(dispatch.cap_kw * 1000)))
            if not await self._write_u32(REG_ESS_MAX_DISCHARGING_LIMIT, cap_raw):
                return False
        # SELF_CONSUME → no cap to write

        # THEN assert the mode register. Always written (even if unchanged)
        # so a mode reset from a brief comms drop gets re-asserted.
        if not await self._write_u16(
            REG_REMOTE_EMS_CONTROL_MODE,
            dispatch.mode.value,
        ):
            return False

        logger.info(
            "Applied LP dispatch: mode=%s cap=%.2fkW intent=%+.2fkW",
            dispatch.mode.name,
            dispatch.cap_kw,
            dispatch.signed_intent_kw,
        )
        return True

    async def read_battery_power_kw(self) -> float | None:
        """Fast-path read of register 30037 (ESS power) only.

        Returns signed kW: positive = charging, negative = discharging.
        Same sign convention as the LP's `slot_0.battery_kw`.

        Used by the verification watcher loop, which polls more frequently
        than the planning tick. Returns None on read failure.
        """
        raw = await self._read_input_s32(REG_PLANT_ESS_POWER)
        if raw is None:
            return None
        return raw / 1000.0
