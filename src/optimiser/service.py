"""Main service: the tick loop that ties all components together."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

from .clients.amber import AmberClient
from .clients.bom import BOMClient
from .clients.shelly import ManagedLoadManager
from .clients.sigenergy import SigenergyController
from .clients.solcast import SolcastClient
from .clients.unifi import UniFiOccupancyDetector
from .config import Config
from .curtailment import CurtailmentState, evaluate as evaluate_curtailment
from .logging_utils import SnapshotWriter, emit, new_tick_id
from .lp.dispatch import dispatch_from_slot
from .lp.fallback import trigger_fallback
from .lp.loads import build_lp_loads
from .lp.result import SolveStatus
from .lp.runtime import FallbackReason, LPRuntime
from .lp.snapshot_adapter import (
    fallback_planner_output,
    lp_solution_to_planner_output,
)
from .lp.solver import solve_stochastic
from .lp.watcher import WATCHER_PERIOD_S, VerificationWatcher
from .profiler import build_load_profile
from .state_machine import StateMachine
from .store import TelemetryStore
from .time_utils import now_utc, snap_to_interval
from .types import (
    BatteryAction,
    EventType,
    LoadTelemetryRow,
    PriceInterval,
    PVForecast,
    TelemetryRow,
    TickSnapshot,
)
from .validation import validate_telemetry

logger = logging.getLogger(__name__)

_VERSION = "0.2.0"


class Service:
    """Main energy optimiser service."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._running = False

        # Components
        self._amber = AmberClient(config.amber)
        self._solcast = SolcastClient(config.solcast) if config.solcast.enabled else None
        self._sigenergy = SigenergyController(config.sigenergy, config.battery)
        self._bom = BOMClient(config.weather)
        self._occupancy = UniFiOccupancyDetector(config.occupancy)
        self._loads = ManagedLoadManager(config.managed_loads)
        self._store = TelemetryStore(config.storage)
        self._state_machine = StateMachine()
        self._snapshots = SnapshotWriter(config.storage.snapshot_dir)

        # LP runtime — shared with the verification watcher.
        # Built once at startup so the runtime/breaker survives across ticks.
        self._lp_runtime = LPRuntime()
        self._lp_loads = build_lp_loads(config.managed_loads)
        # Watcher reads the same runtime as `_tick`, so deviation/clean
        # counts flow naturally between probe (tick) and verify (watcher).
        self._watcher = VerificationWatcher(
            runtime=self._lp_runtime,
            sigenergy=self._sigenergy,
            shelly_controllers=self._loads.controllers,
        )

        # Cached state
        self._prices: list[PriceInterval] = []
        self._pv_forecast: list[PVForecast] | None = None
        self._last_price_fetch = 0.0
        self._last_solcast_fetch = 0.0
        self._last_bom_fetch = 0.0
        self._last_action: BatteryAction | None = None
        self._last_export_limit_kw: float | None = None
        self._curtailment_state = CurtailmentState()
        self._wake_loops: list = []

    async def start(self) -> None:
        """Start the service: connect, initialise, then run wake loops."""
        logger.info("Starting energy optimiser v%s", _VERSION)
        self._running = True

        # Connect to Modbus
        modbus_ok = await self._sigenergy.connect()

        # Initial fetches (5-min prices drive ticks; 30-min drives planning)
        amber_ok = await self._fetch_5min_prices()
        await self._fetch_30min_prices()
        if self._solcast:
            # Solcast has a hard 10/day quota — a crashloop without this
            # seed path burns it fast. If the log has a forecast from
            # within the last hour, seed the client from it and skip the
            # initial API call; the next scheduled poll (every ~2.4h)
            # will refresh. If nothing cached, fall through to a live fetch.
            cached = self._store.read_latest_pv_forecast(max_age_minutes=60)
            if cached is not None:
                forecasts, fetched_at = cached
                self._solcast.seed_cache(forecasts, fetched_at)
                self._pv_forecast = forecasts
                age_min = (
                    datetime.now(fetched_at.tzinfo) - fetched_at
                ).total_seconds() / 60
                logger.info(
                    "Seeded Solcast cache from log (%d intervals, %.0f min old)"
                    " — skipping initial fetch",
                    len(forecasts),
                    age_min,
                )
            else:
                await self._fetch_solcast()
        await self._fetch_bom()

        # Notify state machine
        self._state_machine.on_startup_complete(modbus_ok, amber_ok)

        if self._state_machine.should_fallback:
            await self._sigenergy.set_fallback()

        # Build wake loops — each runs independently, aligned to wall clock
        from .wake_loop import WakeLoop

        self._wake_loops = [
            WakeLoop("tick", self._config.planner.tick_interval_s, self._tick),
            WakeLoop("verify", WATCHER_PERIOD_S, self._watcher.poll),
            WakeLoop(
                "prices_30min", self._config.amber.poll_30min_interval_s, self._fetch_30min_prices
            ),
            WakeLoop(
                "telemetry", self._config.planner.telemetry_write_interval_s, self._write_telemetry
            ),
            WakeLoop("bom", self._config.weather.poll_interval_s, self._fetch_bom),
            WakeLoop("unifi", self._config.occupancy.poll_interval_s, self._poll_occupancy),
        ]
        if self._solcast:
            self._wake_loops.append(
                WakeLoop("solcast", self._config.solcast.poll_interval_s, self._fetch_solcast)
            )
            # Daily actuals backfill — populates pv_forecast_log.actual_kw
            # so replay can compute "forecast vs reality" and a nightly
            # curtailment summary. Runs every 24h; first fire happens
            # one full period after startup, which is fine — the
            # pv_forecast_log starts accumulating from tick 1 and the
            # backfill is an analysis-time concern, not real-time.
            # Costs 1 of 10 daily Solcast quota.
            self._wake_loops.append(
                WakeLoop("pv_actuals", 86400, self._backfill_pv_actuals)
            )

        logger.info("Spawning %d wake loops", len(self._wake_loops))
        try:
            await asyncio.gather(*[wl.run() for wl in self._wake_loops])
        except asyncio.CancelledError:
            logger.info("Wake loops cancelled")

    async def stop(self) -> None:
        """Gracefully stop the service."""
        logger.info("Stopping service")
        self._running = False

        # Stop wake loops first so no new ticks are scheduled
        for wl in self._wake_loops:
            wl.stop()

        # Set safe fallback before shutting down. Bounded wait: if the
        # Modbus write hangs (comms drop, inverter unresponsive), we'd
        # rather proceed with cleanup and let the inverter fall back on
        # its own watchdog than block shutdown indefinitely.
        if self._sigenergy.connected:
            try:
                await asyncio.wait_for(
                    self._sigenergy.set_fallback(),
                    timeout=5.0,
                )
            except TimeoutError:
                logger.warning(
                    "set_fallback timed out during shutdown; inverter will "
                    "revert to local control after its own comms watchdog"
                )
            except Exception:
                logger.exception("Failed to set fallback during shutdown")

        # Cleanup
        await self._amber.close()
        if self._solcast:
            await self._solcast.close()
        await self._bom.close()
        await self._occupancy.close()
        await self._loads.close()
        self._store.close()
        self._snapshots.close()
        logger.info("Service stopped")

    async def _tick(self) -> None:
        """Execute one planning tick."""
        tick_id = new_tick_id()
        now = now_utc()

        # 1. Read system state from Modbus
        outdoor_temp = self._bom.last_temp
        occupied = await self._occupancy.poll()
        state = await self._sigenergy.read_state(
            outdoor_temp_c=outdoor_temp,
            occupied=occupied,
        )

        if state is not None:
            self._state_machine.on_modbus_success()
        else:
            self._state_machine.on_modbus_failure()
            if self._state_machine.should_fallback:
                # Try to set fallback even though reads failed. Best-effort:
                # a failure here is logged but not re-raised — the tick
                # can't proceed without state anyway.
                try:
                    await self._sigenergy.set_fallback()
                except Exception:
                    logger.exception("set_fallback failed after state read returned None")
            return

        # 2. Refresh 5-min prices every tick (drives micro-arbitrage)
        await self._fetch_5min_prices()
        self._state_machine.on_price_age_check(self._amber.prices_5min_age)

        # 3. (Solcast/BOM/UniFi/30-min refresh handled by their own wake loops)

        # 4. Read managed load statuses
        load_statuses = await self._loads.all_statuses()
        grid_kw_shelly = self._loads.get_mains_power(load_statuses)

        # 5. Build load profile and grab cached price arrays
        prices_5min = self._amber.last_5min_prices or []
        prices_30min = self._amber.last_prices or []
        pv_forecast = self._solcast.last_forecast if self._solcast else None
        load_profile = build_load_profile(
            self._store,
            outdoor_temp_c=outdoor_temp,
            occupied=occupied,
            timestamp=now,
        )

        # 6. Run the LP (or use safe-default if disabled / latched / no prices).
        #
        # Decision tree for what reaches the inverter:
        #   - State machine says no planner → SELF_CONSUME, no apply
        #   - No prices yet → SELF_CONSUME, no apply (we'd be flying blind)
        #   - Breaker latched & still in cooldown → SELF_CONSUME, no apply
        #   - Breaker latched & cooldown elapsed → run LP as a probe;
        #     if it succeeds and applies cleanly, the watcher will eventually
        #     clear the latch after N clean verifications. Until then we keep
        #     applying LP outputs but the breaker stays armed.
        #   - Otherwise → normal LP path.
        prices_planning = prices_30min if prices_30min else prices_5min
        breaker = self._lp_runtime.breaker
        is_probe = breaker.can_probe(now)

        if not self._state_machine.should_run_planner:
            output = fallback_planner_output("planner disabled in current state")
            lp_solution = None
            lp_dispatch = None
        elif not prices_planning:
            output = fallback_planner_output("no price data available")
            lp_solution = None
            lp_dispatch = None
        elif breaker.latched and not is_probe:
            output = fallback_planner_output(
                f"breaker latched in cooldown: {breaker.last_fallback_reason.value}"
            )
            lp_solution = None
            lp_dispatch = None
        else:
            if is_probe:
                emit(
                    EventType.BREAKER_PROBE,
                    {
                        "reason_for_latch": breaker.last_fallback_reason.value,
                    },
                    tick_id=tick_id,
                )

            lp_solution, lp_dispatch = await self._run_lp(
                state=state,
                prices_planning=prices_planning,
                pv_forecast=pv_forecast,
                load_profile=load_profile,
                managed_loads=load_statuses,
            )
            if lp_solution is not None and lp_dispatch is not None:
                output = lp_solution_to_planner_output(lp_solution, lp_dispatch)
            else:
                # _run_lp already triggered the fallback (set_self_consume,
                # opened relays, latched breaker). Just record what happened
                # in the snapshot.
                output = fallback_planner_output(
                    f"lp fallback: {breaker.last_fallback_reason.value}"
                )

        # 9. Apply commands.
        # If the LP succeeded we apply the dispatch; if not, we either set
        # the safe default (state-machine fallback) or do nothing more
        # (the LP fallback path already pushed the inverter to mode 2).
        if (
            self._state_machine.should_apply_commands
            and lp_solution is not None
            and lp_dispatch is not None
        ):
            apply_ok = await self._sigenergy.apply_lp_dispatch(lp_dispatch)
            if apply_ok:
                await self._lp_runtime.record_command(lp_dispatch)
                # Probe succeeded at the apply level. Don't clear the latch
                # here — the watcher does that after `clean_probe_threshold`
                # clean verifications, which proves the inverter is actually
                # respecting our dispatch over time, not just accepting the
                # writes. If the watcher sees another deviation during this
                # probe window, it re-latches with extended cooldown.
                if is_probe:
                    emit(
                        EventType.BREAKER_PROBE,
                        {
                            "phase": "apply_succeeded_awaiting_watcher",
                        },
                        tick_id=tick_id,
                    )
            else:
                # Modbus write failed mid-apply. Cap registers may be in an
                # inconsistent state (mode set but cap not, or vice versa) —
                # fall back paranoidly. The breaker latches so the next
                # tick won't immediately retry against a flaky inverter.
                await trigger_fallback(
                    self._sigenergy,
                    self._loads.controllers,
                    FallbackReason.LP_ERROR,
                    extra_context={"phase": "apply_lp_dispatch"},
                )
                await self._lp_runtime.latch(FallbackReason.LP_ERROR)
                emit(
                    EventType.BREAKER_LATCHED,
                    {
                        "reason": FallbackReason.LP_ERROR.value,
                    },
                    tick_id=tick_id,
                )

            # Apply export limit (independent of battery dispatch).
            if output.grid_export_limit_kw is not None:
                await self._sigenergy.set_export_limit_kw(output.grid_export_limit_kw)
                self._last_export_limit_kw = output.grid_export_limit_kw

            # Apply load commands.
            for cmd in output.load_commands:
                if cmd.desired_relay_on is not None:
                    # SIGNAL_DRIVEN: continuous relay state, idempotent
                    await self._loads.set_relay(cmd.load_id, cmd.desired_relay_on)
                elif cmd.start_cycle:
                    # SHIFTABLE (legacy): one-shot cycle trigger
                    await self._loads.start_cycle(cmd.load_id)

        elif self._state_machine.should_fallback:
            await self._sigenergy.set_fallback()

        # 10. Build and validate telemetry row
        # Current price comes from 5-min for accuracy; snapshot keeps both
        current_import = prices_5min[0].import_per_kwh if prices_5min else None
        current_export = prices_5min[0].export_per_kwh if prices_5min else None
        current_spot = prices_5min[0].spot_per_kwh if prices_5min else None
        current_renewables = prices_5min[0].renewables_pct if prices_5min else None
        current_spike = prices_5min[0].spike_status if prices_5min else None

        # Find current PV forecast
        pv_fcst_kw: float | None = None
        if pv_forecast:
            for pv in pv_forecast:
                if pv.start <= now < pv.end:
                    pv_fcst_kw = pv.pv_estimate_kw
                    break

        row = TelemetryRow(
            ts=snap_to_interval(now),
            soc_pct=state.soc_pct,
            battery_kw=state.battery_power_kw,
            pv_kw=state.pv_power_kw,
            grid_kw=state.grid_power_kw,
            grid_kw_shelly=grid_kw_shelly,
            house_load_kw=state.house_load_kw,
            import_price=current_import,
            export_price=current_export,
            spot_price=current_spot,
            renewables_pct=current_renewables,
            spike_status=current_spike,
            pv_forecast_kw=pv_fcst_kw,
            outdoor_temp_c=outdoor_temp,
            occupied=occupied,
            ems_mode=state.ems_mode,
            planner_action=output.battery_action.value,
            planner_reason=output.reason,
        )

        # Validate
        rolling_p95 = self._store.get_rolling_p95()
        row, validation = validate_telemetry(
            row,
            grid_sensor_online=self._sigenergy.grid_sensor_online,
            bom_data_age=self._bom.data_age,
            rolling_p95=rolling_p95,
            grid_kw_shelly=grid_kw_shelly,
        )

        # 11. Write telemetry (only on 5-min boundary)
        if now.minute % 5 == 0 or self._last_action != output.battery_action:
            try:
                self._store.write_telemetry(row)
                for ls in load_statuses:
                    self._store.write_load_telemetry(
                        LoadTelemetryRow(
                            ts=row.ts,
                            load_id=ls.load_id,
                            category=ls.category.value,
                            power_kw=ls.power_kw,
                            energy_today_kwh=ls.energy_today_kwh,
                            cycle_state=ls.cycle_state.value if ls.cycle_state else None,
                            relay_on=ls.relay_on,
                        )
                    )
            except Exception:
                logger.exception("Failed to write telemetry")
        self._last_action = output.battery_action

        # 12. Write tick snapshot
        try:
            snapshot = TickSnapshot(
                tick_id=tick_id,
                timestamp=now,
                version=_VERSION,
                system_state=state,
                price_forecast=prices_30min if prices_30min else prices_5min,
                pv_forecast=pv_forecast,
                load_profile=load_profile,
                managed_loads=load_statuses,
                maturity_level=load_profile.maturity_level,
                output=output,
                lp_solution=lp_solution,
                lp_dispatch=lp_dispatch,
            )
            self._snapshots.write(snapshot)
        except Exception:
            logger.exception("Failed to write snapshot")

        # 13. Emit tick complete event
        emit(
            EventType.TICK_COMPLETE,
            {
                "soc": state.soc_pct,
                "action": output.battery_action.value,
                "price_ckwh": current_import,
                "reason": output.reason,
                "state": self._state_machine.state.value,
                "maturity": load_profile.maturity_level,
            },
            tick_id=tick_id,
        )

        # 14. Curtailment signal — flat-top heuristic. See curtailment.py.
        # Cheap (no network), purely from state we already have in hand.
        self._check_curtailment(state, tick_id)

        # 15. Heartbeat — update the mtime of the file the external watchdog
        # polls. This is the liveness signal that tells the watchdog "the
        # tick loop is still running". Touching happens here, at the end of
        # a successful tick, so an LP failure or Modbus hang earlier in the
        # pipeline leaves the heartbeat stale and eventually fires the
        # watchdog. Best-effort: never crash the tick on heartbeat-write
        # failure — the watchdog itself will fire if we go silent.
        self._touch_heartbeat()

    def _check_curtailment(self, state, tick_id: str) -> None:
        kind, body = evaluate_curtailment(
            soc_pct=state.soc_pct,
            battery_power_kw=state.battery_power_kw,
            pv_kw=state.pv_power_kw,
            house_load_kw=state.house_load_kw,
            grid_export_limit_kw=self._last_export_limit_kw,
            soc_ceiling_pct=self._config.battery.soc_ceiling_pct,
            state=self._curtailment_state,
        )
        if kind == "suspected":
            emit(EventType.PV_CURTAILMENT_SUSPECTED, body, tick_id=tick_id)
        elif kind == "cleared":
            emit(EventType.PV_CURTAILMENT_CLEARED, body, tick_id=tick_id)

    def _touch_heartbeat(self) -> None:
        path = Path(
            os.environ.get(
                "EO_HEARTBEAT_PATH", "/var/lib/energy-optimiser/heartbeat"
            )
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
        except Exception:
            logger.exception("heartbeat touch failed (path=%s)", path)

    # ── LP Execution ─────────────────────────────────────────────

    async def _run_lp(
        self,
        *,
        state,
        prices_planning,
        pv_forecast,
        load_profile,
        managed_loads,
    ):
        """Run the stochastic LP with a wall-clock timeout, returning
        `(solution, dispatch)` on success or `(None, None)` after fallback.

        Solver internals run in a worker thread (PuLP/HiGHS is sync CPU
        work) so they don't block the event loop. The wall-clock timeout
        wraps the whole thread and is set higher than the solver's own
        internal limit — it catches the pathological case of HiGHS hanging
        rather than the normal "ran out of time, returned best feasible".

        On any failure path: triggers `trigger_fallback`, latches the
        breaker, emits BREAKER_LATCHED, returns `(None, None)`.
        """
        try:
            solution = await asyncio.wait_for(
                asyncio.to_thread(
                    solve_stochastic,
                    state=state,
                    prices_planning=prices_planning,
                    pv_forecast=pv_forecast,
                    load_profile=load_profile,
                    managed_loads=managed_loads,
                    lp_loads=self._lp_loads,
                    battery_config=self._config.battery,
                    scenario_weights=self._config.planner.lp_scenario_weights,
                ),
                timeout=self._config.planner.lp_wall_clock_timeout_s,
            )
        except TimeoutError:
            await self._lp_fallback(FallbackReason.LP_TIMEOUT)
            return None, None
        except Exception:
            logger.exception("LP solve raised unexpectedly")
            await self._lp_fallback(FallbackReason.LP_ERROR)
            return None, None

        emit(
            EventType.LP_SOLVE_COMPLETE,
            {
                "status": solution.status.value,
                "cost_cents": solution.expected_total_cost_cents,
                "solve_ms": solution.solve_time_ms,
                "reason": solution.reason,
            },
        )

        # Treat anything below FEASIBLE as a fallback trigger. OPTIMAL
        # and FEASIBLE both yield a usable slot_0; INFEASIBLE / UNBOUNDED
        # / TIMEOUT (solver-internal) / ERROR don't.
        if solution.status not in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE):
            reason_map = {
                SolveStatus.INFEASIBLE: FallbackReason.LP_INFEASIBLE,
                SolveStatus.UNBOUNDED: FallbackReason.LP_INFEASIBLE,
                SolveStatus.TIMEOUT: FallbackReason.LP_TIMEOUT,
                SolveStatus.ERROR: FallbackReason.LP_ERROR,
            }
            await self._lp_fallback(reason_map.get(solution.status, FallbackReason.LP_ERROR))
            return None, None

        if solution.slot_0 is None:
            # Defensive: status-OK but no slot_0 means the solver returned
            # something we don't know how to interpret. Treat as error.
            logger.warning("LP returned %s but slot_0 is None", solution.status.value)
            await self._lp_fallback(FallbackReason.LP_ERROR)
            return None, None

        dispatch = dispatch_from_slot(solution.slot_0, self._config.battery)
        return solution, dispatch

    async def _lp_fallback(self, reason: FallbackReason) -> None:
        """Trigger the paranoid fallback writes, latch the breaker, and
        emit the BREAKER_LATCHED event. Used by every LP failure path so
        the side effects stay consistent."""
        await trigger_fallback(
            self._sigenergy,
            self._loads.controllers,
            reason,
        )
        await self._lp_runtime.latch(reason)
        emit(EventType.BREAKER_LATCHED, {"reason": reason.value})

    # ── Data Fetching ────────────────────────────────────────────

    async def _fetch_5min_prices(self) -> bool:
        """Fetch 5-min Amber prices (drives micro-arbitrage)."""
        try:
            await self._amber.get_5min_prices()
            self._state_machine.on_amber_success()
            self._persist_price_log()
            return True
        except Exception:
            logger.exception("Amber 5-min price fetch failed")
            self._state_machine.on_amber_failure()
            return False

    async def _fetch_30min_prices(self) -> bool:
        """Fetch 30-min Amber forecast (drives planning)."""
        try:
            self._prices = await self._amber.get_current_prices()
            emit(EventType.PRICE_UPDATE, {"intervals": len(self._prices), "resolution": 30})
            self._persist_price_log()
            return True
        except Exception:
            logger.exception("Amber 30-min price fetch failed")
            return False

    def _persist_price_log(self) -> None:
        """Drain and persist the Amber client's pending log rows. Called
        after every successful fetch. Best-effort — store swallows
        exceptions internally."""
        rows = self._amber.drain_log_rows()
        if rows:
            self._store.write_price_forecast_log(rows)

    async def _poll_occupancy(self) -> None:
        try:
            await self._occupancy.poll()
        except Exception:
            logger.exception("UniFi occupancy poll failed")

    async def _write_telemetry(self) -> None:
        """Wake loop target — telemetry write happens inline in tick.

        This wake loop currently exists for future use (e.g. flushing a
        write-ahead buffer). Telemetry rows are written from _tick on the
        5-min boundary check.
        """
        return

    async def _fetch_solcast(self) -> None:
        if not self._solcast:
            return
        try:
            self._pv_forecast = await self._solcast.get_forecast()
            self._persist_pv_forecast_log()
        except Exception:
            logger.exception("Solcast forecast fetch failed")

    def _persist_pv_forecast_log(self) -> None:
        """Drain and persist the Solcast client's pending log rows.
        Mirrors `_persist_price_log` for Amber. Best-effort."""
        if not self._solcast:
            return
        rows = self._solcast.drain_log_rows()
        if rows:
            self._store.write_pv_forecast_log(rows)

    async def _backfill_pv_actuals(self) -> None:
        """Daily wake loop target: fetch Solcast estimated actuals for the
        recent past, populate `pv_forecast_log.actual_kw`. One Solcast call
        per run (1/10 daily quota). No-op if Solcast is disabled or quota
        is already exhausted."""
        if not self._solcast:
            return
        try:
            actuals = await self._solcast.get_actuals_by_period_end()
        except Exception:
            logger.exception("Solcast estimated_actuals fetch failed")
            return
        if not actuals:
            return
        touched = self._store.update_pv_actuals(actuals)
        logger.info(
            "PV actuals backfill: %d Solcast intervals → %d forecast-log rows updated",
            len(actuals),
            touched,
        )

    async def _fetch_bom(self) -> None:
        try:
            await self._bom.get_outdoor_temp()
        except Exception:
            logger.exception("BOM weather fetch failed")
