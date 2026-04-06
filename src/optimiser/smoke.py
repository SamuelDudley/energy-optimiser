"""Smoke test CLI — verifies wiring and credentials before the main service
touches real hardware.

This is a separate entry point from `python -m optimiser` so there's no
chance of a "dry run" flag accidentally being left enabled in production.
By design, this module can **never** write to Modbus, toggle a relay, or
mutate anything on any API — every operation is read-only or purely in-memory.

Risk profile:
  --modbus-read : reads a handful of input registers; no writes, no side
                   effects on the inverter
  --api-probe   : one read each against Amber, Solcast, BOM, UniFi;
                   Solcast costs 1 of the 10/day quota — consider before
                   running repeatedly
  --dry-tick    : runs the full tick pipeline (LP solve, dispatch decision,
                   snapshot write) without applying anything to hardware.
                   Consumes 1 Amber + 1 BOM + maybe 1 Solcast call. No writes.
  --all         : runs all of the above, in order, stopping at the first failure

Usage:
    python -m optimiser.smoke --config /etc/energy-optimiser/config.toml --all
    python -m optimiser.smoke -c config.toml --modbus-read
    python -m optimiser.smoke -c config.toml --dry-tick
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime

from .clients.amber import AmberClient
from .clients.bom import BOMClient
from .clients.shelly import ManagedLoadManager
from .clients.sigenergy import (
    REG_EMS_WORK_MODE,
    REG_GRID_ACTIVE_POWER,
    REG_GRID_SENSOR_STATUS,
    REG_PLANT_ESS_POWER,
    REG_PLANT_ESS_SOC,
    REG_PLANT_PV_POWER,
    SigenergyController,
)
from .clients.solcast import SolcastClient
from .clients.unifi import UniFiOccupancyDetector
from .config import Config, load_config
from .logging_utils import setup_logging
from .lp.dispatch import dispatch_from_slot
from .lp.loads import build_lp_loads
from .lp.result import SolveStatus
from .lp.solver import solve_stochastic
from .profiler import build_load_profile
from .store import TelemetryStore

logger = logging.getLogger(__name__)


# ANSI colour codes for terminal output. Falls back gracefully if stdout
# isn't a TTY (Docker logs, piped output).
_USE_COLOUR = sys.stdout.isatty()
_GREEN = "\033[32m" if _USE_COLOUR else ""
_RED = "\033[31m" if _USE_COLOUR else ""
_YELLOW = "\033[33m" if _USE_COLOUR else ""
_DIM = "\033[2m" if _USE_COLOUR else ""
_RESET = "\033[0m" if _USE_COLOUR else ""


def _ok(msg: str) -> None:
    print(f"  {_GREEN}✓{_RESET} {msg}")


def _fail(msg: str) -> None:
    print(f"  {_RED}✗{_RESET} {msg}")


def _warn(msg: str) -> None:
    print(f"  {_YELLOW}!{_RESET} {msg}")


def _info(msg: str) -> None:
    print(f"  {_DIM}·{_RESET} {msg}")


def _section(title: str) -> None:
    print(f"\n{title}")
    print("─" * len(title))


# ── Modbus read smoke ────────────────────────────────────────────


async def smoke_modbus_read(config: Config) -> bool:
    """Read key registers from the Sigenergy inverter. Pure reads — no writes.

    Closes KNOWN-ISSUES #3 if the values look sane: SOC matches the app,
    PV power is roughly today's generation, EMS mode is what you expect.

    Returns True if all reads succeeded with plausible values.
    """
    _section("Modbus read smoke test")
    _info(f"Connecting to {config.sigenergy.host}:{config.sigenergy.port}")

    controller = SigenergyController(config.sigenergy, config.battery)
    try:
        connected = await controller.connect()
    except Exception as exc:
        _fail(f"Connect raised: {exc}")
        return False

    if not connected:
        _fail("Failed to connect to Modbus")
        return False
    _ok("Connected")

    # Read individual registers with per-register reporting so a single
    # bad register doesn't mask successes. Reads go via the private
    # helpers; this is the only place we reach through the abstraction,
    # because we want to report each register separately for diagnostics.
    registers = [
        (REG_EMS_WORK_MODE, "u16", "EMS work mode", None),
        (REG_GRID_SENSOR_STATUS, "u16", "Grid sensor status", None),
        (REG_PLANT_ESS_SOC, "u16", "Plant ESS SOC", 10.0),  # gain=10, %
        (REG_GRID_ACTIVE_POWER, "s32", "Grid active power", 1000.0),  # gain=1000, kW
        (REG_PLANT_ESS_POWER, "s32", "Plant ESS power", 1000.0),
        (REG_PLANT_PV_POWER, "s32", "Plant PV power", 1000.0),
    ]

    all_ok = True
    for addr, dtype, name, gain in registers:
        try:
            if dtype == "u16":
                raw = await controller._read_input_u16(addr)
            else:
                raw = await controller._read_input_s32(addr)
        except Exception as exc:
            _fail(f"{name} ({addr}): read raised {exc}")
            all_ok = False
            continue

        if raw is None:
            _fail(f"{name} ({addr}): returned None")
            all_ok = False
            continue

        if gain is not None:
            scaled = raw / gain
            unit = "%" if gain == 10.0 else "kW"
            _ok(f"{name} ({addr}): raw={raw}, scaled={scaled:.2f} {unit}")
        else:
            _ok(f"{name} ({addr}): raw={raw}")

    if all_ok:
        # Derive the composite state as a final sanity check
        state = await controller.read_state(outdoor_temp_c=None, occupied=True)
        if state is not None:
            grid_s = f"{state.grid_power_kw:+.2f}" if state.grid_power_kw is not None else "None"
            hl_s = (
                f"{state.house_load_kw:.2f}"
                if state.house_load_kw is not None
                else "None (grid sensor offline or derivation absurd)"
            )
            _info(
                f"Derived house_load = pv ({state.pv_power_kw:.2f}) + "
                f"grid ({grid_s}) − battery "
                f"({state.battery_power_kw:.2f}) = {hl_s} kW"
            )
            if state.house_load_kw is not None and abs(state.house_load_kw) > 20.0:
                _warn("house_load outside plausible range (> 20kW) — check register signs")
            if state.house_load_kw is None:
                _warn(
                    "house_load nulled — see event log for reason; "
                    "closes KNOWN-ISSUES S2 when resolved on hardware"
                )
        else:
            _fail("read_state returned None after individual reads succeeded")
            all_ok = False

    controller.close()
    return all_ok


# ── API probes ───────────────────────────────────────────────────


async def smoke_api_probe(config: Config) -> bool:
    """One read against each enabled external API. Confirms credentials,
    network reachability, and response shape.

    NOTE: Solcast costs 1 of 10/day quota. Don't call this in a loop."""
    _section("API probe smoke test")
    all_ok = True

    # Amber
    _info("Amber: fetching 5-min prices (1 request)")
    amber = AmberClient(config.amber)
    try:
        prices = await amber.get_5min_prices()
        if prices:
            p = prices[0]
            _ok(
                f"Amber: got {len(prices)} intervals, "
                f"first: import={p.import_per_kwh:.2f}c, "
                f"export={p.export_per_kwh:.2f}c, spike={p.spike_status}"
            )
        else:
            _fail("Amber: returned empty list")
            all_ok = False
    except Exception as exc:
        _fail(f"Amber: {exc}")
        all_ok = False
    finally:
        await amber.close()

    # Solcast — guarded by enabled flag
    if config.solcast.enabled:
        _info("Solcast: fetching PV forecast (costs 1/10 daily quota)")
        solcast = SolcastClient(config.solcast)
        try:
            forecast = await solcast.get_forecast()
            if forecast:
                p = forecast[0]
                _ok(
                    f"Solcast: got {len(forecast)} intervals, "
                    f"next: p10={p.pv_estimate10_kw:.2f}, "
                    f"p50={p.pv_estimate_kw:.2f}, p90={p.pv_estimate90_kw:.2f} kW"
                )
            else:
                _warn("Solcast: returned empty list (quota exhausted?)")
        except Exception as exc:
            _fail(f"Solcast: {exc}")
            all_ok = False
        finally:
            await solcast.close()
    else:
        _info("Solcast: disabled in config, skipping")

    # BOM — no auth, no quota
    _info("BOM: fetching outdoor temp")
    bom = BOMClient(config.weather)
    try:
        temp = await bom.get_outdoor_temp()
        if temp is not None:
            _ok(f"BOM: {temp:.1f}°C")
            if not (-10 < temp < 50):
                _warn("BOM temperature outside plausible range")
        else:
            _warn("BOM: returned None (stale data?)")
    except Exception as exc:
        _fail(f"BOM: {exc}")
        all_ok = False
    finally:
        await bom.close()

    # UniFi — read-only, may require session auth
    _info("UniFi: polling client list")
    unifi = UniFiOccupancyDetector(config.occupancy)
    try:
        occupied = await unifi.poll()
        _ok(f"UniFi: occupied={occupied}")
    except Exception as exc:
        # UniFi compat is a known-risky area (#7). Don't fail the whole
        # suite — report and continue.
        _warn(f"UniFi: {exc} (known issue #7 — varies by controller firmware)")
    finally:
        await unifi.close()

    # Shelly — LAN, no auth for status reads
    if config.managed_loads:
        _info("Shelly: reading status for all managed loads")
        loads = ManagedLoadManager(config.managed_loads)
        try:
            statuses = await loads.all_statuses()
            if len(statuses) < len(config.managed_loads):
                _warn(
                    f"Shelly: {len(statuses)}/{len(config.managed_loads)} "
                    "loads responded (check network / credentials)"
                )
            for s in statuses:
                _ok(
                    f"Shelly {s.load_id}: power={s.power_kw:.2f} kW, "
                    f"relay={s.relay_on}, energy_today={s.energy_today_kwh:.2f} kWh"
                )
        except Exception as exc:
            _fail(f"Shelly: {exc}")
            all_ok = False
        finally:
            await loads.close()
    else:
        _info("Shelly: no managed loads configured, skipping")

    return all_ok


# ── Dry tick ─────────────────────────────────────────────────────


async def smoke_dry_tick(config: Config) -> bool:
    """Run the full tick pipeline but skip every write.

    Fetches real prices/PV/weather, reads real Modbus state, builds a real
    load profile, solves the real LP, computes the real dispatch — then
    *prints* what would be written and exits. No `apply_lp_dispatch`,
    no `set_relay`, no `set_export_limit_kw`. The snapshot is written
    to the configured path.
    """
    _section("Dry tick — full pipeline, no writes")
    all_ok = True

    # 1. Read Modbus state
    _info("Reading Modbus state...")
    sigenergy = SigenergyController(config.sigenergy, config.battery)
    if not await sigenergy.connect():
        _fail("Modbus connect failed — dry tick cannot proceed")
        return False
    state = await sigenergy.read_state(outdoor_temp_c=None, occupied=True)
    sigenergy.close()
    if state is None:
        _fail("read_state returned None")
        return False
    grid_s = f"{state.grid_power_kw:+.2f}" if state.grid_power_kw is not None else "None"
    hl_s = f"{state.house_load_kw:.2f}" if state.house_load_kw is not None else "None"
    _ok(
        f"State: SOC={state.soc_pct:.1f}% grid={grid_s} "
        f"pv={state.pv_power_kw:.2f} battery={state.battery_power_kw:+.2f} "
        f"house={hl_s} kW"
    )

    # 2. Fetch prices
    _info("Fetching Amber 30-min prices (for LP horizon)...")
    amber = AmberClient(config.amber)
    try:
        prices_30min = await amber.get_current_prices()
        _ok(
            f"Prices: {len(prices_30min)} intervals, "
            f"next: import={prices_30min[0].import_per_kwh:.2f}c, "
            f"export={prices_30min[0].export_per_kwh:.2f}c"
        )
    except Exception as exc:
        _fail(f"Amber: {exc}")
        await amber.close()
        return False
    await amber.close()

    # 3. Fetch PV forecast
    pv_forecast = None
    if config.solcast.enabled:
        _info("Fetching Solcast forecast (costs 1/10 daily quota)...")
        solcast = SolcastClient(config.solcast)
        try:
            pv_forecast = await solcast.get_forecast()
            if pv_forecast:
                _ok(f"PV forecast: {len(pv_forecast)} intervals")
        except Exception as exc:
            _warn(f"Solcast: {exc} (LP will run without PV forecast)")
        finally:
            await solcast.close()

    # 4. Build load profile from DuckDB
    _info("Building load profile from DuckDB...")
    store = TelemetryStore(config.storage)
    try:
        load_profile = build_load_profile(
            store,
            outdoor_temp_c=state.outdoor_temp_c,
            occupied=True,
            timestamp=datetime.now(UTC),
        )
        _ok(
            f"Load profile: maturity={load_profile.maturity_level}, "
            f"context={load_profile.context!r}"
        )
    finally:
        store.close()

    # 5. Fetch managed load statuses
    load_statuses: list = []
    if config.managed_loads:
        _info("Fetching managed load statuses...")
        loads = ManagedLoadManager(config.managed_loads)
        try:
            load_statuses = await loads.all_statuses()
            _ok(f"Loads: {len(load_statuses)} responded")
        finally:
            await loads.close()

    # 6. Build LP loads and solve
    _info("Running stochastic LP solve...")
    lp_loads = build_lp_loads(config.managed_loads)

    try:
        solution = await asyncio.wait_for(
            asyncio.to_thread(
                solve_stochastic,
                state=state,
                prices_planning=prices_30min,
                pv_forecast=pv_forecast,
                load_profile=load_profile,
                managed_loads=load_statuses,
                lp_loads=lp_loads,
                battery_config=config.battery,
                scenario_weights=config.planner.lp_scenario_weights,
            ),
            timeout=config.planner.lp_wall_clock_timeout_s,
        )
    except TimeoutError:
        _fail(f"LP exceeded wall-clock timeout ({config.planner.lp_wall_clock_timeout_s}s)")
        return False
    except Exception as exc:
        _fail(f"LP raised: {exc}")
        return False

    _ok(
        f"LP: status={solution.status.value}, "
        f"cost_cents={solution.expected_total_cost_cents:.2f}, "
        f"solve_ms={solution.solve_time_ms}"
    )

    if solution.status not in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE):
        _warn("LP didn't find a usable solution — real service would fall back")
        return False

    if solution.slot_0 is None:
        _fail("LP returned OK status but slot_0 is None")
        return False

    # 7. Derive dispatch and report what WOULD be written
    dispatch = dispatch_from_slot(solution.slot_0)
    _info("")
    _info("Would write to inverter:")
    _info(f"  Register 40031 (mode)    = {dispatch.mode.value} ({dispatch.mode.name})")
    if dispatch.kind.value == "charge":
        _info(
            f"  Register 40032 (cap)     = {int(dispatch.cap_kw * 1000)} "
            f"({dispatch.cap_kw:.2f} kW charge)"
        )
    elif dispatch.kind.value == "discharge":
        _info(
            f"  Register 40034 (cap)     = {int(dispatch.cap_kw * 1000)} "
            f"({dispatch.cap_kw:.2f} kW discharge)"
        )
    else:
        _info("  (no cap — SELF_CONSUME)")

    if solution.grid_export_limit_kw is not None:
        _info(f"  Register 40038 (export)  = {int(solution.grid_export_limit_kw * 1000)}")

    _info("")
    _info("Would send to managed loads:")
    if solution.load_commands:
        for cmd in solution.load_commands:
            action = (
                "close relay"
                if cmd.desired_relay_on
                else "open relay"
                if cmd.desired_relay_on is False
                else "no change"
            )
            _info(f"  {cmd.load_id}: {action} (reason: {cmd.reason})")
    else:
        _info("  (none)")

    _info("")
    _info(f"LP intent: battery_kw = {solution.slot_0.battery_kw:+.2f} ({dispatch.kind.value})")
    _info(f"Expected SOC end-of-slot: {solution.slot_0.soc_pct_end:.1f}%")

    return all_ok


# ── Offline sanity ───────────────────────────────────────────────


async def smoke_offline(config: Config) -> bool:
    """Purely in-memory sanity check — no network, no Modbus.

    Confirms the LP and dispatch modules work end-to-end with synthetic
    inputs. Useful after a code change, before trying the real thing.
    """
    _section("Offline LP sanity check")
    from datetime import timedelta

    from .types import PriceInterval, SystemState

    now = datetime.now(UTC)

    # Synthetic state: low SOC so the LP has real reason to care about
    # charging during the cheap window. Mid-SOC + modest load often yields
    # SELF_CONSUME (battery covers house load) even when prices are cheap,
    # which is a reasonable LP decision but makes for a fuzzy sanity check.
    state = SystemState(
        timestamp=now,
        soc_pct=25.0,
        battery_power_kw=0.0,
        pv_power_kw=0.0,
        grid_power_kw=1.5,
        house_load_kw=1.5,
        ems_mode=2,
        outdoor_temp_c=18.0,
        occupied=True,
    )

    # Synthetic prices: very cheap for 4 hours, then very expensive — big
    # enough spread that the LP should definitely want to charge now to
    # discharge later, overcoming wear cost + round-trip efficiency loss.
    prices = []
    for i in range(96):
        minute_offset = i * 30
        start = now + timedelta(minutes=minute_offset)
        cheap = i < 8
        prices.append(
            PriceInterval(
                start=start,
                end=start + timedelta(minutes=30),
                import_per_kwh=2.0 if cheap else 50.0,
                export_per_kwh=1.0 if cheap else 20.0,
                spot_per_kwh=1.0 if cheap else 25.0,
                renewables_pct=40.0,
                spike_status="none",
                descriptor="neutral",
            )
        )

    from .types import LoadProfile

    profile = LoadProfile(slots=[1.5] * 48, maturity_level=0, context="synthetic")

    _info("Solving with synthetic inputs (cheap now, expensive later)...")
    try:
        solution = await asyncio.wait_for(
            asyncio.to_thread(
                solve_stochastic,
                state=state,
                prices_planning=prices,
                pv_forecast=None,
                load_profile=profile,
                managed_loads=[],
                lp_loads=[],
                battery_config=config.battery,
            ),
            timeout=config.planner.lp_wall_clock_timeout_s,
        )
    except Exception as exc:
        _fail(f"LP raised: {exc}")
        return False

    if solution.status not in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE):
        _fail(f"LP status: {solution.status.value}")
        return False

    _ok(f"LP solved: status={solution.status.value}, solve_ms={solution.solve_time_ms}")

    if solution.slot_0 is None:
        _fail("slot_0 is None")
        return False

    # Sanity: with a 25× price ratio between cheap and expensive windows,
    # the LP should show a clear charge-then-discharge pattern somewhere
    # in the trajectory. Slot-0 alone isn't diagnostic — the LP might
    # correctly cover small load from battery in slot 0 and ramp up
    # charging later in the cheap window. Look at the whole cheap period.
    slot_minutes = 5
    cheap_end_slot = (4 * 60) // slot_minutes  # 4h × 60min / 5min = 48 slots
    cheap_trajectory = solution.forward_trajectory[:cheap_end_slot]
    cheap_charge_kwh = sum(max(0.0, s.battery_kw) * (slot_minutes / 60.0) for s in cheap_trajectory)
    cheap_discharge_kwh = sum(
        max(0.0, -s.battery_kw) * (slot_minutes / 60.0) for s in cheap_trajectory
    )
    expensive_trajectory = solution.forward_trajectory[cheap_end_slot : cheap_end_slot * 2]
    expensive_discharge_kwh = sum(
        max(0.0, -s.battery_kw) * (slot_minutes / 60.0) for s in expensive_trajectory
    )

    _info(
        f"Cheap window (first 4h):  charged {cheap_charge_kwh:.2f} kWh, "
        f"discharged {cheap_discharge_kwh:.2f} kWh"
    )
    _info(f"Expensive window (next 4h): discharged {expensive_discharge_kwh:.2f} kWh")

    if cheap_charge_kwh > 5.0 and expensive_discharge_kwh > 2.0:
        _ok(
            "LP charges during cheap window and discharges during expensive — "
            "arbitrage working as expected"
        )
    elif cheap_charge_kwh < 1.0:
        _warn(
            "LP barely charges during cheap window — check battery config "
            "(capacity, SOC bounds, charge rate)"
        )
    else:
        _info("LP behaviour: charge/discharge amounts plausible but not decisive")

    return True


# ── Entry point ──────────────────────────────────────────────────


async def main_async(args) -> int:
    config = load_config(args.config)

    run_offline = args.offline or args.all
    run_modbus = args.modbus_read or args.all
    run_api = args.api_probe or args.all
    run_tick = args.dry_tick or args.all

    if not any([run_offline, run_modbus, run_api, run_tick]):
        print(
            "No smoke test selected. Use --all or --offline/--modbus-read/--api-probe/--dry-tick."
        )
        return 1

    results: dict[str, bool] = {}

    if run_offline:
        results["offline"] = await smoke_offline(config)
    if run_modbus:
        results["modbus"] = await smoke_modbus_read(config)
    if run_api:
        results["api"] = await smoke_api_probe(config)
    if run_tick:
        results["dry_tick"] = await smoke_dry_tick(config)

    _section("Summary")
    any_failed = False
    for name, ok in results.items():
        if ok:
            _ok(f"{name}")
        else:
            _fail(f"{name}")
            any_failed = True

    return 1 if any_failed else 0


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Read-only smoke tests for the energy optimiser. "
        "No writes, no hardware actuation.",
    )
    parser.add_argument("--config", "-c", required=True, help="Path to TOML config")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Synthetic LP solve, no network at all",
    )
    parser.add_argument(
        "--modbus-read",
        action="store_true",
        help="Read key Sigenergy registers (no writes)",
    )
    parser.add_argument(
        "--api-probe",
        action="store_true",
        help="One call to each API (costs 1 Solcast call)",
    )
    parser.add_argument(
        "--dry-tick",
        action="store_true",
        help="Full tick pipeline without any writes",
    )
    parser.add_argument("--all", action="store_true", help="Run all smoke tests")
    args = parser.parse_args()

    rc = asyncio.run(main_async(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
