"""Shelly Pro EM managed load controller.

Each managed load has a Shelly device with a CT clamp for measurement
and optionally a dry contact relay for control. Uses Shelly Gen2 RPC API.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from ..config import ManagedLoadConfig
from ..logging_utils import api_call, emit
from ..time_utils import now_utc, utc_to_local
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

        # Cycle state. Surfaces on the load card via ManagedLoadStatus.
        # SHIFTABLE drives this off measured power (one-shot run model);
        # SIGNAL_DRIVEN_CONTINUOUS drives it off relay state + daily-target
        # progress. OBSERVABLE / SIGNAL_DRIVEN leave the field unset (None
        # is returned in status()).
        self._cycle_state = LoadCycleState.IDLE
        self._cycle_started: datetime | None = None
        self._energy_at_cycle_start: float = 0.0
        self._energy_today_imp_kwh: float = 0.0
        self._today_date: datetime | None = None

        # Returned-energy (export) accumulator — parallel to the imported
        # one above. EM1Data exposes `total_act_energy` (forward) and
        # `total_act_ret_energy` (reverse) separately because the meter
        # is bidirectional. Loads like the HW heat pump only ever import
        # so the export accumulator stays at 0; the bidirectional grid
        # CT (mains) needs both to report meaningful net daily energy.
        self._energy_at_cycle_start_ret: float = 0.0
        self._energy_today_ret_kwh: float = 0.0

        # Last seen monotonic counters — used to detect Shelly reboots
        # (counters reset to 0). None until first successful read.
        self._last_total_energy_kwh: float | None = None
        self._last_total_energy_ret_kwh: float | None = None

        # Issue #2 fix: queue async relay stop from sync transition
        self._pending_relay_stop: bool = False

        # Relay state-change tracking for SIGNAL_DRIVEN_CONTINUOUS block
        # enforcement. `_last_relay_state` is the most recently observed
        # value; `_relay_state_since` is when the current state began.
        # Both stay None until the first successful relay read.
        self._last_relay_state: bool | None = None
        self._relay_state_since: datetime | None = None

        # Time-mode daily accumulator: minutes the relay has been observed
        # ON since *local* midnight. Parallel to _energy_today_kwh but
        # anchored on local date (matches deadline_hour_local — the LP's
        # daily-target windows are local-midnight to local-deadline).
        # Integrated by left-Riemann across status() polls: at each tick
        # we add (now - last_status_at) minutes if the relay was observed
        # ON at the previous poll. Local-date rollover zeros the counter
        # and reseats the anchor — discards the small pre-midnight tail
        # of the in-flight interval (≤1 poll period of error).
        self._relay_on_minutes_today: float = 0.0
        self._relay_today_local_date: datetime | None = None
        self._last_status_at: datetime | None = None

    @property
    def load_id(self) -> str:
        return self._config.load_id

    @property
    def has_relay(self) -> bool:
        """Whether this controller drives a dry-contact relay. False for
        measurement-only loads (e.g. the grid CT on the Shelly's second
        channel). The fallback path uses this to skip relay-less loads
        rather than trip the `set_relay → no relay` error log."""
        return self._config.has_relay

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
        energy_ret_kwh = 0.0
        relay_on: bool | None = None
        read_ok = False

        try:
            # Pro EM 50: live power on EM1.GetStatus, lifetime energy on
            # EM1Data.GetStatus (different endpoint from the 3-phase Pro
            # 3EM, which exposes both inside EM.GetStatus). Sign convention
            # for grid CT: act_power < 0 = export, > 0 = import — matches
            # the inverter's grid_power_kw convention for the validation
            # cross-check.
            with api_call("shelly", "em1_status") as call:
                call.extra["load_id"] = self._config.load_id
                resp = await self._client.get(
                    f"{self._base_url}/rpc/EM1.GetStatus",
                    params={"id": self._config.shelly_channel},
                )
                call.set_response(resp)
                resp.raise_for_status()
                em_data = resp.json()
                power_kw = em_data.get("act_power", 0) / 1000.0

            with api_call("shelly", "em1_data") as call:
                call.extra["load_id"] = self._config.load_id
                resp = await self._client.get(
                    f"{self._base_url}/rpc/EM1Data.GetStatus",
                    params={"id": self._config.shelly_channel},
                )
                call.set_response(resp)
                resp.raise_for_status()
                energy_data = resp.json()
                energy_kwh = energy_data.get("total_act_energy", 0) / 1000.0
                energy_ret_kwh = energy_data.get("total_act_ret_energy", 0) / 1000.0
                read_ok = True

            # Read relay state if applicable
            if self._config.has_relay:
                with api_call("shelly", "switch_status") as call:
                    call.extra["load_id"] = self._config.load_id
                    resp = await self._client.get(
                        f"{self._base_url}/rpc/Switch.GetStatus",
                        params={"id": 0},
                    )
                    call.set_response(resp)
                    resp.raise_for_status()
                    sw_data = resp.json()
                    relay_on = sw_data.get("output", False)

        except Exception:
            logger.warning("Shelly read failed for %s", self._config.load_id)

        # Track daily energy ONLY on successful read — a failed read
        # would otherwise be misinterpreted as a counter reset to 0.
        if read_ok:
            self._track_daily_energy(energy_kwh, energy_ret_kwh)

            # Update cycle state for shiftable loads
            if self._config.category == LoadCategory.SHIFTABLE:
                self._update_cycle_state(power_kw)
            elif self._config.category == LoadCategory.SIGNAL_DRIVEN_CONTINUOUS:
                self._update_cycle_state_continuous(relay_on)

        # Track relay state-change timestamp for SIGNAL_DRIVEN_CONTINUOUS
        # block enforcement. Only on successful read with an observed
        # relay value — None values (read failure) leave state untouched
        # so a transient blip doesn't reset the elapsed-time window.
        if read_ok and relay_on is not None:
            self._track_relay_state(relay_on)
            self._track_relay_on_minutes(relay_on)

        return ManagedLoadStatus(
            load_id=self._config.load_id,
            category=self._config.category,
            power_kw=power_kw,
            energy_today_kwh=self._energy_today_kwh,
            relay_on=relay_on,
            cycle_state=self._cycle_state
            if self._config.category
            in (LoadCategory.SHIFTABLE, LoadCategory.SIGNAL_DRIVEN_CONTINUOUS)
            else None,
            relay_state_since=self._relay_state_since,
            relay_on_minutes_today=(
                self._relay_on_minutes_today if self._config.has_relay else None
            ),
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
            with api_call("shelly", "switch_set") as call:
                call.extra["load_id"] = self._config.load_id
                call.extra["on"] = True
                resp = await self._client.get(
                    f"{self._base_url}/rpc/Switch.Set",
                    params={"id": 0, "on": "true"},
                )
                call.set_response(resp)
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
            with api_call("shelly", "switch_set") as call:
                call.extra["load_id"] = self._config.load_id
                call.extra["on"] = False
                resp = await self._client.get(
                    f"{self._base_url}/rpc/Switch.Set",
                    params={"id": 0, "on": "false"},
                )
                call.set_response(resp)
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
            with api_call("shelly", "switch_set") as call:
                call.extra["load_id"] = self._config.load_id
                call.extra["on"] = on
                resp = await self._client.get(
                    f"{self._base_url}/rpc/Switch.Set",
                    params={"id": 0, "on": "true" if on else "false"},
                )
                call.set_response(resp)
                resp.raise_for_status()
            return True
        except Exception:
            logger.exception(
                "Failed to set relay=%s for %s",
                on,
                self._config.load_id,
            )
            return False

    def _track_relay_on_minutes(self, relay_on: bool) -> None:
        """Integrate relay-on time across status() polls (right-Riemann).

        If the relay is observed ON at this poll, the interval since the
        previous poll counts toward today's accumulator. With 60s polls
        the per-transition error is ≤1 poll period; over a 4 h block
        across two transitions that's ≤0.5 % error. Resets at local
        midnight to align with deadline_hour_local.
        """
        now = now_utc()
        today_local = utc_to_local(now).date()

        if self._relay_today_local_date != today_local:
            # Local-day rollover — zero the counter, anchor to now.
            self._relay_today_local_date = today_local
            self._relay_on_minutes_today = 0.0
            self._last_status_at = now
            return

        if relay_on and self._last_status_at is not None:
            elapsed_min = (now - self._last_status_at).total_seconds() / 60.0
            self._relay_on_minutes_today += max(0.0, elapsed_min)

        self._last_status_at = now

    def _track_relay_state(self, relay_on: bool) -> None:
        """Record the timestamp when `relay_on` last transitioned.

        Called from `status()` after each successful read. On the first
        observation post-startup the timestamp anchors to now: worst case
        the LP holds an extra full block before allowing a transition,
        which is the safe direction (won't violate min-on/min-off).
        """
        if self._last_relay_state is None or self._last_relay_state != relay_on:
            self._relay_state_since = now_utc()
        self._last_relay_state = relay_on

    @property
    def _energy_today_kwh(self) -> float:
        """Net energy today (imp − exp).

        Sign convention matches `power_kw` and `grid_power_kw`: + = imported,
        − = exported. Unidirectional loads (HW heat pump) export ≈ 0 so net
        == imported, preserving prior behaviour for kWh daily-target loads.
        """
        return self._energy_today_imp_kwh - self._energy_today_ret_kwh

    def _track_daily_energy(
        self, total_energy_kwh: float, total_energy_ret_kwh: float = 0.0
    ) -> None:
        """Track accumulated energy today, resetting at midnight.

        Tracks imported and exported counters in parallel: unidirectional
        loads (HW heat pump relay) leave the export side at 0, the mains
        CT exercises both. The public read is the net (`_energy_today_kwh`
        property) so a heavy-export site reads negative.

        Detects Shelly counter resets (device reboot → counters back to 0)
        per side by watching for backward jumps. Each side is monotonic on
        its own; the net isn't, so we can't reset-detect on net.
        """
        now = now_utc()
        today = now.date()

        # Counter reset detection (Shelly reboot) — per side.
        reset_imp = (
            self._last_total_energy_kwh is not None
            and total_energy_kwh < self._last_total_energy_kwh - _COUNTER_RESET_THRESHOLD_KWH
        )
        reset_ret = (
            self._last_total_energy_ret_kwh is not None
            and total_energy_ret_kwh
            < self._last_total_energy_ret_kwh - _COUNTER_RESET_THRESHOLD_KWH
        )

        if self._today_date != today:
            # New day — reset tracker. Normal midnight rollover.
            self._today_date = today
            self._energy_at_cycle_start = total_energy_kwh
            self._energy_at_cycle_start_ret = total_energy_ret_kwh
            self._energy_today_imp_kwh = 0.0
            self._energy_today_ret_kwh = 0.0
        else:
            if reset_imp or reset_ret:
                # Counter reboot mid-day. Preserve each side's accumulator by
                # shifting its baseline: new_baseline = total − current_today.
                emit(
                    EventType.VALIDATION_WARNING,
                    {
                        "load_id": self._config.load_id,
                        "message": "Shelly counter reset detected (likely reboot)",
                        "previous_total_kwh": self._last_total_energy_kwh,
                        "previous_total_ret_kwh": self._last_total_energy_ret_kwh,
                        "new_total_kwh": total_energy_kwh,
                        "new_total_ret_kwh": total_energy_ret_kwh,
                        "preserved_today_kwh": self._energy_today_kwh,
                        "preserved_today_imp_kwh": self._energy_today_imp_kwh,
                        "preserved_today_ret_kwh": self._energy_today_ret_kwh,
                    },
                )
                if reset_imp:
                    self._energy_at_cycle_start = total_energy_kwh - self._energy_today_imp_kwh
                if reset_ret:
                    self._energy_at_cycle_start_ret = (
                        total_energy_ret_kwh - self._energy_today_ret_kwh
                    )
            self._energy_today_imp_kwh = total_energy_kwh - self._energy_at_cycle_start
            self._energy_today_ret_kwh = total_energy_ret_kwh - self._energy_at_cycle_start_ret

        self._last_total_energy_kwh = total_energy_kwh
        self._last_total_energy_ret_kwh = total_energy_ret_kwh

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

    def _update_cycle_state_continuous(self, relay_on: bool | None) -> None:
        """Cycle state for SIGNAL_DRIVEN_CONTINUOUS loads.

        The appliance handles its own internal compressor cycles while
        the LP holds the dry contact. So relay_on directly maps to
        RUNNING/IDLE — there's no "wait for power to drop" semantic
        like SHIFTABLE has. Once the day's target is hit, latch to
        COMPLETE_TODAY until midnight rolls energy_today_kwh back to 0
        (handled by `_track_daily_energy`).
        """
        # Two daily-target modes — see ManagedLoadConfig docstring.
        kwh_target = self._config.daily_target_kwh
        min_target = self._config.daily_run_minutes
        target_met = (kwh_target is not None and self._energy_today_kwh >= kwh_target) or (
            min_target is not None and self._relay_on_minutes_today >= min_target
        )
        if target_met:
            self._cycle_state = LoadCycleState.COMPLETE_TODAY
        elif relay_on:
            self._cycle_state = LoadCycleState.RUNNING
        else:
            self._cycle_state = LoadCycleState.IDLE


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
