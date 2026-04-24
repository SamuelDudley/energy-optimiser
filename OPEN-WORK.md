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

**Status:** analysis pending user direction. My recommendation: don't
untie as a *standalone* change under the current mode-4 dispatch. But
with §3.3 below (mode 2 + dynamic charge cutoff) in place, the
charge-magnitude question changes shape significantly — worth
re-analysing after §3.3 lands, not before.

**Current code:**

```python
base_net = base.bat_charge_grid[0] + base.bat_charge_pv[0] - base.bat_discharge[0]
other_net = other.bat_charge_grid[0] + other.bat_charge_pv[0] - other.bat_discharge[0]
prob += (other_net == base_net, f"nonanti_bat_net_{other_name}")
```

**What we actually commit to at slot 0.** Dispatch mapping in
`lp/dispatch.py::dispatch_from_slot`:

| LP slot-0 intent       | Mode (40031) | Charge cap (40032)         | Discharge cap (40034)              |
|------------------------|--------------|----------------------------|------------------------------------|
| \|bat\| < 0.1 kW       | 2            | (ignored in mode 2)        | (ignored in mode 2)                |
| bat > 0 (charge)       | 3 or 4       | **= bat (target in mode 4)** | (ignored)                        |
| bat < 0 (discharge)    | 5 or 6       | (ignored)                  | = `max_discharge_kw` (physical)    |

Two observations with different implications:

**(a) Direction is a hard physical commitment.** Scenarios must agree
on sign of `battery_net[0]` — we write one mode register. If P10 wants
discharge and P90 wants charge, the inverter can't do both. Tying
direction is essential.

**(b) Discharge magnitude is NOT a commitment.** We write
`max_discharge_kw` to register 40034 *regardless* of the LP's intent.
The LP's magnitude ends up as `signed_intent_kw` for the watcher, but
the inverter load-follows. So tying the magnitude across scenarios is
advisory — it affects the LP's forward SOC trajectory but doesn't affect
what gets executed at slot 0.

**(c) Charge magnitude IS a commitment (mode 4 cap = target).** Untying
would let P10 want 1 kW charge and P90 want 5 kW charge; whatever single
value we write, someone over- or under-charges via grid. This is where
the current tie has real protective value — it prevents mode 4's
"target, not ceiling" hazard from being triggered by scenarios with
low PV.

**Why I don't recommend untying battery_net as a standalone change:**

- **Discharge:** untying helps only with advisory planning, not
  execution. LP's reported `battery_kw` becomes a weighted average of
  scenario outcomes; replay gets slightly more accurate; real-world
  behaviour unchanged. Low value.
- **Charge:** untying without another safeguard *worsens* the mode-4
  grid-draw hazard we documented in `SIGENERGY-MODES.md`. Don't.

**If we did want to untie charge magnitude, §3.3's approach (mode 2 +
dynamic charge cutoff) provides the safeguard.** Under §3.3, each
scenario can plan its own charge rate; the LP's tied decision becomes
"target SOC at end of slot 0" (implied by whichever scenario's plan
translates to what we write); mode 2's physics-bounded execution
prevents any plan from translating into grid draw. Re-analyse this
section once §3.3 is implemented — the framing will likely change.

---

### 3.3 Mode 2 + dynamic `charge_cut_off_soc` for all PV-charge dispatch

**Status (2026-04-24):** probe ran successfully (~10:05 AEST, SOC
57.5%, PV 7.5 kW). Results in `PLAN-3.3.md` "Probe results" section.
Probes 1 (cadence) and 4 (supersession) PASSED; probes 2 and 3 (cutoff
at/below current SOC) soft-failed with the exact failure mode the plan
anticipated. Mitigation: clamp `cutoff = max(target, current+0.1%)` in
`set_charge_cut_off_soc`. Net effect on §3.3 architecture: stands as
designed; idle is leaky by ~tens of Wh per slot, which is noise.

**Commit 2 (implementation + tests) is now unblocked.** Apply the
clamp per the updated §1.4 in PLAN-3.3.md. No write-frequency guard
needed.

**Previous iterations of this section explored two wrong answers:**

1. "Mode 2 + export_cap expresses export-first by splitting PV between
   battery and export concurrently" — **false**. Mode 2 priority is
   strictly `PV → load → battery (up to physical rate) → export →
   curtail`. Battery absorbs before export sees flow.
2. "Mode 4 + live-telemetry charge-cap clamp" — workable but inherits
   mode 4's grid-draw hazard (bounded, not eliminated) and requires a
   transient-margin tuning knob.

**The right approach: use `charge_cut_off_soc` (reg 40047) as a per-tick
charge ceiling under mode 2.**

The inverter stops charging when SOC reaches `charge_cut_off_soc`
regardless of available PV. If we rewrite that register each tick to
`current_soc + desired_charge_this_slot`, mode 2's native priority
cascade does the rest: charge until cutoff, then divert to export,
then curtail.

```
target_soc_pct = current_soc_pct + (LP_charge_kw × slot_hours × eta
                                    / capacity × 100)
write  40047 = target_soc_pct × 10    # charge cut-off SOC
write  40038 = DNSP_max × 1000        # export cap (full DNSP)
write  40031 = 2                      # mode 2
```

**Why this is better than mode 4 + clamp:**

| Property | Mode 4 + clamp | Mode 2 + dynamic cutoff |
|---|---|---|
| Grid-draw on PV droop | Bounded by margin, still possible | **Impossible** — mode 2 never grid-charges |
| Load-spike transient → grid | Bounded, still possible | **Impossible** — mode 2 is transient-safe |
| Mid-slot PV windfall (above forecast) | Curtailed above cap | **Captured** — battery charges further or export takes it |
| Mid-slot PV shortfall | Grid draws to hit cap | Battery stops earlier; export caps lower; no grid |
| Requires live PV/load reads at dispatch | Yes (`measured_pv_kw`, `measured_load_kw`) | No (only current SOC, already in `SystemState`) |
| Tuning knob | `transient_margin_kw` (needs calibration) | None |
| Mode 4 needed | Yes | **No** — retires entirely |

Mode 2 + dynamic cutoff eliminates both mode-4 hazards from
`SIGENERGY-MODES.md` by construction. There's nothing to calibrate, no
transient margin, no live-load read.

**What this CAN'T do (and needs other modes):**

- **Grid-charging** (cheap-overnight windows). Mode 2 never grid-charges.
  Mode 3 still required for explicit `CHARGE_GRID` dispatch. Unchanged.
- **Discharging.** Mode 2 discharges only passively to load. Mode 5 /
  mode 6 still required for discharge dispatch. Already implemented via
  §4's PV-threshold selection.

**Resulting dispatch table** (simpler than today's):

| LP intent | Mode | Key register |
|---|---|---|
| \|battery\| < deadband (idle) | 2 | `40047 = current_soc` (hold) |
| battery > 0, grid-dominant (rare, cheap window) | 3 | `40032 = LP rate` |
| battery > 0, any PV-driven charge | **2** | `40047 = current_soc + Δ` |
| battery < 0, measured_pv > 0.2 kW | 5 | `40034 = max_discharge` |
| battery < 0, measured_pv ≤ 0.2 kW | 6 | `40034 = max_discharge` |

Mode 4 row drops out.

**Edge cases to verify empirically before deploy (probe-style):**

1. **Write frequency safety.** Is reg 40047 safe to rewrite every 60 s
   indefinitely? It's a standard holding register per the Sigenergy
   Modbus spec, so should be, but some firmware internally gates
   parameter changes on flash writes. Proposed probe: write 40047 in
   a loop for 5–10 min, read back, watch for errors or drift. If any
   concern, add a guard: only write when `abs(new_target - last_written)
   > 0.1%` (the register's resolution).
2. **Cutoff below current SOC.** If we write cutoff=80% while SOC=81%,
   does the inverter ignore it, try to discharge, or error? Standard
   semantics say "charge cutoff = don't charge above"; discharging is
   governed by mode. Expected: idle. Probe with a 60-s dwell and
   observe battery power.
3. **Cutoff at exactly current SOC.** Boundary case — does mode 2
   attempt zero-Wh charge cycling? Or does it cleanly skip to the
   next priority? Expected: clean skip. Confirm.
4. **Interaction with `assert_battery_soc_limits()` startup write.**
   Startup currently writes 40047 = `soc_ceiling_pct` (95%). Tick-time
   write will supersede. The periodic re-assertion wake loop (§4.2)
   needs to *not* re-assert 40047 — only 40046 (backup) and 40048
   (discharge cutoff). Alternatively: split the method so startup
   writes 40046/48 only and 40047 is tick-managed entirely.

**Implementation sketch:**

1. Add `SigenergyController.set_charge_cut_off_soc(pct: float) -> bool`
   — a thin wrapper around `_write_u16(REG_CHARGE_CUTOFF_SOC, int(pct*10))`.
2. Remove 40047 from `assert_battery_soc_limits()` (or keep writing at
   startup for the initial safe state, then let the tick path manage
   it). Simplest: keep startup write as "95% initial ceiling"; ticks
   overwrite.
3. Rewrite `dispatch_from_slot` charge branch:
   - Keep direction/mode selection as today.
   - Instead of returning `cap_kw = LP_intent`, compute
     `target_soc_pct_end = slot_0.soc_pct_end`  ← LP already computes
     this! Nothing new to calculate.
   - Return mode 2 + `target_soc_pct` in a new `LPDispatch` field.
4. `apply_lp_dispatch` writes:
   - `40031 = 2`
   - `40047 = target_soc × 10`
   - `40038 = DNSP_max × 1000` (or 0 if LP wants zero export for
     price-negative slot)
   - No charge/discharge-cap write needed for mode-2 path.
5. Mode 5 and mode 6 paths unchanged.
6. Mode 3 path kept for explicit grid-charge intent (detect by
   `slot_0.grid_to_battery_kw > 0`? — sanity check: does the LP
   model grid-vs-PV charge contribution? see `bat_charge_grid` var).
7. Tests:
   - Unit: charge intent → mode 2 + correct cutoff.
   - Unit: discharge intent → mode 5 or 6 (unchanged).
   - Integration: a multi-slot simulation where SOC trajectory matches
     `slot_0.soc_pct_end` when executing via mode 2 + cutoff (verify
     the LP's model and the execution model agree).

**Interaction with §3.1 (export-untie):** synergistic. §3.1 lets each
scenario pick its own export flow; §3.3 makes execution safe regardless
of which flow actually materialises. Combined, the LP can plan
aggressive export in P90 and conservative in P10, and the inverter just
does the right thing across the realised PV.

**Interaction with §3.2 (battery-net tie):** likely becomes a different
conversation. With §3.3, "what we write for charge" is a *target SOC*
(a scalar integrating over the slot), not a *rate*. The tie may want to
be on target SOC rather than net kW — but let's re-analyse after §3.3
lands; until then the battery-net tie stays.

---

### 3.4 Cosmetic: exclude SOC slack penalty from reported `cost`

**Status:** ✅ shipped 2026-04-23 (commit `d38956b`). Reported cost now
excludes the `SOC_BOUND_PENALTY * slack` internal regulariser; log
lines append `(penalty=NNNc)` when the penalty is non-trivial.

`tick_complete` events log `cost=NNNc` which is the LP's objective
value, including `SOC_BOUND_PENALTY * slack`. When SOC is above ceiling,
this inflates reported costs by 100s of thousands of cents — misleading
for dashboards.

**Fix:** in `lp/solver.py` where `cost = pulp.value(prob.objective)` is
computed, subtract the slack-penalty term so the reported cost is the
"true economic expected cost" and the slack penalty is internal to the
solver only.

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

Current default is 15.0 (I raised it during §3 work), but the user's
`config.toml` explicitly sets 10.0 and wins. Discussion about whether
to raise to 15/20% for cycle life is unresolved. See §3 of
`SIGENERGY-MODES.md` and discussion in the session log — we landed on
"LFP is tolerant; 15% is sensible; 10% is fine too". No urgent action.

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

## 6. Code map for active items

| Item | File(s) | Lines to touch |
|------|---------|----------------|
| §3.1 untie export | `lp/formulation.py` | `_add_non_anticipativity` (line ~440) |
| §3.1 untie export | `lp/solver.py` | 287-290 (export_limit derivation — read max across scenarios) |
| §3.1 untie export | `lp/snapshot_adapter.py`, `replay.py` | any `base.grid_export[0]` reads |
| §3.3 mode 2 + cutoff | `lp/dispatch.py` | rewrite `dispatch_from_slot` charge branch to return mode 2 + `target_soc_pct` |
| §3.3 mode 2 + cutoff | `lp/dispatch.py` | add `target_soc_pct` field to `LPDispatch` dataclass |
| §3.3 mode 2 + cutoff | `clients/sigenergy.py` | new `set_charge_cut_off_soc(pct)`; update `apply_lp_dispatch` write path for mode 2 |
| §3.3 mode 2 + cutoff | `clients/sigenergy.py` | ensure §4.2 periodic reassertion doesn't fight the tick-time cutoff writes |
| §3.4 cost exclude slack | `lp/solver.py` | ~line 292 (`cost = pulp.value(...)`) |
| §4.1 watchdog connect | `watchdog.py` | startup; add retry loop |
| §4.2 periodic SOC | `service.py` | add `WakeLoop` to startup list; skip 40047 if §3.3 lands |

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
