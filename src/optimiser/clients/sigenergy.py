"""Sigenergy hybrid inverter Modbus TCP controller.

Reads system state and writes EMS control registers. Uses pymodbus
for async TCP communication.
"""

from __future__ import annotations

import asyncio
import logging
from time import perf_counter
from typing import TYPE_CHECKING

from pymodbus.client import AsyncModbusTcpClient

from ..config import BatteryConfig, SigenergyConfig
from ..logging_utils import emit
from ..time_utils import now_utc
from ..types import (
    EventType,
    PVProbeResult,
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

# Dynamic BMS-reported charge/discharge headroom. Drops below nameplate
# when the battery is cold, near SOC floor/ceiling, or thermally derated —
# the observable thermal-derate signal.
REG_ESS_AVAIL_MAX_CHARGING_POWER = 30047  # U32 gain=1000 kW
REG_ESS_AVAIL_MAX_DISCHARGING_POWER = 30049  # U32 gain=1000 kW

# Lifetime energy counters: U64, gain=100 → kWh. Stored as DOUBLE in
# DuckDB because REAL (float32) loses precision at ~10^7 kWh.
REG_LIFETIME_PV_KWH = 30088
REG_LIFETIME_LOAD_KWH = 30094
REG_LIFETIME_CHARGE_KWH = 30200
REG_LIFETIME_DISCHARGE_KWH = 30204
REG_LIFETIME_IMPORT_KWH = 30216
REG_LIFETIME_EXPORT_KWH = 30220

# Per-inverter health block. Single-inverter install, so inverter_* and
# plant_* are effectively identical for these fields.
REG_INVERTER_RUNNING_STATE = 30578  # U16 (Appendix 1)
REG_INVERTER_ESS_SOH = 30602  # U16 gain=10 %
REG_INVERTER_ESS_CELL_TEMP_AVG = 30603  # S16 gain=10 °C
REG_INVERTER_ESS_CELL_VOLT_AVG = 30604  # U16 gain=1000 V
REG_INVERTER_ALARM1 = 30605  # U16 (Appendix 2)
REG_INVERTER_ALARM2 = 30606  # U16 (Appendix 3)
REG_INVERTER_ALARM3 = 30607  # U16 (Appendix 4)
REG_INVERTER_ALARM4 = 30608  # U16 (Appendix 5)
REG_INVERTER_ALARM5 = 30609  # U16 (Appendix 11)
REG_INVERTER_ESS_CELL_TEMP_MAX = 30620  # S16 gain=10
REG_INVERTER_ESS_CELL_TEMP_MIN = 30621  # S16 gain=10

# PCS (power conversion system) internal temperature — relevant for
# summer derating modelling.
REG_INVERTER_PCS_TEMP = 31003  # S16 gain=10 °C

# Grid AC quality.
REG_INVERTER_GRID_FREQ = 31002  # U16 gain=100 Hz
REG_INVERTER_PHASE_A_VOLT = 31011  # U32 gain=100 V
REG_INVERTER_PHASE_B_VOLT = 31013  # U32 gain=100 V
REG_INVERTER_PHASE_C_VOLT = 31015  # U32 gain=100 V

# Per-MPPT string V/I. Four strings captured unconditionally; null reads
# are expected on installs with fewer strings wired.
REG_INVERTER_PV1_VOLTAGE = 31027  # S16 gain=10 V
REG_INVERTER_PV1_CURRENT = 31028  # S16 gain=100 A
REG_INVERTER_PV2_VOLTAGE = 31029
REG_INVERTER_PV2_CURRENT = 31030
REG_INVERTER_PV3_VOLTAGE = 31031
REG_INVERTER_PV3_CURRENT = 31032
REG_INVERTER_PV4_VOLTAGE = 31033
REG_INVERTER_PV4_CURRENT = 31034

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


# ── Mode-2 adaptive dispatch tunables ────────────────────────────
# See `_apply_mode2_adaptive_charge`. Phase A uncaps 40032 so the cascade
# soaks all surplus PV and we can read true MPP from telemetry; Phase B
# trims 40032 so the battery + export cascade splits rather than the
# battery saturating before any export flows. Validated by
# probe_two_phase.py and probe_no_cutoff.py.
MODE2_PROBE_SECONDS: float = 5.0
# Trim safety floor: never trim below the LP's intended charge rate even
# if measured surplus says we could. Protects against transient PV droop
# during the Phase-A window collapsing the trim toward zero.
MODE2_TRIM_FLOOR_HEADROOM_KW: float = 0.5


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
        # Set on every successful read of REG_GRID_SENSOR_STATUS in
        # read_state. Initialised here so MODBUS_READ_BATCH can be
        # emitted in the finally block even on first-call reconnect
        # failures (no attribute-error from the unread state).
        self._grid_sensor_status: int = 0
        # Per-call counters for MODBUS_READ_BATCH. Each `_read_*` helper
        # increments `_reads_total`; failed reads (isError or exception)
        # increment `_reads_failed`. read_state snapshots both at entry
        # and computes deltas at exit so the batch event reflects only
        # this tick's reads.
        self._reads_total = 0
        self._reads_failed = 0
        # Reconnect telemetry. _reconnect_attempts is monotonic since
        # process start so the dashboard can compute attempts/hr from
        # any window of MODBUS_RECONNECTED events.
        self._reconnect_attempts = 0
        self._last_reconnect_ms: float | None = None

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
            if connected:
                self._mark_connected()
                logger.info(
                    "Connected to Sigenergy at %s:%d",
                    self._config.host,
                    self._config.port,
                )
            else:
                self._connected = False
            return connected
        except Exception:
            logger.exception("Modbus connection failed")
            self._connected = False
            return False

    async def disconnect(self) -> None:
        self._client.close()
        self._connected = False

    def _mark_connected(self) -> None:
        """Flip `_connected` True; emit MODBUS_RECONNECTED on rising edge.

        Called after any successful Modbus operation (read_state's reconnect,
        successful writes that recover from a prior drop). The rising-edge
        guard means steady-state ticks don't spam the event stream.

        Payload includes ``attempts`` (cumulative since process start) and
        ``ms`` (last reconnect latency) so a flapping link is visible as a
        rising attempt count even though each rising-edge emit is unique.
        """
        if not self._connected:
            logger.info("Modbus connection restored")
            emit(
                EventType.MODBUS_RECONNECTED,
                {
                    "host": self._config.host,
                    "port": self._config.port,
                    "attempts": self._reconnect_attempts,
                    "ms": self._last_reconnect_ms,
                },
            )
        self._connected = True

    async def _attempt_reconnect(self) -> bool:
        """Quiet reconnect attempt — used by `read_state` after a drop.

        Distinct from `connect()`:
          * Logs WARNING (no traceback) on failure to avoid flooding the log
            during sustained outages — every tick (60s) re-attempts, and a
            multi-hour Wi-Fi/inverter outage would otherwise produce dozens
            of stack traces.
          * Emits MODBUS_RECONNECTED via `_mark_connected` on success so the
            recovery is queryable in the NDJSON event stream. Latency_ms +
            cumulative attempt count are attached so the ops dashboard can
            distinguish a one-shot blip from a reconnect storm.

        pymodbus's `AsyncModbusTcpClient.connect()` is idempotent — fast when
        already connected at the TCP layer, fails fast on host-unreachable.
        """
        self._reconnect_attempts += 1
        t0 = perf_counter()
        try:
            connected = await self._client.connect()
        except Exception as exc:
            logger.warning("Modbus reconnect failed: %s", exc)
            self._connected = False
            return False
        if connected:
            self._last_reconnect_ms = round((perf_counter() - t0) * 1000.0, 2)
            self._mark_connected()
            return True
        # connect() returned False (host unreachable) — quiet, will retry next tick.
        self._connected = False
        return False

    # ── Read Methods ─────────────────────────────────────────────

    async def _read_input_u16(self, address: int) -> int | None:
        """Read a single U16 input register."""
        self._reads_total += 1
        try:
            # Sigenergy uses absolute addressing
            result = await self._client.read_input_registers(
                address=address,
                count=1,
                device_id=self._config.slave_id,
            )
            if result.isError():
                logger.warning("Modbus read error at %d: %s", address, result)
                self._reads_failed += 1
                return None
            return result.registers[0]
        except Exception:
            logger.exception("Modbus read failed at %d", address)
            self._connected = False
            self._reads_failed += 1
            return None

    async def _read_input_s32(self, address: int) -> float | None:
        """Read an S32 input register pair (gain=1000 → kW)."""
        self._reads_total += 1
        try:
            result = await self._client.read_input_registers(
                address=address,
                count=2,
                device_id=self._config.slave_id,
            )
            if result.isError():
                logger.warning("Modbus read error at %d: %s", address, result)
                self._reads_failed += 1
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
            self._reads_failed += 1
            return None

    async def _read_holding_u16(self, address: int) -> int | None:
        """Read a single U16 holding register."""
        self._reads_total += 1
        try:
            result = await self._client.read_holding_registers(
                address=address,
                count=1,
                device_id=self._config.slave_id,
            )
            if result.isError():
                self._reads_failed += 1
                return None
            return result.registers[0]
        except Exception:
            logger.exception("Modbus holding read failed at %d", address)
            self._connected = False
            self._reads_failed += 1
            return None

    # The helpers below are used exclusively for extended observational
    # reads (temps, alarms, lifetime counters, etc.). They are BEST-EFFORT:
    # a single-register failure must not mark the whole controller as
    # disconnected, because the core reads for the same tick may have
    # already succeeded. Contrast with _read_input_u16/_read_input_s32
    # used on critical-path fields, which do mark the controller
    # disconnected on exception — the correct behaviour there, since
    # those reads are the connection's liveness signal.

    async def _read_input_s16(
        self,
        address: int,
        gain: float = 1.0,
        slave_id: int | None = None,
    ) -> float | None:
        """Read a single S16 input register and scale by gain.

        gain=10 is the Sigenergy convention for tenths-of-°C fields;
        gain=100 is used for current (centi-amps). `slave_id` defaults
        to the plant slave; pass the inverter slave explicitly for
        per-inverter registers (305xx-306xx, 310xx range).
        """
        self._reads_total += 1
        try:
            result = await self._client.read_input_registers(
                address=address,
                count=1,
                device_id=slave_id if slave_id is not None else self._config.slave_id,
            )
            if result.isError():
                self._reads_failed += 1
                return None
            raw = result.registers[0]
            if raw >= 0x8000:
                raw -= 0x10000
            return raw / gain
        except Exception:
            logger.debug("Best-effort S16 read failed at %d", address)
            self._reads_failed += 1
            return None

    async def _read_input_u32(
        self,
        address: int,
        gain: float = 1.0,
        slave_id: int | None = None,
    ) -> float | None:
        """Read a U32 input register pair and scale by gain.

        Sigenergy's convention for "this field is not applicable" (e.g.
        phase B/C on a single-phase install, or a register your firmware
        doesn't populate) is to return 0xFFFFFFFF. Treat that as None
        so the validation layer doesn't see 42,949,672.95 as an outlier.
        """
        self._reads_total += 1
        try:
            result = await self._client.read_input_registers(
                address=address,
                count=2,
                device_id=slave_id if slave_id is not None else self._config.slave_id,
            )
            if result.isError():
                self._reads_failed += 1
                return None
            raw = (result.registers[0] << 16) | result.registers[1]
            if raw == 0xFFFFFFFF:
                return None
            return raw / gain
        except Exception:
            logger.debug("Best-effort U32 read failed at %d", address)
            self._reads_failed += 1
            return None

    async def _read_input_u64(
        self,
        address: int,
        gain: float = 1.0,
        slave_id: int | None = None,
    ) -> float | None:
        """Read a U64 input register quad (big-endian word order).

        Sentinel 0xFFFFFFFFFFFFFFFF means "not applicable" per Sigenergy
        convention — returned as None rather than a ~1.8e17 outlier.
        """
        self._reads_total += 1
        try:
            result = await self._client.read_input_registers(
                address=address,
                count=4,
                device_id=slave_id if slave_id is not None else self._config.slave_id,
            )
            if result.isError():
                self._reads_failed += 1
                return None
            r = result.registers
            raw = (r[0] << 48) | (r[1] << 32) | (r[2] << 16) | r[3]
            if raw == 0xFFFFFFFFFFFFFFFF:
                return None
            return raw / gain
        except Exception:
            logger.debug("Best-effort U64 read failed at %d", address)
            self._reads_failed += 1
            return None

    async def _read_input_u16_scaled(
        self,
        address: int,
        gain: float = 1.0,
        slave_id: int | None = None,
    ) -> float | None:
        """Read an unsigned U16 and scale (e.g. SOH=gain 10, cell V=gain 1000).

        Uses a local try/except rather than delegating to _read_input_u16
        so a failure doesn't mark the controller disconnected.
        """
        self._reads_total += 1
        try:
            result = await self._client.read_input_registers(
                address=address,
                count=1,
                device_id=slave_id if slave_id is not None else self._config.slave_id,
            )
            if result.isError():
                self._reads_failed += 1
                return None
            return result.registers[0] / gain
        except Exception:
            logger.debug("Best-effort U16 read failed at %d", address)
            self._reads_failed += 1
            return None

    async def _read_input_u16_best_effort(
        self,
        address: int,
        slave_id: int | None = None,
    ) -> int | None:
        """Read a raw U16 input register without flipping connection state."""
        self._reads_total += 1
        try:
            result = await self._client.read_input_registers(
                address=address,
                count=1,
                device_id=slave_id if slave_id is not None else self._config.slave_id,
            )
            if result.isError():
                self._reads_failed += 1
                return None
            return result.registers[0]
        except Exception:
            logger.debug("Best-effort U16 read failed at %d", address)
            self._reads_failed += 1
            return None

    async def _read_holding_u16_best_effort(self, address: int) -> int | None:
        """Read a holding register without flipping connection state on failure."""
        self._reads_total += 1
        try:
            result = await self._client.read_holding_registers(
                address=address,
                count=1,
                device_id=self._config.slave_id,
            )
            if result.isError():
                self._reads_failed += 1
                return None
            return result.registers[0]
        except Exception:
            logger.debug("Best-effort holding read failed at %d", address)
            self._reads_failed += 1
            return None

    async def read_state(
        self,
        outdoor_temp_c: float | None = None,
        occupied: bool = True,
    ) -> SystemState | None:
        """Read current inverter/battery/grid state.

        Emits a single ``MODBUS_READ_BATCH`` event on every invocation —
        success, partial-success, or hard-failure — capturing wall-clock
        ms, register count, error count, whether a reconnect happened
        this tick, and grid-sensor liveness. The ops dashboard reads
        these to produce the read latency p95 panel and the per-tick
        error-rate signal.
        """
        t0 = perf_counter()
        reads_before = self._reads_total
        errs_before = self._reads_failed
        reconnected = False
        try:
            if not self._connected:
                # Sticky-disconnect recovery. A prior read raised
                # ConnectionException (Wi-Fi outage, inverter reboot, router
                # cycle) and flipped `_connected = False`. Without an explicit
                # reconnect path the flag stays False forever — pymodbus's
                # internal auto-reconnect heals the socket transparently, but
                # nothing here knew to re-arm. Attempt a reconnect; on failure
                # return None and let the next tick retry.
                if not await self._attempt_reconnect():
                    return None
                reconnected = True

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

                extended = await self._read_extended_telemetry()

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
                    **extended,
                )
            except Exception:
                logger.exception("Failed to read system state")
                self._connected = False
                return None
        finally:
            ms = (perf_counter() - t0) * 1000.0
            emit(
                EventType.MODBUS_READ_BATCH,
                {
                    "ms": round(ms, 2),
                    "reg_count": self._reads_total - reads_before,
                    "err_count": self._reads_failed - errs_before,
                    "reconnected": reconnected,
                    "grid_sensor_ok": self._grid_sensor_status == 1,
                },
            )

    async def _read_extended_telemetry(self) -> dict[str, float | int | None]:
        """Read the ~35 extended observational registers.

        Purely observational: these don't feed the LP or the control path.
        Every field is independently allowed to fail — a bad read surfaces
        as a NULL in the DB rather than failing the tick. Returns a dict
        that can be splatted into ``SystemState(**extended)``.

        The Sigenergy gateway exposes plant-level aggregates at one
        slave ID and per-inverter registers at another. All 305xx-306xx
        and 310xx reads below target the per-inverter slave; 300xx and
        lifetime counters target the plant slave.
        """
        inv = self._config.inverter_slave_id

        # Battery health & thermal (inverter slave)
        soh_pct = await self._read_input_u16_scaled(REG_INVERTER_ESS_SOH, gain=10, slave_id=inv)
        cell_temp_avg_c = await self._read_input_s16(
            REG_INVERTER_ESS_CELL_TEMP_AVG, gain=10, slave_id=inv
        )
        cell_temp_max_c = await self._read_input_s16(
            REG_INVERTER_ESS_CELL_TEMP_MAX, gain=10, slave_id=inv
        )
        cell_temp_min_c = await self._read_input_s16(
            REG_INVERTER_ESS_CELL_TEMP_MIN, gain=10, slave_id=inv
        )
        cell_volt_avg_v = await self._read_input_u16_scaled(
            REG_INVERTER_ESS_CELL_VOLT_AVG, gain=1000, slave_id=inv
        )
        pcs_temp_c = await self._read_input_s16(REG_INVERTER_PCS_TEMP, gain=10, slave_id=inv)

        # Dynamic power constraints (plant slave)
        available_charge_kw = await self._read_input_u32(
            REG_ESS_AVAIL_MAX_CHARGING_POWER, gain=1000
        )
        available_discharge_kw = await self._read_input_u32(
            REG_ESS_AVAIL_MAX_DISCHARGING_POWER, gain=1000
        )

        # Alarms + running state (inverter slave). These use the
        # best-effort U16 variant rather than the strict helper above:
        # the strict helper emits a warning per tick per register on
        # unsupported firmwares, flooding the log.
        running_state = await self._read_input_u16_best_effort(
            REG_INVERTER_RUNNING_STATE, slave_id=inv
        )
        alarm1 = await self._read_input_u16_best_effort(REG_INVERTER_ALARM1, slave_id=inv)
        alarm2 = await self._read_input_u16_best_effort(REG_INVERTER_ALARM2, slave_id=inv)
        alarm3 = await self._read_input_u16_best_effort(REG_INVERTER_ALARM3, slave_id=inv)
        alarm4 = await self._read_input_u16_best_effort(REG_INVERTER_ALARM4, slave_id=inv)
        alarm5 = await self._read_input_u16_best_effort(REG_INVERTER_ALARM5, slave_id=inv)

        # Lifetime energy counters (plant slave)
        lifetime_pv_kwh = await self._read_input_u64(REG_LIFETIME_PV_KWH, gain=100)
        lifetime_load_kwh = await self._read_input_u64(REG_LIFETIME_LOAD_KWH, gain=100)
        lifetime_charge_kwh = await self._read_input_u64(REG_LIFETIME_CHARGE_KWH, gain=100)
        lifetime_discharge_kwh = await self._read_input_u64(REG_LIFETIME_DISCHARGE_KWH, gain=100)
        lifetime_import_kwh = await self._read_input_u64(REG_LIFETIME_IMPORT_KWH, gain=100)
        lifetime_export_kwh = await self._read_input_u64(REG_LIFETIME_EXPORT_KWH, gain=100)

        # Per-MPPT strings (inverter slave)
        mppt1_voltage_v = await self._read_input_s16(
            REG_INVERTER_PV1_VOLTAGE, gain=10, slave_id=inv
        )
        mppt1_current_a = await self._read_input_s16(
            REG_INVERTER_PV1_CURRENT, gain=100, slave_id=inv
        )
        mppt2_voltage_v = await self._read_input_s16(
            REG_INVERTER_PV2_VOLTAGE, gain=10, slave_id=inv
        )
        mppt2_current_a = await self._read_input_s16(
            REG_INVERTER_PV2_CURRENT, gain=100, slave_id=inv
        )
        mppt3_voltage_v = await self._read_input_s16(
            REG_INVERTER_PV3_VOLTAGE, gain=10, slave_id=inv
        )
        mppt3_current_a = await self._read_input_s16(
            REG_INVERTER_PV3_CURRENT, gain=100, slave_id=inv
        )
        mppt4_voltage_v = await self._read_input_s16(
            REG_INVERTER_PV4_VOLTAGE, gain=10, slave_id=inv
        )
        mppt4_current_a = await self._read_input_s16(
            REG_INVERTER_PV4_CURRENT, gain=100, slave_id=inv
        )

        # Grid AC quality (inverter slave)
        grid_freq_hz = await self._read_input_u16_scaled(
            REG_INVERTER_GRID_FREQ, gain=100, slave_id=inv
        )
        phase_a_voltage_v = await self._read_input_u32(
            REG_INVERTER_PHASE_A_VOLT, gain=100, slave_id=inv
        )
        phase_b_voltage_v = await self._read_input_u32(
            REG_INVERTER_PHASE_B_VOLT, gain=100, slave_id=inv
        )
        phase_c_voltage_v = await self._read_input_u32(
            REG_INVERTER_PHASE_C_VOLT, gain=100, slave_id=inv
        )

        # Readback of holding reg 40031 (plant slave) — what the inverter
        # currently has as its commanded remote EMS mode. Closes the
        # loop against our writes; diverging from our last write is a
        # red flag.
        remote_ems_mode = await self._read_holding_u16_best_effort(REG_REMOTE_EMS_CONTROL_MODE)

        return {
            "soh_pct": soh_pct,
            "cell_temp_avg_c": cell_temp_avg_c,
            "cell_temp_max_c": cell_temp_max_c,
            "cell_temp_min_c": cell_temp_min_c,
            "cell_volt_avg_v": cell_volt_avg_v,
            "pcs_temp_c": pcs_temp_c,
            "available_charge_kw": available_charge_kw,
            "available_discharge_kw": available_discharge_kw,
            "running_state": running_state,
            "alarm1": alarm1,
            "alarm2": alarm2,
            "alarm3": alarm3,
            "alarm4": alarm4,
            "alarm5": alarm5,
            "lifetime_pv_kwh": lifetime_pv_kwh,
            "lifetime_load_kwh": lifetime_load_kwh,
            "lifetime_charge_kwh": lifetime_charge_kwh,
            "lifetime_discharge_kwh": lifetime_discharge_kwh,
            "lifetime_import_kwh": lifetime_import_kwh,
            "lifetime_export_kwh": lifetime_export_kwh,
            "mppt1_voltage_v": mppt1_voltage_v,
            "mppt1_current_a": mppt1_current_a,
            "mppt2_voltage_v": mppt2_voltage_v,
            "mppt2_current_a": mppt2_current_a,
            "mppt3_voltage_v": mppt3_voltage_v,
            "mppt3_current_a": mppt3_current_a,
            "mppt4_voltage_v": mppt4_voltage_v,
            "mppt4_current_a": mppt4_current_a,
            "grid_freq_hz": grid_freq_hz,
            "phase_a_voltage_v": phase_a_voltage_v,
            "phase_b_voltage_v": phase_b_voltage_v,
            "phase_c_voltage_v": phase_c_voltage_v,
            "remote_ems_mode": remote_ems_mode,
        }

    # ── Write Methods ────────────────────────────────────────────

    async def _write_u16(self, address: int, value: int) -> bool:
        """Write a single U16 holding register."""
        t0 = perf_counter()
        try:
            result = await self._client.write_register(
                address=address,
                value=value,
                device_id=self._config.slave_id,
            )
            ms = round((perf_counter() - t0) * 1000.0, 2)
            if result.isError():
                logger.warning("Modbus write error at %d: %s", address, result)
                emit(
                    EventType.MODBUS_ERROR,
                    {
                        "register": address,
                        "value": value,
                        "error": str(result),
                        "ms": ms,
                    },
                )
                return False
            # Successful write: TCP socket carried a Modbus exchange end-to-end.
            # If `_connected` was False (sticky drop with read path stuck), this
            # re-arms it so the next tick's read_state proceeds without a
            # separate reconnect round-trip.
            self._mark_connected()
            emit(
                EventType.MODBUS_WRITE,
                {"register": address, "value": value, "ms": ms},
            )
            return True
        except Exception:
            ms = round((perf_counter() - t0) * 1000.0, 2)
            logger.exception("Modbus write failed at %d", address)
            emit(
                EventType.MODBUS_ERROR,
                {"register": address, "value": value, "ms": ms},
            )
            self._connected = False
            return False

    async def _write_u32(self, address: int, value: int) -> bool:
        """Write a U32 as two consecutive holding registers."""
        t0 = perf_counter()
        try:
            hi = (value >> 16) & 0xFFFF
            lo = value & 0xFFFF
            result = await self._client.write_registers(
                address=address,
                values=[hi, lo],
                device_id=self._config.slave_id,
            )
            ms = round((perf_counter() - t0) * 1000.0, 2)
            if result.isError():
                logger.warning("Modbus write error at %d: %s", address, result)
                emit(
                    EventType.MODBUS_ERROR,
                    {"register": address, "value": value, "ms": ms},
                )
                return False
            self._mark_connected()
            emit(
                EventType.MODBUS_WRITE,
                {"register": address, "value": value, "ms": ms},
            )
            return True
        except Exception:
            ms = round((perf_counter() - t0) * 1000.0, 2)
            logger.exception("Modbus write failed at %d", address)
            emit(
                EventType.MODBUS_ERROR,
                {"register": address, "value": value, "ms": ms},
            )
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
        """Write all three hardware SOC limits from `BatteryConfig`.

        These registers are honoured by the inverter regardless of EMS
        mode — which means they're the only way to stop mode 2 (or any
        other local mode) from charging past the ceiling or discharging
        past the floor. Writes are idempotent; called at service
        startup to assert an initial safe state.

        - 40046 backup SOC: reserve held for blackouts (never discharged
          to below this when grid is up).
        - 40047 charge cutoff SOC: hard upper bound on charging.
        - 40048 discharge cutoff SOC: hard lower bound on on-grid
          discharge (also a safety stop).

        For the hourly re-assertion loop, use
        `assert_discharge_soc_limits()` instead. It skips 40047 because
        the charge cutoff is set ONCE at startup here and the tick path
        never rewrites it (see SPEC-ENERGY-01.md §5.4).
        """
        ceiling_raw = int(self._battery.soc_ceiling_pct * 10)
        discharge_cutoff_raw = int(self._battery.discharge_cutoff_pct * 10)
        backup_raw = int(self._battery.backup_soc_pct * 10)
        logger.info(
            "Asserting battery SOC limits: ceiling=%.1f%% discharge_cutoff=%.1f%% backup=%.1f%%",
            self._battery.soc_ceiling_pct,
            self._battery.discharge_cutoff_pct,
            self._battery.backup_soc_pct,
        )
        ok = True
        ok &= await self._write_u16(REG_CHARGE_CUTOFF_SOC, ceiling_raw)
        ok &= await self._write_u16(REG_DISCHARGE_CUTOFF_SOC, discharge_cutoff_raw)
        ok &= await self._write_u16(REG_BACKUP_SOC, backup_raw)
        return ok

    async def assert_discharge_soc_limits(self) -> bool:
        """Re-assert the two SOC limits that are NOT tick-managed.

        Writes 40046 (backup SOC) and 40048 (discharge cutoff) only.
        Reg 40047 (charge cutoff) is deliberately skipped: it's written
        once by `assert_battery_soc_limits()` at startup and pinned at
        `soc_ceiling_pct` for the lifetime of the service. See
        SPEC-ENERGY-01.md §5.4.

        Use this from the periodic re-assertion loop — idempotent and
        defends against firmware resetting these limits silently (power
        cycle, firmware update, local EMS override).
        """
        discharge_cutoff_raw = int(self._battery.discharge_cutoff_pct * 10)
        backup_raw = int(self._battery.backup_soc_pct * 10)
        logger.info(
            "Re-asserting discharge SOC limits: discharge_cutoff=%.1f%% backup=%.1f%%",
            self._battery.discharge_cutoff_pct,
            self._battery.backup_soc_pct,
        )
        ok = True
        ok &= await self._write_u16(REG_DISCHARGE_CUTOFF_SOC, discharge_cutoff_raw)
        ok &= await self._write_u16(REG_BACKUP_SOC, backup_raw)
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
                if export_price_ckwh is not None
                else "price=unknown"
            )
        logger.info(
            "Setting fallback: Maximum Self Consumption + export=%.1fkW (%s)",
            export_cap_kw,
            export_reason,
        )
        if not self._remote_ems_enabled:
            if not await self.enable_remote_ems():
                return False
        # Uncap the charge leg (reg 40032) before we switch to mode 2 —
        # otherwise a stale low value from a prior mode-3 dispatch caps
        # battery DC charge in the fallback state too, and surplus PV
        # curtails instead of soaking into the battery. See the note in
        # apply_lp_dispatch's mode-2 branch; fallback inherits the same
        # hazard because mode 2's cascade honours 40032.
        max_charge_raw = int(round(self._battery.max_dc_charge_kw * 1000))
        charge_cap_ok = await self._write_u32(REG_ESS_MAX_CHARGING_LIMIT, max_charge_raw)
        mode_ok = await self._write_u16(
            REG_REMOTE_EMS_CONTROL_MODE,
            RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION.value,
        )
        export_ok = await self._write_u32(
            REG_GRID_EXPORT_LIMIT,
            int(export_cap_kw * 1000),
        )
        return charge_cap_ok and mode_ok and export_ok

    # ── LP dispatch path ──────────────────────────────────────────
    #
    # The LP outputs a continuous signed `battery_kw` for slot 0 (+ charge,
    # − discharge). We map that to one of the inverter's load-following
    # modes (2/3/5/6) plus a charge or discharge cap, rather than mode 0
    # + a fixed plant-level setpoint. Reason: mode 0 holds whatever number
    # we last wrote and doesn't react to actual house load — every load
    # transient leaks as unintended grid import or export. The load-
    # following modes let the inverter handle real-time response within
    # our magnitude cap; the LP supplies intent, the inverter supplies
    # sub-second response.
    #
    # Mode mapping (post 2026-04-25 cutoff retire):
    #   Idle (|battery|<deadband)      → mode 2, write 40032 = 0
    #   Charge, grid > pv              → mode 3, write 40032 = battery_kw
    #   Charge, pv ≥ grid              → mode 2 ADAPTIVE: phase A
    #                                     40032=max + measure, phase B
    #                                     40032 = trim so battery+export
    #                                     split rather than cascade-saturate
    #   Discharge, PV producing        → mode 5, write 40034 = max_discharge
    #   Discharge, no PV               → mode 6, write 40034 = max_discharge
    #
    # The charge cutoff (40047) is NOT written per tick. It's pinned at
    # the configured `soc_ceiling_pct` by `assert_battery_soc_limits`
    # at startup, and stays there. 40032 alone governs charge rate —
    # validated by probe_no_cutoff.py (mode-2 idle via 40032=0 produced
    # bat=-0.02 kW; cutoff held at 950 across 256s of unrelated writes).

    async def apply_lp_dispatch(
        self,
        dispatch: LPDispatch,
        *,
        export_cap_kw: float = 0.0,
        prefetched_probe: PVProbeResult | None = None,
    ) -> bool:
        """Apply the LP's slot-0 decision via mode + cap registers.

        `export_cap_kw` is the resolved DNSP export cap currently in force
        (caller is responsible for writing 40038 BEFORE this call so the
        Phase-A surplus measurement reflects the real export window).

        ``prefetched_probe`` is the result of an earlier-in-tick PV probe
        (option 2 plumbing). When provided AND the dispatch resolves to
        a mode-2 path, the redundant Phase-A measurement is skipped —
        the caller has already done it. Required precondition: the
        caller has not rewritten 40032 or 40031 since the probe ran.
        Non-mode-2 dispatch paths ignore the prefetch (cap registers
        will be overwritten anyway).

        **Write order: cap first, then mode.** If any write fails partway,
        the inverter must never be in (new mode, stale cap). Cap-first
        means a mid-apply failure leaves the inverter in (old mode, new
        cap) — always safe.

        Returns True if all writes succeeded. False on any failure; the
        caller must trigger fallback — partial application is never safe.
        """
        if not self._remote_ems_enabled:
            if not await self.enable_remote_ems():
                return False

        mode = dispatch.mode
        kind_name = dispatch.kind.name if dispatch.kind is not None else "?"

        # Every mode-2 dispatch — CHARGE or SELF_CONSUME — runs the
        # adaptive trim. For CHARGE, the LP's intended rate becomes the
        # trim floor; for SELF_CONSUME the floor is 0 and the trim
        # collapses to "soak whatever PV is left after the export cap".
        # The earlier "idle = 40032=0" path was fragile: it relied on
        # the cascade fully respecting cap=0, and any unforecast PV
        # surplus was left for the inverter to curtail rather than store.
        # Adaptive trim measures actual PV and sets 40032 to the surplus
        # over the export cap — soaking the rest into the battery.
        if mode == RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION:
            return await self._apply_mode2_adaptive_charge(
                dispatch,
                export_cap_kw=export_cap_kw,
                prefetched_probe=prefetched_probe,
            )

        if mode == RemoteEMSControlMode.COMMAND_CHARGING_GRID_FIRST:
            cap_raw = max(0, int(round(dispatch.cap_kw * 1000)))
            if not await self._write_u32(REG_ESS_MAX_CHARGING_LIMIT, cap_raw):
                return False
        elif mode in (
            RemoteEMSControlMode.COMMAND_DISCHARGING_PV_FIRST,
            RemoteEMSControlMode.COMMAND_DISCHARGING_ESS_FIRST,
        ):
            cap_raw = max(0, int(round(dispatch.cap_kw * 1000)))
            if not await self._write_u32(REG_ESS_MAX_DISCHARGING_LIMIT, cap_raw):
                return False
        else:
            logger.error(
                "apply_lp_dispatch: unexpected mode %s; refusing to write",
                mode.name,
            )
            return False

        if not await self._write_u16(REG_REMOTE_EMS_CONTROL_MODE, mode.value):
            return False

        logger.info(
            "Applied LP dispatch: mode=%s kind=%s cap=%.2fkW intent=%+.2fkW",
            mode.name,
            kind_name,
            dispatch.cap_kw,
            dispatch.signed_intent_kw,
        )
        return True

    async def measure_uncapped_pv(
        self,
        *,
        settle_seconds: float = MODE2_PROBE_SECONDS,
        export_cap_kw: float | None = None,
    ) -> PVProbeResult | None:
        """Phase-A "uncap and measure" PV probe.

        Writes 40032 = max_dc_charge_kw + mode = 2 to make the cascade
        absorb all available PV into the battery, sleeps for
        ``settle_seconds`` (cascade + MPPT need ~3 s to converge), then
        reads telemetry. Leaves 40032=max + mode 2 in force on return —
        the caller is expected to issue the next write (Phase-B trim or
        a fresh dispatch) immediately.

        Returns:

        - ``None`` when the uncap write to 40032 or the mode write to
          40031 failed. The inverter is in an unknown register state —
          the caller MUST treat this as a hard failure (abort dispatch,
          trigger fallback). Distinct from the telemetry-blind case
          below.
        - ``PVProbeResult(pv_kw=None, ...)`` when the writes succeeded
          but the post-settle ``read_state`` returned None (Modbus
          blip). The cascade is in a known-safe "uncapped" state —
          caller can leave it in force or overwrite. Equivalent to the
          legacy "telemetry blind during Phase A" branch.
        - ``PVProbeResult(pv_kw=X, ...)`` on a clean measurement.
          ``saturated=True`` means the cascade had no slack (battery
          at BMS acceptance AND export at cap), so measured_pv is only
          a lower bound on true MPP — the LP override path should fall
          back to forecast in that case.

        ``export_cap_kw`` is the cap the caller has currently written
        to 40038. It's used for the saturation check (we compare
        measured export against it). The probe does not modify 40038
        — keeping the operator's DNSP envelope intact during the 5 s
        window.

        Validity: if either battery or export had slack, ``pv_kw`` is
        the unthrottled MPP, accurate to inverter telemetry precision
        (~0.05 kW). When both are saturated, the cascade refused part
        of the available PV and MPPT throttled. In that mode the
        measurement is only useful as a lower bound, which is the
        ``saturated=True`` case.
        """
        max_charge_raw = int(round(self._battery.max_dc_charge_kw * 1000))
        if not await self._write_u32(REG_ESS_MAX_CHARGING_LIMIT, max_charge_raw):
            return None
        if not await self._write_u16(
            REG_REMOTE_EMS_CONTROL_MODE,
            RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION.value,
        ):
            return None

        await asyncio.sleep(settle_seconds)

        state = await self.read_state()
        bat_avail_kw = await self._read_input_u32(REG_ESS_AVAIL_MAX_CHARGING_POWER, gain=1000)
        if state is None or state.pv_power_kw is None:
            return PVProbeResult(
                pv_kw=None,
                saturated=False,
                bat_kw=None,
                bat_avail_kw=bat_avail_kw,
                grid_export_kw=None,
                export_cap_kw=export_cap_kw,
                house_kw=None,
                soc_pct=None,
            )

        pv_kw = max(0.0, state.pv_power_kw)
        bat_kw = state.battery_power_kw or 0.0
        # `grid_power_kw` follows the convention: + = import, − = export.
        grid_export_kw = max(0.0, -(state.grid_power_kw or 0.0))

        # Saturation check: cascade had no slack iff BOTH sinks at cap.
        # Tolerance covers BMS hunting / sub-second noise (~0.3 kW).
        cap_tolerance_kw = 0.3
        bat_at_cap = bat_avail_kw is not None and bat_kw >= bat_avail_kw - cap_tolerance_kw
        export_at_cap = (
            export_cap_kw is not None and grid_export_kw >= export_cap_kw - cap_tolerance_kw
        )
        saturated = bat_at_cap and export_at_cap

        return PVProbeResult(
            pv_kw=pv_kw,
            saturated=saturated,
            bat_kw=bat_kw,
            bat_avail_kw=bat_avail_kw,
            grid_export_kw=grid_export_kw,
            export_cap_kw=export_cap_kw,
            house_kw=state.house_load_kw,
            soc_pct=state.soc_pct,
        )

    async def _apply_mode2_adaptive_charge(
        self,
        dispatch: LPDispatch,
        *,
        export_cap_kw: float,
        prefetched_probe: PVProbeResult | None = None,
    ) -> bool:
        """Two-phase mode-2 dispatch — covers both CHARGE and SELF_CONSUME.

        Phase A: write 40032 = max_dc_charge_kw, mode = 2. Cascade soaks
        all surplus PV into the battery so we can read true MPP from
        telemetry. Sleep `MODE2_PROBE_SECONDS` for the cascade to settle.

        Phase B: read PV, compute trim:

            trim_kw = max(LP_rate, pv_kw - export_cap_kw - headroom)
            trim_kw = min(trim_kw, max_dc_charge_kw)

        Write 40032 = trim_kw. The mode-2 cascade then splits available
        PV: house at priority 1 (sub-second), battery up to `trim_kw`,
        export up to the DNSP cap, curtail anything left.

        ``prefetched_probe`` is the Phase-A measurement from the pre-LP
        PV probe (option 2 plumbing). When provided, this method skips
        its own Phase-A — it's already happened — and goes straight to
        Phase-B trim. Saves ~5 s per mode-2 tick. The caller must have
        left 40032=max + mode 2 in force at the time of measurement and
        not rewritten them since. When ``None`` the original two-phase
        sequence runs.

        Why **PV alone** in the trim, not `pv − house`: the cascade
        already serves house at priority 1 from PV. Including house in
        the trim makes the 5-second Phase-A sample fragile to load
        transients (kettle/microwave cycling) — a single high-load
        sample collapses trim toward zero and the rest of the slot ends
        up curtailed. Trimming on PV alone gives up roughly `house` kW
        of nominal export (mid-day baseline ~0.5 kW, a few cents/slot)
        in exchange for being immune to load-side noise.

        Revisit when the load model is mature enough to provide a
        reliable rolling-average house value (see KNOWN-ISSUES #25).
        Two paths once that's true: use `LP-planned pv_to_house_kw`
        (already a load-profile-derived estimate) or a controller-side
        rolling median of the last N measured loads. Until then PV
        alone is the right trade-off.

        For CHARGE: `lp_rate = |signed_intent_kw|` (LP's intended rate),
        which acts as the trim floor — a transient PV droop during
        Phase A can't collapse the trim below the LP's intent.

        For SELF_CONSUME: `lp_rate ≈ 0`, trim collapses to "soak any PV
        beyond the export cap". Replaces the older `40032 = 0` idle
        behaviour, which left unforecast PV surplus to be curtailed by
        the inverter rather than stored.

        See SPEC-ENERGY-01.md §5.4, probe_two_phase.py, probe_no_cutoff.py.
        """
        kind_name = dispatch.kind.name if dispatch.kind is not None else "?"
        lp_rate = abs(dispatch.signed_intent_kw) if dispatch.signed_intent_kw else 0.0

        if prefetched_probe is None:
            probe = await self.measure_uncapped_pv(
                settle_seconds=MODE2_PROBE_SECONDS,
                export_cap_kw=export_cap_kw,
            )
            if probe is None:
                # Hard write failure inside the probe — same semantics
                # as a failed cap-or-mode write in the legacy inline
                # Phase A. Caller's fallback path handles it.
                return False
        else:
            # Pre-LP probe already ran; 40032=max + mode 2 are still in
            # force from that step. Reuse the measurement directly.
            # `prefetched_probe` is non-None here by the contract above.
            probe = prefetched_probe

        if probe.pv_kw is None:
            # Telemetry blind — leave Phase-A state in force (uncapped
            # charge). All PV soaks into battery for the rest of the slot,
            # which matches the "soak first" intent. Log AND emit so this
            # is queryable in the NDJSON event stream.
            logger.warning(
                "mode-2 adaptive (%s): phase-A telemetry unavailable; staying uncapped (40032=max)",
                kind_name,
            )
            emit(
                EventType.MODE2_TRIM_BLIND,
                {
                    "kind": kind_name,
                    "lp_rate_kw": lp_rate,
                    "export_cap_kw": export_cap_kw,
                    "prefetched": prefetched_probe is not None,
                },
            )
            return True

        pv_a = probe.pv_kw
        trim_kw = max(
            lp_rate,
            max(0.0, pv_a - export_cap_kw) - MODE2_TRIM_FLOOR_HEADROOM_KW,
        )
        trim_kw = min(trim_kw, self._battery.max_dc_charge_kw)
        trim_raw = int(round(trim_kw * 1000))
        logger.info(
            "Mode-2 adaptive (%s): phaseA pv=%.2fkW "
            "→ phaseB trim=%.2fkW (lp_rate=%.2fkW, export_cap=%.2fkW)",
            kind_name,
            pv_a,
            trim_kw,
            lp_rate,
            export_cap_kw,
        )
        # Structured event so replay / analytics can answer "what did
        # Phase A read at this tick, and how did the trim formula
        # combine that with LP rate and export cap to land on the
        # value written to 40032?" without parsing log text.
        emit(
            EventType.MODE2_TRIM,
            {
                "kind": kind_name,
                "phase_a_pv_kw": pv_a,
                "phase_a_house_kw": probe.house_kw,
                "phase_a_battery_kw": probe.bat_kw,
                "phase_a_soc_pct": probe.soc_pct,
                "phase_a_saturated": probe.saturated,
                "lp_rate_kw": lp_rate,
                "export_cap_kw": export_cap_kw,
                "headroom_kw": MODE2_TRIM_FLOOR_HEADROOM_KW,
                "trim_kw": trim_kw,
                "prefetched": prefetched_probe is not None,
            },
        )
        return await self._write_u32(REG_ESS_MAX_CHARGING_LIMIT, trim_raw)

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
