# CLAUDE.md — Agentic Development Guide

This file provides context for AI-assisted development of the energy optimiser. Read this before making any changes.

## Project overview

A standalone Python service that optimises a Sigenergy hybrid inverter + 40kWh battery + solar against Amber Electric wholesale pricing. Runs as a Docker container (plus a watchdog sidecar) on Proxmox. Controls battery charge/discharge and shiftable loads (hot water heat pump) via Modbus TCP and Shelly HTTP APIs.

**Spec document:** `SPEC-ENERGY-01.md` is the source of truth. Every design decision is documented there with rationale. Read relevant sections before modifying code.

**Known issues:** `KNOWN-ISSUES.md` tracks all bugs, gaps, and improvements with severity ratings. Check it before starting work — the issue you're about to fix may have context there.

## Architecture

```
service.py          # Spawns N wake loops via asyncio.gather; touches heartbeat
  ├── __main__.py       # Entry point; Service is constructed inside asyncio.run
  ├── wake_loop.py      # Wall-clock-aligned async wake loops (no drift)
  ├── state_machine.py  # Operational lifecycle: ACTIVE/DEGRADED/FALLBACK
  ├── profiler.py       # Builds load profiles from DuckDB historical data
  ├── store.py          # DuckDB persistence (telemetry, price/pv forecast logs)
  ├── validation.py     # Per-field validation before DuckDB writes
  ├── logging_utils.py  # Structured JSON events + NDJSON tick snapshots
  ├── time_utils.py     # NEM/local/UTC conversions — DST-aware
  ├── config.py         # TOML config loading
  ├── types.py          # All value types — frozen dataclasses, enums
  ├── replay.py         # Backtest LP configs against historical snapshots
  ├── watchdog.py       # Dead-man sidecar entry point (eo-watchdog)
  ├── lp/
  │   ├── constants.py      # Slot size, horizon, solver timeout, scenario weights
  │   ├── formulation.py    # LP/MILP builder (deterministic + stochastic)
  │   ├── solver.py         # PuLP + HiGHS wrapper, returns LPSolution
  │   ├── loads.py          # LPLoad protocol + implementations (HW heat pump, etc.)
  │   ├── dispatch.py       # LPDispatch: maps slot-0 → mode 3/4/6 + cap
  │   ├── runtime.py        # Shared state: CommandedState, CircuitBreaker, LPRuntime
  │   ├── watcher.py        # Post-write verification (10s poll of reg 30037)
  │   ├── fallback.py       # Paranoid fallback: mode 2 + relays off
  │   ├── snapshot_adapter.py  # LPSolution → PlannerOutput for snapshot compat
  │   └── result.py         # LPSolution, SlotDecision, SolveStatus
  └── clients/
      ├── amber.py      # Amber Electric REST API
      ├── solcast.py    # Solcast rooftop PV forecast REST API
      ├── bom.py        # BOM weather observations (JSON feed)
      ├── unifi.py      # UniFi WiFi client presence detection
      ├── sigenergy.py  # Modbus TCP read/write to inverter
      └── shelly.py     # Shelly Pro EM HTTP RPC (CT measurement + relay)
```

### Two-container deployment

The service is deployed as two containers managed by `docker-compose.yml`:

- **`energy-optimiser`** — runs `energy-optimiser` (the tick loop). Imports the full project.
- **`energy-optimiser-watchdog`** — runs `eo-watchdog`. Imports only `watchdog.py` (pymodbus + stdlib). Polls `/var/lib/energy-optimiser/heartbeat`; if the main service stops touching it, pins the inverter to an explicit safe state — three writes in order: `(40031=2, 40038=0, 40029=1)` — with a last-resort `40029=0` fallthrough if any write fails. Re-asserts every poll while stale (writes are idempotent). Separate failure domain from the main service — covers OOM, Python deadlock, container-runtime death.

Both containers are brought up at boot by `energy-optimiser.service` (systemd).

## Critical rules

### Safety

- **The LP must never crash the tick loop.** All LP code runs in `asyncio.to_thread` with a wall-clock timeout. If the solver fails, times out, or returns infeasible, the service falls back to `SELF_CONSUME` mode via the paranoid fallback path (mode 2 + relays off). The circuit breaker latches for 5 minutes before re-attempting.
- **Modbus writes are real-world actuations.** A wrong value to register 40031 changes the physical behaviour of a 10kW inverter. Triple-check register addresses, gain values, and sign conventions.
- **The Sigenergy firmware has NO Modbus-comms watchdog.** Verified on live hardware (KNOWN-ISSUES #0d). A crash of the main service leaves the inverter executing the last commanded mode indefinitely. Fallback is enforced by three layers: (1) `set_fallback()` on clean shutdown, (2) Docker `restart: unless-stopped` for Python-level crashes, (3) the dead-man watchdog sidecar (`watchdog.py`) for OOM/deadlock/container-runtime failures. Don't break any of these layers without understanding which failure modes fall through.
- **Never write to Modbus registers that aren't in the spec.** The register map in `SPEC-ENERGY-01.md §7` and `types.py` is the approved set. Register 40001 (continuous power setpoint) is documented but deliberately not used — see §7.2 for rationale.
- **Mode 0 (PCS Remote Control) is deliberately not used.** It doesn't track dynamic house load. Use modes 3/4/6 with magnitude caps via `apply_lp_dispatch()`. See `lp/dispatch.py` for the mapping.
- **Discharge cap writes the physical max, not the LP's point estimate.** `dispatch_from_slot` sets reg 40034 to `battery_config.max_discharge_kw` on discharge so transient house loads (kettle, AC, oven) stay on battery instead of leaking to grid. The LP's signed magnitude is preserved on the dispatch as `signed_intent_kw` for the watcher's direction check. Charge cap is still the LP's intended rate — charge is directly controllable and over-charging past the plan spends too much from grid.

### Battery & charge rates

The inverter has **two different charge rate limits** depending on source:

| Path | Limit | Config field | Used when |
|---|---|---|---|
| AC charge (grid → battery) | 10 kW | `max_ac_charge_kw` | `CHARGE_GRID` action |
| DC charge (solar → battery) | 13 kW | `max_dc_charge_kw` | `CHARGE_PV` action |
| Discharge (battery → house/grid) | 10 kW | `max_discharge_kw` | All discharge actions |

The LP formulation uses separate charge rate limits for AC (grid) and DC (PV) paths. The dispatch module (`lp/dispatch.py`) writes the total intended charge/discharge rate as the cap to register 40032 or 40034. The PV array is ~13kW nameplate — DC charge is bounded by available solar, not by the battery's acceptance rate (which may be higher).

The Modbus register `REG_ESS_MAX_CHARGING_LIMIT` (40032) sets the limit the inverter enforces. The LP dispatch writes the cap each tick via `apply_lp_dispatch()`.

### Data integrity

- **Never write derived values when inputs are suspect.** If the grid sensor is offline (register 30004 ≠ 1), `grid_power_kw` and `house_load_kw` are nulled at the read layer (`clients/sigenergy.py::read_state`) before any downstream code sees them. `validation.py` is a defence-in-depth second check at the telemetry-row layer.
- **Don't poison the load profile.** The load profiler (§6.7) builds slowly over months. Bad data corrupts it silently. Always validate before writing to DuckDB.
- **Null over wrong.** A missing data point is recoverable. A wrong data point fed into a 90-day rolling average is not.

### Time

- **All storage is UTC.** The `ts` column in DuckDB is `TIMESTAMPTZ`. Never store NEM time or local time.
- **NEM time is NOT local time.** NEM is always UTC+10. Canberra is UTC+10 in winter (AEST) and UTC+11 in summer (AEDT). A 1-hour error in the evening reserve window costs real money. See `time_utils.py` and spec §6.8.
- **Config times are local.** `deadline_hour_local = 22` means 10pm Canberra time, not NEM time. Conversion happens in `lp/loads.py` and `time_utils.py`.

## Code conventions

- Python 3.12+. Full type hints on all functions, including return types.
- All value types are frozen dataclasses with `slots=True` in `types.py`. Don't scatter dataclass definitions across modules.
- Async throughout. All I/O (Modbus, HTTP, DuckDB) is async. The LP solver is synchronous (runs in `asyncio.to_thread`).
- No global state. All state lives in the `Service` class or in individual client instances.
- Structured logging only. Use `logging_utils.emit()` for events, `logger.info/warning/exception` for operational messages. Never `print()`.

## Testing

```bash
cd energy-optimiser
uv sync                  # installs project + dev group (pytest, ruff, freezegun)
uv run pytest tests/ -v  # full suite
```

If uv isn't installed, see https://docs.astral.sh/uv/getting-started/installation/.
The project pins Python 3.12 via `.python-version`; `uv sync` will use the
interpreter specified there. Install-mode is PEP 660 editable by default, so
source edits take effect without reinstall.

- Full suite currently passes at ~250+ tests. All must pass before committing; check the latest count with `pytest tests/ -q`.
- LP tests use synthetic fixtures from `tests/conftest.py`. Add new test helpers there, not in individual test files.
- DuckDB tests use in-memory databases (`:memory:`) — no disk I/O, no cleanup needed.
- Service LP tests use `Service.__new__` + hand-injected mocks to avoid standing up real clients. See `tests/test_service_lp.py` for the pattern.
- LP solver tests run actual HiGHS solves (~200ms each). Watcher tests mock the Modbus read.

### Adding tests

When fixing a bug or adding a feature, write the test first. The test should:
1. Map to a spec acceptance criterion where possible (§9.x)
2. Use the existing fixture helpers in `conftest.py`
3. Test behaviour, not implementation — assert on `LPSolution.slot_0.battery_kw` or `LPDispatch.mode`, not internal method calls

## Common tasks

### Adding a new managed load

1. Add a `[[managed_load]]` entry to `config.toml`
2. No code changes needed — `ManagedLoadManager` auto-discovers from config
3. If it's a new `LoadCategory`, add the category to `types.py` and add an `LPLoad` implementation to `lp/loads.py`

### Modifying LP behaviour

1. Read the current formulation in spec §5.2
2. Write a failing test in `tests/test_lp_scaffolding.py` or `tests/test_lp_stochastic.py`
3. Modify `lp/formulation.py` (constraints/objective) or `lp/dispatch.py` (mode mapping)
4. Run the full test suite
5. **Always run replay against historical snapshots** before deploying:
   ```bash
   python -m optimiser.replay_cli \
     --snapshots '/var/lib/energy-optimiser/snapshots/2026-*.ndjson.gz' \
     --config config.toml -v
   ```
   This tells you whether the change would have saved or lost money historically.

### Adding a new data source

1. Create a client in `clients/` following the pattern of `bom.py` (simplest)
2. Add config dataclass to `config.py`
3. Add to `Service.__init__()` and wire into the tick loop in `service.py`
4. If it feeds the LP, add to `SystemState` or as a new solver input in `lp/formulation.py`
5. If persisted, add column to telemetry DDL in `store.py` and update `TelemetryRow`

### Fixing a Modbus register issue

1. Cross-reference with `Sigenergy-Local-Modbus-main/custom_components/sigen/modbusregisterdefinitions.py`
2. Check the register type (READ_ONLY = input register FC4, HOLDING = FC3/FC6/FC16)
3. Check gain and data type (U16, S32, U32) — wrong gain means values off by 10x or 1000x
4. Test with a single read before deploying a write change
5. Update `SPEC-ENERGY-01.md §7` register map if the address changes

## Replay workflow

The replay engine is the primary tool for validating LP configuration changes. It works by:

1. Every tick writes a `TickSnapshot` to NDJSON (full LP input + output)
2. The replay engine reads these snapshots and runs a candidate `solve_stochastic` with the same inputs but different parameters (battery config, scenario weights, managed load configs)
3. It compares decisions and estimates cost deltas

```bash
# Compare candidate LP config against last 30 days
python -m optimiser.replay_cli \
  -s '/var/lib/energy-optimiser/snapshots/2026-03-*.ndjson.gz' \
  -c candidate-config.toml \
  -o results.ndjson \
  -v

# Analyse with DuckDB
duckdb -c "
  SELECT
    DATE_TRUNC('day', timestamp) AS day,
    SUM(delta_cents)/100 AS daily_delta_aud,
    COUNT(*) FILTER (WHERE candidate_action != original_action) AS changed,
    AVG(solve_ms) AS avg_solve_ms
  FROM read_json('results.ndjson')
  GROUP BY day ORDER BY day
"
```

**Rule:** never deploy a config change that shows negative delta (costs more) over the historical window without understanding why. Also check `solve_ms` for performance regressions — the stochastic solve must stay under 10s mean.

## Environment

- **Runtime:** Docker on Proxmox (host network mode for Modbus/LAN access)
- **Python:** 3.12+
- **Key deps:** httpx (async HTTP), pymodbus (Modbus TCP), duckdb (analytics DB)
- **Data volumes:** `/var/lib/energy-optimiser/` contains `telemetry.duckdb` and `snapshots/`
- **DuckDB concurrency:** only one process can hold `telemetry.duckdb` at a time — DuckDB takes a file lock on open, even for `read_only=True`. The running service holds it; ad-hoc queries must `cp` the file aside first and open the copy. See DEPLOY.md "Phase 2" for the snapshot-and-query pattern. **Always copy `telemetry.duckdb.wal` alongside the main file** — DuckDB checkpoints infrequently (default threshold 16 MB of WAL), so between checkpoints everything recent lives only in the WAL. Copying the main file alone silently drops hours of data. DuckDB replays the WAL in-memory on open (read-only is fine). If you add a second process that needs live data, prefer reading the NDJSON tick snapshots (append-only, concurrent-safe) over opening the DuckDB.
- **`pytz` is a direct dep even though the service never imports it.** DuckDB imports pytz lazily when materialising TIMESTAMPTZ columns to Python. The service's own queries only fetch aggregates (COUNT, AVG) and never hit that path, but operators running ad-hoc queries inside the container do. We pin pytz so `docker exec ... python` works first-try.
- **Config:** `/etc/energy-optimiser/config.toml`
- **Logs:** stdout (JSON structured events), collected by Docker log driver

## Decision log

| Decision | Rationale | Spec ref |
|---|---|---|
| DuckDB over SQLite | Analytical read pattern (load profiles = columnar aggregation). Write rate is trivial (1 row/5 min). | §6 |
| NDJSON snapshots separate from DuckDB | Append-only, write-once. Keeps telemetry table lean. DuckDB reads NDJSON natively for replay. | §6.9 |
| Greedy → stochastic MILP | Greedy couldn't coordinate battery+loads optimally — discrete planners fight each other. LP handles all resources in a single objective with stochastic PV scenarios (P10/P50/P90). | §5.2 |
| Load-following modes over mode 0 | Mode 0 (PCS_REMOTE_CONTROL + reg 40001) doesn't track house load — every transient leaks grid import/export (~$300/yr). Modes 3/4/6 let the inverter handle sub-second load following within our cap. | §5.4, §7.2 |
| Null over wrong for validation | Bad data in a 90-day rolling profile is worse than missing data. Profiler queries filter `IS NOT NULL`. | §6.5 |
| Local time in config, UTC in storage | Users think in local time. DST errors cost money. Internal consistency requires UTC. | §6.8 |
| Managed loads as a generalised system | Hot water was first, but aircon, EV, oven all fit the same pattern with different scheduling strategies. | §11, types.py |
| Host network mode in Docker | Needs direct access to Modbus (TCP 502), Shelly (HTTP), and UniFi (HTTPS) on the LAN. Bridge mode would need port mapping for each device. | docker-compose.yml |
| AC/DC charge rate split | Grid import is AC-coupled (10kW inverter limit). Solar is DC-coupled directly to battery (13kW, bounded by PV array MPPT). LP formulation uses different limits per charge path. | config.py, lp/formulation.py |
| Wall-clock-aligned wake loops over sleep-based loop | Sleep-based loops drift; wake loops fire at exact UTC second boundaries. Each cadence (60s tick, 5-min telemetry, 30-min BOM, etc.) runs as an independent loop via asyncio.gather. Slow tasks don't delay other loops, overruns are skipped not queued. | wake_loop.py, §4.2.1 |
| Dual-resolution Amber polling | 5-min prices for acute decisions (spike, neg export, neg import); 30-min for planning (cheapest window, future_max). 5-min only meaningful for current+next 30-min period, beyond that 30-min is the only reliable data. | amber.py, lp/formulation.py, §5.1.1 |
| SIGNAL_DRIVEN load category | Hot water HP runs in PV mode triggered by a continuously-held dry contact — the appliance manages its own internal cycles. The original SHIFTABLE one-shot model didn't fit. SIGNAL_DRIVEN is now an LP binary variable with daily-target-by-deadline constraint. Same pattern fits future EV charging. | types.py, lp/loads.py, §5.5 |
| 5-min slot resolution in the LP | LP uses 5-min slots over a horizon that's the lesser of `HORIZON_HOURS` (48h ceiling) and the actual priced coverage from Amber (currently up to ~36h). Never extrapolates: if prices end at hour 36, the LP ends at hour 36, with a terminal SOC floor at the last slot to preserve reserve into the unpriced tail. Negative 5-min spikes inside an otherwise-expensive 30-min interval are visible and exploitable. | lp/formulation.py, lp/constants.py `TERMINAL_SOC_FLOOR_PCT`, §5.2 |
| Discharge cap = physical max, not LP point estimate | The LP's `battery_kw` is its expected load (e.g. 2 kW from a default load profile). Writing that as the discharge cap meant any transient above the forecast (kettle, AC) leaked to grid import at retail. Writing `max_discharge_kw` lets mode 6 load-follow up to the physical limit while the LP still controls direction via `signed_intent_kw`. | lp/dispatch.py:109 |
| External dead-man watchdog sidecar | Sigenergy firmware has no Modbus watchdog (verified on hardware, KNOWN-ISSUES #0d). Without a sidecar, a main-service crash leaves the inverter executing the last command forever. The watchdog is a tiny pymodbus-only process in a separate container watching a heartbeat file; on staleness it pins the inverter explicitly (mode=2, export=0, remote_ems=1) rather than just disabling remote control — a deterministic safe state that doesn't depend on whatever local EMS the operator had configured. Separate dep surface + separate container = separate failure domain. | watchdog.py, docker-compose.yml |
| Normalise Amber NEM 1-sec gap at parse time | Amber's API returns intervals offset by +1s (10:30:01→11:00:00, 11:00:01→11:30:00) producing 1-second gaps between intervals that the LP's slot grid falls into. Normalising to wall-clock boundaries at the client layer is cleaner than making the LP gap-tolerant. | clients/amber.py |
| Solcast startup seed from DuckDB | Solcast has a hard 10/day quota. A crashloop before this change burned through it. On startup, if `pv_forecast_log` has a fetch <60 min old, restore the in-memory cache from it and skip the initial API call. Next scheduled poll refreshes normally. | service.py startup path, store.read_latest_pv_forecast |
