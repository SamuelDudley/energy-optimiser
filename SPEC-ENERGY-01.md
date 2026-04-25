# SPEC-ENERGY-01: Amber Wholesale Energy Optimiser

**Status:** Draft
**Author:** Sam / Claude
**Date:** 2026-04-02
**System:** Sigenergy hybrid inverter + 40kWh ESS + solar + heat pump HW

---

## 1. Overview

A standalone Python service that optimises battery charge/discharge and hot water
scheduling against Amber Electric's wholesale pricing. Runs as a systemd unit on
Proxmox.

### 1.1 Goals

- Minimise total energy cost by exploiting wholesale price volatility
- Charge battery during negative/low price windows
- Discharge battery during high price / spike windows
- Schedule heat pump hot water into cheapest available windows
- Degrade gracefully when data sources are unavailable
- Persist all telemetry for load profiling and cost analysis

### 1.2 Constraints

| Parameter                  | Value       |
|----------------------------|-------------|
| Battery capacity           | 40 kWh      |
| Grid export limit          | 5 kW        |
| SOC floor (configurable)   | 10%         |
| SOC ceiling (configurable) | 95%         |
| Max AC charge rate (grid)  | 10 kW       |
| Max DC charge rate (solar) | 13 kW       |
| Max discharge rate         | 10 kW       |
| PV array capacity          | ~13 kW      |
| Battery round-trip efficiency | ~90%     |
| HW heat pump draw          | ~1.0–1.5 kW |
| HW heat pump daily need    | ~3 kWh      |
| Amber forecast horizon     | 48 × 30-min intervals (24h) |
| Solcast API calls/day       | 10 (free tier) |

---

## 2. Architecture

Two containers managed by `docker-compose.yml`, brought up at boot by
a systemd unit. The main service runs the LP + dispatch; a dead-man
sidecar enforces a safe state if the main service stops ticking.

```
┌──────────┐ ┌────────┐ ┌──────┐ ┌──────┐ ┌────────┐
│ Amber API│ │Solcast │ │BOM   │ │UniFi │ │Shelly  │
└────┬─────┘ └────┬───┘ └──┬───┘ └──┬───┘ └────┬───┘
     ▼            ▼        ▼        ▼          │
┌──────────────────────────────────────────┐   │
│  energy-optimiser  (main service)        │   │
│   stochastic MILP → LPDispatch           │   │
│   state machine, profiler, replay        │   │
│   touches heartbeat each successful tick │◄──┘
└────────────┬───────────────────┬─────────┘
             │                   │
             ▼                   ▼
┌──────────────────────┐   ┌──────────────┐
│ Sigenergy Modbus TCP │   │   DuckDB +   │
│  reg 40xxx writes    │   │  NDJSON      │
└──────────▲───────────┘   │   snapshots  │
           │               └──────────────┘
           │ REMOTE_EMS_ENABLE=0
           │ (only on heartbeat stale)
           │
┌──────────┴───────────────────────────────┐
│  energy-optimiser-watchdog  (sidecar)    │
│   pymodbus + stdlib only                 │
│   polls /var/lib/energy-optimiser/heartbeat │
└──────────────────────────────────────────┘
```

### 2.1 Components

| Component                | Container | Responsibility                                        |
|--------------------------|-----------|-------------------------------------------------------|
| `AmberClient`            | main      | Poll prices + forecasts; log every interval to `price_forecast_log` |
| `SolcastClient`          | main      | Poll rooftop PV forecast; log to `pv_forecast_log`; startup-seeds cache from log |
| `SigenergyController`    | main      | Modbus TCP read/write to inverter                     |
| `ManagedLoadManager`     | main      | Generalised Shelly-based load monitor/control (N loads) |
| `BOMClient`              | main      | Poll weather observations (outdoor temp)              |
| `UniFiOccupancyDetector` | main      | UniFi client presence detection (phone MAC addresses) |
| `LoadProfiler`           | main      | Build typical load curves from historical telemetry   |
| `Service` / LP           | main      | Tick loop, LP solve, dispatch, verify; touches heartbeat |
| `StateMachine`           | main      | Manage operational lifecycle and failure recovery      |
| `TelemetryStore`         | main      | Persist interval data + forecast logs to DuckDB       |
| `eo-watchdog`            | sidecar   | Write `REMOTE_EMS_ENABLE=0` if heartbeat goes stale   |

---

## 3. Interfaces

`src/optimiser/types.py` is the canonical declaration of every value type
the system exchanges. Read that file rather than the spec when you need
an exact field list — duplicating it here would drift. This section
sketches the high-level language so the rest of the spec makes sense.

**Key value types** (all frozen dataclasses, `slots=True`):

- `SystemState` — SOC, battery/PV/grid/house power, EMS mode, outdoor
  temp, occupancy. `grid_power_kw` and `house_load_kw` are `float | None`
  — null when the grid sensor reports offline.
- `PriceInterval` — one slot of Amber's general + feedIn data: import/
  export c/kWh, spot, renewables %, spike, descriptor, and optional
  advanced-price bands.
- `PVForecast` — one 30-min slot from Solcast with p10/p50/p90 kW.
- `LoadProfile` — the profiler's per-slot expected house load plus a
  maturity level (L0 default → L3 seasoned).
- `ManagedLoadStatus` / `ManagedLoadConfig` — Shelly-managed loads
  (see §5.5 for the SIGNAL_DRIVEN scheduler).
- `LPSolution` / `SlotDecision` / `LPDispatch` (in `lp/result.py`,
  `lp/dispatch.py`) — the LP's output and the derived inverter command.

**Load categories** (`LoadCategory` enum):

| Value              | Use                                                    |
|--------------------|--------------------------------------------------------|
| `SIGNAL_DRIVEN`    | Continuous relay assertion; appliance manages its own cycles (HP in PV mode, future EV). See §5.5. |
| `SHIFTABLE`        | One-shot cycle model (legacy HW). Preserved for back-compat. |
| `PRECONDITIONABLE` | Pre-run during cheap windows to reduce peak demand (aircon). |
| `OBSERVABLE`       | Measurement only — feeds the load profiler, no control. |
| `DEADLINE_BIDIR`   | Charge/discharge with a departure deadline (EV via V2H, §11). |

**Architectural flow per tick** (see `service.py::_tick` for the
authoritative sequence):

```
read Modbus state → fetch prices (if due) → fetch PV forecast (if due)
  → build load profile → solve stochastic LP → dispatch_from_slot
  → apply_lp_dispatch (reg 40031 mode, 40032/40034 cap, 40038 export)
  → schedule managed load relays → write telemetry + snapshot
  → emit TICK_COMPLETE → touch heartbeat
```

---

## 4. Operational State Machine

Manages the service lifecycle. The planner runs inside `ACTIVE` state on each
tick. All other states handle connectivity and degradation.

```
                   ┌──────────┐
         ┌─────────│INITIALISE│
         │         └──────────┘
         │ modbus connected
         │ amber authenticated
         ▼
    ┌──────────┐  modbus lost   ┌──────────┐
    │  ACTIVE  │───────────────►│ DEGRADED │
    │          │◄───────────────│          │
    └──────────┘  reconnected   └──────────┘
         │                           │
         │ amber lost                │ timeout (5 min)
         ▼                           ▼
    ┌──────────────┐           ┌──────────┐
    │ACTIVE_NO_PRICE│          │ FALLBACK │
    │(use last fcst)│          │(self-con)│
    └──────────────┘           └──────────┘
         │                           │
         │ forecast stale (>1h)      │ reconnected
         ▼                           │
    ┌──────────┐                     │
    │ FALLBACK │◄────────────────────┘
    └──────────┘
```

### 4.1 State Definitions

| State              | Planner runs? | Battery mode            | Entry condition                 |
|--------------------|---------------|-------------------------|---------------------------------|
| `INITIALISE`       | No            | Unchanged               | Service start                   |
| `ACTIVE`           | Yes           | Per planner output      | Modbus + Amber both healthy     |
| `ACTIVE_NO_PRICE`  | Yes (stale)   | Per planner (last fcst) | Amber unreachable, Modbus OK    |
| `DEGRADED`         | No            | Last command held       | Modbus unreachable, Amber OK    |
| `FALLBACK`         | No            | Self Consumption (2)    | Both unreachable or stale >1h   |

### 4.2 Tick Cadence

**Primary tick: every 60 seconds.** Drives micro-arbitrage decisions and
matches Amber's 5-min price granularity (we read fresh 5-min data each
tick to catch developing spikes/dips early).

**Polling cadence per tick:**

| Source       | Interval     | Why                                            |
|--------------|--------------|------------------------------------------------|
| Modbus state | every tick   | Cheap (~100ms), needed for every decision      |
| Shelly loads | every tick   | Cheap, drives load-aware planning              |
| Amber 5-min  | every tick   | `next=12&previous=2&resolution=5` — micro window |
| Amber 30-min | every 5 min  | `next=48&resolution=30` — planning horizon     |
| BOM weather  | every 30 min | BOM only updates every 30 min anyway           |
| Solcast      | every ~10 min| 10/day API budget                              |
| UniFi        | every 5 min  | Phone presence is slow-changing                |

**Telemetry write cadence: every 5 minutes** (snap to 5-min boundary).
Writing every minute would 5× the row count without proportional value.
Action changes are also captured immediately for audit, regardless of
the 5-min boundary.

**Tick steps:**
1. Read `SystemState` from Modbus
2. Poll managed loads (Shelly)
3. Fetch fresh Amber 5-min prices (always)
4. Conditional refresh: 30-min prices, BOM, Solcast, UniFi
5. Build load profile from DuckDB
6. Run planner (with both 5-min and 30-min price arrays)
7. Apply battery + load + export-limit commands to Modbus/Shelly
8. Write telemetry on 5-min boundary
9. Write tick snapshot (NDJSON)
10. Emit `TICK_COMPLETE` event

**Why 60s instead of 30s?** A negative-export 5-min interval at 11:00–11:05
needs to be detected and acted on before 11:05 to extract value. With a
60s tick aligned to wall-clock minutes, we get at least 4 ticks within
each 5-min interval — enough to react and verify the action took effect.
A 30s tick would double Modbus traffic without meaningfully improving
reaction time to interval boundaries that occur every 300s.

**Race condition:** an interval boundary may fall mid-tick. Use the
interval that was current when the tick *started*, not whatever Modbus
returns mid-write. The planner takes a snapshot of prices at tick start
and operates on that snapshot for the rest of the tick.

### 4.2.1 Wake Loop Pattern

Sleep-based loops (`await tick(); await sleep(60)`) drift over time —
each iteration is "60s after the previous tick *finished*", not "60s
after the previous wall-clock minute". Drift means ticks land mid-interval
rather than aligned to the boundaries we care about (5-min slot starts,
Amber price update times, telemetry write boundaries).

**Solution: independent wake loops aligned to wall-clock boundaries.**

```python
def next_aligned_wake(period_s: int) -> datetime:
    """Compute the next wake time aligned to UTC second boundaries.
    
    For period_s=60, returns the next minute boundary.
    For period_s=300, returns the next 5-min boundary on UTC clock.
    """
    now = datetime.now(UTC)
    epoch_s = int(now.timestamp())
    next_s = ((epoch_s // period_s) + 1) * period_s
    return datetime.fromtimestamp(next_s, UTC)


async def wake_loop(
    self,
    period_s: int,
    target: Callable[[], Awaitable[None]],
    name: str,
) -> None:
    """Wake aligned to wall-clock boundaries and fire target as a task.
    
    The wake loop never awaits the target — it just schedules it. A slow
    target run does NOT delay the next wake.
    """
    while self._running:
        next_wake = next_aligned_wake(period_s)
        delay = (next_wake - datetime.now(UTC)).total_seconds()
        await asyncio.sleep(max(0, delay))
        
        if name in self._running_tasks:
            logger.warning("Skipping %s — previous still running", name)
            emit(EventType.TICK_OVERRUN, {"loop": name, "scheduled_at": next_wake})
            continue
        
        task = asyncio.create_task(self._wrapped(name, target))
        self._tick_tasks.add(task)
        task.add_done_callback(self._tick_tasks.discard)


async def _wrapped(self, name: str, target: Callable) -> None:
    """Track running tasks and ensure exceptions don't kill the loop."""
    self._running_tasks.add(name)
    try:
        await target()
    except Exception:
        logger.exception("%s failed", name)
    finally:
        self._running_tasks.discard(name)
```

**Multiple independent wake loops run concurrently:**

| Loop          | Period | Target                          | Aligned to       |
|---------------|--------|----------------------------------|------------------|
| `tick`        | 60s    | Read state, plan, apply         | UTC minute       |
| `prices_30min`| 300s   | Refresh 30-min Amber forecast   | UTC 5-min mark   |
| `telemetry`   | 300s   | Write DuckDB row                | UTC 5-min mark   |
| `bom`         | 1800s  | Refresh BOM weather             | UTC 30-min mark  |
| `solcast`     | ~600s  | Refresh PV forecast (rate-limited) | UTC 10-min mark |
| `unifi`       | 300s   | Poll occupancy                  | UTC 5-min mark   |
| `profile`     | 86400s | Rebuild load profile from DuckDB| 02:00 local      |

**Key properties:**

1. **Wall-clock aligned.** A 60s tick fires at exactly `:00` of every minute, not at "60s after the last tick finished".
2. **Independent.** A slow Modbus tick doesn't delay the BOM refresh, and vice versa.
3. **Overrun-safe.** If the previous tick is still running when the next wake fires, the new tick is skipped (logged as `TICK_OVERRUN`). Better to drop one than queue them.
4. **Exception-safe.** A target raising an exception is logged but doesn't kill the wake loop. The next aligned wake still fires.
5. **Clock-jump resilient.** A forward NTP jump just means the next wake is computed against the new clock — no accumulated drift to recover from. A backward jump may fire two ticks in quick succession, which is harmless.

**Tick start time is canonical.** The planner records "this tick is for
interval starting at HH:MM:00 UTC" and operates on prices current at
that moment, even if the actual tick execution starts a few hundred ms
late. This makes replay deterministic.

**Shutdown:** all wake loops are cancelled together, then any in-flight
tick tasks are awaited (with a timeout) to allow Modbus writes to
complete cleanly before the inverter is set to fallback.

---

## 5. Planner Algorithm

### 5.1 Inputs

| Input              | Source                                   | Refresh rate          |
|--------------------|------------------------------------------|-----------------------|
| 5-min prices       | Amber `?next=12&previous=2&resolution=5` | Every tick (60s)      |
| 30-min prices      | Amber `?next=48&resolution=30`           | 5 min                 |
| PV forecast        | Solcast rooftop API                      | Every ~10 min (10/day)|
| Current SOC        | Modbus `30014` (plant_ess_soc)           | Every tick            |
| House load         | Modbus derived                           | Every tick            |
| Managed load state | Shelly Pro EM                            | Every tick            |
| Load profile       | DuckDB (historical avg)                  | Daily rebuild         |
| Outdoor temp       | BOM Canberra (stn 94926)                 | 30 min                |
| Occupancy          | UniFi client API                         | 5 min                 |

### 5.1.1 Dual-resolution price usage

The planner receives **two price arrays** that are merged into a single
LP input — `prices_planning = [*prices_5min, *prices_30min]`. The
linear `_price_at` lookup returns the first matching interval, so 5-min
entries take precedence within their coverage window (current + ~30 min
ahead, where Amber publishes 5-min granularity), and 30-min fills the
rest of the horizon.

**5-min prices (`prices_5min`)** — give the LP intra-30-min resolution:
- Spike detection (`spike_status` on the current 5-min interval)
- Negative export sub-windows inside an otherwise-positive 30-min interval
- Negative import sub-windows
- "Right now" pricing for current-tick cost calculations and the
  stale-price export guard

**30-min prices (`prices_30min`)** — fill the planning horizon beyond
5-min coverage:
- Future maximum import (charge value calculation)
- Future minimum import (discharge threshold)
- Cheapest contiguous window for shiftable loads (hot water)
- Evening reserve sizing
- Pre-conditioning windows (aircon)

**Why merge rather than alternate:** the LP plans on a single 5-min
slot grid; without merging, every 5-min slot inside a 30-min interval
would receive the same 30-min average price, hiding intra-interval
spikes the LP could exploit (e.g. a brief negative-export sub-window
inside a generally-expensive evening half-hour). The merge keeps
5-min precision where Amber actually has it and falls through to 30-min
elsewhere — no false long-horizon precision because 5-min entries
don't extend past their coverage window.

**Interval-boundary normalisation (NEM quirk).** Amber returns `startTime`
offset by +1s from the NEM boundary (e.g. interval 0: `10:30:01 → 11:00:00`;
interval 1: `11:00:01 → 11:30:00`). This produces a 1-second gap between
every consecutive interval. The LP's slot grid lands on exact boundaries
(`11:00:00`, `11:30:00`, ...) — without normalisation, every top-of-half-hour
slot would fall into the gap and `_price_at` would raise "no price interval
covers ...". `clients/amber.py` truncates both `start` and `end` back to
wall-clock boundaries at parse time, so the rest of the system can treat
intervals as contiguous `[start, end)`.

### 5.2 Optimisation (Stochastic MILP)

The energy optimiser uses a mixed-integer linear program (MILP) to find the
cost-minimising battery schedule over a rolling horizon at 5-minute resolution.
The horizon is the lesser of a 48h ceiling (`HORIZON_HOURS`) and the actual
priced coverage returned by Amber this tick; prices are never extrapolated
past the forecast edge, and a terminal SOC constraint (`TERMINAL_SOC_FLOOR_PCT`,
default 20%) at the last planned slot preserves reserve into the unpriced tail.
A stochastic formulation with PV percentile scenarios (P10/P50/P90) handles
forecast uncertainty; non-anticipativity constraints tie the here-and-now
slot-0 decision across all scenarios so the system commits to a single action
before knowing which PV scenario materialises.

#### 5.2.1 Objective

Minimise the weighted expected cost across scenarios:

```
min Σ_s weight_s × Σ_t [
    grid_import[s,t] × import_price[t]
  − grid_export[s,t] × export_price[t]
  + (bat_charge_grid[s,t] + bat_discharge[s,t]) × WEAR_COST_PER_KWH
]
```

Default scenario weights: P10=0.20, P50=0.60, P90=0.20.
Wear cost: 1 c/kWh (discourages unnecessary cycling).

#### 5.2.2 Decision variables (per scenario, per slot)

| Variable               | Type       | Unit | Description                         |
|------------------------|------------|------|-------------------------------------|
| `bat_charge_grid[s,t]` | Continuous | kW   | Battery charge from grid            |
| `bat_charge_pv[s,t]`   | Continuous | kW   | Battery charge from PV              |
| `bat_discharge[s,t]`   | Continuous | kW   | Battery discharge                   |
| `grid_import[s,t]`     | Continuous | kW   | Grid import (purchase)              |
| `grid_export[s,t]`     | Continuous | kW   | Grid export (sale, ≤ 5kW)          |
| `pv_to_house[s,t]`     | Continuous | kW   | PV directly serving house load      |
| `pv_to_battery[s,t]`   | Continuous | kW   | PV routed to battery                |
| `pv_to_export[s,t]`    | Continuous | kW   | PV exported to grid                 |
| `pv_curtailed[s,t]`    | Continuous | kW   | PV curtailed (wasted)               |
| `soc_pct[s,t]`         | Continuous | %    | SOC at end of slot                  |
| `hw_on[s,t]`           | Binary/LP  | 0/1  | Heat pump relay signal (slot 0 binary, future LP-relaxed) |

#### 5.2.3 Key constraints

**House load balance:** PV + grid + battery discharge = house_load + battery charge

**PV allocation:** pv_to_house + pv_to_battery + pv_to_export + pv_curtailed = pv_forecast[scenario]

**SOC dynamics:** soc[t] = soc[t−1] + (charge × η − discharge / η) × slot_hours / capacity × 100

**Rate limits:** charge ≤ max_ac_charge (grid) or max_dc_charge (PV); discharge ≤ max_discharge

**SOC bounds:** soc_floor ≤ soc[t] ≤ soc_ceiling

**Grid export cap:** grid_export[t] ≤ 5.0 kW (DNSP limit, register 40038)

**Non-anticipativity:** all slot-0 decision variables are tied across scenarios (the system commits before observing which PV scenario occurs)

#### 5.2.4 Stochastic scenarios

Each scenario uses a different PV percentile from the Solcast forecast:

| Scenario | PV source          | Default weight |
|----------|--------------------|----------------|
| P10      | pv_estimate10_kw   | 0.20           |
| P50      | pv_estimate_kw     | 0.60           |
| P90      | pv_estimate90_kw   | 0.20           |

P10 (pessimistic) prevents over-commitment on sunny forecasts. P90 (optimistic)
captures value from high-PV days that the deterministic P50 would miss.

#### 5.2.5 Solver

PuLP + HiGHS (via `highspy`). Internal solver timeout: 10s. Wall-clock timeout
(wrapping the thread): 12s. If the solver returns FEASIBLE (hit time limit but
found a usable solution), we use it. If INFEASIBLE/UNBOUNDED/TIMEOUT/ERROR,
the system falls back to MAXIMUM_SELF_CONSUMPTION (mode 2) on the inverter.

Performance benchmark: 3-scenario stochastic, up to 576 slots (48h × 12/h),
HW heat pump load, battery + ~72 30-min price intervals + 2-day PV ≈ 826ms
mean solve time on the target hardware. Truncated-horizon solves (e.g. 24h
price coverage → 288 slots) are proportionally faster.

### 5.3 SOC Reservation (Implicit)

The greedy planner required an explicit SOC reservation heuristic for evening
peaks. The LP handles this implicitly: with a rolling horizon out to the edge
of Amber's price coverage (typically 24–36h), the LP sees the expensive 5–9pm
prices ahead and retains enough SOC to discharge into them. No explicit
`evening_reserve_kwh` calculation is needed — the optimiser derives the
optimal reserve from the price forecast and PV/load projections. A terminal
SOC floor (§5.2) guards against arriving at the end of the priced horizon
empty and then facing an unpriced tail.

### 5.4 Dispatch to Inverter (Modes 2/3/5/6 + Adaptive Trim)

The LP outputs a continuous signed `battery_kw` for slot 0. This maps to
the Sigenergy's load-following EMS modes rather than mode 0 (continuous
setpoint). Mode 0 was rejected because it doesn't track dynamic house load
— every load transient leaks as unintended grid import or export.

**Mapping rules:**

| LP output                   | Mode                          | Cap register | Cap value                                   |
|-----------------------------|-------------------------------|--------------|---------------------------------------------|
| \|battery_kw\| < 100W       | SELF_CONSUME (mode 2)         | 40032        | 0 (block PV charge; surplus exports)        |
| battery_kw > 0, grid-dom.   | CHARGE_GRID_FIRST (mode 3)    | 40032        | battery_kw                                  |
| battery_kw > 0, PV-dom.     | SELF_CONSUMPTION (mode 2)     | 40032        | adaptive trim (see below)                   |
| battery_kw < 0, PV producing| DISCHARGE_PV_FIRST (mode 5)   | 40034        | `max_discharge_kw`                          |
| battery_kw < 0, no PV       | DISCHARGE_ESS_FIRST (mode 6)  | 40034        | `max_discharge_kw`                          |

"Grid-dominant" means the LP plans to source more charge from grid than PV
in that slot. The mode's "first" preference tells the inverter which source
to prefer.

**Mode 4 (CHARGE_PV_FIRST) is no longer emitted.** Register 40032 under
mode 4 is a *target*, not a ceiling — when PV droops mid-slot the
inverter pulls grid to hit it (silent grid-draw hazard, see
`SIGENERGY-MODES.md`). The PV-dominant charge path uses mode 2 with
the adaptive trim below; mode 4 stays in `RemoteEMSControlMode` for
historical-snapshot replay only.

**Mode-2 adaptive trim** (`clients/sigenergy.py::_apply_mode2_adaptive_charge`).
Mode 2's cascade is `PV → house → battery (up to 40032) → export → curtail`.
With 40032 set to `max_dc_charge_kw`, the battery soaks all surplus PV
before any export flows — losing the daytime split where a mixed
battery+export slot would be more profitable. The dispatch handles this
in two phases:

1. **Phase A**: write `40032 = max_dc_charge_kw, mode = 2`. The cascade
   absorbs all surplus PV. Wait `MODE2_PROBE_SECONDS` (5 s) for steady
   state, read `pv_power_kw` and `house_load_kw`.
2. **Phase B**: compute and write
   `40032 = max(LP_rate, surplus − export_cap_kw − headroom)`, clamped
   to `[0, max_dc_charge_kw]`. Cascade now splits: battery up to trim,
   export up to DNSP, no curtail unless surplus exceeds both.

The LP rate is a floor: a transient PV droop during the 5 s probe
window cannot collapse the trim toward zero. Validated by
`probe_two_phase.py` (P1 split, P2 under-trim, P3 over-trim curtail).

**Charge cutoff (40047) is pinned at the configured `soc_ceiling_pct`**
by `assert_battery_soc_limits` at startup; no tick-path code touches
it. Earlier designs rewrote 40047 to `slot_0.soc_pct_end` each tick,
but probe_no_cutoff.py confirmed 40032 alone is a sufficient rate
knob for both charge (adaptive trim) and idle (`40032 = 0`).
`LPDispatch.target_soc_pct` carries the LP's planned end-of-slot SOC
as advisory data only, for snapshot replay and Prometheus
observability.

**Discharge cap** uses the *physical* `battery_config.max_discharge_kw`
(typically 10 kW), **not** `|battery_kw|`. The LP's `battery_kw` is its
point estimate of required discharge (e.g. 2 kW from the L0 default load
profile). Writing that as a hard cap meant any house transient above the
forecast (kettle, AC, oven) leaked to grid import at retail price while
the battery sat idle. Writing the physical max lets the load-following
discharge mode supply exactly what the house consumes, no more. The LP's
signed magnitude is preserved on the dispatch as `signed_intent_kw` for
the watcher's direction check.

**Discharge mode selection (5 vs 6).** `dispatch_from_slot` reads live
`measured_pv_kw` and picks mode 5 when PV is producing
(`> PV_PRODUCING_THRESHOLD_KW = 0.2 kW`), mode 6 otherwise. Mode 6
zeroes PV generation entirely (verified on hardware, see
`SIGENERGY-MODES.md`), so any meaningful PV surplus is worth routing
through mode 5 instead — mode 5 load-follows, using PV first and
topping up from battery if short.

#### 5.4.1 Post-write verification (watcher)

A separate 10-second watcher loop polls register 30037 (ESS power) and
checks direction + magnitude against the commanded dispatch:

- **Sub-cap operation is OK** — the inverter may use less than the full cap
  (house load absorbed the energy, or PV covered the load).
- **Wrong direction** — discharging when we commanded charge (or vice versa),
  beyond a 300W floor to suppress measurement noise.
- **Over cap** — magnitude exceeds cap × 1.05 tolerance.
- **3 consecutive deviations** (~30s) → trigger fallback (SELF_CONSUME + relays off).
- **Latched circuit breaker:** 5-minute cooldown, then probe; 3 consecutive
  clean verifications to clear.


### 5.5 Hot Water Scheduling (Signal-Driven Rolling Daily Scheduler)

The hot water heat pump is a **Haier HP330M1-U1** with a built-in PV mode
triggered by a dry-contact input on the control board. The contact is driven
by a Shelly Pro EM relay. Critically:

- The **HP itself** decides when it's done heating (its internal thermostat
  cuts off at the configured Lb temperature, default 75°C).
- The **optimiser's** job is to decide *when to assert the dry contact*
  across the day so that the HP does its heating during the cheapest /
  PV-richest slots while still meeting a daily energy target.
- The relay state is **continuous** — held closed during the heating window,
  not pulsed.

This is fundamentally different from the original "shiftable load with a
90-min cycle" model. The category is `LoadCategory.SIGNAL_DRIVEN`. The same
category applies to a future EV charger: continuous signal, appliance manages
its own cycles, daily energy target with a deadline.

#### 5.5.1 HP unit settings (one-time, on the appliance)

| Setting | Value | Meaning |
|---|---|---|
| LP | 03 | PV mode — heating triggered by dry-contact input |
| LA | ON | Auxiliary input enabled |
| Lb | 75 | Tank target temperature (°C) |
| LC | 03 | HP-only mode (no resistive element backup) |

LC=03 is critical for safety and economics: the resistive element draws
~3 kW on grid and would defeat the optimisation. Element-draw protection
in software (see §5.5.4) detects misconfiguration.

#### 5.5.2 LP formulation

The HW load is an `LPLoad` of type `BinarySignalDrivenLoad`. It adds
one decision variable per 5-min slot — `relay[t] ∈ {0, 1}` for slot 0
(the tick's commitment), relaxed to `[0, 1]` for future slots
(`RELAX_FUTURE_BINARIES` is `True` by default; we re-decide every tick,
so integrality on the look-ahead buys little solve-time benefit).

The load's per-slot power contribution to the system energy balance is
`relay[t] × draw_kw`, which enters the house-load-side of the balance
constraint just like baseline (measured-profile) load does.

**Daily-target constraints.** For every local-calendar-day deadline
that overlaps the LP horizon, one constraint is added:

```
sum(relay[t] × draw_kw × slot_hours for t in that day's pre-deadline
window ∩ horizon) ≥ day_target
```

Where `day_target` is:

- **Today's deadline still in the future:** `daily_target_kwh − energy_today_kwh`
  (measured running total from the Shelly CT since midnight local).
- **Today's deadline already past at the time of the tick:** no today
  constraint is added. The unmet shortfall is *rolled forward* into the
  next deadline's target, capped at
  `ROLL_FORWARD_CAP_MULTIPLIER × daily_target_kwh` (default 2×) so that
  a multi-day service outage can't pile an impossible load into one
  day. Excess past the cap is silently forgiven — a tank that's been
  cold for 2+ days needs operator attention, not a frantic LP.
- **Future-day deadline within horizon:** `daily_target_kwh` plus any
  rolled-forward shortfall, again subject to the 2× cap.

At most three deadlines fit within a 72h ceiling; in practice the
horizon is 24–36h so one or two deadlines are the common cases.

**Why no explicit hysteresis or force-assert.** Earlier (pre-LP)
versions of this algorithm used a heuristic sort-by-score with a
hysteresis hold band to avoid tick-to-tick relay chatter. The LP
formulation doesn't need this: every tick re-solves the whole problem
with the same objective against essentially the same data, so slot-0
decisions are stable by construction. The deadline constraint provides
the soft "force-assert by deadline" guarantee implicitly — if the
deadline is imminent and the target is unmet, the LP has no feasible
way to leave the relay off through enough future slots to miss the
constraint.

#### 5.5.3 Slot resolution and price source

Slots are 5 min (`SLOT_MINUTES`). The LP's horizon is the lesser of
`HORIZON_HOURS` (48h ceiling) and Amber's actual priced coverage that
tick. Each slot looks up its price from `prices_planning` via
`_price_at` — this raises if the slot falls outside coverage, which
can't happen because the slot grid is truncated to priced coverage
before the LP is built (see §5.2).

5-min prices (the `prices_5min` array) are used by the service for
acute ticked decisions, but the LP itself consumes only the 30-min
planning array. The 5-min array is captured in snapshots for replay
accuracy but doesn't drive the LP directly in v1.

#### 5.5.4 Element-draw safety detection

If the Shelly CT measures `power_kw > element_warning_threshold_kw` (default
2.5 kW), a `LOAD_CYCLE_FAULT` event is emitted with the message "element
draw suspected, check HP setting LC=03". The relay decision is unaffected —
the operator is alerted but heating continues. Setting LC=03 on the HP
prevents the element from engaging in the first place; this check catches
the case where someone has reset the HP to factory defaults.

#### 5.5.5 Effect on battery decisions

When the relay is asserted and the HP is drawing (`power_kw >
power_zero_threshold`), the optimiser's other planner branches see the
elevated house load via `state.house_load_kw` (derived from the inverter's
grid + PV − battery readings). Battery decisions naturally account for it:
discharging into a higher house load offsets more grid import; charging
during a HP run uses more grid kWh.

No special "is HW running?" check is needed in the battery branches because
the HP draw is reflected in the measured house load that the planner already
uses.

### 5.6 Export Curtailment (Negative Feed-in)

When the export price (`feedIn` channel) goes negative, exporting solar to
the grid **costs money**. The planner must curtail export independently
of import-side decisions.

**Priority chain for negative export:**

1. **Export < 0 AND SOC < ceiling:** force `CHARGE_PV` mode to absorb all
   PV into battery. Set grid export limit (register 40038) to 0.
2. **Export < 0 AND SOC ≥ ceiling:** battery is full. Reduce PV output via
   `plant_pv_max_power_limit` (register 40036) to match house load + HW +
   other controllable loads. This is PV curtailment — sacrificing free
   energy to avoid paying to export.
3. **Export < 0 AND shiftable/preconditionable loads IDLE:** opportunistic
   load dump — start HW cycle and/or pre-cool aircon to consume PV at zero
   marginal cost (since the alternative is paying to export it).

**Implementation approach:** Use register 40038 (`plant_grid_point_maximum_export_limitation`) as the primary mechanism combined with `CHARGE_PV` EMS mode. Do NOT use register 40036 (`plant_pv_max_power_limit`) — keeping a PV cap in sync with changing house loads creates a race condition. When export limit is set to 0, the inverter self-regulates and curtails PV automatically to match house load + battery charging.

**Note on register 40038:** setting grid export limit to 0 blocks all
export, including battery discharge. The inverter will stop exporting
even if the battery is discharging. Use carefully with discharge modes.

**Real example (from Amber screenshot, 2026-04-11):** forecast shows
export at -1 to -2 c/kWh for 09:30–12:00. Planner should:
- Ensure battery is charging from solar (CHARGE_PV)
- Start HW cycle during this window (free hot water)
- Set export limit to 0 once battery approaches full

**Observable load awareness:** when the planner sees a high-draw observable
load active (e.g. oven at 2kW), it can factor this into discharge decisions.
Discharging into the house while the oven is running offsets more grid
import at the current (possibly high) price.

**Pre-conditionable loads:** the planner can issue a `start_cycle` command
to pre-cool/pre-heat during cheap windows. Unlike shiftable loads, the
cycle duration is variable (run until comfort band is reached). The
strategy is: "if aircon is likely needed at 5pm (hot day + occupied),
and current price is cheap, pre-run it now to build thermal mass."

---

## 6. Data Model (DuckDB)

### 6.1 Telemetry Table

```sql
CREATE TABLE telemetry (
    ts              TIMESTAMPTZ NOT NULL,
    soc_pct         REAL,
    battery_kw      REAL,          -- +charge, -discharge
    pv_kw           REAL,
    grid_kw         REAL,          -- +import, -export
    grid_kw_shelly  REAL,          -- Independent mains CT (Shelly EM ch2)
    house_load_kw   REAL,          -- derived
    import_price    REAL,          -- c/kWh at time of interval
    export_price    REAL,          -- c/kWh feedIn
    spot_price      REAL,
    renewables_pct  REAL,
    spike_status    VARCHAR,
    pv_forecast_kw  REAL,          -- Solcast estimate at time of interval
    outdoor_temp_c  REAL,          -- BOM observation
    occupied        BOOLEAN,       -- UniFi presence detection
    ems_mode        INTEGER,
    planner_action  VARCHAR,
    planner_reason  VARCHAR,
);

-- Per-load measurements, one row per load per tick
CREATE TABLE load_telemetry (
    ts              TIMESTAMPTZ NOT NULL,
    load_id         VARCHAR NOT NULL,    -- e.g. "hot_water", "aircon", "oven"
    category        VARCHAR NOT NULL,    -- shiftable | preconditionable | observable
    power_kw        REAL,                -- Live draw from Shelly CT
    energy_today_kwh REAL,               -- Accumulated energy today
    cycle_state     VARCHAR,             -- idle | running | complete_today (nullable)
    relay_on        BOOLEAN,             -- Contactor state (nullable if no relay)
);
```

**Why two tables:** the telemetry table stays fixed-width regardless of how
many loads are added. Adding a Shelly to the oven doesn't alter the core
schema. The load profiler can join or decompose as needed:

```sql
-- Decomposed house load: what's the residual after known loads?
SELECT
    t.ts,
    t.house_load_kw,
    COALESCE(SUM(l.power_kw), 0) AS known_loads_kw,
    t.house_load_kw - COALESCE(SUM(l.power_kw), 0) AS residual_kw
FROM telemetry t
LEFT JOIN load_telemetry l ON t.ts = l.ts
GROUP BY t.ts, t.house_load_kw;
```

**Mains cross-validation:** `grid_kw` (Sigenergy Modbus) vs `grid_kw_shelly`
(Shelly Pro EM channel 2 on mains). Persistent divergence >0.5kW indicates
a measurement problem with one of the sensors.

**Partitioning:** None needed. At 1 row per 5 min per load, even 10 managed
loads = ~1M rows/year in `load_telemetry`. DuckDB handles this trivially.

### 6.2 Load Profile Views

```sql
-- Base load profile by day type
CREATE OR REPLACE VIEW load_profile_weekday AS
SELECT
    (EXTRACT(HOUR FROM ts) * 2 + EXTRACT(MINUTE FROM ts) / 30)::INT AS slot,
    AVG(house_load_kw) AS mean_kw,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY house_load_kw) AS p75_kw,
    COUNT(*) AS samples
FROM telemetry
WHERE EXTRACT(DOW FROM ts) BETWEEN 1 AND 5
  AND ts > NOW() - INTERVAL '90 days'
GROUP BY slot
ORDER BY slot;

-- Temperature-bucketed load profile (captures HVAC impact)
CREATE OR REPLACE VIEW load_profile_by_temp AS
SELECT
    (EXTRACT(HOUR FROM ts) * 2 + EXTRACT(MINUTE FROM ts) / 30)::INT AS slot,
    CASE
        WHEN outdoor_temp_c < 10 THEN 'cold'      -- heating load
        WHEN outdoor_temp_c < 20 THEN 'mild'      -- minimal HVAC
        WHEN outdoor_temp_c < 30 THEN 'warm'      -- light cooling
        ELSE 'hot'                                  -- heavy cooling
    END AS temp_bucket,
    occupied,
    AVG(house_load_kw) AS mean_kw,
    COUNT(*) AS samples
FROM telemetry
WHERE ts > NOW() - INTERVAL '90 days'
  AND outdoor_temp_c IS NOT NULL
GROUP BY slot, temp_bucket, occupied
ORDER BY slot;
```

Over time, this builds a model: "on a cold occupied weekday evening, the
house draws ~4.5kW; on a mild unoccupied day, it's ~0.6kW." The planner
selects the appropriate profile based on today's forecast temp and current
occupancy.

### 6.3 Solcast Forecast Tracking

Every Solcast fetch writes one row per forecast interval to
`pv_forecast_log`. Two use cases:

1. **Calibration / drift analysis.** Compare logged `pv_estimate*` against
   realised PV from the telemetry table (or Solcast's `/estimated_actuals`,
   which is satellite-derived). `actual_kw` is NULL at insert time and
   left for a backfill job to populate.
2. **Startup-seed cache.** Solcast has a hard 10 calls/day quota on the
   hobbyist tier. A crashloop burns through it fast. On startup, if the
   log's most recent `fetched_at` is within the last 60 min, the service
   reads back the unexpired intervals and seeds the in-memory cache
   (`SolcastClient.seed_cache`), skipping the initial live fetch. The
   next scheduled poll (every ~2.4 h) refreshes on its own cadence.

```sql
CREATE TABLE pv_forecast_log (
    fetched_at      TIMESTAMPTZ NOT NULL,  -- When forecast was retrieved
    period_end      TIMESTAMPTZ NOT NULL,  -- Forecast interval end
    pv_estimate_kw  REAL,
    pv_estimate10_kw REAL,
    pv_estimate90_kw REAL,
    actual_kw       REAL                   -- Backfilled from estimated_actuals
);
```

Write path: `SolcastClient.get_forecast()` builds a `PVForecastLogRow` per
interval, `drain_log_rows()` returns them to the service, and
`TelemetryStore.write_pv_forecast_log()` appends via `executemany`.
Read path on startup: `TelemetryStore.read_latest_pv_forecast(max_age_minutes=60)`
returns `(forecasts, fetched_at)` if the most recent fetch is fresh
*and* has unexpired intervals, else `None` (fall through to live fetch).

### 6.3.1 Amber Price Forecast Tracking

Symmetric with the PV forecast log but with a different shape and
different analysis goal. The LP uses `advancedPrice.predicted` as the
point estimate (see §5.2) and the swagger tells us `advancedPrice.low/
high` exist as a confidence band. The question we care about post-
deploy is: **is that band calibrated?**

```sql
CREATE TABLE price_forecast_log (
    fetched_at          TIMESTAMPTZ NOT NULL,
    resolution          INTEGER NOT NULL,        -- 5 or 30
    interval_start      TIMESTAMPTZ NOT NULL,
    interval_end        TIMESTAMPTZ NOT NULL,
    interval_type       VARCHAR,                 -- Actual/Current/Forecast
    per_kwh             REAL,                    -- AEMO point estimate
    export_per_kwh      REAL,
    spot_per_kwh        REAL,
    forecast_predicted  REAL,                    -- Amber advancedPrice.predicted
    forecast_low        REAL,                    -- advancedPrice.low
    forecast_high       REAL,                    -- advancedPrice.high
    spike_status        VARCHAR,
    descriptor          VARCHAR,
    is_locked           BOOLEAN,                 -- CurrentInterval.estimate inverted
    renewables_pct      REAL
);
```

Every price fetch (5-min and 30-min cadence) logs one row per interval.
The redundancy is the point: the same `interval_start` logged over
successive fetches traces how the forecast evolved, and the last entry
(`interval_type = 'ActualInterval'`) is the realised truth. Calibration
analysis joins the earlier forecast rows to the later actual row:

```sql
-- How often was realised within the advertised band?
WITH forecasts AS (
  SELECT interval_start, forecast_low, forecast_predicted, forecast_high
  FROM price_forecast_log
  WHERE interval_type = 'ForecastInterval'
    AND forecast_predicted IS NOT NULL
),
actuals AS (
  SELECT interval_start, per_kwh AS realised
  FROM price_forecast_log
  WHERE interval_type = 'ActualInterval'
)
SELECT
  COUNT(*) AS total,
  SUM(CASE WHEN a.realised BETWEEN f.forecast_low AND f.forecast_high
           THEN 1 ELSE 0 END) AS within_band,
  AVG(ABS(a.realised - f.forecast_predicted)) AS mae_predicted
FROM forecasts f JOIN actuals a USING (interval_start);
```

This is the data needed to decide, post-deploy, whether to add price
scenarios to the LP (see §5.2.1 deferred). If the band is well-
calibrated (e.g. ~66% of realised values fall within [low, high]) and
the mean absolute error of `predicted` is non-trivial compared to wear
cost (~2.5 c/kWh), scenarios are worth building. If the band is noise
or `predicted` is already tight, scenarios would add complexity without
benefit.

### 6.4 Data Point Rationalisation

Every field in the telemetry table falls into one of three trust categories:

| Field             | Category    | Source                    | Risk                          |
|-------------------|-------------|---------------------------|-------------------------------|
| `soc_pct`         | Measured    | Modbus 30014              | BMS drift over time           |
| `battery_kw`      | Measured    | Modbus 30037              | Sign convention error         |
| `pv_kw`           | Measured    | Modbus 30035              | Low risk                      |
| `grid_kw`         | Measured    | Modbus 30005 (CT sensor)  | Requires grid sensor online (30004=1) |
| `grid_kw_shelly`  | Measured    | Shelly Pro EM ch2 (mains) | Independent cross-validation source   |
| `house_load_kw`   | **Derived** | `pv_kw + grid_kw - battery_kw` | **Highest risk** — compound error from 3 inputs |
| `import_price`    | External    | Amber API                 | Stale if API unreachable      |
| `export_price`    | External    | Amber API                 | Stale if API unreachable      |
| `spot_price`      | External    | Amber API                 | Stale if API unreachable      |
| `renewables_pct`  | External    | Amber API                 | Informational only            |
| `spike_status`    | External    | Amber API                 | Critical — drives discharge   |
| `pv_forecast_kw`  | External    | Solcast                   | Stale (max 2.4h old)          |
| `outdoor_temp_c`  | External    | BOM                       | Stale (max 30 min old)        |
| `occupied`        | Derived     | UniFi client list         | Flapping risk (grace period)  |
| `ems_mode`        | Measured    | Modbus 30003              | Low risk                      |
| `planner_action`  | Internal    | Planner output            | No risk                       |
| `planner_reason`  | Internal    | Planner output            | No risk                       |

Per-load fields (in `load_telemetry`):

| Field             | Category    | Source                    | Risk                          |
|-------------------|-------------|---------------------------|-------------------------------|
| `power_kw`        | Measured    | Shelly CT per load        | Low risk — ground truth       |
| `energy_today_kwh`| Measured    | Shelly accumulated        | Low risk — ground truth       |
| `cycle_state`     | Derived     | Shelly power + state logic| Misdetection if draw fluctuates |
| `relay_on`        | Measured    | Shelly relay state        | Low risk                      |

### 6.5 Data Validation Rules

The telemetry writer must validate each row before persisting. Invalid
rows are logged but **not written** to the telemetry table — bad data in
the load profile is worse than missing data.

```python
@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    warnings: list[str]
    rejected_fields: list[str]  # Fields nulled out (row still written)


def validate_telemetry(row: TelemetryRow) -> ValidationResult:
    """Validate a telemetry row before persistence."""
    warnings: list[str] = []
    rejected: list[str] = []

    # 1. Grid sensor must be online for grid/house_load to be meaningful
    #    If 30004 != 1, null out grid_kw and house_load_kw
    if not grid_sensor_online:
        rejected.extend(["grid_kw", "house_load_kw"])
        warnings.append("Grid sensor offline — grid/load data excluded")

    # 2. Energy balance sanity check
    #    house_load_kw should be non-negative (house doesn't generate)
    if row.house_load_kw is not None and row.house_load_kw < -0.1:
        rejected.append("house_load_kw")
        warnings.append(f"Negative house load ({row.house_load_kw:.2f}kW) — derivation error")

    # 3. SOC bounds
    if not (0 <= row.soc_pct <= 100):
        rejected.append("soc_pct")
        warnings.append(f"SOC out of range: {row.soc_pct}")

    # 4. Stale external data — mark but don't reject
    #    (price staleness is handled by the state machine)
    if row.outdoor_temp_c is not None and bom_data_age > timedelta(hours=2):
        rejected.append("outdoor_temp_c")
        warnings.append("BOM data stale >2h — temp excluded")

    # 5. Outlier detection for house_load
    #    If load exceeds 3× the rolling 7-day P95, flag it
    if row.house_load_kw is not None and row.house_load_kw > 3 * rolling_p95:
        warnings.append(f"House load outlier: {row.house_load_kw:.1f}kW")
        # Still write it — could be legitimate (EV charger, party)
        # but flag for review

    # 6. Mains CT cross-validation
    #    If Shelly mains reading diverges from Sigenergy grid sensor
    if (row.grid_kw is not None and row.grid_kw_shelly is not None
            and abs(row.grid_kw - row.grid_kw_shelly) > 0.5):
        warnings.append(
            f"Grid sensor divergence: Modbus={row.grid_kw:.2f}kW "
            f"Shelly={row.grid_kw_shelly:.2f}kW"
        )

    return ValidationResult(
        valid=len(rejected) == 0,
        warnings=warnings,
        rejected_fields=rejected,
    )
```

**Key principle:** reject individual fields, not entire rows. A row with
valid SOC, battery, PV, and price data is still useful even if house_load
is rejected. The load profiler query filters on `house_load_kw IS NOT NULL`.

### 6.6 Cross-Validation

Three independent validation channels:

**1. Mains CT (Shelly Pro EM channel 2) vs Sigenergy grid sensor:**
```
grid_divergence = abs(grid_kw - grid_kw_shelly)

If grid_divergence > 0.5kW sustained over 10+ minutes:
    → VALIDATION_WARNING: one sensor is drifting
    → Prefer Shelly mains reading (independent, known-good CT)
```

**2. Load decomposition sanity check:**
```
known_loads_kw = sum(load.power_kw for load in load_telemetry at timestamp)
residual_kw = house_load_kw - known_loads_kw

If residual_kw < -0.2kW consistently:
    → Known loads exceed total house load → derivation error
```

**3. Solcast forecast vs actual PV:**
```
forecast_error = pv_forecast_kw - pv_kw
```
Track in `pv_forecast_log` to build a dampening factor over time.
Track this in `pv_forecast_log` to build a dampening factor over time.

### 6.7 Bootstrap & Data Maturity

The load profiler is a **background process** that rebuilds profiles
periodically. It must not block the 5-minute tick loop.

```
Schedule:
  - Full rebuild: daily at 02:00 (low activity, cheap power)
  - Incremental: not needed (DuckDB views are fast over 105K rows/year)
  - On startup: rebuild once, then proceed
```

#### Maturity Levels

The planner selects its strategy based on how much data is available:

| Level | Condition                  | Load profile strategy              | Duration    |
|-------|----------------------------|------------------------------------|-------------|
| **L0 — Cold start** | < 7 days of data  | Flat default: 2kW occupied, 0.5kW unoccupied. No temp adjustment. Planner relies on price-only arbitrage. | Days 0–7 |
| **L1 — Basic**    | 7–30 days           | Weekday/weekend average curves. Single temp bucket. Occupancy binary. | Days 7–30 |
| **L2 — Seasonal** | 30–90 days          | Temperature-bucketed profiles emerge. Occupancy patterns visible. Evening reserve uses temp-adjusted curves. | Days 30–90 |
| **L3 — Mature**   | 90+ days, ≥2 seasons| Full `slot × temp_bucket × occupancy` model with statistical confidence. Solcast dampening calibrated. | 90+ days |

#### Maturity Detection

```python
def assess_maturity(db: duckdb.DuckDBPyConnection) -> int:
    """Determine data maturity level from telemetry row count and span."""
    stats = db.sql("""
        SELECT
            COUNT(*) AS total_rows,
            COUNT(*) FILTER (WHERE house_load_kw IS NOT NULL) AS valid_load_rows,
            DATEDIFF('day', MIN(ts), MAX(ts)) AS span_days,
            COUNT(DISTINCT CASE
                WHEN outdoor_temp_c < 10 THEN 'cold'
                WHEN outdoor_temp_c < 20 THEN 'mild'
                WHEN outdoor_temp_c < 30 THEN 'warm'
                ELSE 'hot'
            END) AS temp_buckets_seen
        FROM telemetry
    """).fetchone()

    if stats.span_days < 7 or stats.valid_load_rows < 1000:
        return 0
    if stats.span_days < 30:
        return 1
    if stats.span_days < 90 or stats.temp_buckets_seen < 3:
        return 2
    return 3
```

#### Profile Fallback Chain

When the planner requests a load profile for a specific context
(e.g., `cold + occupied + weekday`), it falls back through
progressively broader queries if the target bucket has insufficient
samples (< 50 data points):

```
1. slot × temp_bucket × occupied × day_type     (best, needs L3)
2. slot × temp_bucket × occupied                 (drop day_type)
3. slot × occupied                               (drop temp, needs L1)
4. slot                                          (flat average, needs L1)
5. constant 2.0 kW                               (L0 cold start default)
```

Each step up loses specificity but gains sample size. The planner
logs which level it's operating at for observability.

#### Protecting the Model

Rules to prevent data poisoning:

1. **Never write derived values when inputs are suspect.** If grid sensor
   is offline (30004 ≠ 1), `house_load_kw` is NULL for that row. The
   load profiler ignores NULLs.

2. **Don't let the planner's actions corrupt the model.** The load profile
   must reflect *gross house demand*, not net grid flow. The derivation
   `house_load = pv + grid - battery` is specifically designed to be
   invariant to battery action — verify this with the cross-validation
   check in §6.6.

3. **Outliers are flagged, not excluded.** A 15kW spike might be an EV
   charger or a pool pump — legitimate loads. But they're flagged so the
   profiler can use robust statistics (median/P75 rather than mean) for
   evening reserve calculations.

4. **Stale external data is excluded, not guessed.** If BOM hasn't updated
   in 2 hours, `outdoor_temp_c` is NULL for that row. The load profiler
   won't attribute that row to a temperature bucket.

5. **Clock alignment matters.** Amber intervals, Solcast intervals, and
   Modbus reads may not align perfectly. The telemetry writer snaps to
   the nearest 5-min boundary. Price and forecast values are the ones
   active at that timestamp, not interpolated.

### 6.8 Time Handling

Four data sources, four time conventions. Getting this wrong means the
evening reserve starts at the wrong hour for half the year.

| Source      | Timezone                  | DST? | Example                      |
|-------------|---------------------------|------|------------------------------|
| Amber API   | NEM time (UTC+10 always)  | No   | `nemTime: 2026-04-03T17:30:00+10:00` |
| Solcast     | UTC                       | No   | `period_end: 2026-04-03T07:30:00Z`   |
| BOM         | Local (AEST/AEDT)         | Yes  | Observation at "5:30pm EST"           |
| Sigenergy   | None (registers, no time) | N/A  | Read at wall-clock time               |
| DuckDB      | **UTC (TIMESTAMPTZ)**     | No   | All storage in UTC                    |

**Canonical time: UTC.** All timestamps are converted to UTC on ingestion.
The telemetry table stores `TIMESTAMPTZ` which DuckDB handles correctly.

**NEM time trap:** NEM time is always UTC+10, even during daylight saving.
Canberra is UTC+10 in winter (AEST) and UTC+11 in summer (AEDT). This
means:

```
NEM 17:00 = 17:00 AEST = 17:00 local in winter (correct)
NEM 17:00 = 17:00 AEST = 18:00 AEDT = 18:00 local in summer (off by 1h)
```

**Impact on the planner:** the evening reserve window (17:00–21:00) must
be defined in **local time**, not NEM time. If defined in NEM time,
the reserve window shifts by 1 hour in summer relative to when people
actually get home.

```python
import zoneinfo

CANBERRA_TZ = zoneinfo.ZoneInfo("Australia/Canberra")
NEM_TZ = zoneinfo.ZoneInfo("Australia/Brisbane")  # UTC+10, no DST

def nem_to_local(nem_dt: datetime) -> datetime:
    """Convert NEM time to Canberra local time."""
    # NEM is always UTC+10 (same as Brisbane)
    utc = nem_dt.astimezone(zoneinfo.ZoneInfo("UTC"))
    return utc.astimezone(CANBERRA_TZ)

def local_to_nem(local_dt: datetime) -> datetime:
    """Convert Canberra local time to NEM time."""
    utc = local_dt.astimezone(zoneinfo.ZoneInfo("UTC"))
    return utc.astimezone(NEM_TZ)
```

**Price alignment:** Amber returns `perKwh` tagged with `nemTime` and
`startTime` (UTC). Always use `startTime` (UTC) for joining with
telemetry. The `nemTime` is the interval end in NEM time — useful for
display only.

**BOM observation times:** BOM JSON includes `local_date_time_full` in
local time. Convert to UTC on ingestion.

**Config rule:** all user-facing times in config (evening reserve hours,
rebuild schedule) are specified in **local time** and converted
internally. This is what users expect and avoids DST confusion.

```toml
[planner]
# These are LOCAL times (AEST/AEDT), not NEM time
evening_reserve_start_hour = 17  # 5pm local
evening_reserve_end_hour = 21    # 9pm local
```

### 6.9 Structured Logging & Replay

#### Motivation

The planner is a pure function: given identical inputs, it produces
identical output. If every tick captures the full input state, any
planner version can be replayed against historical data to answer:
"would this algorithm change have saved or lost money?"

This is the primary observability and testing mechanism.

#### Tick Snapshot

Each tick produces a `TickSnapshot` — the complete input/output record
for one planner invocation. This is the atomic unit of replay.

```python
@dataclass(frozen=True, slots=True)
class TickSnapshot:
    """Complete record of one planner tick. Immutable, serialisable."""
    tick_id: str                           # UUID
    timestamp: datetime                    # UTC
    version: str                           # Planner version (git sha or semver)

    # Inputs — everything the planner saw
    system_state: SystemState
    price_forecast: list[PriceInterval]    # Full 48-interval forecast
    pv_forecast: list[PVForecast] | None
    load_profile: LoadProfile
    managed_loads: list[ManagedLoadStatus]  # All load statuses
    maturity_level: int

    # Output — what the planner decided
    output: PlannerOutput

    # Outcome (backfilled next tick)
    actual_cost_cents: float | None        # What this interval actually cost
    counterfactual_cost_cents: float | None # What self-consume would have cost
```

#### Storage

Tick snapshots are stored as **newline-delimited JSON (NDJSON)** files,
one per day, gzipped.

```
/var/lib/energy-optimiser/snapshots/
  2026-04-03.ndjson.gz
  2026-04-04.ndjson.gz
  ...
```

Why NDJSON, not DuckDB:
- Snapshots are append-only, write-once
- Each snapshot is ~2–5KB (48 price intervals is the bulk)
- At 288 ticks/day = ~1MB/day compressed, ~365MB/year
- DuckDB can read NDJSON directly for analysis: `SELECT * FROM read_json('snapshots/2026-04-*.ndjson.gz')`
- Separating snapshots from telemetry keeps the hot telemetry table lean

#### Replay Engine

The replay engine reads historical snapshots and runs a candidate planner
against them:

```python
from typing import Iterator


@dataclass(frozen=True, slots=True)
class ReplayResult:
    tick_id: str
    timestamp: datetime
    original_action: BatteryAction
    candidate_action: BatteryAction
    original_cost_cents: float
    candidate_cost_cents: float
    delta_cents: float             # negative = candidate saved money


def replay(
    snapshots: Iterator[TickSnapshot],
    candidate_planner: Planner,
) -> Iterator[ReplayResult]:
    """Replay a candidate planner against historical tick snapshots.

    For each snapshot, run the candidate planner with the same inputs
    the original planner saw, then compare costs.
    """
    for snap in snapshots:
        candidate_output = candidate_planner.plan(
            state=snap.system_state,
            prices=snap.price_forecast,
            pv_forecast=snap.pv_forecast,
            load_profile=snap.load_profile,
        )
        candidate_cost = estimate_interval_cost(
            action=candidate_output,
            state=snap.system_state,
            prices=snap.price_forecast[0],  # current interval
        )
        yield ReplayResult(
            tick_id=snap.tick_id,
            timestamp=snap.timestamp,
            original_action=snap.output.battery_action,
            candidate_action=candidate_output.battery_action,
            original_cost_cents=snap.actual_cost_cents or 0,
            candidate_cost_cents=candidate_cost,
            delta_cents=candidate_cost - (snap.actual_cost_cents or 0),
        )
```

Usage for backtesting:

```python
import duckdb

# Load 30 days of snapshots
snapshots = load_snapshots("snapshots/2026-03-*.ndjson.gz")

# Run candidate planner
results = list(replay(snapshots, CandidatePlannerV2()))

# Aggregate
total_delta = sum(r.delta_cents for r in results)
print(f"Candidate vs original over 30 days: {total_delta/100:+.2f} AUD")

# Or load into DuckDB for deeper analysis
db = duckdb.connect()
db.sql("""
    SELECT
        DATE_TRUNC('day', timestamp) AS day,
        SUM(delta_cents) / 100 AS daily_delta_aud,
        COUNT(*) FILTER (WHERE candidate_action != original_action) AS changed_decisions
    FROM replay_results
    GROUP BY day
    ORDER BY day
""")
```

#### Structured Event Log

In addition to tick snapshots, the service emits structured events for
operational observability. All events are JSON, written to stdout
(Docker logs).

```python
class EventType(StrEnum):
    TICK_COMPLETE = auto()
    TICK_OVERRUN = auto()             # Wake fired while previous tick still running
    STATE_TRANSITION = auto()
    MODBUS_WRITE = auto()
    MODBUS_ERROR = auto()
    PRICE_UPDATE = auto()
    PRICE_STALE = auto()
    HW_CYCLE_START = auto()
    HW_CYCLE_COMPLETE = auto()
    HW_CYCLE_FAULT = auto()
    VALIDATION_WARNING = auto()
    VALIDATION_REJECT = auto()
    OCCUPANCY_CHANGE = auto()
    PROFILE_REBUILD = auto()
    PLANNER_FALLBACK = auto()     # Using broader load profile bucket


@dataclass(frozen=True, slots=True)
class Event:
    timestamp: datetime
    event_type: EventType
    data: dict                    # Event-specific payload
    tick_id: str | None = None    # Links event to tick snapshot
```

Example log output:

```json
{"ts":"2026-04-03T07:30:00Z","event":"TICK_COMPLETE","tick_id":"a1b2c3","data":{"soc":42.1,"action":"CHARGE_GRID","price_ckwh":-2.3,"reason":"negative price"}}
{"ts":"2026-04-03T07:30:00Z","event":"MODBUS_WRITE","tick_id":"a1b2c3","data":{"register":40031,"value":3,"name":"remote_ems_control_mode"}}
{"ts":"2026-04-03T07:35:00Z","event":"OCCUPANCY_CHANGE","data":{"occupied":false,"phones_seen":0,"grace_remaining_min":25}}
{"ts":"2026-04-03T08:00:00Z","event":"OCCUPANCY_CHANGE","data":{"occupied":false,"phones_seen":0,"grace_remaining_min":0,"confirmed":true}}
```

#### Alerting

Alerts are a subset of events that require attention. Rather than
building an alerting system, emit structured events and let Docker
log monitoring (Loki, or a simple log grep) handle notification.

Critical events worth alerting on:
- `STATE_TRANSITION` to `FALLBACK` (something is wrong)
- `MODBUS_ERROR` repeated 3+ times (connectivity issue)
- `VALIDATION_REJECT` on `house_load_kw` repeated (derivation broken)
- `HW_CYCLE_FAULT` (heat pump failed to start)
- `PRICE_STALE` >1h (Amber API down)

---

## 7. Sigenergy Modbus Register Map

### 7.1 Read Registers (Input — 30xxx)

All addresses verified against the Sigenergy HA integration source
(`modbusregisterdefinitions.py` and `modbus.py`). The HA integration
uses pymodbus 3.8.3+ (we use 3.12.1) and passes raw absolute addresses
(e.g. `30014`) directly to `client.read_input_registers(address=...)`.
This is the same pattern our code uses, so addressing is consistent.

Plant-level registers (3001x–3003x) are preferred over per-inverter
registers (3059x) for multi-inverter deployments and consistency.

**EMS work mode behaviour (30003):** when remote EMS is enabled
(40029=1), reading 30003 returns `7` (`REMOTE_EMS`), regardless of which
sub-mode is active. The active sub-mode is what we wrote to 40031
(`plant_remote_ems_control_mode`). The optimiser tracks intent
internally — `state.ems_mode` records what 30003 returns, not the
sub-mode.

| Register | Name                       | Type | Gain | Unit | Description                          |
|----------|----------------------------|------|------|------|--------------------------------------|
| 30003    | plant_ems_work_mode        | U16  | 1    | –    | Current EMS mode (enum)              |
| 30004    | plant_grid_sensor_status   | U16  | 1    | –    | 0=disconnected, 1=connected          |
| 30005–6  | plant_grid_sensor_active_power | S32 | 1000 | kW | >0 buy from grid, <0 sell to grid    |
| 30014    | plant_ess_soc              | U16  | 10   | %    | Battery state of charge (plant-level)|
| 30035–6  | plant_sigen_photovoltaic_power | S32 | 1000 | kW | Solar generation (plant-level)      |
| 30037–8  | plant_ess_power            | S32  | 1000 | kW   | Battery power (>0 charging, <0 discharge) |
| 30085    | plant_ess_charge_cut_off_soc | U16 | 10   | %    | Current charge ceiling (read-back)   |
| 30086    | plant_ess_discharge_cut_off_soc | U16 | 10 | %  | Current discharge floor (read-back)  |

**WARNING — historical bug:** earlier versions of this spec listed
`30083` as the SOC register. That register is actually
`plant_ess_rated_energy_capacity` (U32, gain=100, kWh) — a constant
showing total battery capacity, not the current SOC. Code reading SOC
from 30083 and dividing by 10 returns nonsense values like 400% (for a
40 kWh battery at any SOC). The correct register is `30014`.

**WARNING — register 30599:** `inverter_ess_charge_discharge_power` is a
*per-inverter* value. For plant-level battery power, use `30037`
(`plant_ess_power`). They will read the same value in single-inverter
deployments but differ in multi-inverter setups.

### 7.2 Write Registers (Holding — 40xxx)

| Register | Name                     | Type | Gain | Unit | Description                         | Used by |
|----------|--------------------------|------|------|------|-------------------------------------|---------|
| 40001–02 | Active power fixed adj.  | S32  | 1000 | kW   | Continuous plant setpoint (mode 0)  | **Not used** — see note |
| 40029    | Remote EMS enable        | U16  | 1    | –    | 0=disabled, 1=enabled               | `enable_remote_ems()` |
| 40031    | Remote EMS control mode  | U16  | 1    | –    | See §7.3 RemoteEMSControlMode       | `apply_lp_dispatch()` |
| 40032–33 | ESS max charging limit   | U32  | 1000 | kW   | Charge-rate cap honoured in modes 2/3 (in mode 2 it caps the cascade's battery leg; 0 = block charge / idle) | `apply_lp_dispatch()` |
| 40034–35 | ESS max discharging limit| U32  | 1000 | kW   | Max discharge rate (modes 5, 6)     | `apply_lp_dispatch()` |
| 40038–39 | Grid export limit        | U32  | 1000 | kW   | Max grid export (5kW DNSP limit)    | `set_export_limit_kw()` |
| 40046    | ESS backup SOC           | U16  | 10   | %    | Backup SOC reserve (V2.6+)          | `assert_battery_soc_limits()` (startup) + `assert_discharge_soc_limits()` (hourly) |
| 40047    | Charge cut-off SOC       | U16  | 10   | %    | SOC ceiling for charging — pinned at `soc_ceiling_pct` at startup, never rewritten in tick path | `assert_battery_soc_limits()` (startup only) |
| 40048    | Discharge cut-off SOC    | U16  | 10   | %    | SOC floor for discharging           | `assert_battery_soc_limits()` (startup) + `assert_discharge_soc_limits()` (hourly) |

**Register 40001 (deliberately not used):** Per Sigenergy V2.7 §5.2 note,
registers without an explicit "Comment" field (including 40001) only take
effect when `40031=0` (PCS Remote Control). Mode 0 drives the inverter to
a fixed plant-level active-power setpoint without tracking dynamic house
load — every load transient (kettle, oven, aircon) leaks as unintended
grid import or export, wasting ~$300/year on load-tracking errors. We use
load-following modes 3/4/6 instead, which let the inverter handle sub-second
load response within the LP's magnitude cap. Register 40001 is defined in
the codebase as a documented constant with rationale, but is never in the
write path.

**Registers 40032/40034 semantics:** per Sigenergy V2.7 §5.2, register
40032 (ESS max charging limit) is documented as taking effect in modes
3 and 4. **Empirically (verified on hardware) it also caps the battery
leg of mode 2's cascade** — `PV → house → battery (up to 40032) → export
→ curtail`. This is the basis for the mode-2 adaptive trim (§5.4) and
the mode-2 idle path (`40032 = 0` blocks PV-to-battery charge). Register
40034 takes effect only in modes 5 and 6; writing to the "wrong" register
is silently ignored, so stale values don't bleed across mode switches.
The optimiser writes both the relevant cap and the mode atomically each
tick.

### 7.3 Remote EMS Control Modes (Appendix 6 in Sigenergy V2.7)

| Value | Mode                            | Our usage                                                         |
|-------|----------------------------------|-------------------------------------------------------------------|
| 0     | PCS Remote Control              | **Not used** — doesn't track load                                 |
| 1     | Standby                         | Not used                                                          |
| 2     | Maximum Self Consumption        | Fallback, idle (40032=0), and PV-dominant charge (adaptive trim)  |
| 3     | Command Charging (Grid First)   | LP charge, grid-dominant source                                   |
| 4     | Command Charging (PV First)     | **Not emitted** — kept in enum for replay; see §5.4               |
| 5     | Command Discharging (PV First)  | LP discharge when PV is producing                                 |
| 6     | Command Discharging (ESS First) | LP discharge when no PV                                           |

**Mode-5-vs-mode-6 selection** is driven by live `measured_pv_kw`. Mode
6 zeroes PV generation (verified on hardware), so any PV producing
above 0.2 kW is worth routing through mode 5 instead. Mode 5
load-follows: PV serves the load and export first, battery covers any
shortfall, and if PV alone can do the job the battery idles (zero
wear for the same revenue). Earlier versions of this spec marked mode
5 as "not used" — that was a misreading of the LP's "discharge"
intent. See `SIGENERGY-MODES.md` mode 5 section.

---

## 8. Configuration

`config.example.toml` in the repo root is the authoritative template
for every field, with comments explaining each knob. Runtime loading
happens in `config.py` — the dataclasses there are the ground truth
if the example drifts.

Architectural points the example can't convey on its own:

- **Two cadences for Amber polling** (`poll_5min_interval_s`,
  `poll_30min_interval_s`) because 5-min prices are only reliable for
  the current and next 30-min window; see §5.1.1 and the decision log.
- **Separate AC and DC charge limits** (`max_ac_charge_kw`,
  `max_dc_charge_kw`). Grid import is AC-coupled through the 10 kW
  inverter; solar is DC-coupled directly to the battery and bounded
  by the ~13 kW PV array, not the inverter. The LP keeps them separate.
- **Slave ID 247 is the plant aggregate.** Individual inverters live
  on slaves 1..N; we read/write the plant to get combined-system state.
- **All times in config are local** (`deadline_hour_local`, evening
  reserve bounds). Internal storage is UTC. DST conversion lives in
  `time_utils.py` and `lp/loads.py`. See §6.8.
- **`config.toml` holds live API keys** — gitignored; edit in place
  under `/etc/energy-optimiser/config.toml` when deploying.

---

## 9. Acceptance Criteria

### 9.1 Operational State Machine

**Given** the service starts
**When** Modbus connection succeeds AND Amber API returns prices
**Then** state transitions to `ACTIVE`

**Given** the service is `ACTIVE`
**When** Modbus TCP connection is lost
**Then** state transitions to `DEGRADED` and last command is held

**Given** the service is `DEGRADED`
**When** 5 minutes elapse without Modbus reconnection
**Then** state transitions to `FALLBACK` and inverter is set to Self Consumption (mode 2)

**Given** the service is `ACTIVE`
**When** Amber API returns HTTP 5xx for 3 consecutive polls
**Then** state transitions to `ACTIVE_NO_PRICE` and planner uses last known forecast

**Given** the service is `ACTIVE_NO_PRICE`
**When** the last successful forecast is >1 hour old
**Then** state transitions to `FALLBACK`

### 9.2 Planner — Price Arbitrage

**Given** SOC is 30% and current import price is -5 c/kWh
**When** the planner runs
**Then** output is `CHARGE_GRID` with hot_water_start_cycle=True (if IDLE)

**Given** SOC is 80% and current import price is 45 c/kWh (spike)
**When** the planner runs AND spike_status == "spike"
**Then** output is `DISCHARGE_ESS`

**Given** SOC is 90% and import price is 12 c/kWh
**When** the planner runs AND no future interval exceeds 12 / 0.9 = 13.3 c/kWh
**Then** output is `SELF_CONSUME` (not worth cycling the battery)

### 9.3 Planner — Solar Awareness

**Given** Solcast forecasts 6kW PV output for the next 2 hours
**When** current import price is 8 c/kWh and SOC is 40%
**Then** output is `SELF_CONSUME` or `CHARGE_PV` (wait for free solar)
**And NOT** `CHARGE_GRID` (would waste money when free energy is imminent)

### 9.4 Planner — Evening Reserve

**Given** it is 2pm, SOC is 60%, and evening load profile shows 12kWh needed 5–9pm
**When** a 2pm price of 20 c/kWh would normally trigger discharge
**And** the planner calculates min_soc_before_evening = 10% + (12/40 × 100) = 40%
**Then** discharge is permitted (60% > 40%) but limited to not breach reserve

**Given** it is 2pm, SOC is 35%
**When** the same conditions apply
**Then** output is `SELF_CONSUME` (protect evening reserve)

### 9.4.1 Planner — Occupancy

**Given** no tracked phones are connected to UniFi for > `away_threshold_min`
**When** the planner calculates evening reserve
**Then** `occupancy_factor` = 0.2 (baseline appliances only)
**And** reserve is substantially reduced, freeing SOC for arbitrage

**Given** a phone briefly disconnects and reconnects within `away_threshold_min`
**When** the occupancy detector polls
**Then** `occupied` remains True (grace period prevents flapping)

**Given** the house is unoccupied
**When** a manual override sets `occupied = True` (e.g. guests expected)
**Then** the planner uses full reserve sizing

### 9.4.2 Planner — Temperature

**Given** BOM reports outdoor temp of 5°C (cold)
**When** the planner calculates evening reserve AND house is occupied
**Then** `temp_adjustment_factor` > 1.0 (inflated for heating load)

**Given** BOM reports outdoor temp of 22°C (mild)
**When** the planner calculates evening reserve
**Then** `temp_adjustment_factor` = 1.0 (no HVAC adjustment)

**Given** BOM reports outdoor temp of 38°C (hot)
**When** the planner calculates evening reserve AND house is occupied
**Then** `temp_adjustment_factor` > 1.0 (inflated for cooling load)

**Given** BOM API is unreachable
**When** the planner runs
**Then** `temp_adjustment_factor` = 1.0 (safe default, no adjustment)

### 9.5 Hot Water Scheduling (Signal-Driven)

**Given** a SIGNAL_DRIVEN load with `status.energy_today_kwh < daily_target_kwh`
**And** today's `deadline_hour_local` is still in the future
**When** the LP is built
**Then** a daily-target constraint is added for the today window such that
`sum(relay[t] × draw_kw × slot_hours for t in slots before deadline) ≥
daily_target_kwh − status.energy_today_kwh`

**Given** a SIGNAL_DRIVEN load with `status.energy_today_kwh ≥ daily_target_kwh`
**When** the LP is built
**Then** no today constraint is added (target already met)

**Given** `state.timestamp` is past today's `deadline_hour_local` local time
**And** `status.energy_today_kwh < daily_target_kwh`
**When** the LP is built
**Then** no today constraint is added (deadline passed; the shortfall
`max(0, daily_target_kwh − energy_today_kwh)` is rolled forward)
**And** tomorrow's constraint target becomes `min(daily_target_kwh + shortfall,
2 × daily_target_kwh)`

**Given** an extreme shortfall (e.g. multi-day service outage)
**When** the LP is built
**Then** tomorrow's constraint target is capped at `2 × daily_target_kwh`;
excess shortfall beyond the cap is forgiven silently

**Given** `status.power_kw > element_warning_threshold_kw` (default 2.5 kW)
**When** the Shelly client processes the reading
**Then** a `LOAD_CYCLE_FAULT` event is emitted with the load_id and a
message indicating possible element activation (LC misconfigured)

### 9.5.1 Cycle Detection (legacy SHIFTABLE — preserved for back-compat)

**Given** the relay was energised (cycle started)
**When** Shelly EM reports power > `power_zero_threshold_kw`
**Then** cycle state is `RUNNING`

**Given** cycle state is `RUNNING`
**When** Shelly EM power drops below `power_zero_threshold_kw` (heat pump finished)
**Then** cycle state transitions to `IDLE` (or `COMPLETE_TODAY` if `energy_today_kwh >= daily_energy_kwh`)
**And** the dry contact relay is de-energised on the next `status()` call
(via `_pending_relay_stop` flag — fixes the original fire-and-forget bug)

**Given** cycle state is `IDLE` and relay is on but power remains below threshold for >5 min
**Then** log a warning (heat pump may have failed to start) and de-energise relay

### 9.6 Telemetry

**Given** the service is in any active state (ACTIVE, ACTIVE_NO_PRICE)
**When** a tick completes
**Then** a row is written to DuckDB with all fields populated

**Given** 90 days of telemetry data exists
**When** the load profiler rebuilds
**Then** it produces 48-slot weekday and weekend profiles with mean and P75 values

### 9.7 Fallback Safety

**Given** the service receives SIGTERM
**When** `set_fallback()` runs during shutdown
**Then** the inverter is left in mode 2 (MAX_SELF_CONSUMPTION)

**Given** the service is killed ungracefully (SIGKILL, OOM)
**When** Remote EMS was enabled (register 40029 = 1)
**Then** the inverter remains in last-set mode until the watchdog fires
**And** the dead-man watchdog drives the fallback within
  `stale_seconds` + `poll_seconds` (default 90 s + 15 s = 105 s worst case)

**Given** the watchdog fires
**When** Modbus is healthy
**Then** three registers are written in order:
  - `REMOTE_EMS_CONTROL_MODE (40031) = 2` (MAXIMUM_SELF_CONSUMPTION)
  - `GRID_EXPORT_POWER_LIMIT (40038) = 0`
  - `REMOTE_EMS_ENABLE (40029) = 1`
**And** the inverter is pinned to an explicit known-safe state
  (not "whatever the operator had configured as local EMS")

**Given** the watchdog fires
**When** any of the three writes fails
**Then** the watchdog falls through to a last-resort
  `REMOTE_EMS_ENABLE (40029) = 0`, handing control to local EMS

**Given** the watchdog has fired and the heartbeat remains stale
**When** subsequent polls find the heartbeat still stale
**Then** the fallback writes run again on every poll (re-assertion model)
  — idempotent, defends against transient Modbus drops between poll and
  service recovery

**Given** the service restarts after a crash
**When** it reads register 40029 = 1 (Remote EMS still active, from the watchdog pin)
**Then** it resumes control, and the next tick touches the heartbeat —
  the watchdog sees the fresh mtime and stops re-asserting on its next poll

### 9.8 Data Quality & Validation

**Given** the grid sensor is offline (register 30004 ≠ 1)
**When** the telemetry writer creates a row
**Then** `grid_kw` and `house_load_kw` are NULL (not zero, not derived)

**Given** a derived `house_load_kw` is negative (< -0.1 kW)
**When** the telemetry writer validates the row
**Then** `house_load_kw` is set to NULL and a warning is logged

**Given** BOM data is older than 2 hours
**When** the telemetry writer creates a row
**Then** `outdoor_temp_c` is NULL for that row

**Given** a `house_load_kw` value exceeds 3× the rolling 7-day P95
**When** the telemetry writer validates the row
**Then** the value is written (legitimate outlier) but flagged in logs

### 9.9 Bootstrap & Data Maturity

**Given** the database has < 7 days of telemetry
**When** the planner requests a load profile
**Then** it receives a flat 2.0 kW profile (L0 cold start)
**And** the planner operates on price-only arbitrage

**Given** the database has 14 days of telemetry
**When** the planner requests a load profile for "cold + occupied + weekday"
**And** that specific bucket has < 50 samples
**Then** the profiler falls back to "occupied + weekday" (broader bucket)

**Given** the database has 90+ days spanning 3+ temperature buckets
**When** `assess_maturity()` runs
**Then** it returns maturity level 3 (full model)

**Given** the load profiler is rebuilding
**When** the 5-minute tick fires
**Then** the tick proceeds with the last-built profile (profiler does
not block the tick loop)

**Given** the service starts with an empty database
**When** the first tick runs
**Then** the planner functions with L0 defaults and does not error

### 9.10 Time Handling

**Given** it is January (AEDT, UTC+11) and evening_reserve_start_hour = 17
**When** the planner converts to UTC for comparison with Amber prices
**Then** 17:00 local = 06:00 UTC (not 07:00 UTC, which would be NEM 17:00)

**Given** an Amber price interval with `nemTime: 2026-01-15T17:30:00+10:00`
**When** the telemetry writer stores this
**Then** it is stored as `2026-01-15T07:30:00Z` (UTC)
**And** maps to local time 18:30 AEDT (not 17:30)

**Given** the clocks change from AEDT to AEST in April
**When** the evening reserve window is evaluated
**Then** it shifts correctly in UTC terms (17:00 AEST = 07:00 UTC, was 06:00 UTC under AEDT)
**And** NEM time alignment does not affect local-time config

### 9.11 Tick Snapshots & Replay

**Given** the planner completes a tick
**When** the tick snapshot is written
**Then** the NDJSON file contains the complete input state (system state,
full 48-interval price forecast, PV forecast, load profile, HW status)
**And** the planner output (action, limits, reason)

**Given** 30 days of tick snapshots exist
**When** a candidate planner is replayed against them
**Then** for each snapshot, the candidate receives identical inputs to
what the original planner saw
**And** the replay produces a cost delta per tick

**Given** the replay engine runs against snapshots from a period where
the original planner operated at L0 maturity
**When** the candidate planner also receives L0 load profiles
**Then** the comparison is fair (same maturity context)

**Given** a tick snapshot from 3 months ago
**When** the planner version field is checked
**Then** it identifies which algorithm version made that decision

### 9.12 Micro-arbitrage (5-min resolution)

**Given** the current 5-min export price is -2c/kWh
**And** the 30-min average export price is +5c/kWh (positive)
**When** the planner runs
**Then** it curtails export based on the 5-min price (the immediate reality wins)

**Given** the current 5-min import price is -3c/kWh for a single 5-min slot
**And** the surrounding 30-min average is +20c/kWh
**When** the planner runs
**Then** it triggers `CHARGE_GRID` for this tick
**And** reverts to normal logic when the 5-min slot ends

**Given** Amber 5-min price polling fails for one tick
**When** the planner runs
**Then** it falls back to the 30-min price for the current interval
**And** the failure is logged via `PRICE_STALE` event

**Given** the tick interval is 60 seconds
**When** a 5-min interval boundary is crossed mid-execution
**Then** the planner uses the price interval that was current at tick start
**And** the next tick picks up the new interval naturally

**Given** the planner ticks every 60s
**When** telemetry write boundary (5-min mark) is reached
**Then** a row is written to DuckDB
**And** intermediate ticks do NOT write telemetry rows
**And** action changes are still captured immediately as audit events

### 9.13 Wake Loop Alignment

**Given** the wake loop period is 60 seconds
**When** the service starts at HH:MM:42.5
**Then** the first wake fires at HH:(MM+1):00.0 (next minute boundary)
**And** subsequent wakes fire at HH:(MM+2):00.0, HH:(MM+3):00.0, etc.

**Given** a tick takes 8 seconds to complete
**When** the next 60s wake fires
**Then** the next tick still fires at the next minute boundary (no drift)
**And** the previous tick continues running concurrently if not yet finished

**Given** a tick is still running when the next wake fires
**When** the wake loop checks `_running_tasks`
**Then** the new tick is skipped
**And** a `TICK_OVERRUN` event is emitted with the loop name and scheduled time

**Given** a tick raises an unhandled exception
**When** the wake loop's `_wrapped` handler catches it
**Then** the exception is logged
**And** the wake loop continues to fire at the next aligned boundary
**And** `_running_tasks` is cleared so the next tick can run

**Given** the system clock jumps forward by 90 seconds (NTP correction)
**When** the next wake is computed
**Then** it aligns to the next valid boundary on the new clock
**And** no accumulated drift carries over

**Given** the service receives SIGTERM during a tick
**When** shutdown begins
**Then** all wake loops are cancelled
**And** in-flight tick tasks are awaited (with timeout) before fallback is set

---

## 10. Open Questions

1. **Max discharge rate** — AC charge confirmed 10 kW, DC charge ~13 kW
   (bounded by PV array). Discharge rate assumed 10 kW; still wants
   explicit confirmation against the inverter's nameplate, though no
   deviation has been observed in operation.

2. ~~**Heartbeat / watchdog on the inverter side**~~ — Answered
   definitively 2026-04-22: **there is no firmware watchdog**. Verified
   by (a) live SIGKILL test on hardware: the inverter held mode 6
   discharge for 3+ minutes with no revert; (b) protocol audit of the
   HA integration register definitions (3344 lines) and vendor Modbus
   Protocol PDF v2.8 — no `watchdog|heartbeat|keep-alive|timeout|auto-revert`
   mechanism outside alarm-code string tables. Mitigation implemented:
   external dead-man sidecar (§2) pins the inverter to an explicit
   safe state (mode=2, export=0, remote_ems=1) when the main service
   stops touching its heartbeat file, with a last-resort
   `REMOTE_EMS_ENABLE=0` fallthrough if any of those writes fails. See
   KNOWN-ISSUES #0d for the full audit record, the re-assertion model,
   and residual failure modes.

3. **Hot water temperature feedback** — Not currently available. The
   Shelly Pro EM energy monitoring partially compensates: if measured
   `energy_today_kwh` is low, the tank likely needed less heating
   (was already warm). Future enhancement: add a temperature probe,
   or reverse-engineer the HP330M1-U1 CN10 display bus.

---

## 11. Future: EV Integration (Sigenergy DC Charger + V2H)

**Status:** Planned. Architecture supports it; planner logic deferred until
EV and charger are installed.

### 11.1 Capability

The Sigenergy DC charger supports bidirectional charging (V2H). Running
states include `CHARGING (0x03)` and `DISCHARGING (0x08)`. This makes the
EV a second battery with a departure constraint.

With a 60kWh EV parked at home, total controllable storage rises from
40kWh (home ESS) to ~80kWh. The arbitrage window roughly doubles.

### 11.2 Modbus Registers

**Read (31xxx):**

| Register  | Name                  | Type | Gain | Unit | Description                |
|-----------|-----------------------|------|------|------|----------------------------|
| 31502–3   | DC charger output power | S32 | 1000 | kW  | Current charge/discharge   |
| 31504     | Vehicle SOC           | U16  | 10   | %    | SOC reported by vehicle    |
| 31513     | Running state         | U16  | 1    | –    | See DCChargerRunningState  |

**Write (41xxx):**

| Register  | Name                  | Type | Gain | Unit | Description                |
|-----------|-----------------------|------|------|------|----------------------------|
| 41000     | Start/Stop            | U16  | 1    | –    | 0=Start, 1=Stop            |

### 11.3 Load Category: DEADLINE_BIDIR

Unlike other managed loads, the EV has three unique constraints:

1. **Departure deadline** — must reach target SOC by a specific time
2. **Bidirectional** — can charge (load) OR discharge (source, like V2H)
3. **Variable capacity** — energy needed depends on current vehicle SOC
   and target SOC, which change per session

The planner treats the EV as a secondary battery with a reservation:

```
available_ev_kwh = (current_ev_soc - min_ev_soc) × ev_capacity
required_ev_kwh  = (target_ev_soc - current_ev_soc) × ev_capacity
charge_deadline  = departure_time - buffer

# Time available to deliver required energy
intervals_remaining = (charge_deadline - now) / interval_duration
min_intervals_charging = ceil(required_ev_kwh / charge_rate_kw / interval_hours)

# If plenty of time: fill cheapest intervals before deadline
# If tight: charge now regardless of price
```

**V2H strategy:** when the EV is parked with no imminent departure,
the planner can discharge the EV during price spikes (same logic as
home ESS discharge, but respecting the EV's SOC floor for driving
reserve).

### 11.4 Integration Notes

- EV presence detected via `dc_charger_running_state` (IDLE vs OCCUPIED)
- Vehicle SOC read directly from register 31504 — no OBD/car API needed
- Departure time must come from user input (config schedule, or manual)
- The `load_telemetry` table already supports this — add rows with
  `load_id = "ev"` and `category = "deadline_bidir"`
- The `TickSnapshot` already captures `managed_loads` which will include
  EV status when present
