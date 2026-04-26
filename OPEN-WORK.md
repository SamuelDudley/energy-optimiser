# Open work + thought process

Captures the state of in-progress LP/dispatch work so a fresh session can
pick up without re-deriving context. Written 2026-04-23 after a day of
debugging the LP's export/charge behaviour.

**How to use:** skim §1 for context, read §3 for active items (each has
the full reasoning), glance at §4 for smaller queued work. Authoritative
docs for specifics referenced inline.

---

## 1. Context

The energy optimiser runs a stochastic LP (P10/P50/P90 PV scenarios) to
dispatch a Sigenergy hybrid inverter + 40 kWh LFP battery + 13 kW PV on
Amber wholesale pricing. Previous session established:

- **Amber export sign convention was flipped** — fixed, historical data
  flipped too. See `clients/amber.py:201`.
- **Mode 4/5/6 semantics probed on hardware** — results in
  `SIGENERGY-MODES.md`. Key empirical findings:
  - Register `40032` (charge cap) is a **target**, not a ceiling — mode
    4 will pull grid to hit it.
  - Mode 5 load-follows: PV → load+export first, battery supplies
    shortfall. With surplus PV beyond load+export_cap, MPPT curtails.
  - Mode 6 **zeroes PV entirely** (pathological when PV is producing).
  - Mode 2 (self-consume) is the transient-safe default but charges to
    whatever the hardware `plant_charge_cut_off_soc` (reg 40047) allows.

## 2. Recently completed (ship order)

1. **Amber sign flip** — positive export price = revenue to customer.
2. **Fallback export cap reset** — `set_fallback()` writes export=DNSP or
   0 based on current price sign. Watcher forces export=0 on verify
   deviation (lost control ⇒ curtail). See `clients/sigenergy.py`,
   `lp/fallback.py`, `lp/watcher.py`.
3. **LP infeasibility at SOC=100% fixed** — SOC ceiling relaxed from
   hard bound to slack+penalty (`SOC_BOUND_PENALTY = 1e4`). New regression
   tests in `tests/test_lp_scaffolding.py::TestSOCBounds`. Added
   `BatteryConfig.backup_soc_pct` (default 15%) and
   `SigenergyController.assert_battery_soc_limits()` which writes regs
   40046/47/48 at startup.
4. **Dispatch: mode 5 when PV producing, mode 6 otherwise** —
   `dispatch_from_slot(slot_0, battery_config, measured_pv_kw)` threads
   live PV reading. Threshold `PV_PRODUCING_THRESHOLD_KW = 0.2`. See
   `lp/dispatch.py`.
5. **Hard SOC floor + per-slot enforcement** (commit `8893964`,
   2026-04-26). After the 2026-04-25 overnight (LP drained pack 70% →
   0.1% chasing 7-8c export at flat-pricing evening, then panic-
   recharged on Sunday morning), restored a hard `soc_pct[t] >=
   effective_floor` constraint with feasibility clamp `min(state.soc_pct,
   max(soc_floor_pct, backup_soc_pct, discharge_cutoff_pct))`. NO slack
   penalty on the lower side — that was the panic-buy regression
   retired in `9d23d14`. Now: LP can't sell into the safety reserve;
   if it enters a tick already below floor, just can't discharge
   further (no incentive to grid-charge "back up"). Tests in
   `tests/test_lp_properties.py::TestLPSOCBoundsTrajectory` (full
   forward-trajectory), `tests/test_lp_multitick_soc.py` (closed-loop
   solve→apply→re-solve over realistic shapes incl. sub-floor entry +
   morning-peak).
6. **Wear cost lifted 2.5 → 5 c/kWh + closed-loop simulator** (commit
   `0ab9e44`, 2026-04-26). Above the floor the LP was still cycling at
   speculative low-export prices because round-trip wear at 2.5c × 2 =
   5c was below typical low-tier ep. Per-tick diagnostic at the
   2026-04-25 23:00 AEST failure moment: wear=2.5 → -6 kW with 5 kW
   speculative export at 8c; wear=4.5+ → drops to -1 kW (house only).
   New default 5 c/kWh halfway to "true" LFP wear (~10 c/kWh per
   CLAUDE.md sizing); break-even spread now ~13 c rather than ~7.8 c.
   Validated via new `simulate.py` closed-loop simulator on 2026-04-25:
   pre-fix worst-case daily cost $2.90 → post-fix $0.85 (–$2.05/day
   under PV-bust scenarios) at the cost of ~$0.78/day less arbitrage on
   optimal sunny days. Also exposed `wear_cost_per_kwh` on
   `solve_stochastic` for replay/sweep work.

## 3. Active items

### 3.1 Untie `grid_export[0]` non-anticipativity (highest $ impact)

**Status:** ✅ shipped 2026-04-23 (commit `81ae377`). Replay validation
skipped (no local snapshot archive). Full test suite green.

**Current code** (`lp/formulation.py`, `_add_non_anticipativity`):

```python
prob += (other.grid_export[0] == base.grid_export[0], f"nonanti_export_{other_name}")
```

**Core insight:** what we commit to at slot 0 is the **export cap**
(register 40038) — a ceiling, not a setpoint. Each scenario's export
*flow* can legitimately differ inside that ceiling. The current
formulation ties the flow across scenarios, which is stricter than the
physical commitment requires.

**Effect of the over-tight tie.** With PV surplus varying by scenario,
tying forces one export value that minimises expected cost jointly. The
joint optimum typically sits near the P10 (low-PV) scenario's
feasibility ceiling because P10 can't sustain high export without grid
import. P50 and P90 then have to curtail PV to match.

Numerical example from the "SOC near ceiling, PV surplus" case:

| Scenario | PV  | Tied export | Untied export | Tied → curtail | Untied → curtail |
|----------|-----|-------------|---------------|----------------|------------------|
| P10      | 1kW | 0.5kW       | 0kW           | 0              | 0                |
| P50      | 3kW | 0.5kW       | 2.5kW         | 0              | 0                |
| P90      | 5kW | 0.5kW       | 4.5kW         | 4kW wasted     | 0                |

Weighted expected curtailment drops from ~800 Wh/slot to 0 — multi-kWh
per sunny day.

**Proposed change (Option A — minimal diff):**

1. Drop the non-anticipativity constraint on `grid_export[0]`. Each
   scenario picks its own.
2. In `lp/solver.py:287-290`, change the cap-extraction rule from
   "base scenario's export" to "max over all scenarios' export":
   ```python
   max_planned = max(sol_per_scenario[s].grid_export_kw[0] for s in scenarios)
   export_limit = 0.0 if max_planned < NUMERIC_EPS else battery_config.export_limit_kw
   ```
3. Update snapshot adapter + replay logic to read the max (not base)
   when extracting `grid_export_limit_kw`.

**Cleaner alternative (Option B):** introduce an explicit shared
`export_cap_0` decision variable and add per-scenario constraints
`scenario.grid_export[0] <= export_cap_0`. Semantically the "correct"
stochastic-programming formulation. Slightly more work. My recommendation
is to ship A, validate via replay, then consider B as internal cleanup.

**Risks:**

- Relaxing a non-anticipativity constraint strictly adds flexibility —
  no feasibility risk.
- Downstream consumers (snapshot adapter, replay) read `base.grid_export[0]`.
  Need updating together.
- Expected direction: same or better. Validate via replay against last
  ~30 days of snapshots before deploying.

**Test to add:** a property test showing that given scenarios with
P10-scarce/P90-abundant PV, the untied formulation produces zero P90
curtailment when `pv - load > tied_export > 0` under the current
formulation.

---

### 3.2 Reconsider `battery_net[0]` tie

**Status:** analysis deferred — needs a fresh pass under the post-
2026-04-25 dispatch (mode 2 adaptive trim + cutoff retire). The
original framing assumed the live mode-4 cap was a target, so untying
charge magnitude would re-introduce a grid-draw hazard. Mode 4 is now
retired; under mode 2 + adaptive trim, every charge is physically
bounded by 40032 to a value the dispatch derives at apply time from
live telemetry. The "untying loses the safeguard" argument no longer
applies — the safeguard moved to the dispatch layer.

**Current code:**

```python
base_net = base.bat_charge_grid[0] + base.bat_charge_pv[0] - base.bat_discharge[0]
other_net = other.bat_charge_grid[0] + other.bat_charge_pv[0] - other.bat_discharge[0]
prob += (other_net == base_net, f"nonanti_bat_net_{other_name}")
```

**Re-evaluation should consider:**

- Direction is still a hard commitment (one mode register written per
  tick). Tying the SIGN of `battery_net[0]` across scenarios stays
  essential.
- Magnitude is no longer a commitment on either side. Charge magnitude
  is bounded by the adaptive trim's Phase-B write at apply time;
  discharge magnitude is bounded by `max_discharge_kw` on 40034. The
  LP's tied magnitude is purely advisory for the LP's forward SOC
  trajectory.
- Untying magnitude means each scenario plans its own rate; the
  expected battery_net feeds the LP's cost model. Whether the savings
  vs the tied formulation are material is a replay question.

Defer until there's a concrete behaviour-shaped reason to revisit.

---

### 3.3 Mode 2 + dynamic `charge_cut_off_soc` — superseded

**Status (2026-04-24 → 2026-04-25): SHIPPED then SUPERSEDED.** The
per-tick `charge_cut_off_soc` rewrite landed in commit `acea3f5`, then
was retired in commit `1f363a7` (cutoff-pinned-at-ceiling + adaptive
trim on 40032). The dispatch behaviour now lives in
`SPEC-ENERGY-01.md §5.4` and `clients/sigenergy.py::_apply_mode2_adaptive_charge`.
Authoritative empirical record in `SIGENERGY-MODES.md` (mode 2 section
+ banner at top). Validation probes: `probe_two_phase.py`,
`probe_no_cutoff.py`. Nothing pending here.

**Interaction with §3.2 (battery-net tie):** the magnitude question
changed shape again. With adaptive trim, charge rate is dispatch-
bounded at apply time rather than determined by the LP's plan, so the
LP's tied magnitude is purely advisory. See §3.2 above for the
deferred-analysis note.

---

### 3.4 Cosmetic: exclude SOC slack penalty from reported `cost`

**Status:** ✅ shipped 2026-04-23 (commit `d38956b`). Reported cost
excludes the `SOC_BOUND_PENALTY * slack` internal regulariser; log
lines append `(penalty=NNNc)` when the penalty is non-trivial.

## 4. Smaller queued items

### 4.1 Watchdog explicit `connect()` + write-retry on startup

**Status:** ✅ shipped 2026-04-23 (commit `d5792ec`). `run()` now
pre-connects on startup; `_write_register` retries once on Exception
with a reconnect between attempts. isError() responses are not
retried (deterministic protocol errors).

`clients/sigenergy.py` connects lazily via pymodbus. If the watchdog
fires fallback on startup (heartbeat stale), the first Modbus writes
fail with "Not connected" because the client never explicitly connected.
Seen during the long-downtime test earlier today — ~60 s of "FALLBACK
FAILED" logs before it recovered.

**Fix:** explicit `await client.connect()` in watchdog startup. Add
retry-on-transient-failure around writes.

Covered by 3 latent risks:
1. If main service crashes and watchdog restarts cleanly, first
   fallback attempt could sit unable-to-write for ~60 s.
2. If Modbus connection drops transiently mid-loop, a single write
   failure could cause watchdog to declare failure instead of
   reconnecting.
3. Race between watchdog startup and optimiser holding the socket —
   watchdog blocked on connect until optimiser disconnects.

Low probability per event, but all three are mitigated by the same
change.

### 4.2 Re-assert SOC limits periodically

**Status:** ✅ shipped 2026-04-23 (commit `d2d350a`). Forward-correct
split: new `assert_discharge_soc_limits()` writes only 40046 + 40048
from a `WakeLoop("soc_limits", 3600s, ...)`. Reg 40047 is deliberately
skipped so §3.3 can land without touching this code path —
`assert_battery_soc_limits()` (all three regs) remains the startup
path unchanged.

### 4.3 Drop default `soc_floor_pct` from 10% in `BatteryConfig`

**Status:** ✅ resolved 2026-04-26. Default is 15.0 in `BatteryConfig`,
deployed `config.toml` sets 15.0 explicitly. The default is now also a
HARD operational floor in the LP (commit `8893964`), not just a soft
preference. See §2 entry 5.

### 4.4 Verify hardware SOC limits wrote correctly

We haven't actually confirmed that writing reg 40047 changed the
inverter's behaviour. Easy verification: at next tick with sun, read
reg 40047 back. If it's at 950 (95%), good. Can also inspect via
probe script (see §3 of `SIGENERGY-MODES.md` for pattern).

## 5. References

- `SPEC-ENERGY-01.md` — canonical spec, §5.2 covers LP, §7.3 covers
  modes.
- `SIGENERGY-MODES.md` — hardware-verified mode semantics including the
  mode-4-is-a-target correction and §3's operational hazards.
- `CLAUDE.md` — development guide with decision log (see "Decision log"
  table near the end).
- `KNOWN-ISSUES.md` — existing issue tracker; worth a read before
  starting each session.

## 6. Code map

All §3.1 / §3.3 / §3.4 / §4.1 / §4.2 entries previously in this table
are shipped. The only active item (§3.2 battery-net tie) is a deferred
analysis with no code-map entry until a fresh pass is scheduled.

## 7. Testing / validation checklist per change

1. `uv run pytest tests/ -q` — full suite must pass.
2. For LP formulation changes: replay against `/var/lib/energy-optimiser/snapshots/2026-*.ndjson.gz`
   and check delta is non-negative (savings or wash). Command:
   ```bash
   python -m optimiser.replay_cli \
     -s '/var/lib/energy-optimiser/snapshots/2026-*.ndjson.gz' \
     -c config.toml -o results.ndjson -v
   ```
3. For dispatch changes: add at least one unit test covering the new
   branch.
4. Before deploying any LP change that affects real-money decisions:
   run replay over at least the last 14 days.
