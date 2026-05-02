"""Main service: the tick loop that ties all components together."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .api import APIServer
from .api.log_buffer import RingBufferHandler
from .api.metrics import Metrics
from .clients.amber import AmberClient
from .clients.bom import BOMClient
from .clients.shelly import ManagedLoadManager
from .clients.sigenergy import SigenergyController
from .clients.solcast import SolcastClient
from .clients.unifi import UniFiOccupancyDetector
from .config import Config
from .curtailment import CurtailmentState
from .curtailment import evaluate as evaluate_curtailment
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
    PVProbeResult,
    SystemState,
    TelemetryRow,
    TickSnapshot,
    WeatherForecastLogRow,
)
from .validation import validate_telemetry

logger = logging.getLogger(__name__)

_VERSION = "0.2.0"

# If the most recent 5-min Amber fetch is older than this, we no longer trust
# the cached export price for the *current* slot. Wholesale feed-in prices can
# flip sign within minutes during solar-glut windows, so once we lose recency
# we clamp the export limit to 0 — choosing "miss revenue" over "pay to export
# while blind". 15 min is ~3 missed Amber update cycles, well beyond any
# transient 429 cool-down (≤300s observed in production).
EXPORT_PRICE_STALE_THRESHOLD = timedelta(minutes=15)

# Daily Amber usage fetch fires at 16:00 UTC every day (= 02:00 AEDT /
# 03:00 AEST local Canberra time). The NEM day rolls over at 14:00 UTC,
# so we land 2-3 hours after settlement and Amber has finished
# publishing yesterday. The same handler also runs once at startup, so
# this scheduled fire is the steady-state path; missed-day catch-up is
# automatic via the backfill logic.
_AMBER_USAGE_WAKE_OFFSET_S = 16 * 3600

# How far back to backfill amber_usage on first run (empty table).
# 30 days gives the dashboard a useful 30-day daily-summary panel
# straight after deploy without burning a lot of API budget — at most
# ceil(30/7)=5 calls (Amber's /usage window cap is 7 days).
_AMBER_USAGE_BACKFILL_DAYS = 30

# Pre-LP "uncap and measure" PV probe gate. The probe writes
# 40032=max + mode 2, sleeps 5 s for cascade settle, then reads true MPP.
# Below this threshold the probe is uninformative (no surplus to discover)
# and we save the 5 s plus the briefly-forced charge-then-overwrite if
# the LP picks discharge. PV_power_kw at the pre-probe `read_state` call
# is used for the gate check; it may be curtailed but it's a reliable
# floor on actual PV.
PV_PROBE_MIN_KW = 0.5


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
        self._metrics = Metrics()
        self._log_buffer: RingBufferHandler | None = None
        self._file_log_handler: logging.Handler | None = None
        self._api_server = APIServer(config.api, self)

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
        # Freshest snapshot kept in-memory so /plan/current can return
        # it without re-reading the NDJSON file. Set at the end of every
        # successful tick; stays None until the first snapshot is written.
        self._last_snapshot: TickSnapshot | None = None

    async def start(self) -> None:
        """Start the service: connect, initialise, then run wake loops."""
        # Attach the logging handlers before anything else logs — both
        # handlers capture the full startup sequence, not just the
        # post-connect tail.
        self._attach_log_handlers()
        logger.info("Starting energy optimiser v%s", _VERSION)
        self._running = True

        # Connect to Modbus
        modbus_ok = await self._sigenergy.connect()

        # Assert battery SOC limits at hardware level (reg 40046/47/48).
        # These bound local-mode charging/discharging so the inverter can't
        # drift outside our operating band during mode 2 or any fallback
        # path. Safe to call even without Remote EMS enabled — these are
        # basic battery-protection registers, not EMS-mode-specific.
        if modbus_ok:
            await self._sigenergy.assert_battery_soc_limits()

        # Initial fetches (5-min prices drive ticks; 30-min drives planning)
        amber_ok = await self._fetch_5min_prices()
        await self._fetch_30min_prices()
        if self._solcast:
            # Solcast has a hard 10/day quota — a crashloop without this
            # seed path burns it fast. If the log has a forecast from
            # within the last 4 hours, seed the client from it and skip
            # the initial API call; the next scheduled poll (every ~2.4h)
            # will refresh. 4h tolerates a restart during a normal
            # inter-poll gap without spending another quota slot — the
            # seed is a defensive cache, not a freshness guarantee.
            # If nothing cached, fall through to a live fetch.
            cached = self._store.read_latest_pv_forecast(max_age_minutes=240)
            if cached is not None:
                forecasts, fetched_at = cached
                self._solcast.seed_cache(forecasts, fetched_at)
                self._pv_forecast = forecasts
                age_min = (datetime.now(fetched_at.tzinfo) - fetched_at).total_seconds() / 60
                logger.info(
                    "Seeded Solcast cache from log (%d intervals, %.0f min old)"
                    " — skipping initial fetch",
                    len(forecasts),
                    age_min,
                )
            else:
                await self._fetch_solcast()
        await self._fetch_bom()

        # Backfill amber_usage so the dashboard daily-spend panel has
        # historical context immediately. Best-effort; failure here just
        # leaves the panel sparser until the daily wake loop catches up.
        await self._backfill_amber_usage()

        # Notify state machine
        self._state_machine.on_startup_complete(modbus_ok, amber_ok)

        if self._state_machine.should_fallback:
            await self._sigenergy.set_fallback()

        # Start the read-only HTTP API. Safe to run alongside the tick
        # loop — handlers never block on Modbus or take locks that the
        # tick path holds. Failure to start (e.g. port in use, missing
        # bearer-token env var) is logged and re-raised so systemd /
        # docker can restart and surface the misconfiguration.
        await self._api_server.start()

        # Build wake loops — each runs independently, aligned to wall clock
        from .wake_loop import WakeLoop

        self._wake_loops = [
            WakeLoop("tick", self._config.planner.tick_interval_s, self._tick),
            WakeLoop("verify", WATCHER_PERIOD_S, self._watcher.poll),
            # Amber 5-min poll: once per Amber slot, mid-slot. Aligned to
            # `:02:30/:07:30/:12:30…` UTC (period 300s, offset 150s) so
            # we land 2.5 min into each slot — late enough that Amber has
            # the new slot's prices, early enough that the next 2.5 min
            # of slot can still benefit. Replaces the old per-tick fetch
            # (5 calls/slot → blew through Amber's 50/5min bucket and
            # caused mid-slot LP plan flips when prices wobbled). On
            # failure, `last_5min_prices` is preserved and consumers use
            # `current_5min_price(now)` for time-correct lookup.
            WakeLoop(
                "prices_5min",
                self._config.amber.poll_5min_interval_s,
                self._fetch_5min_prices,
                offset_s=self._config.amber.poll_5min_offset_s,
            ),
            WakeLoop(
                "prices_30min", self._config.amber.poll_30min_interval_s, self._fetch_30min_prices
            ),
            WakeLoop(
                "telemetry", self._config.planner.telemetry_write_interval_s, self._write_telemetry
            ),
            WakeLoop("bom", self._config.weather.poll_interval_s, self._fetch_bom),
            WakeLoop("unifi", self._config.occupancy.poll_interval_s, self._poll_occupancy),
            # Re-assert hardware SOC limits hourly. These registers
            # (40046 backup SOC, 40048 discharge cutoff) can be reset
            # silently by the inverter — firmware update, power cycle,
            # local EMS override — and we'd only notice at the next
            # restart. Idempotent write; no wake-up race risk.
            # Deliberately skips 40047 (charge cutoff): pinned at
            # `soc_ceiling_pct` by `assert_battery_soc_limits` at
            # startup and never rewritten — see SPEC-ENERGY-01.md §5.4.
            # Excluding it here keeps the hourly re-assertion idempotent
            # against the startup write.
            WakeLoop("soc_limits", 3600, self._reassert_soc_limits),
            # Daily Amber /usage fetch — settled per-5-min spend that
            # lands on the bill. Fires at 16:00 UTC = 02:00 AEDT /
            # 03:00 AEST, well after NEM midnight. The handler is the
            # same backfill path used at startup, so a missed day (Amber
            # outage, service down) is caught next run automatically.
            WakeLoop(
                "amber_usage",
                86400,
                self._backfill_amber_usage,
                offset_s=_AMBER_USAGE_WAKE_OFFSET_S,
            ),
        ]
        # BOM hourly forecast — separate cadence from current-obs. Only
        # spawn the loop if a forecast URL is configured; empty URL means
        # the user hasn't opted in. Cheap to skip and easy to add later.
        if self._config.weather.bom_forecast_url:
            self._wake_loops.append(
                WakeLoop(
                    "bom_forecast",
                    self._config.weather.forecast_poll_interval_s,
                    self._fetch_bom_forecast,
                )
            )
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
            self._wake_loops.append(WakeLoop("pv_actuals", 86400, self._backfill_pv_actuals))

        logger.info("Spawning %d wake loops", len(self._wake_loops))
        try:
            await asyncio.gather(*[wl.run() for wl in self._wake_loops])
        except asyncio.CancelledError:
            logger.info("Wake loops cancelled")

    async def stop(self) -> None:
        """Gracefully stop the service."""
        logger.info("Stopping service")
        self._running = False

        # Stop the HTTP API first so no new requests can arrive while
        # we're tearing down the store / clients they might have read.
        try:
            await self._api_server.stop()
        except Exception:
            logger.exception("API server stop failed")

        # Detach our logging handlers so a subsequent Service (unusual
        # in production but common in tests) doesn't see duplicates on
        # the root logger.
        self._detach_log_handlers()

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
        """Wrap the real tick to capture wall-clock duration and error
        status in metrics. The exception is re-raised so the wake loop's
        own error handling still fires."""
        t0 = time.monotonic()
        errored = False
        try:
            await self._tick_body()
        except Exception:
            errored = True
            raise
        finally:
            duration_ms = (time.monotonic() - t0) * 1000.0
            self._metrics.record_tick_end(duration_ms, errored)

    async def _tick_body(self) -> None:
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

        # 2. Price age check — the 5-min poll runs on its own slot-aligned
        # wake loop now, not per-tick. We just sample the age here so the
        # state machine can degrade if the cache goes stale (e.g. sustained
        # 429 cool-down).
        self._state_machine.on_price_age_check(self._amber.prices_5min_age)

        # 3. (5-min/30-min/Solcast/BOM/UniFi refresh handled by their own wake loops)

        # 4. Read managed load statuses
        load_statuses = await self._loads.all_statuses()
        grid_kw_shelly = self._loads.get_mains_power(load_statuses)

        # 5. Build load profile and grab cached price arrays
        prices_5min = self._amber.last_5min_prices or []
        prices_30min = self._amber.last_prices or []
        # Current 5-min slot by time, not by index. The cached list may be
        # stale (poll failure or pre-mid-slot fetch) and `prices_5min[0]`
        # is one of the `previous=2` entries, not "now". None means no
        # 5-min interval covers the current wall-clock — callers fall
        # through to 30-min coverage or skip price-conditional logic.
        current_5min = self._amber.current_5min_price(now)
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
        # Overlay 5-min prices on top of 30-min for the LP's planning
        # horizon. 5-min entries (current + ~30 min ahead at 5-min
        # granularity from Amber) come FIRST so `_price_at`'s linear
        # scan finds them preferentially; the 30-min entries fill the
        # rest of the horizon. This lets the LP see and exploit 5-min
        # spikes inside otherwise-flat 30-min intervals — e.g. a brief
        # negative-export sub-window within a generally-expensive
        # evening interval.
        prices_planning = list(prices_5min) + list(prices_30min)
        breaker = self._lp_runtime.breaker
        is_probe = breaker.can_probe(now)

        # Will be populated by the PV probe and consumed by both the LP
        # (as slot-0 override) and the dispatch (as prefetched Phase-A).
        pv_probe: PVProbeResult | None = None
        pv_probe_lp_override_kw: float | None = None

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

            # Pre-LP "uncap and measure" PV probe. Replaces the Solcast
            # P10 forecast at slot 0 with measured truth so the LP's
            # non-anticipativity hedge doesn't gimp slot-0 battery rate
            # against a forecast scenario the present moment already
            # contradicts. Runs only when PV is meaningfully present
            # (gates out at night, deep cloud, dusk) and the measurement
            # is reliable (cascade had slack — battery and export were
            # below their respective caps). On any failure / saturation
            # we fall back silently to the per-scenario forecast.
            pv_probe = await self._maybe_run_pv_probe(state, tick_id)
            if pv_probe is not None and pv_probe.pv_kw is not None and not pv_probe.saturated:
                pv_probe_lp_override_kw = pv_probe.pv_kw

            lp_solution, lp_dispatch = await self._run_lp(
                state=state,
                prices_planning=prices_planning,
                pv_forecast=pv_forecast,
                load_profile=load_profile,
                managed_loads=load_statuses,
                slot_0_pv_override_kw=pv_probe_lp_override_kw,
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
            # Resolve and apply the export cap BEFORE the dispatch — the
            # mode-2 adaptive trim path reads live PV/load with the cap
            # already in force, and uses (export_cap_kw) directly when
            # computing the Phase-B trim.
            export_limit_kw = self._resolve_export_limit_kw(output.grid_export_limit_kw, tick_id)
            if export_limit_kw is not None:
                await self._sigenergy.set_export_limit_kw(export_limit_kw)
                self._last_export_limit_kw = export_limit_kw
            apply_ok = await self._sigenergy.apply_lp_dispatch(
                lp_dispatch,
                export_cap_kw=export_limit_kw or 0.0,
                # Reuse the pre-LP probe so the dispatch's mode-2 path
                # doesn't pay the Phase-A 5 s sleep again. Skipped
                # automatically on non-mode-2 dispatch paths (cap
                # registers get overwritten anyway). None when the
                # probe was gated out — dispatch falls back to its
                # built-in Phase-A.
                prefetched_probe=pv_probe,
            )
            self._metrics.record_dispatch_write(apply_ok)
            if apply_ok:
                self._metrics.record_dispatch(lp_dispatch)
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
                    export_price_ckwh=(current_5min.export_per_kwh if current_5min else None),
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

            # Apply load commands.
            for cmd in output.load_commands:
                if cmd.desired_relay_on is not None:
                    # SIGNAL_DRIVEN: continuous relay state, idempotent
                    await self._loads.set_relay(cmd.load_id, cmd.desired_relay_on)
                elif cmd.start_cycle:
                    # SHIFTABLE (legacy): one-shot cycle trigger
                    await self._loads.start_cycle(cmd.load_id)

        elif self._state_machine.should_fallback:
            # Same stale-price guard as the active path: don't trust a cached
            # export price that's aged out — block export entirely instead.
            price_age = self._amber.prices_5min_age
            stale = price_age is None or price_age > EXPORT_PRICE_STALE_THRESHOLD
            await self._sigenergy.set_fallback(
                export_price_ckwh=(current_5min.export_per_kwh if current_5min else None),
                block_export=stale,
            )

        # 9b. Post-dispatch read. The state captured at step 1 is what
        # the LP solved against; that's the right input for replay. But
        # the snapshot also wants to record what the inverter is doing
        # *in response* — without this second read, the snapshot at a
        # slot transition would show the previous slot's mode (the read
        # at step 1 happened before the dispatch). Best-effort: a failure
        # here just leaves `state_post_dispatch=None` and the snapshot
        # falls back to the pre-dispatch view.
        state_post_dispatch: SystemState | None = None
        try:
            state_post_dispatch = await self._sigenergy.read_state(
                outdoor_temp_c=outdoor_temp,
                occupied=occupied,
            )
        except Exception:
            logger.exception("Post-dispatch state read failed")

        # 10. Build and validate telemetry row
        # Current price comes from 5-min for accuracy; snapshot keeps both.
        # Time-based slot lookup (not [0]) so stale `last_5min_prices` after
        # a poll failure doesn't smuggle in an obsolete previous-window entry.
        current_import = current_5min.import_per_kwh if current_5min else None
        current_export = current_5min.export_per_kwh if current_5min else None
        current_spot = current_5min.spot_per_kwh if current_5min else None
        current_renewables = current_5min.renewables_pct if current_5min else None
        current_spike = current_5min.spike_status if current_5min else None

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
            # Extended inverter telemetry — 1:1 passthrough from state.
            soh_pct=state.soh_pct,
            cell_temp_avg_c=state.cell_temp_avg_c,
            cell_temp_max_c=state.cell_temp_max_c,
            cell_temp_min_c=state.cell_temp_min_c,
            cell_volt_avg_v=state.cell_volt_avg_v,
            cell_volt_max_v=state.cell_volt_max_v,
            cell_volt_min_v=state.cell_volt_min_v,
            pcs_temp_c=state.pcs_temp_c,
            available_charge_kw=state.available_charge_kw,
            available_discharge_kw=state.available_discharge_kw,
            running_state=state.running_state,
            alarm1=state.alarm1,
            alarm2=state.alarm2,
            alarm3=state.alarm3,
            alarm4=state.alarm4,
            alarm5=state.alarm5,
            lifetime_pv_kwh=state.lifetime_pv_kwh,
            lifetime_load_kwh=state.lifetime_load_kwh,
            lifetime_charge_kwh=state.lifetime_charge_kwh,
            lifetime_discharge_kwh=state.lifetime_discharge_kwh,
            lifetime_import_kwh=state.lifetime_import_kwh,
            lifetime_export_kwh=state.lifetime_export_kwh,
            mppt1_voltage_v=state.mppt1_voltage_v,
            mppt1_current_a=state.mppt1_current_a,
            mppt2_voltage_v=state.mppt2_voltage_v,
            mppt2_current_a=state.mppt2_current_a,
            mppt3_voltage_v=state.mppt3_voltage_v,
            mppt3_current_a=state.mppt3_current_a,
            mppt4_voltage_v=state.mppt4_voltage_v,
            mppt4_current_a=state.mppt4_current_a,
            grid_freq_hz=state.grid_freq_hz,
            phase_a_voltage_v=state.phase_a_voltage_v,
            phase_b_voltage_v=state.phase_b_voltage_v,
            phase_c_voltage_v=state.phase_c_voltage_v,
            remote_ems_mode=state.remote_ems_mode,
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
                # Same merged view the LP solved with — 5-min entries first
                # so they take precedence within their coverage window.
                price_forecast=prices_planning,
                pv_forecast=pv_forecast,
                load_profile=load_profile,
                managed_loads=load_statuses,
                maturity_level=load_profile.maturity_level,
                output=output,
                lp_solution=lp_solution,
                lp_dispatch=lp_dispatch,
                system_state_post_dispatch=state_post_dispatch,
                pv_probe=pv_probe,
                pv_avail_slot_0_used_kw=pv_probe_lp_override_kw,
            )
            self._snapshots.write(snapshot)
            self._last_snapshot = snapshot
        except Exception:
            logger.exception("Failed to write snapshot")

        # 13. Emit tick complete event
        # `price_ckwh` is the current 5-min IMPORT price (kept under that
        # name for back-compat with existing log readers); `export_ckwh`
        # is the same slot's export price. Both come from the merged
        # 5-min/30-min view the LP solved with — an export change without
        # an import change is exactly the signal that drove the
        # discharge-for-export decisions seen on 2026-04-25 deploy.
        emit(
            EventType.TICK_COMPLETE,
            {
                "soc": state.soc_pct,
                "action": output.battery_action.value,
                "price_ckwh": current_import,
                "export_ckwh": current_export,
                "reason": output.reason,
                "state": self._state_machine.state.value,
                "maturity": load_profile.maturity_level,
            },
            tick_id=tick_id,
        )

        # 14. Curtailment signal — flat-top heuristic. See curtailment.py.
        # Cheap (no network), purely from state we already have in hand.
        self._check_curtailment(state, tick_id)

        # 15. Update Prometheus gauges from the state we've already read.
        # Done before the heartbeat touch so a scrape between tick end
        # and heartbeat_age_s=0 can never see a state/heartbeat pair
        # that contradicts (age=0 but stale state).
        self._metrics.record_live_state(
            state=state,
            current_import_price=current_import,
            current_export_price=current_export,
            sigenergy_connected=self._sigenergy.connected,
            service_state=self._state_machine.state.value,
            circuit_breaker_open=self._lp_runtime.breaker.latched,
            heartbeat_age_s=None,  # derived at scrape time from file mtime
        )

        # 16. Heartbeat — update the mtime of the file the external watchdog
        # polls. This is the liveness signal that tells the watchdog "the
        # tick loop is still running". Touching happens here, at the end of
        # a successful tick, so an LP failure or Modbus hang earlier in the
        # pipeline leaves the heartbeat stale and eventually fires the
        # watchdog. Best-effort: never crash the tick on heartbeat-write
        # failure — the watchdog itself will fire if we go silent.
        self._touch_heartbeat()

    def _resolve_export_limit_kw(
        self, lp_export_limit_kw: float | None, tick_id: str
    ) -> float | None:
        """Apply the stale-price guard to the LP's intended export limit.

        Returns 0.0 instead of the LP's value when the cached 5-min Amber
        price is older than EXPORT_PRICE_STALE_THRESHOLD. The LP may have
        based its decision on a price that has since flipped sign, in which
        case exporting would cost us real money. Pass-through otherwise.
        """
        if lp_export_limit_kw is None or lp_export_limit_kw <= 0:
            return lp_export_limit_kw
        age = self._amber.prices_5min_age
        if age is not None and age <= EXPORT_PRICE_STALE_THRESHOLD:
            return lp_export_limit_kw
        emit(
            EventType.EXPORT_BLOCKED_STALE_PRICE,
            {
                "lp_export_limit_kw": lp_export_limit_kw,
                "price_age_seconds": age.total_seconds() if age else None,
                "threshold_seconds": EXPORT_PRICE_STALE_THRESHOLD.total_seconds(),
            },
            tick_id=tick_id,
        )
        return 0.0

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
        try:
            self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            self.heartbeat_path.touch(exist_ok=True)
        except Exception:
            logger.exception("heartbeat touch failed (path=%s)", self.heartbeat_path)

    # ── ServiceProbe surface ─────────────────────────────────────
    # Read-only properties the API server reaches via request.app.

    @property
    def version(self) -> str:
        return _VERSION

    @property
    def heartbeat_path(self) -> Path:
        return Path(os.environ.get("EO_HEARTBEAT_PATH", "/var/lib/energy-optimiser/heartbeat"))

    @property
    def service_state(self) -> str:
        return self._state_machine.state.value

    @property
    def sigenergy_connected(self) -> bool:
        return self._sigenergy.connected

    @property
    def db_connection(self):  # duckdb.DuckDBPyConnection — avoid import cost here
        return self._store.connection

    @property
    def metrics(self) -> Metrics:
        return self._metrics

    @property
    def log_buffer(self) -> RingBufferHandler | None:
        return self._log_buffer

    @property
    def last_snapshot(self) -> TickSnapshot | None:
        """Most recent TickSnapshot written this tick, or None until the
        first post-startup tick completes. Exposed so /plan/current can
        return the live plan without re-reading the NDJSON file."""
        return self._last_snapshot

    @property
    def snapshot_dir(self) -> Path:
        """Directory where TickSnapshots are persisted as daily .ndjson.gz
        files. Used by /snapshots to build the DuckDB read_json glob."""
        return Path(self._config.storage.snapshot_dir)

    @property
    def battery_config(self):
        """Battery config (SOC floor/ceiling, charge/discharge caps).
        Exposed for /dashboard/config so the SOC panel draws the floor
        line at the actually-configured value."""
        return self._config.battery

    def _attach_log_handlers(self) -> None:
        """Attach the ring buffer and (if configured) a rotating file
        handler to the root logger.

        - The ring buffer powers /logs — bounded, in-memory, fast. It
          is always attached: the overhead is a few MB of dicts and it
          can be queried by operators or agents even when the rotating
          file on disk isn't configured.
        - The file handler is the durable log of record — Docker's log
          driver also captures stdout, but a host-mounted file gives
          operators something to grep without invoking docker logs.
        """
        import logging.handlers

        api_cfg = self._config.api
        root = logging.getLogger()

        self._log_buffer = RingBufferHandler(api_cfg.log_ring_buffer_size)
        self._log_buffer.setLevel(logging.DEBUG)
        root.addHandler(self._log_buffer)

        if api_cfg.log_file_path:
            try:
                log_path = Path(api_cfg.log_file_path)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                fh = logging.handlers.RotatingFileHandler(
                    log_path,
                    maxBytes=api_cfg.log_file_max_bytes,
                    backupCount=api_cfg.log_file_backup_count,
                    encoding="utf-8",
                )
                fh.setLevel(logging.INFO)
                fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
                root.addHandler(fh)
                self._file_log_handler = fh
            except Exception:
                logger.exception(
                    "Failed to open rotating log file at %s — continuing "
                    "with stdout/ring-buffer logging only",
                    api_cfg.log_file_path,
                )

    def _detach_log_handlers(self) -> None:
        root = logging.getLogger()
        for h in (self._log_buffer, self._file_log_handler):
            if h is not None:
                try:
                    root.removeHandler(h)
                    h.close()
                except Exception:
                    logger.exception("Failed to close log handler %r", h)
        self._log_buffer = None
        self._file_log_handler = None

    # ── LP Execution ─────────────────────────────────────────────

    async def _maybe_run_pv_probe(self, state: SystemState, tick_id: str) -> PVProbeResult | None:
        """Run a Phase-A "uncap and measure" PV probe with gating.

        Skips when:
          - PV at the pre-tick read is below ``PV_PROBE_MIN_KW`` (night,
            deep cloud — no signal to recover).
          - Last applied export cap is unknown (haven't dispatched yet
            this session — saturation check would be missing the export
            term and would falsely report unsaturated).

        Otherwise writes 40032=max + mode 2, sleeps for the cascade
        settle time, reads true MPP plus battery acceptance and
        export, and returns a ``PVProbeResult``. The dispatch path
        downstream consumes the same probe so the redundant Phase-A
        sleep is paid only once per tick.

        Failures (Modbus blip, telemetry blind during settle) return a
        result with ``pv_kw=None``; the caller falls back to the
        per-scenario forecast at the LP and to the in-dispatch Phase-A
        retry inside ``_apply_mode2_adaptive_charge``.
        """
        if state.pv_power_kw is None or state.pv_power_kw < PV_PROBE_MIN_KW:
            return None
        # Saturation check needs export_cap_kw. Fall back to None when
        # we haven't applied a cap yet this session (first tick after
        # boot before any dispatch); the probe still runs but
        # `saturated` is reported as False (no cap = no saturation
        # signal) and the override path treats that as "trust the
        # measurement".
        export_cap_kw = self._last_export_limit_kw

        probe = await self._sigenergy.measure_uncapped_pv(
            export_cap_kw=export_cap_kw,
        )
        if probe is None:
            # Hard write failure — the inverter's register state is
            # unknown. Logged for visibility; downstream fallback path
            # in `apply_lp_dispatch` will overwrite cap+mode anyway.
            emit(
                EventType.MODE2_TRIM_BLIND,
                {
                    "phase": "pre_lp_probe",
                    "reason": "uncap_write_failed",
                },
                tick_id=tick_id,
            )
            return None
        emit(
            EventType.MODE2_TRIM,
            {
                "phase": "pre_lp_probe",
                "pv_kw": probe.pv_kw,
                "saturated": probe.saturated,
                "bat_kw": probe.bat_kw,
                "bat_avail_kw": probe.bat_avail_kw,
                "grid_export_kw": probe.grid_export_kw,
                "export_cap_kw": probe.export_cap_kw,
                "house_kw": probe.house_kw,
                "soc_pct": probe.soc_pct,
            },
            tick_id=tick_id,
        )
        return probe

    async def _run_lp(
        self,
        *,
        state,
        prices_planning,
        pv_forecast,
        load_profile,
        managed_loads,
        slot_0_pv_override_kw: float | None = None,
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
        t0 = time.monotonic()
        # Parse the price-scenario mode once per tick — fails loud
        # rather than silently degrading to POINT if the operator
        # mistypes the config string.
        try:
            price_scenario_mode = self._config.planner.parsed_price_scenario_mode
        except ValueError:
            logger.exception(
                "lp_price_scenario_mode=%r invalid; falling back to PRICE_SCENARIO_MODE",
                self._config.planner.lp_price_scenario_mode,
            )
            price_scenario_mode = None
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
                    price_scenario_mode=price_scenario_mode,
                    wear_cost_per_kwh=self._config.planner.lp_wear_cost_per_kwh,
                    terminal_floor_override_pct=self._config.planner.lp_terminal_floor_override_pct,
                    slot_0_pv_override_kw=slot_0_pv_override_kw,
                ),
                timeout=self._config.planner.lp_wall_clock_timeout_s,
            )
        except TimeoutError:
            self._metrics.record_lp_solve("timeout", (time.monotonic() - t0) * 1000.0)
            await self._lp_fallback(FallbackReason.LP_TIMEOUT)
            return None, None
        except Exception:
            logger.exception("LP solve raised unexpectedly")
            self._metrics.record_lp_solve("error", (time.monotonic() - t0) * 1000.0)
            await self._lp_fallback(FallbackReason.LP_ERROR)
            return None, None

        # solve_time_ms comes from the solver itself (wall-clock inside
        # the thread); prefer it over our wait_for measurement because
        # it excludes thread-scheduling overhead.
        self._metrics.record_lp_solve(solution.status.value.lower(), float(solution.solve_time_ms))

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

        dispatch = dispatch_from_slot(
            solution.slot_0,
            self._config.battery,
            current_soc_pct=state.soc_pct,
            measured_pv_kw=state.pv_power_kw,
        )
        return solution, dispatch

    async def _lp_fallback(self, reason: FallbackReason) -> None:
        """Trigger the paranoid fallback writes, latch the breaker, and
        emit the BREAKER_LATCHED event. Used by every LP failure path so
        the side effects stay consistent."""
        prices = self._amber.last_5min_prices
        export_price = prices[0].export_per_kwh if prices else None
        await trigger_fallback(
            self._sigenergy,
            self._loads.controllers,
            reason,
            export_price_ckwh=export_price,
        )
        await self._lp_runtime.latch(reason)
        self._metrics.record_circuit_breaker_trip(reason.value)
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

    async def _backfill_amber_usage(self) -> None:
        """Pull settled Amber /usage rows from the latest persisted day
        up to yesterday (NEM date). Idempotent: re-fetched days UPSERT
        on (ts, channel) so wake loop overlap and Amber's occasional
        same-day re-publishes are both safe.

        Called at startup AND on the daily wake loop — the wake handler
        and the startup-catchup handler are the same code path. Empty
        table on first run triggers a `_AMBER_USAGE_BACKFILL_DAYS` window
        backfill so the dashboard's daily-spend panel has history straight
        away. Each /usage call covers ≤7 days (Amber's cap) so the loop
        chunks accordingly.
        """
        # NEM is UTC+10 year-round. "Yesterday's NEM date" is the date
        # of the most recent fully-settled NEM day — that's the latest
        # Amber will publish.
        nem_now = now_utc() + timedelta(hours=10)
        yesterday_nem = (nem_now - timedelta(days=1)).date()

        latest = self._store.latest_amber_usage_date()
        if latest is None:
            start = yesterday_nem - timedelta(days=_AMBER_USAGE_BACKFILL_DAYS - 1)
        else:
            try:
                start = date.fromisoformat(latest) + timedelta(days=1)
            except ValueError:
                logger.warning(
                    "amber_usage latest_nem_date=%r unparseable; backfilling default window",
                    latest,
                )
                start = yesterday_nem - timedelta(days=_AMBER_USAGE_BACKFILL_DAYS - 1)

        if start > yesterday_nem:
            return  # already up to date

        cur = start
        total = 0
        actual_min: str | None = None
        actual_max: str | None = None
        while cur <= yesterday_nem:
            chunk_end = min(cur + timedelta(days=6), yesterday_nem)
            try:
                rows = await self._amber.get_usage_intervals(
                    cur.isoformat(),
                    chunk_end.isoformat(),
                )
            except Exception:
                logger.exception(
                    "amber_usage fetch failed for %s..%s — stopping backfill",
                    cur,
                    chunk_end,
                )
                return
            if rows:
                self._store.write_amber_usage(rows)
                total += len(rows)
                # Track the date range Amber actually returned. The
                # requested window can extend past Amber's retention
                # (typically ~20 days for newer sites) — logging the
                # requested range was misleading.
                chunk_dates = sorted({r.nem_date for r in rows})
                if chunk_dates:
                    if actual_min is None or chunk_dates[0] < actual_min:
                        actual_min = chunk_dates[0]
                    if actual_max is None or chunk_dates[-1] > actual_max:
                        actual_max = chunk_dates[-1]
            cur = chunk_end + timedelta(days=1)
        if total:
            logger.info(
                "amber_usage backfill: %d rows covering %s..%s (requested %s..%s)",
                total,
                actual_min,
                actual_max,
                start,
                yesterday_nem,
            )

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

    async def _reassert_soc_limits(self) -> None:
        """Hourly re-assertion of the hardware SOC limits the LP doesn't
        tick-manage. Best-effort — a failed write logs but doesn't
        propagate; the next hour will try again."""
        try:
            await self._sigenergy.assert_discharge_soc_limits()
        except Exception:
            logger.exception("assert_discharge_soc_limits failed")

    async def _fetch_bom_forecast(self) -> None:
        """Fetch BOM hourly forecast and persist every interval.

        Best-effort: never raises, empty list on any failure. The client
        itself swallows HTTP/parse errors. Each fetch is logged in full
        so forecast evolution can be analysed offline, mirroring the
        pv_forecast_log redundancy pattern.
        """
        try:
            intervals = await self._bom.get_hourly_forecast()
        except Exception:
            logger.exception("BOM forecast fetch raised unexpectedly")
            return
        if not intervals:
            return
        fetched_at = now_utc()
        rows = [
            WeatherForecastLogRow(
                fetched_at=fetched_at,
                period_end=iv.period_end,
                temp_c=iv.temp_c,
                apparent_temp_c=iv.apparent_temp_c,
                humidity_pct=iv.humidity_pct,
                rain_chance_pct=iv.rain_chance_pct,
                rain_mm=iv.rain_mm,
                wind_kmh=iv.wind_kmh,
            )
            for iv in intervals
        ]
        try:
            self._store.write_weather_forecast_log(rows)
            logger.info("Logged %d BOM forecast intervals", len(rows))
        except Exception:
            logger.exception("weather_forecast_log persist failed")
