"""Shelly Pro EM managed load controller.

Each managed load has a Shelly device with a CT clamp for measurement
and optionally a dry contact relay for control. Uses Shelly Gen2 RPC API.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from ..config import ManagedLoadConfig
from ..logging_utils import emit
from ..time_utils import now_utc
from ..types import (
    EventType,
    LoadCategory,
    LoadCycleState,
    ManagedLoadStatus,
)

logger = logging.getLogger(__name__)

# Backward-jump threshold for the energy counter. Real reboots take
# total_act_energy from N back to 0; small backward jitter (sub-100Wh)
# would be unusual but is treated as noise rather than a reset.
_COUNTER_RESET_THRESHOLD_KWH = 0.1


class ShellyLoadController:
    """Controller for a single Shelly-monitored load."""

    def __init__(self, config: ManagedLoadConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(timeout=5.0)
        self._base_url = f"http://{config.shelly_host}"

        # Cycle state tracking (for shiftable loads)
        self._cycle_state = LoadCycleState.IDLE
        self._cycle_started: datetime | None = None
        self._energy_at_cycle_start: float = 0.0
        self._energy_today_kwh: float = 0.0
        self._today_date: datetime | None = None

        # Last seen monotonic counter — used to detect Shelly reboots
        # (counter resets to 0). None until first successful read.
        self._last_total_energy_kwh: float | None = None

        # Issue #2 fix: queue async relay stop from sync transition
        self._pending_relay_stop: bool = False

    @property
    def load_id(self) -> str:
        return self._config.load_id

    async def close(self) -> None:
        await self._client.aclose()

    async def status(self) -> ManagedLoadStatus:
        """Read live power, energy, and compute cycle state."""
        # Issue #2: apply any queued relay stop from a previous tick's
        # sync state transition.
        if self._pending_relay_stop:
            await self._stop_relay()
            self._pending_relay_stop = False

        power_kw = 0.0
        energy_kwh = 0.0
        relay_on: bool | None = None
        read_ok = False

        try:
            # Read energy meter status
            resp = await self._client.get(
                f"{self._base_url}/rpc/EM.GetStatus",
                params={"id": self._config.shelly_channel},
            )
            resp.raise_for_status()
            em_data = resp.json()
            # Power in watts, convert to kW
            power_kw = em_data.get("act_power", 0) / 1000.0
            # Total energy in Wh
            energy_kwh = em_data.get("total_act_energy", 0) / 1000.0
            read_ok = True

            # Read relay state if applicable
            if self._config.has_relay:
                resp = await self._client.get(
                    f"{self._base_url}/rpc/Switch.GetStatus",
                    params={"id": 0},
                )
                resp.raise_for_status()
                sw_data = resp.json()
                relay_on = sw_data.get("output", False)

        except Exception:
            logger.warning("Shelly read failed for %s", self._config.load_id)

        # Track daily energy ONLY on successful read — a failed read
        # would otherwise be misinterpreted as a counter reset to 0.
        if read_ok:
            self._track_daily_energy(energy_kwh)

            # Update cycle state for shiftable loads
            if self._config.category == LoadCategory.SHIFTABLE:
                self._update_cycle_state(power_kw)

        return ManagedLoadStatus(
            load_id=self._config.load_id,
            category=self._config.category,
            power_kw=power_kw,
            energy_today_kwh=self._energy_today_kwh,
            relay_on=relay_on,
            cycle_state=self._cycle_state
            if self._config.category == LoadCategory.SHIFTABLE
            else None,
        )

    async def start_cycle(self) -> bool:
        """Energise the dry contact relay to start a load cycle.

        Only valid for shiftable loads with a relay.
        """
        if not self._config.has_relay:
            logger.error("Cannot start cycle on %s — no relay", self._config.load_id)
            return False

        if self._cycle_state != LoadCycleState.IDLE:
            logger.warning(
                "Cannot start cycle on %s — state is %s",
                self._config.load_id,
                self._cycle_state,
            )
            return False

        try:
            resp = await self._client.get(
                f"{self._base_url}/rpc/Switch.Set",
                params={"id": 0, "on": "true"},
            )
            resp.raise_for_status()

            self._cycle_state = LoadCycleState.RUNNING
            self._cycle_started = now_utc()
            emit(EventType.LOAD_CYCLE_START, {"load_id": self._config.load_id})
            logger.info("Started cycle for %s", self._config.load_id)
            return True

        except Exception:
            logger.exception("Failed to start cycle for %s", self._config.load_id)
            return False

    async def _stop_relay(self) -> bool:
        """De-energise the relay."""
        if not self._config.has_relay:
            return False
        try:
            resp = await self._client.get(
                f"{self._base_url}/rpc/Switch.Set",
                params={"id": 0, "on": "false"},
            )
            resp.raise_for_status()
            return True
        except Exception:
            logger.exception("Failed to stop relay for %s", self._config.load_id)
            return False

    async def set_relay(self, on: bool) -> bool:
        """Set the dry-contact relay to a continuous state.

        For SIGNAL_DRIVEN loads where the appliance manages its own
        cycles. Idempotent — safe to call every tick.
        """
        if not self._config.has_relay:
            logger.error(
                "Cannot set relay on %s — no relay configured",
                self._config.load_id,
            )
            return False
        try:
            resp = await self._client.get(
                f"{self._base_url}/rpc/Switch.Set",
                params={"id": 0, "on": "true" if on else "false"},
            )
            resp.raise_for_status()
            return True
        except Exception:
            logger.exception(
                "Failed to set relay=%s for %s",
                on,
                self._config.load_id,
            )
            return False

    def _track_daily_energy(self, total_energy_kwh: float) -> None:
        """Track accumulated energy today, resetting at midnight.

        Detects Shelly counter resets (device reboot → counter back to 0)
        by watching for backward jumps in `total_energy_kwh`. On reset,
        the baseline is shifted so `_energy_today_kwh` is preserved across
        the reboot rather than going wildly negative.
        """
        now = now_utc()
        today = now.date()

        # Counter reset detection (Shelly reboot)
        reset_detected = (
            self._last_total_energy_kwh is not None
            and total_energy_kwh < self._last_total_energy_kwh - _COUNTER_RESET_THRESHOLD_KWH
        )

        if self._today_date != today:
            # New day — reset tracker. This is the normal midnight rollover.
            self._today_date = today
            self._energy_at_cycle_start = total_energy_kwh
            self._energy_today_kwh = 0.0
        elif reset_detected:
            # Counter reboot mid-day. Preserve today's accumulator by
            # shifting the baseline: new_baseline = total - current_today.
            # That way the next computation gives back the same _energy_today_kwh.
            emit(
                EventType.VALIDATION_WARNING,
                {
                    "load_id": self._config.load_id,
                    "message": "Shelly counter reset detected (likely reboot)",
                    "previous_total_kwh": self._last_total_energy_kwh,
                    "new_total_kwh": total_energy_kwh,
                    "preserved_today_kwh": self._energy_today_kwh,
                },
            )
            self._energy_at_cycle_start = total_energy_kwh - self._energy_today_kwh
        else:
            self._energy_today_kwh = total_energy_kwh - self._energy_at_cycle_start

        self._last_total_energy_kwh = total_energy_kwh

    def _update_cycle_state(self, power_kw: float) -> None:
        """Update cycle state based on measured power for shiftable loads."""
        threshold = self._config.power_zero_threshold_kw

        if self._cycle_state == LoadCycleState.IDLE:
            # Nothing to do
            pass

        elif self._cycle_state == LoadCycleState.RUNNING:
            if power_kw < threshold:
                # Power dropped — cycle finished naturally
                emit(
                    EventType.LOAD_CYCLE_COMPLETE,
                    {
                        "load_id": self._config.load_id,
                        "energy_today_kwh": self._energy_today_kwh,
                    },
                )
                logger.info(
                    "Cycle complete for %s (%.2f kWh today)",
                    self._config.load_id,
                    self._energy_today_kwh,
                )

                # Check if daily target met
                if (
                    self._config.daily_energy_kwh
                    and self._energy_today_kwh >= self._config.daily_energy_kwh
                ):
                    self._cycle_state = LoadCycleState.COMPLETE_TODAY
                else:
                    self._cycle_state = LoadCycleState.IDLE

                # Queue relay de-energise for next status() call.
                # We can't await here from sync context.
                self._pending_relay_stop = True

            elif self._cycle_started:
                # Check for fault: relay on but no power for too long
                elapsed = (now_utc() - self._cycle_started).total_seconds()
                if elapsed > 300 and power_kw < threshold:  # 5 min
                    emit(
                        EventType.LOAD_CYCLE_FAULT,
                        {
                            "load_id": self._config.load_id,
                            "message": "Relay on but no power draw for >5 min",
                        },
                    )
                    logger.warning("Cycle fault for %s — no power draw", self._config.load_id)
                    self._cycle_state = LoadCycleState.IDLE

        elif self._cycle_state == LoadCycleState.COMPLETE_TODAY:
            # Reset at midnight (handled by _track_daily_energy)
            pass


class ManagedLoadManager:
    """Manages all Shelly-based load controllers."""

    def __init__(self, configs: list[ManagedLoadConfig]) -> None:
        self._controllers: dict[str, ShellyLoadController] = {}
        for cfg in configs:
            self._controllers[cfg.load_id] = ShellyLoadController(cfg)

    async def close(self) -> None:
        for ctrl in self._controllers.values():
            await ctrl.close()

    def get(self, load_id: str) -> ShellyLoadController | None:
        return self._controllers.get(load_id)

    @property
    def controllers(self) -> list[ShellyLoadController]:
        """All managed load controllers. Used by the LP fallback path to
        open every relay in a single batch."""
        return list(self._controllers.values())

    async def all_statuses(self) -> list[ManagedLoadStatus]:
        """Read status for all managed loads."""
        statuses = []
        for ctrl in self._controllers.values():
            try:
                status = await ctrl.status()
                statuses.append(status)
            except Exception:
                logger.exception("Failed to read status for %s", ctrl.load_id)
        return statuses

    async def start_cycle(self, load_id: str) -> bool:
        """Start a cycle on a shiftable load."""
        ctrl = self._controllers.get(load_id)
        if ctrl is None:
            logger.error("Unknown load: %s", load_id)
            return False
        return await ctrl.start_cycle()

    async def set_relay(self, load_id: str, on: bool) -> bool:
        """Set the relay state on a signal-driven load."""
        ctrl = self._controllers.get(load_id)
        if ctrl is None:
            logger.error("Unknown load: %s", load_id)
            return False
        return await ctrl.set_relay(on)

    def get_mains_power(self, statuses: list[ManagedLoadStatus]) -> float | None:
        """Get the mains CT reading from the 'mains' load if configured."""
        for s in statuses:
            if s.load_id == "mains":
                return s.power_kw
        return None
