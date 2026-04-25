# Plan: §3.3 — Mode 2 + dynamic `charge_cut_off_soc`

Elaborates on `OPEN-WORK.md §3.3`. Informed by a second round of code
reading after §3.1 landed — the §3.3 stub is correct in shape but a
few implementation details shifted.

## Context

Mode 4 (`COMMAND_CHARGING_PV_FIRST`) writes register 40032 as a
**target**, not a ceiling — verified on hardware
(`SIGENERGY-MODES.md`). When actual PV drops below the commanded rate,
the inverter pulls grid to hit the target. This is a silent-grid-draw
hazard in cloudy conditions.

The replacement: use mode 2 (`MAXIMUM_SELF_CONSUMPTION`) plus a
per-tick rewrite of reg 40047 (`charge_cut_off_soc`) to the LP's
intended end-of-slot SOC. Mode 2's native priority cascade (PV → load
→ battery-up-to-40047 → export → curtail) delivers "charge from PV up
to a ceiling" without any grid-draw risk, no transient-margin knob,
no live-load read, and self-corrects on mid-slot windfalls or
shortfalls. Mode 4 retires from the dispatch path entirely (the enum
stays for replay compatibility).

This is synergistic with §3.1: untied export lets each stochastic
scenario plan its own export flow; mode 2 + cutoff makes execution
safe regardless of which PV realisation materialises.

## Findings from re-reading the code (corrections to OPEN-WORK §3.3)

1. **`SlotDecision` does not expose `grid_to_battery_kw` yet.** Only
   `pv_to_battery_kw` is plumbed. The existing `dispatch_from_slot`
   infers the grid portion via `grid_kw = max(0, battery_kw - pv_kw)`.
   We should add `grid_to_battery_kw` to `SlotDecision` as part of
   this change — it removes an arithmetic rederivation and makes the
   mode-2-vs-mode-3 decision a clean "read the field".

2. **`_MODE_TO_ACTION` in `snapshot_adapter.py` is keyed on mode
   alone.** Under the new scheme a slot-0 dispatch is
   `(mode=2, kind=CHARGE, target_soc_pct=…)` — the current mapping
   would log this as `SELF_CONSUME`, not `CHARGE_PV`. Fix: map off
   `(mode, kind)` so mode-2-with-CHARGE-kind → `CHARGE_PV` for
   snapshot/replay continuity. Historical snapshots with mode-4 still
   round-trip because the mode-4 entry stays in the table — we only
   stop *producing* mode 4.

3. **`DispatchKind.CHARGE` still fits.** No new enum value needed; the
   *kind* describes the intent ("charge"), separate from the *mode*
   (how we execute it). `LPDispatch` gains a new field
   `target_soc_pct: float | None` populated only on the mode-2 charge
   path.

4. **Watcher is safe.** It reads `dispatch.signed_intent_kw` and has a
   30-second grace period before acting on deviation. A mode-2 charge
   may show `battery_power_kw ≈ 0` for the first few seconds while PV
   ramps, which is well inside grace; no false-positive fallback
   risk.

5. **`apply_lp_dispatch` write-order safety inverts.** Today, charge
   writes cap (40032) before mode (40031) so a cap-write failure
   aborts before changing mode. Under the new scheme the analogous
   safety is *cutoff before mode*: write 40047 (cutoff SOC) *then*
   40031=2, so a cutoff-write failure leaves the inverter in its
   previous (known) mode. Mirror the existing `test_cap_failure_
   aborts_before_mode_write` pattern, substituting 40047 for 40032.

## Phase 0: Hardware probes (BEFORE any code)

Four unknowns must be resolved empirically before committing. Each is
a small variation on `probe_mode4.py` / `probe_mode5.py` — three-phase
(baseline / probe / recovery), heartbeat-touch each loop, safe-state
revert in `finally`, NDJSON sample dump.

Create `src/optimiser/probe_charge_cutoff.py` covering:

1. **Rewrite frequency safety.** Write reg 40047 every 10 s for 10
   minutes with small varying values (e.g. `current_soc + 1%` then
   `current_soc + 2%`, alternating). Read back each write. Pass
   criterion: every write succeeds, readback matches within 0.1%, no
   modbus errors. **If fail:** add a guard to the tick path so 40047
   is only written when `abs(new − last_written) > 0.1%` (the
   register's own resolution).

2. **Cutoff below current SOC.** Precondition: SOC ≥ 50%. Write reg
   40047 = `current_soc_raw − 50` (5% below current). Hold for 60 s
   and observe battery power. Pass criterion: battery idles
   (`battery_power_kw ≈ 0 ± 0.1` kW), no discharge attempt, no
   alarm bits flipping. **If fail:** the dispatch must coerce cutoff
   to `max(current_soc, target)` so we never write a cutoff below
   current. Add the clamp in `set_charge_cut_off_soc`.

3. **Cutoff at exactly current SOC.** Write reg 40047 = current SOC
   raw value. Hold 60 s. Pass: clean skip, no zero-Wh cycling
   (`battery_power_kw` stays within ±0.05 kW, no oscillation
   signature in the sample log).

4. **Interaction with `assert_battery_soc_limits`.** Sequence:
   (a) write 40047 = 950 (95%) via startup-path equivalent
   (b) write 40047 = `current_soc + 2%` (tick-path equivalent)
   (c) read back, confirm the second write won
   (d) repeat 40 times at 5s cadence
   Pass: every read reflects the latest write; no firmware reverting
   to a "remembered" value.

All four probes write to NDJSON. Analyse with DuckDB; commit the
probe script; document findings in `SIGENERGY-MODES.md` under a new
"§4 charge-cutoff-SOC behaviour" section.

**Gate:** no code changes until all four probes pass. If any fail,
amend the dispatch logic per the fallback notes above and re-probe.

## Phase 1: Code changes

### 1.1 `SigenergyController` additions (`src/optimiser/clients/sigenergy.py`)

- **New method** `set_charge_cut_off_soc(pct: float) -> bool` — thin
  wrapper `return await self._write_u16(REG_CHARGE_CUTOFF_SOC,
  int(pct * 10))`. Clamps input to `[0, 100]` defensively; logs at
  INFO with the resolved raw value.
- **`assert_battery_soc_limits` unchanged for now.** Keep the
  startup write of reg 40047 as the `soc_ceiling_pct` initial value.
  Tick-time writes supersede. (Splitting into `assert_discharge_
  limits` + `assert_initial_charge_ceiling` is deferred to §4.2 when
  periodic re-assertion lands.)
- **`apply_lp_dispatch` rewrite** (the main behavioural change).
  Add a branch on `dispatch.mode == MAXIMUM_SELF_CONSUMPTION and
  dispatch.kind == CHARGE`:
  - Require `dispatch.target_soc_pct is not None` (assertion failure
    is a programmer error, not a data condition).
  - Safety order: `set_charge_cut_off_soc(target_soc_pct)` **before**
    `_write_u16(REG_REMOTE_EMS_CONTROL_MODE, 2)`. A cutoff-write
    failure returns False without touching the mode register.
  - No write to reg 40032 or 40034. Mode 2 doesn't consult them.
  - Export cap (reg 40038) write stays as today.

  Existing mode-3 charge branch stays: writes 40032 = cap × 1000
  before mode. Existing discharge branches (mode 5 / mode 6) stay:
  write 40034 before mode.

  Idle dispatch (`DispatchKind.SELF_CONSUME`): explicitly write reg
  40047 = current SOC so a stale cutoff from the previous tick
  doesn't cause unintended charging. This requires the dispatch to
  carry `target_soc_pct = current_soc_pct` for the idle case too —
  see 1.3.

### 1.2 `SlotDecision` + solver plumbing

- **Add field** `grid_to_battery_kw: float = 0.0` to `SlotDecision`
  in `src/optimiser/lp/result.py`. Default preserves test
  compatibility.
- **Extract in solver** at `src/optimiser/lp/solver.py:~253`:
  ```python
  grid_to_battery_kw=_v(vars.bat_charge_grid[t]),
  pv_to_battery_kw=_v(vars.bat_charge_pv[t]),  # existing
  ```
- No change to LP formulation (`bat_charge_grid` already exists as a
  decision variable, constrained separately from `bat_charge_pv` —
  confirmed during exploration).

### 1.3 `LPDispatch` + `dispatch_from_slot` (`src/optimiser/lp/dispatch.py`)

- **Add field** `target_soc_pct: float | None = None` to `LPDispatch`.
  Populated only on the mode-2 charge and idle paths.
- **Rewrite `dispatch_from_slot`.** Decision tree becomes:

  | LP intent | Condition | Mode | Cap write | Cutoff write |
  |---|---|---|---|---|
  | Idle (\|battery\| < DEADBAND) | — | 2 | — | current SOC |
  | Charge | `grid_to_battery > pv_to_battery` | 3 | 40032 = rate | — |
  | Charge | otherwise (PV-dominant or equal) | 2 | — | slot_0.soc_pct_end |
  | Discharge | `measured_pv_kw > 0.2` | 5 | 40034 = max_discharge | — |
  | Discharge | otherwise | 6 | 40034 = max_discharge | — |

  The mode-3-vs-mode-2 threshold is **`grid_to_battery_kw >
  pv_to_battery_kw`** — the existing mode-3-vs-mode-4 test. Trade-off
  analysis:
  - PV-dominant mixed plans under mode 2 under-execute the grid
    portion (e.g. plan 3 kW PV + 0.5 kW grid → inverter charges 3 kW;
    0.5 kW dropped). SOC ends ~0.6% below plan; LP re-plans next
    tick. Acceptable — well within forecast noise.
  - Grid-dominant mixed plans (> 50% grid) still go mode 3, fully
    executed as today.
  - Equal-split edge case (`grid = pv`) → mode 2, under-executes by
    50%. Rare in practice; LP re-plans next tick. If this turns out
    to be non-rare in replay (we don't have data yet), we can
    revisit the threshold.
- **`target_soc_pct` for mode-2 charge**: `slot_0.soc_pct_end`
  (already computed by the LP — no new math).
- **`target_soc_pct` for idle**: `current_soc_pct`. Requires
  `dispatch_from_slot` to take `current_soc_pct: float` as a new arg.
  (Or read it from `slot_0.soc_pct_end` for idle — for a zero-net
  tick that's the same as current. Cleaner to be explicit and pass
  it.)
- **Docstring update** including the SIGENERGY-MODES.md cross-ref
  explaining why mode 4 retires.

### 1.4 Service wiring (`src/optimiser/service.py`)

- Update the `dispatch_from_slot` call site to pass
  `current_soc_pct=state.soc_pct`. The existing `measured_pv_kw`
  plumbing (from §2.4) stays.

### 1.5 Snapshot adapter (`src/optimiser/lp/snapshot_adapter.py`)

Change `lp_solution_to_planner_output` to key on `(mode, kind)`:
```python
if dispatch.kind == DispatchKind.CHARGE and \
   dispatch.mode == RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION:
    action = BatteryAction.CHARGE_PV
else:
    action = _MODE_TO_ACTION.get(dispatch.mode, BatteryAction.SELF_CONSUME)
```
Historical snapshots (mode 4 → CHARGE_PV via the table) still
replay unchanged. The `charge_limit_kw` field in `PlannerOutput` is
0 for mode-2 charge (no cap written); `target_soc` already carries
the SOC intent so replay doesn't lose information.

## Phase 2: Tests

### 2.1 Migrate

- `tests/test_lp_integration.py::TestDispatchFromSlot`
  (lines 92–111): three tests currently assert mode 4.
  - `test_charge_pv_dominant_picks_mode_4` → rename to
    `test_charge_pv_dominant_picks_mode_2_with_cutoff`. Assert
    `mode == MAXIMUM_SELF_CONSUMPTION`, `target_soc_pct ==
    slot.soc_pct_end`, `cap_kw == 0` (no cap write on mode-2 path).
  - `test_charge_pv_only_picks_mode_4` → same migration.
  - `test_charge_equal_split_prefers_pv` → keep the name but update
    assertion to mode 2 (the semantic "prefers PV" intent holds;
    execution layer differs).
- `tests/test_sigenergy_apply_dispatch.py::TestWriteOrdering::
  test_charge_writes_cap_before_mode` (line 90): replace with
  `test_charge_writes_cutoff_before_mode`. Assert
  `rec.calls[0] == ("u16", 40047, raw)` then u16 40031. Drop the
  40032 assertion (mode 2 doesn't write it).
- `TestWriteFailureSafety::test_cap_failure_aborts_before_mode_
  write` (line 147): rename to
  `test_cutoff_failure_aborts_before_mode_write`, same pattern
  against 40047.
- `tests/test_service_lp.py`: tests use a `state` MagicMock with
  `pv_power_kw = 0.0`. Add `state.soc_pct = 50.0` now that
  `dispatch_from_slot` reads it.

### 2.2 Add

- `test_idle_writes_cutoff_at_current_soc` — assert that an idle
  dispatch writes reg 40047 = `current_soc * 10`, preventing stale
  cutoff-driven charging.
- `test_mode_3_still_fires_on_grid_dominant` — regression guard:
  when `grid_to_battery_kw > pv_to_battery_kw`, still mode 3,
  still writes 40032. (Existing `test_charge_grid_dominant_picks_
  mode_3` nearly covers this but uses the old inferred-grid logic;
  update to drive the new `grid_to_battery_kw` field explicitly.)
- `test_mode_2_charge_snapshots_as_charge_pv` — in the adapter:
  build an `LPDispatch(mode=2, kind=CHARGE, target_soc_pct=80.0)`,
  call `lp_solution_to_planner_output`, assert
  `battery_action == BatteryAction.CHARGE_PV` and `target_soc ==
  80.0`. Regression guard on the mapping change.
- `test_historical_mode_4_still_maps_to_charge_pv` — same adapter,
  but with `dispatch.mode == COMMAND_CHARGING_PV_FIRST`. Proves we
  didn't break snapshot replay for the pre-§3.3 history.

### 2.3 Integration

- `test_lp_trajectory_matches_dispatch_execution` — pick a
  multi-slot LP scenario (e.g. charge-then-hold), simulate execution
  slot-by-slot by: (1) solving, (2) reading `slot_0.soc_pct_end`,
  (3) advancing time, (4) feeding the new SOC back in as
  `state.soc_pct`, (5) re-solving. Assert the realised SOC
  trajectory stays within 1%-SOC of the LP's planned trajectory.
  This is the closest thing to a replay we can run without real
  snapshots.

Target: full suite 295 + 5 new + 5 migrated (no net regression in
count; net +5 tests).

## Phase 3: Rollout

1. Unit + integration tests green (`uv run pytest tests/ -q`).
2. Local smoke via `eo-smoke`.
3. Deploy to the running container (`docker compose up -d
   --build`). Watch structured logs for the first hour:
   - `apply_lp_dispatch` success rate (expect ~100%).
   - Any `lp_timeout` or `lp_error` (should be none).
   - `battery_power_kw` vs `signed_intent_kw` deviation (watcher's
     view). Expect normal ramp under mode 2, no verify-deviation
     fallbacks.
   - `soh_pct` / `cell_temp_max_c` from the extended telemetry —
     baseline for later analysis.
4. Collect ~7 days of NDJSON snapshots, then run replay retroactively
   against the old (mode 4) config if we can reconstruct one — this
   is a nice-to-have, not a gate.

No replay gate before deploy because:
- We don't have historical snapshot data locally.
- The change is structurally low-risk (strictly-safer execution path;
  mode 4's grid-draw hazard is eliminated by construction).
- Unit and property tests cover the dispatch logic; the hardware
  probes cover the cutoff behaviour; snapshot-adapter tests cover
  replay compat.

## Open questions / deferred

- **§3.2 (battery-net untie).** After §3.3 lands, the slot-0 "charge
  magnitude" tie effectively becomes a "target SOC" tie — re-analyse
  whether it should be relaxed. Not part of this change.
- **§4.2 (periodic SOC re-assertion).** Will need to split
  `assert_battery_soc_limits` into discharge-limits-only and
  charge-ceiling-only variants so the hourly loop doesn't fight the
  tick-time 40047 writes. Called out in OPEN-WORK §4.2; out of scope
  here, but the interaction is documented in §1.1 above so the
  §4.2 implementation knows what to avoid.
- **Price-aware mode-3 routing.** Current threshold
  (`grid > pv`) is behaviour-continuous with today. A smarter
  threshold that considers import vs export price is possible but
  adds complexity; revisit if replay shows equal-split plans are
  common and the under-execution matters.

## Risks

- **Mode 2 under-execution on mixed-source LP plans.** Mitigated by
  LP re-planning at 60s cadence. Worst case: 1–2% SOC drift from
  plan within a single tick, self-corrected within 2–3 ticks.
- **Firmware behaviour on reg 40047 rewrite cadence.** Phase-0
  probe explicitly tests. If it fails, guard is documented.
- **Idle-state cutoff writes could over-pin SOC** — if we write
  40047 = current_soc on idle, and PV then exceeds load mid-slot,
  the inverter curtails rather than storing the surplus. Mitigation:
  next tick re-evaluates (60s cadence) and either plans a charge
  (raises cutoff) or accepts the curtailment. Net effect is bounded
  by one tick's worth of mid-slot windfall, ~Wh scale — acceptable.
- **Watcher false positives during PV ramp.** 30s grace period is
  ~3× a typical PV ramp to steady-state; confirmed safe.

## Critical files to modify

| Purpose | File | Function / Line region |
|---|---|---|
| New controller method | `src/optimiser/clients/sigenergy.py` | add `set_charge_cut_off_soc` near existing SOC writes |
| Dispatch write rewrite | `src/optimiser/clients/sigenergy.py` | `apply_lp_dispatch` (lines 875–932) |
| SlotDecision field | `src/optimiser/lp/result.py` | `SlotDecision` dataclass (lines 22–34) |
| Solver plumbing | `src/optimiser/lp/solver.py` | `_extract_solution` (~line 253) |
| LPDispatch field | `src/optimiser/lp/dispatch.py` | `LPDispatch` dataclass (lines 50–62) |
| Dispatch rewrite | `src/optimiser/lp/dispatch.py` | `dispatch_from_slot` (lines 65–156) |
| Service wiring | `src/optimiser/service.py` | `dispatch_from_slot` call site |
| Adapter mapping | `src/optimiser/lp/snapshot_adapter.py` | `lp_solution_to_planner_output` (lines 41–79) |
| New probe script | `src/optimiser/probe_charge_cutoff.py` | new file, mirrors probe_mode4/5/6 structure |
| Migrated tests | `tests/test_lp_integration.py` | `TestDispatchFromSlot` (lines 72–157) |
| Migrated tests | `tests/test_sigenergy_apply_dispatch.py` | `TestWriteOrdering`, `TestWriteFailureSafety` |
| Test helper update | `tests/test_service_lp.py` | `_solver_args` state mock |
| New adapter tests | new file `tests/test_snapshot_adapter.py` or extend existing | 2 tests (mode-2-CHARGE + historical mode-4) |

## Reused utilities (nothing new invented)

- `_write_u16`, `REG_CHARGE_CUTOFF_SOC`, `REG_REMOTE_EMS_CONTROL_
  MODE` in `clients/sigenergy.py`.
- `DispatchKind.CHARGE`, existing `DEADBAND_KW` /
  `PV_PRODUCING_THRESHOLD_KW` constants in `lp/dispatch.py`.
- `SlotDecision.soc_pct_end` already populated in
  `solver.py::_extract_solution`.
- `bat_charge_grid` / `bat_charge_pv` decision variables already
  exist in `LPVars`; we just expose the slot-0 value through
  `SlotDecision`.
- Probe script pattern from `probe_mode4.py` / `probe_mode5.py` /
  `probe_mode6.py` — same three-phase shape, same heartbeat-touch,
  same safe-state revert, same NDJSON dump format.
