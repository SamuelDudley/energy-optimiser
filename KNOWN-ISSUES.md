# Known Issues & Gaps

**Project:** Energy Optimiser
**Version:** 0.2.1
**Date:** 2026-04-22

> **LP transition note:** As of v0.2.0, the greedy planner (`planner.py`)
> has been fully replaced by a stochastic MILP optimiser (`lp/` module).
> Historical resolved issues referencing `planner.py` are preserved for
> context but the file no longer exists. The LP handles battery scheduling,
> managed-load coordination, and export management in a single optimisation.
>
> **Swagger integration (v0.2.1):** Amber's swagger (v2.1.0) was reviewed
> against the code; the LP now uses `advancedPrice.predicted` as the
> point estimate, rate-limit headers are parsed, and all forecast fields
> land in a dedicated `price_forecast_log` table for post-deploy
> calibration analysis. See issue 9 (re-opened-and-resolved) and
> issue 24 (deferred).

---

## Critical — Must fix before first run

### 0d. No Sigenergy firmware watchdog — ungraceful service death leaves inverter executing last command indefinitely

**Severity:** high safety — refutes a core assumption.

**Test on 2026-04-22, live hardware (IP 192.168.2.220, slave 247):**
Ran the service, let it reach `active` and command mode 6 (DISCHARGE_ESS_FIRST,
cap 10 kW). Sent SIGKILL to the container (no SIGTERM, `set_fallback()`
never ran). Polled the inverter at 3-s intervals for 3+ minutes.

Result: `EMS work mode` stayed at 7 (REMOTE_EMS), `remote_ems_enabled`
stayed at 1, `remote_mode` stayed at 6, battery kept discharging at
~1.3 kW tracking house load. No firmware-level revert at any point.

**Refutes:**
- DEPLOY.md rollback section: *"the Sigenergy firmware has a built-in
  'no Modbus communication for N seconds → revert to local mode' safety"*
  — not observed in practice.
- CLAUDE.md Critical rules: *"Fallback must always work... Never leave
  the inverter in a state that depends on the service being alive
  unless the heartbeat/watchdog question... has been resolved."* —
  it is now resolved, and the answer is *"it isn't there."*

**Crash-time exposure:** the inverter holds the last command forever. A
crash during an evening discharge ramps SOC to the floor with no
correction; a crash during a cheap-window grid charge could keep
charging straight into the evening peak.

**Mitigations (to be prioritised):**
1. Investigate Sigenergy holding registers for an explicit keep-alive /
   watchdog register (many industrial inverters have one — write a heartbeat
   every N seconds, firmware reverts if it ages out). Cleanest fix if available.
2. External dead-man on the Docker host: tiny shell/Python script
   polls the container's health or last-tick timestamp; if stale > N
   seconds, sends a standalone Modbus write to disable remote EMS
   (write 0 to 40029) and set mode 2. Separate failure domain from
   the main service, doesn't depend on pymodbus, HiGHS, DuckDB etc.
3. Docker `restart: unless-stopped` is already set — covers python-level
   crashes of the service (the container supervisor restarts it, next
   tick retakes control and re-plans). Only the cases where the
   supervisor itself dies (host reboot mid-peak, docker daemon crash)
   fall through to the dead-man above.

### ~~0c. Hot water scheduler refactor (SIGNAL_DRIVEN)~~ — Resolved

**Background:** the original "shiftable load with 90-min cycle" model didn't
fit the Haier HP330M1-U1's PV-mode operation. The HP manages its own
internal cycles when the dry contact is held closed; the optimiser's job is
to decide *when to assert the contact* across the day, continuously.

**Implementation:**
- New `LoadCategory.SIGNAL_DRIVEN` (also intended for future EV charging).
- Extended `LoadCommand` with `desired_relay_on: bool | None` (None = no
  change, True/False = explicit). Legacy `start_cycle: bool` retained for
  the SHIFTABLE back-compat path.
- New `_plan_signal_driven_load` in `planner.py` (now deleted — logic migrated
  to `lp/loads.py` `BinarySignalDrivenLoad`) implementing the rolling
  daily algorithm at 5-min slot resolution end-to-end. Slots use 5-min
  prices where available and 30-min planning prices beyond. PV-surplus
  slots scored by export price (opportunity cost), grid slots by import
  price. Hysteresis hold prevents tick-to-tick chatter.
- Extended `ManagedLoadConfig` with `daily_target_kwh`,
  `deadline_hour_local`, `hysteresis_buffer`, `hysteresis_extra`,
  `pv_surplus_threshold_kw`, `element_warning_threshold_kw`,
  `safety_floor_hours`. `draw_kw` default → 0.9, `power_zero_threshold_kw`
  default → 0.3 (HP standby is ~0.1–0.2 kW).
- New `ShellyLoadController.set_relay(on)` method for continuous,
  idempotent relay control. Wired into `service.py` apply step.
- Element-draw safety detection (`> element_warning_threshold_kw`) emits
  `LOAD_CYCLE_FAULT` event from the planner.
- 10 new tests in `TestSignalDrivenLoad` (cheap window timing, PV surplus,
  uniform expensive, deadline force-assert, target met, target met past
  deadline, hysteresis hold, negative 5-min spike, element warning).
- Spec §5.5 rewritten; §9.5 acceptance criteria rewritten.

**Outstanding:** `draw_kw = 0.9` is a guess. Tune from Shelly CT data once
running. The hysteresis defaults (`+2` / `+4`) give a ~30-min effective hold
band on top of strict need; may need bumping if relay chatter is observed.

**Test count:** 92/92 passing (was 82/82; +10 for `TestSignalDrivenLoad`).

### ~~0a. Dual-resolution price polling (5-min micro-arbitrage)~~ — Resolved

**Implementation:** New `wake_loop.py` module with `WakeLoop` class and `next_aligned_wake()`. Service refactored from single while-loop to N concurrent wake loops via `asyncio.gather()`. Independent loops for tick (60s), prices_30min (300s), telemetry, BOM, UniFi, and Solcast. `AmberClient.get_5min_prices()` added with shared `_fetch()` helper. `Planner.plan()` accepts optional `prices_30min` (back-compat default). Telemetry writes gated to 5-min boundaries (or on action change). 4 wake loop tests + 5 micro-arbitrage tests added. Total: 82/82 tests passing.

**Outstanding:** still need to verify Amber API rate limits in production (1440 fast + 288 slow polls/day). The `solcast_age` and `bom_data_age` checks were removed from the inline tick — they're now driven solely by their wake loops.


### ~~0. Export curtailment on negative feed-in~~ — Resolved

**Implementation:** Option A+D strategy. Added `grid_export_limit_kw` field to `PlannerOutput`, export curtailment priority check in `Planner.plan()` (between spike and negative-import), `SigenergyController.set_export_limit_kw()` method, wired into service tick loop. 4 tests in `TestExportCurtailment`. Uses register 40038 only — does NOT touch register 40036.

### ~~1. PV power register not implemented~~ — Resolved

**Implementation:** Audited the full Sigenergy HA integration source (`modbusregisterdefinitions.py`) and found that *three* register addresses in the original code were wrong, not just the PV placeholder:

| Register | Original code | Reality | Severity |
|---|---|---|---|
| `30083` | "ESS SOC" U16 gain=10 % | Actually `plant_ess_rated_energy_capacity` U32 gain=100 kWh | **Critical** — code was reading rated capacity (~400 raw value for 40 kWh battery) and dividing by 10, returning nonsense SOC values |
| `30599` | "ESS charge/discharge" S32 | `inverter_ess_charge_discharge_power` — per-inverter, not plant-level | Medium — works in single-inverter setups but wrong in plant-level reasoning |
| (none) | `pv_kw = 0.0` placeholder | Should read `30035` (`plant_sigen_photovoltaic_power`, S32 gain=1000) | **Critical** — blocks solar awareness, breaks `house_load_kw` derivation |

**Correct registers now in use:**
- `30014` `plant_ess_soc` — U16 gain=10 %
- `30035` `plant_sigen_photovoltaic_power` — S32 gain=1000 kW
- `30037` `plant_ess_power` — S32 gain=1000 kW (>0 charging, <0 discharging)

Write registers (40029, 40031, 40032, 40034, 40038, 40047, 40048) all verified correct against the integration source. EMS work mode (30003), grid sensor status (30004), and grid active power (30005) also verified correct.

Code change in `clients/sigenergy.py`: replaced the constants block with verified addresses, removed the `pv_kw = 0.0` placeholder, added a `_read_input_s32(REG_PLANT_PV_POWER)` call. Spec §7.1 updated with the corrected table and warnings about the historical bug. All 82 tests still pass (the existing tests don't exercise live Modbus reads).

**Implications for issue #10:** the data poisoning concern is resolved going forward. Any telemetry collected with the old code is still bad — should truncate the telemetry table on first deploy of the fixed code, or add a `schema_version` column for safety.

**Still untested:** the actual register addresses haven't been verified against a live Sigenergy. Issue #3 (Modbus addressing offset) remains the gating concern — pymodbus may need different addressing than the absolute addresses we're using.

### ~~2. Shelly relay de-energise after cycle complete is fire-and-forget~~ — Resolved

**Original issue:** When the heat pump finished its cycle (power dropped below
threshold), the Shelly client's sync `_update_cycle_state()` transitioned
state to IDLE but didn't actually call `_stop_relay()` because awaiting from
sync context isn't possible. The relay stayed energised.

**Fix:** Added `_pending_relay_stop: bool` flag on `ShellyLoadController`. The
sync `_update_cycle_state` sets the flag on cycle-complete; the next
`status()` call drains it at the top by awaiting `_stop_relay()`. Idempotent
and self-correcting — if a status() call fails, the next one retries the
stop.

**Folded into:** the SIGNAL_DRIVEN refactor (the same change introduced
`set_relay(on)` for continuous relay control, which is the primary control
path going forward; the SHIFTABLE path remains as legacy and benefits from
this fix).

### ~~3. Sigenergy Modbus register addressing may need offset~~ — Resolved on live hardware (2026-04-22)

**Investigation:** Audited the Sigenergy HA integration's `modbus.py` (`custom_components/sigen/modbus.py` lines 395-410). It calls `client.read_input_registers(address=register.address, ...)` with the **raw absolute address** (e.g. 30014, 30037), exactly the same pattern our code uses. The integration uses `pymodbus>=3.8.3`; we use 3.12.1 — same major version, same API.

The HA integration is widely deployed and known to work with Sigenergy hardware. Our code following the same pattern should also work. **No offset adjustment needed.**

**Live verification (2026-04-22, inverter 192.168.2.220 slave 247):** `eo-smoke --modbus-read` returned plausible values for all plant registers (SOC 58.2%, grid ~0 kW, ESS power consistent with observed discharge, PV 0 kW at night). SOC matched the Sigenergy app's display within 0.1%. Register addressing is confirmed correct.

**Also surfaced during the live test:** pymodbus 3.13 renamed the `slave=` kwarg to `device_id=` (breaking change vs 3.12 that the spec was written against). Fixed in `clients/sigenergy.py` — commit 8894d92.

### ~~3-old. Original concern~~

**File:** `clients/sigenergy.py`
**Original concern:** pymodbus uses 0-based addressing by default. Sigenergy documentation uses absolute addresses (30003, 40029, etc.). The HA integration may handle this differently. If the offset is wrong, every read/write will hit the wrong register — potentially dangerous for writes.

---

## High — Should fix before production use

### ~~4. No retry/backoff on API calls~~ — Resolved
**Files:** `clients/amber.py`, `clients/solcast.py`, `clients/bom.py`,
new `clients/_retry.py`
**Implementation:** `tenacity` added as dep. New `_retry.py` module with
per-API retry policies derived from each provider's documented limits.
Each policy gates on what's actually retryable for that API:

| API | Limit | 5xx/network | 429 | 4xx | Notes |
|---|---|---|---|---|---|
| Amber | 50 req/5-min/account | 3× exp 2/4/8s | **No** | No | Retrying 429 just re-triggers the per-account limit |
| Solcast | 10 req/day hard quota | 3× exp 2/4/8s | **No** | No | 429 is indistinguishable from quota exhaustion; next poll is the right fallback |
| BOM | None documented | 3× exp 2/4/8s | n/a | No (incl. 403) | 403 is anti-bot; retrying makes it worse. Now sets a polite `User-Agent` header to avoid triggering the rule in the first place |

**Solcast quota guard:** in addition to retry policy, `SolcastClient` now
tracks per-UTC-day successful calls (`_call_count_today`, `calls_today`
property) and pre-flights every fetch. When `count >= max_calls_per_day -
safety_buffer`, the client refuses to call and returns the cached
forecast with a `PRICE_STALE` event. Counter resets at UTC midnight.
Both `get_forecast` and `get_estimated_actuals` are guarded since they
share the same per-site quota. Configurable via new `SolcastConfig` fields
`max_calls_per_day` (default 10) and `safety_buffer` (default 1).

**Tests:** 14 new tests in `test_retry.py` covering every retry decision
(retry vs no-retry per status code) and the Solcast quota tracker
(increment, reset at UTC midnight, pre-flight cached fallback).

### ~~5. No graceful handling of DuckDB write failures~~ — Resolved
**File:** `store.py` `TelemetryStore`
**Implementation:** Write-ahead buffer in `TelemetryStore`. `write_telemetry`
and `write_load_telemetry` now buffer rows in a `deque` and attempt to
flush on every call. If the underlying DuckDB write raises (disk full,
lock contention, transient corruption), the row stays in the buffer and is
retried on the next call. Buffer capped at `max_buffer=500` (configurable);
overflow drops the oldest rows and emits a `VALIDATION_REJECT` event with
the count dropped. Final flush attempted on `close()`. `pending_count`
property exposed for diagnostics.

### ~~6. Shelly energy counter reset detection~~ — Resolved
**File:** `clients/shelly.py` `_track_daily_energy()`
**Implementation:** `ShellyLoadController` now tracks `_last_total_energy_kwh`
and detects backward jumps in `total_act_energy` greater than 0.1 kWh
(threshold protects against measurement jitter). On detection, the baseline
is shifted to `total - _energy_today_kwh` so the day's accumulator is
preserved across the reboot rather than going wildly negative. A
`VALIDATION_WARNING` event is emitted with the previous and new totals.
Additionally, `_track_daily_energy` is now skipped entirely when the Shelly
read failed — this prevents a transient read returning 0 from being
misinterpreted as a counter reset (which would corrupt the baseline).

### 7. UniFi API compatibility
**File:** `clients/unifi.py`
**Impact:** The UniFi controller API is not officially documented and varies between UniFi OS versions (UDM, UDM Pro, Cloud Key). The `/api/login` and `/api/s/{site}/stat/sta` endpoints may differ. Some controllers use `/api/auth/login` instead.
**Fix:** Test against the actual controller. Consider using the `aiounifi` library which handles version differences.

### 8. Snapshot files are never cleaned up
**File:** `logging_utils.py` `SnapshotWriter`
**Impact:** At ~1MB/day compressed, this will consume ~365MB/year. Not critical but will grow indefinitely.
**Fix:** Add a retention policy — delete snapshot files older than N days (configurable, default 180). Run as part of the daily 02:00 profile rebuild.

### ~~9. Planner doesn't use `advancedPrice` confidence bands~~ — Resolved (v0.2.1)
**Original file:** `planner.py` (deleted); resolved in `clients/amber.py`,
`lp/formulation.py`, `types.py`, `store.py`.

**Resolution:** After reviewing Amber's swagger (v2.1.0) in v0.2.1:

1. `advancedPrice.predicted` is now the primary point estimate used by
   the LP's import cost term. Falls back to `perKwh` (AEMO) when
   `predicted` is absent — which covers past intervals and beyond the
   ~24h advanced-forecast window. This is a strictly-additive change;
   worst case (no `predicted`) behaviour matches v0.2.0.
2. `advancedPrice.low` / `high` are captured on every `PriceInterval`
   (as `forecast_low` / `forecast_high`) but **not yet** used as LP
   price scenarios. Adding them requires post-deploy calibration data
   first (see issue 24).
3. `CurrentInterval.estimate` inverted to `is_locked: bool | None` —
   captured on `PriceInterval`, logged, but not yet used for decision
   logic. Present for future differentiation between locked and
   estimated current-interval prices.
4. Rate-limit response headers (`RateLimit-Limit`/`Remaining`/`Reset`)
   now parsed on every Amber response. `AmberClient` exposes
   `rate_limit_remaining` and `rate_limit_reset_seconds` properties;
   a rising-edge `PRICE_STALE` event fires below 5 remaining, re-arms
   after recovery.
5. A dedicated `price_forecast_log` DuckDB table captures every
   forecast at every fetch with both resolutions, enabling later
   forecast-vs-realised calibration analysis (see spec §6.3.1).

### ~~10. No PV register means house_load model trains on wrong data~~ — Resolved
**File:** `store.py`, `profiler.py`
**Implementation:** `schema_version INTEGER` column added to `telemetry` and
`load_telemetry` tables. New rows stamp `CURRENT_SCHEMA_VERSION = 2` (PV
register fixed). `ALTER TABLE … ADD COLUMN IF NOT EXISTS` migration runs
on startup so pre-existing installs get the column added with NULL for
legacy rows. All analytical queries (`get_rolling_p95`, `get_data_span_days`,
`get_valid_load_rows`, `get_temp_buckets_seen`, `get_load_profile_slots`)
now filter `schema_version >= CURRENT_SCHEMA_VERSION`, which excludes both
NULL (legacy) and lower-versioned rows. The first deploy of the fixed code
will see legacy data effectively quarantined — operator can either let it
sit (the queries skip it) or `DELETE FROM telemetry WHERE schema_version
IS NULL` to reclaim space.

---

## Medium — Correctness improvements

### ~~11. Planner doesn't account for charge/discharge duration~~ — Resolved
**File:** `planner.py`
**Implementation:** New `Planner._can_safely_defer_charging(state, until,
pv_forecast, load_profile, min_soc) → bool` method forward-simulates SOC
trajectory in 30-min steps from now to `until`, using P10 PV (pessimistic)
and the load profile. Returns True if SOC stays above `min_soc` the
whole way.

The charge step (§4 in the planner) now uses this: if a strictly cheaper
grid slot exists in the future forecast, the planner only defers to it
when `_can_safely_defer_charging` says it's safe. Otherwise, charges
now at the higher-but-acceptable current price. Prevents the previous
failure mode where the planner waited for the cheapest-of-day slot but
ran out of battery (or arrived with insufficient charging time) before
the evening peak.

**Tests:** 5 new in `TestDeferCharging` covering: defers when safe,
charges-now when defer would breach min_soc, charges-now when "now" is
already cheapest, helper-direct tests for both safe and unsafe windows.

**Note:** the spec acceptance criterion §9.4 (evening reserve) was
already satisfied by `_calculate_min_soc` — this issue was about the
*charge timing* being myopic. The two now cooperate: `_calculate_min_soc`
defines the floor; `_can_safely_defer_charging` enforces it during
deferral decisions.

### ~~12. Hot water cycle_intervals is hardcoded to 3~~ — Resolved (obsolete)

The cheapest-contiguous-window evaluator is no longer used for hot water.
HW is now SIGNAL_DRIVEN and uses the rolling daily scheduler (spec §5.5),
which scores 5-min slots individually and asserts/de-asserts a continuous
relay state — there's no fixed cycle window to size. The legacy SHIFTABLE
path retains the hardcoded `cycle_intervals = 3` but no production load
uses that path. Can be removed entirely once no SHIFTABLE configs remain.

### ~~11b. PV-aware refill cost in discharge decision~~ — Resolved
**File:** `planner.py`
**Implementation:** New `Planner._expected_marginal_charge_cost(state,
prices, pv_forecast, load_profile) → float` (per-kWh-OUT). Replaces the
naïve `future_min_import / efficiency` that the discharge decision (§5
in planner) was using.

For each future 30-min planning slot, generates up to two refill
candidates: PV (capacity = `min(pv_p10 - house_load, max_dc_charge) ×
0.5h`, cost = `export_per_kwh`) and grid (capacity = `max_ac_charge ×
0.5h`, cost = `import_per_kwh`). Sorts ascending, accumulates the
cheapest combination summing to the kWh needed to reach `soc_ceiling`.
Returns weighted average ÷ `round_trip_efficiency` for per-kWh-OUT.

Uses **P10** (pessimistic) PV — we don't bet on optimistic forecasts
when a wrong bet means the battery doesn't refill.

**Effect:** when tomorrow's forecast has plentiful PV at modest export
prices (e.g. 5c), refill cost drops to ~5.6c/kWh-out. The discharge bar
drops with it, so even modestly profitable evening peaks fire correctly.
Negative export prices yield a *negative* refill cost — any positive
discharge value is profit.

**Tests:** 4 new in `TestPVAwareDischarge`: PV forecast lowers charge
cost and unlocks discharge; P10 (not P50) is used; full battery returns
0 cost; no-forecast returns inf (fail-safe).

**Spec ref:** §5.4.1 (rewritten in this change).

### ~~13. No export limit awareness in discharge value calculation~~ — Resolved
**File:** `planner.py`
**Implementation:** New `Planner._effective_discharge_value(import_price,
export_price, house_load_kw) → (per_kwh_value, effective_kw)` method
implements the spec §5.4 formula. Discharge step (§5 in the planner)
now computes the realised per-kWh value (house portion offsets import
at import price; remainder up to export cap exports at export price;
anything beyond is curtailed) and compares THAT against
`future_charge_cost`. Spread threshold check uses the same realised
value, not the headline import price.

**Effect:** in scenarios with high import + low export + tiny house
load (e.g. 30c import, 0c export, 1 kW house, 10 kW discharge), the
old logic saw `30c > charge_cost` and discharged. The new logic
computes `(1×30 + 5×0)/6 = 5c` realised value and correctly falls
through to SELF_CONSUME.

**Tests:** 8 new in `TestEffectiveDischargeValue` covering: high house
load (no clipping), low house load (blended value), zero house load
(export only), the bug case (low export drags value below charge cost
→ no discharge), high export still discharges, export-cap clipping at
realised kW, zero house + zero export edge case, and `export_limit=0`
config.

### ~~14. State machine doesn't track FALLBACK→ACTIVE recovery via both signals~~ — Resolved
**File:** `state_machine.py`
**Fix:** Added `FALLBACK→ACTIVE` and `FALLBACK→ACTIVE_NO_PRICE` recovery
paths in `on_modbus_success()`, symmetric with the existing amber recovery
path. Both Modbus and Amber must be healthy to leave FALLBACK; if only
Modbus recovers but Amber is still failing, the state transitions to
`ACTIVE_NO_PRICE` (can operate without prices but can't plan optimally).
5 new tests in `TestFallbackRecovery` covering all combinations.

**Note:** a minor edge case remains: `on_startup_complete(modbus_ok=False)`
enters FALLBACK without setting `_modbus_lost_at`, so `on_amber_success()`'s
check `if self._modbus_lost_at is None` incorrectly reads "modbus is fine."
This means amber-recovery-during-startup-fallback transitions to ACTIVE
even though modbus never connected. Low severity — the next tick's
`read_state()` will fail and re-enter DEGRADED→FALLBACK. Documented in test.

### ~~15. BOM JSON structure not verified~~ — Resolved
**File:** `clients/bom.py`
**Implementation:** Parsing logic extracted to a pure `_parse_temperature`
method that defends against every realistic structural failure: top-level
not a dict, missing/wrong-typed `observations` key, missing/wrong-typed
`observations.data` key, empty data list, entries that aren't dicts, missing
or null `air_temp`, non-numeric `air_temp` values. The method walks the
records list (most recent first) and returns the first record with a
parseable numeric `air_temp` — so a single broken sensor reading doesn't
discard the whole response. Structural anomalies (real schema changes)
emit a `VALIDATION_WARNING` event with the observed type. 14 tests cover
each defensive branch.

---

## Low — Nice to have

### 16. No HTTP health endpoint
**Impact:** Docker health checks, monitoring tools, and manual debugging have no way to query the service's current state, last tick time, or error counts without parsing logs.
**Fix:** Add a minimal HTTP server (aiohttp or uvicorn) on a configurable port exposing `/health` and `/status` JSON endpoints.

### 17. No config hot-reload
**Impact:** Changing config (e.g. adding a new managed load, adjusting scenario weights) requires a container restart, which briefly sets the inverter to fallback mode.
**Fix:** Watch config file for changes, reload non-connection config (LP params, load configs, scenario weights) without restart. Connection params (Modbus host, API keys) still require restart.

### 18. Replay engine cost estimation is approximate
**File:** `replay.py` `estimate_interval_cost()`
**Impact:** The cost model is simplified — it doesn't account for SOC progression across intervals, partial charge/discharge, or the interaction between battery and PV. The LP's own `expected_total_cost_cents` is more accurate (full grid-flow accounting) but uses a different model than the historical data, so apples-to-apples comparison requires the same simplified model for both sides.
**Fix:** Build a full interval simulator that tracks SOC across the replay window and calculates actual grid flows per interval. Parameterise charge/discharge rates from config (AC vs DC aware). Alternatively, use the LP's native cost for the candidate and develop an equivalent slot-0 cost model for historical greedy actions.

### 19. No Prometheus/OpenTelemetry metrics
**Impact:** Structured logs work for alerting but aren't ideal for dashboards and trend analysis. No histogram of price arbitrage spread, no gauge of current SOC, no counter of planner decisions by type.
**Fix:** Add `prometheus_client` with key metrics. Expose on the health endpoint from issue #16.

### 20. Pre-conditionable load strategy is basic
**File:** `lp/loads.py` (future `PreConditionableLoad` implementation)
**Impact:** Aircon pre-conditioning is not yet implemented as an LP load. When added, it should model thermal mass — the LP needs to know "pre-cooling for 2h at 15c/kWh saves 2kW of demand at 50c/kWh later." Without a thermal model, the LP can only treat aircon as a shiftable binary load, which misses the economic value of pre-conditioning.
**Fix:** This improves naturally as the load profiler matures (L2+). The temperature-bucketed profiles will show observed thermal effects. Implement as a new `LPLoad` subclass with thermal decay constraints.

### 21. Reverse-engineer HP330M1-U1 CN10 display bus for tank temperature
**Files:** new `clients/heatpump_serial.py`, `types.py` (SystemState field), `lp/loads.py` (optional enhancement)
**Impact:** The Haier HP330M1-U1 heat pump water heater has a 4-wire display panel connector (CN10) on the main board. It carries every value the display shows — tank temperature, mode, setpoint, heating state, error codes — bidirectionally between the main MCU and the front panel. The built-in WiFi module uses Haier's SmartHQ cloud platform with no local API and no HA integration for this model class, making it useless for our purposes. Sniffing CN10 would give us complete local observability with no modifications to the appliance. The CN11 (dedicated tank temperature thermistor) tap is a simpler fallback if CN10 turns out to be intractable.

**Why this matters for the optimiser:**
- Real tank temperature enables early "tank full" detection (de-assert PV relay as soon as temp reaches 70°C without waiting for HP power to drop)
- Standing loss measurement (observed decay rate → kWh/day losses → feeds back into scheduler)
- Hygiene floor enforcement ("last time tank was ≥60°C") is a clean check instead of inferring from cumulative energy delivered
- Safety: detect element activation if LC setting is wrong (temp >75°C is impossible for HP-only mode)
- None of the above are required for v1 — the scheduler works with Shelly CT alone — but they materially improve the algorithm

**Reconnaissance plan (2 hours, do first):**
1. Power off HP at breaker, open control panel, locate CN10
2. Continuity-test each of the 4 pins back to the display panel to identify which pin is which
3. Power on HP. With multimeter in DC mode, measure voltage on each CN10 pin vs GND:
   - One pin near 0V → GND
   - One pin steady at 3.3V / 5V / 12V → VCC
   - Two pins near VCC with brief drops → UART TX/RX (idle high)
   - If two active pins mirror each other → RS-485 differential
4. Determine logic level:
   - 3.3V or 5V → TTL UART, use a cheap USB-UART adapter directly (FT232/CP2102)
   - 12V → RS-232 levels, need MAX3232 level shifter (~$3)
   - Differential → RS-485, need TTL-RS485 converter (~$3)
5. **Outcome gates further work.** Clean TTL UART → proceed. Anything weird → fall back to CN11 thermistor tap.

**Decode plan (4 hours to a weekend, if recon is clean):**
1. Hook up USB-UART adapter to CN10 in passive listen mode (only RX wired, never TX — we never drive the bus)
2. Capture 30 seconds at each common baud rate (9600, 19200, 38400, 57600, 115200) until bytes look like a coherent protocol (not random noise)
3. Capture distinct scenarios: idle, heating active, defrost, user button press, mode change, error
4. Diff captures to isolate which bytes change with which events
5. Tank temperature is typically the byte that changes slowly and monotonically during heating — easy to spot
6. Correlate byte values against panel display readings (write down panel temp, note raw byte, derive mapping)
7. Checksum — usually the last byte of each frame, usually XOR or sum-mod-256, sometimes CRC8
8. Document findings in a new `docs/heatpump-cn10-protocol.md`

**Integration plan (once decoded):**
1. New `clients/heatpump_serial.py` — async serial reader, parses frames, exposes `last_temp_c`, `mode`, `heating_active`, `error_code`, `temp_age` properties
2. Add `tank_temp_c: float | None` and `hp_mode: str | None` fields to `SystemState`
3. Add a wake loop `heatpump` with a short period (e.g. every tick, since serial reads are cheap)
4. Optional: enhance scheduler to use tank temp for early stop and hygiene floor
5. Tests: mock the serial stream with captured byte sequences, assert parser correctness

**Risks:**
- Voltage mismatch frying the sniffer — mitigated by measuring before connecting
- Proprietary binary protocol with checksums — possible time sink, but very tractable for consumer appliance UARTs
- Warranty void from opening the panel — certain but reversible (tap, don't cut)
- Protocol may be encrypted — extremely unlikely for a $4k consumer HP

**Fallback:** if CN10 turns out to be impossible (encrypted, unfamiliar framing, RS-485 with proprietary addressing), fall back to CN11 direct thermistor tap. CN11 gives us tank temperature only but is a known-good hack (NTC thermistor, ESP32 ADC, ESPHome integration, ~50 lines of YAML). File as #21b if needed.

**Prerequisite:** do not block v1 of the HW scheduler on this. The scheduler must work with only the Shelly CT as a data source. Tank temp is a v2 enhancement.

### 22. Load is treated as a deterministic forecast in the LP
**File:** `lp/formulation.py`
**Impact:** The stochastic LP currently treats PV as uncertain (P10/P50/P90
scenarios) but house load as a single deterministic curve from the load
profile. Real load varies materially day-to-day — visitors, work-from-home,
oven cycles, heating/cooling — and a wrong load forecast pushes the LP
into plans that are optimal-for-forecast but suboptimal-for-reality.
**Fix:** add a load distribution model — P10/P50/P90 per slot, decomposed
by time-of-day × weekday-vs-weekend × weather (heating/cooling demand) ×
occupancy. Feed scenarios as joint `(pv, load)` realisations. Scenario
count grows from 3 to 9, solve time roughly triples (still well inside
budget). Blocked on having ~3 months of CT data to fit a credible
distribution.
**Priority:** v2. Premature without data. Note in spec §5 once the LP is
in production and we can measure how much load variance actually
affects decision quality.

### 23. No decision sensitivity / Monte Carlo analysis tool
**File:** new `src/optimiser/analysis/`
**Impact:** When the LP makes a non-obvious decision, there's no easy way
to ask "how sensitive was this to forecast inputs?" — we can only see the
single chosen action, not the distribution of decisions across plausible
realisations of the forecast.
**Fix:** offline CLI tool that consumes a snapshot NDJSON file, samples
N realisations of `(pv, load)` from the forecast distributions, solves
the LP for each, and emits a sensitivity report: histogram of slot-0
decisions, identification of which forecast inputs flip the decision,
recommended scenario weight adjustments. Run weekly against the past
week's snapshots.
**Priority:** v2 enhancement. Must NOT run inline (~20s for 100 samples
at 200ms each — way over the 10s tick budget). Build as a CLI under
`src/optimiser/analysis/sensitivity.py` once snapshots are flowing.

### 24. Price scenarios not yet added to the LP (deferred pending calibration data)
**File:** `lp/formulation.py`, `lp/solver.py`
**Impact:** The LP currently treats prices deterministically (using
`advancedPrice.predicted` when available, `perKwh` otherwise). Only PV
uncertainty is modelled stochastically. If Amber's `advancedPrice.low`/
`high` band is meaningfully wide during volatile periods, the LP is
leaving robustness value on the table.
**Why deferred:** Building price scenarios requires three design
choices, each of which needs data we don't yet have:

1. **Band calibration.** Amber hasn't documented what percentile the
   `low`/`high` band represents. We need to measure — across a few
   weeks of realised prices — how often `realised ∈ [low, high]`.
   If ~66% it's a ~1σ band; if ~95% it's ~2σ; if 30% the band is
   garbage and scenarios built from it would be worse than the point
   estimate.
2. **Composition with PV scenarios.** Three viable options: cross-
   product (3 PV × 3 price = 9 scenarios, ~3× solve time), shared-
   percentile (3 scenarios, assumes correlation we haven't measured),
   or proper Monte Carlo (requires joint distributions).
3. **Scenario weights.** Depends on (1) and the cost of being wrong
   — which depends on (2) plus realised loss from miscalibrated
   decisions, which we can only measure via replay.

**Fix:** once `price_forecast_log` has 2-4 weeks of data, write a one-
off analysis script to answer (1) and (2), then add scenarios behind a
config flag that defaults OFF. Compare via replay before flipping.
**Priority:** post-deploy enhancement. The code as-shipped is correct
— this is an improvement, not a fix.
