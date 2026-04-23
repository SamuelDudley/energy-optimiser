# Plan: Mode 2 + dynamic `charge_cut_off_soc` (OPEN-WORK §3.3)

## Context

Today's dispatch sends mode 4 (`COMMAND_CHARGING_PV_FIRST`) for any
PV-dominant charge, with register 40032 as a magnitude cap. Hardware
probes (`SIGENERGY-MODES.md`) showed two hazards of this combination:

1. Reg 40032 in mode 4 is a **target**, not a ceiling. If PV droops
   mid-slot, the inverter pulls grid to hit the target.
2. Load-spike transients during charging leak as grid import before
   the next tick can react.

§3.3 replaces mode 4 entirely with mode 2 (`MAXIMUM_SELF_CONSUMPTION`)
plus per-tick rewrites of reg 40047 (`charge_cut_off_soc`) to the LP's
planned end-of-slot-0 SOC. Mode 2's native priority is strictly
`PV → load → battery (up to cutoff) → export → curtail`; it never
grid-charges. Both hazards eliminated by construction, no tuning knob.

Mode 3 (explicit grid-charge) and modes 5/6 (discharge, already
shipped) unchanged. Idle (|battery| < deadband) collapses into the
mode-2 path with `target = current_soc` (hold).

## Corrections / confirmations from research

Three items that OPEN-WORK §3.3 glosses over or gets subtly wrong:

1. **`apply_lp_dispatch` does not currently rewrite 40047.** Only
   `assert_battery_soc_limits()` writes it, once at startup. §3.3 is a
   new behaviour (tick-rewriting 40047), not a modification of an
   existing frequency. Probe 1 is genuinely necessary.
2. **Legacy `SigenergyController.apply(PlannerOutput)`** at
   `clients/sigenergy.py:748-793` is dead code — only `apply_lp_dispatch`
   is reached from the tick loop. Its 40047 write is unreachable. Cleanup
   is optional (Commit 3).
3. **The grid-dominant threshold needs an LP-exposed signal.** Today's
   `pv_kw = slot_0.pv_to_battery_kw; grid_kw = max(0, battery_kw - pv_kw)`
   infers grid charge by subtraction. Under §3.3 the clean path is to
   expose `bat_charge_grid[0]` directly on `SlotDecision` as
   `grid_to_battery_kw`, so the decision reads LP vars as they actually
   are — no inference, no rounding error.

## Ordering (three commits)

1. **Probe only.** `src/optimiser/probe_charge_cutoff.py`. Run on
   hardware, write findings into `SIGENERGY-MODES.md §4`. All four probe
   pass criteria must be green before Commit 2.
2. **Implementation + tests.** All production code + unit/integration
   tests. Gated on probe results (some fail cases require an extra guard
   or clamp — see Phase 0 below).
3. **Cleanup (optional, can slip).** Remove legacy `SigenergyController.apply()`
   and `PlannerOutput.charge_limit_kw`/`discharge_limit_kw` if they
   become load-bearing only on dead-code paths.

## Phase 0 — Hardware probe (Commit 1)

New file `src/optimiser/probe_charge_cutoff.py`. Mirrors
`probe_mode4.py` / `probe_mode5.py` structure: imports `_sample_loop`,
`_summarise`, `Sample`, `BASELINE_DURATION_S`, `DEFAULT_CONFIG_PATH`.
Runs four sub-probes sequentially, each with
`baseline(10s) → probe(60s) → recovery(15s)`. `finally` block writes a
deterministic safe state: mode=2, export_cap=5 kW, charge_cutoff=ceiling
(e.g. 950). Heartbeat touches in every sample iteration. Total runtime
~6 min.

**Probe 1 — rewrite frequency safety.** Pre-req: PV > 2 kW,
50 ≤ SOC ≤ 85. Alternate 40047 between `(soc+1)*10` and `(soc+2)*10`
every 10 s for 10 min. Read back after every write. Pass: every write
returns True, readback within ±1 raw unit (0.1%), no Modbus exceptions,
no alarm bits flip. Fail → add `self._last_cutoff_raw` state on
`SigenergyController` and gate tick writes at `abs(new - last) > 1`.

**Probe 2 — cutoff below current SOC.** Pre-req: SOC ≥ 55%, PV > 2 kW.
Write 40047 = `(soc_raw - 50)` (5% below current). Dwell 60 s, 1 Hz.
Pass: `|battery_power| < 0.1 kW` throughout (inverter idles), no alarm
bits. Fail → clamp in `set_charge_cut_off_soc(pct)`:
`pct = max(pct, current_soc_pct + 0.1)`.

**Probe 3 — cutoff at exactly current SOC.** Write 40047 = `soc_raw`.
Dwell 60 s. Pass: battery power within ±0.05 kW, no oscillation.
Fail → same clamp as Probe 2.

**Probe 4 — supersession vs `assert_battery_soc_limits`.** Write
40047 = 950 (ceiling); immediately write `(soc+2)*10`; read back.
Repeat 40 times at 5 s cadence. Pass: every readback reflects the
tick-path write, never 950. Fail → split `assert_battery_soc_limits`
into `_startup_initial_ceiling()` (writes 40047 once) and
`_periodic_limits()` (writes only 40046 + 40048). Pulls §4.2's split
forward into this PR.

**Pre-implementation checklist** (all must pass before Commit 2):

- [ ] Probe 1 pass → no write-frequency guard needed.
- [ ] Probe 2 pass → no clamp needed.
- [ ] Probe 3 pass → boundary stable.
- [ ] Probe 4 pass → startup + tick coexist peacefully.
- [ ] Probe dump saved to
      `/var/lib/energy-optimiser/probe_charge_cutoff.ndjson`.
- [ ] `SIGENERGY-MODES.md §4 charge-cutoff-SOC behaviour` appended with
      findings (including any fail → guard/clamp that gets added in
      Commit 2).

**Run command** (takes service offline ~6 min):

```bash
docker compose stop optimiser
docker run --rm --network host \
  -v energy-optimiser_optimiser-data:/var/lib/energy-optimiser \
  -v /home/dudley/code/energy-optimiser/config.toml:/etc/energy-optimiser/config.toml:ro \
  energy-optimiser-optimiser python -m optimiser.probe_charge_cutoff
docker compose start optimiser
```

## Phase 1 — Implementation (Commit 2)

### 1.1 `lp/result.py` — new field on `SlotDecision`

Add `grid_to_battery_kw: float = 0.0` next to `pv_to_battery_kw`.
Default 0.0 preserves compatibility with tests that construct
`SlotDecision` positionally.

### 1.2 `lp/solver.py::_extract_solution` (line ~243)

Populate the new field in the trajectory loop:

```python
grid_to_battery_kw=_v(vars.bat_charge_grid[t]),
```

No formulation change — `bat_charge_grid` already exists
(`formulation.py:44, 253, 458`).

### 1.3 `lp/dispatch.py` — new field + rewritten charge branch

**`LPDispatch` dataclass**: add `target_soc_pct: float | None = None`.
Populated only on mode-2 paths (charge-via-cutoff and idle). Mode 3/5/6
keep it `None`.

**`dispatch_from_slot` signature**: add required kwarg `current_soc_pct`.
Needed for the idle branch (`target = current_soc`).

```python
def dispatch_from_slot(
    slot_0: SlotDecision,
    battery_config: BatteryConfig,
    *,
    current_soc_pct: float,
    measured_pv_kw: float | None = None,
) -> LPDispatch:
```

**Decision table:**

| LP intent | Condition | Mode | `cap_kw` | `target_soc_pct` |
|---|---|---|---|---|
| Idle | `\|battery_kw\| < DEADBAND_KW` | 2 | 0 | `current_soc_pct` |
| Charge, grid-dominant | `battery_kw > 0, grid_to_battery_kw > pv_to_battery_kw` | 3 | `battery_kw` | `None` |
| Charge, PV-dominant or tied | `battery_kw > 0` otherwise | **2** | 0 | `slot_0.soc_pct_end` |
| Discharge, PV producing | `battery_kw < 0, measured_pv > 0.2` | 5 | `max_discharge_kw` | `None` |
| Discharge, no PV | `battery_kw < 0, measured_pv ≤ 0.2` | 6 | `max_discharge_kw` | `None` |

Grid-dominant threshold reads `slot_0.grid_to_battery_kw` and
`slot_0.pv_to_battery_kw` directly — drops the inferred-subtraction in
today's code at dispatch.py:126. Equal split stays "PV-path" (falls to
mode 2).

Remove `COMMAND_CHARGING_PV_FIRST` references from dispatch-selection
logic. Keep the enum in `types.py` for historical snapshot replay.
Update docstring — delete the mode-4 row from the mapping table,
reference `SIGENERGY-MODES.md §4`.

### 1.4 `clients/sigenergy.py`

**New method** near `set_export_limit_kw` (insert before `set_fallback`,
~line 795):

```python
async def set_charge_cut_off_soc(self, pct: float) -> bool:
    """Write register 40047 (charge cutoff SOC). pct in [0, 100]."""
    clamped = max(0.0, min(100.0, pct))
    raw = int(round(clamped * 10))
    logger.info("Setting charge cutoff SOC to %.1f%% (raw=%d)", clamped, raw)
    return await self._write_u16(REG_CHARGE_CUTOFF_SOC, raw)
```

Add guard (`abs(new_raw - last_raw) > 1`) ONLY if Probe 1 fails. Default
is no guard.

**`apply_lp_dispatch` rewrite** (line 875). Safety-ordered: auxiliary
register first, then mode. A failure between the two must leave the
inverter in a consistent (old-mode, old-auxiliary) state.

```python
async def apply_lp_dispatch(self, dispatch: LPDispatch) -> bool:
    if not self._remote_ems_enabled:
        if not await self.enable_remote_ems():
            return False

    # Auxiliary write FIRST (cutoff for mode 2, cap for mode 3/5/6).
    if dispatch.mode == RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION:
        # Mode 2: covers CHARGE-via-cutoff AND SELF_CONSUME (idle).
        # target_soc_pct is required in both cases.
        assert dispatch.target_soc_pct is not None, (
            "mode 2 dispatch requires target_soc_pct"
        )
        if not await self.set_charge_cut_off_soc(dispatch.target_soc_pct):
            return False
    elif dispatch.mode == RemoteEMSControlMode.COMMAND_CHARGING_GRID_FIRST:
        cap_raw = max(0, int(round(dispatch.cap_kw * 1000)))
        if not await self._write_u32(REG_ESS_MAX_CHARGING_LIMIT, cap_raw):
            return False
    elif dispatch.mode in (
        RemoteEMSControlMode.COMMAND_DISCHARGING_PV_FIRST,
        RemoteEMSControlMode.COMMAND_DISCHARGING_ESS_FIRST,
    ):
        cap_raw = max(0, int(round(dispatch.cap_kw * 1000)))
        if not await self._write_u32(REG_ESS_MAX_DISCHARGING_LIMIT, cap_raw):
            return False

    # Mode register SECOND.
    if not await self._write_u16(REG_REMOTE_EMS_CONTROL_MODE, dispatch.mode.value):
        return False

    logger.info(
        "Applied LP dispatch: mode=%s cap=%.2fkW target_soc=%s intent=%+.2fkW",
        dispatch.mode.name,
        dispatch.cap_kw,
        f"{dispatch.target_soc_pct:.1f}%" if dispatch.target_soc_pct is not None else "-",
        dispatch.signed_intent_kw,
    )
    return True
```

Mode 2 path deliberately does **not** write 40032 or 40034. Mode 2
doesn't read them; leaving stale values is harmless. Write count per
tick stays at 3 (cutoff+mode+export), matching today's mode-4 path
(cap+mode+export).

`COMMAND_CHARGING_PV_FIRST` is no longer emitted from `dispatch_from_slot`
so it falls through this `if/elif` chain without any branch — no-op.
Deliberate: removing the enum would break snapshot deserialisation for
pre-§3.3 snapshots.

**`assert_battery_soc_limits`** unchanged by default. If Probe 4 fails,
split it per the Probe 4 fail-action.

**`set_fallback()` — reset 40047 to safe ceiling.** With 40047
tick-managed, a fallback event would otherwise leave the register at
whatever the last tick wrote (potentially a tight LP target like 70%).
Safe per mode-2 physics (no grid-charge possible), but non-deterministic
fallback state. Add one Modbus write to `set_fallback()`:

```python
# Reset the charge cutoff to the standard safe ceiling. Under §3.3 the
# tick path owns 40047 with dynamic targets; on entry to fallback we
# hand control back to the conservative default so the fallback state
# is fully specified regardless of what the last tick planned.
ceiling_raw = int(self._battery.soc_ceiling_pct * 10)
ok_cutoff = await self._write_u16(REG_CHARGE_CUTOFF_SOC, ceiling_raw)
```

Applies `mode_ok and export_ok and ok_cutoff` as the return value. Adds
one write to the fallback path (negligible latency, idempotent).

### 1.5 Watchdog sidecar — `charge_cut_off_soc` reset on fallback

The watchdog (`src/optimiser/watchdog.py`) currently writes mode=2,
export=0, enable=1 on staleness. Under §3.3 it should also reset 40047
so a watchdog-triggered fallback lands in a deterministic SOC-cutoff
state rather than inheriting whatever the main service last wrote.

- Add `REG_CHARGE_CUTOFF_SOC = 40047` constant (duplicated from
  clients/sigenergy.py, consistent with the other register constants
  already duplicated in watchdog.py).
- Add env var `EO_WATCHDOG_CHARGE_CUTOFF_RAW` with default `1000`
  (100%, absolute hardware max — universally safe without needing the
  battery config). Ops can tighten via env var if they want a lower
  ceiling under watchdog.
- Insert the cutoff write into `_trigger_fallback` between `mode=2` and
  `enable=1` (same auxiliary-first-then-enable ordering as current
  sequence). The last-resort `enable=0` path doesn't need a cutoff
  write — local-EMS mode is handing control back to the inverter.

Test additions in `tests/test_watchdog.py`:
- Updated happy-path expects 4 writes (cutoff + mode + export + enable).
- New `test_cutoff_write_uses_env_default` verifying the 100% default.
- Adjust existing failure-path test count expectations.

### 1.6 `service.py` line 657 — wire `current_soc_pct`

```python
dispatch = dispatch_from_slot(
    solution.slot_0,
    self._config.battery,
    current_soc_pct=state.soc_pct,
    measured_pv_kw=state.pv_power_kw,
)
```

### 1.7 `lp/snapshot_adapter.py` — disambiguate mode-2 CHARGE from idle

```python
if (
    dispatch.kind == DispatchKind.CHARGE
    and dispatch.mode == RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION
):
    action = BatteryAction.CHARGE_PV
else:
    action = _MODE_TO_ACTION.get(dispatch.mode, BatteryAction.SELF_CONSUME)
```

`_MODE_TO_ACTION` table unchanged — historical mode-4 snapshots continue
to map to `CHARGE_PV` for backwards-compatible replay of pre-§3.3 data.

Document the semantic shift in the adapter docstring:
`charge_limit_kw == 0` on a `CHARGE_PV` action now means "mode 2 +
cutoff", not "no charge". `target_soc` is the authoritative intent
field for mode-2 charges.

### 1.8 `lp/dispatch.py::verify_battery_response` — watcher fix

Today's over-cap check at line ~194-202 compares measured battery power
to `1.05 * cap_kw`. With `cap_kw = 0` under a mode-2 charge, *any*
charging trips the over-cap alarm. Fix: when `cap_kw == 0` and
`kind == CHARGE`, skip the over-cap check — the physical
`max_dc_charge_kw` is the true bound, and mode 2 can't exceed it.

Exact patch TBD in implementation; add a regression test
`test_mode2_charge_over_cap_skipped_when_cap_zero`.

## Phase 2 — Tests (part of Commit 2)

### 2.1 Update `tests/test_lp_integration.py`

- `dispatch_from_slot` helper at line 40 threads `current_soc_pct=50.0`
  by default.
- Rename `test_charge_pv_dominant_picks_mode_4` →
  `test_charge_pv_dominant_picks_mode_2_with_cutoff`. Construct slot
  with explicit `grid_to_battery_kw=1.0, pv_to_battery_kw=4.0,
  soc_pct_end=57.5, current_soc_pct=55.0`. Assert mode=2, kind=CHARGE,
  cap_kw=0, target_soc_pct=57.5.
- Rename `test_charge_pv_only_picks_mode_4` →
  `test_charge_pv_only_picks_mode_2`.
- Add `test_charge_equal_split_picks_mode_2` — tie falls to PV path.
- Update `test_charge_grid_dominant_picks_mode_3`: explicit
  `grid_to_battery_kw=4.0, pv_to_battery_kw=1.0`. Assert mode=3,
  cap_kw=5.0, target_soc_pct=None.
- Update `test_charge_grid_only_picks_mode_3`:
  `grid_to_battery_kw=5.0, pv_to_battery_kw=0.0`.
- Update `test_deadband_maps_to_self_consume`: extend to assert
  `target_soc_pct == 50.0` (default current_soc_pct).
- New `test_lp_trajectory_matches_dispatch_execution`: 3-slot horizon,
  solve → dispatch → simulate SOC respecting cutoff → next slot solves
  continuously. Assert realised trajectory within 1% of LP's forward
  plan.

### 2.2 Update `tests/test_sigenergy_apply_dispatch.py`

- Delete `test_charge_writes_cap_before_mode`. Replace with
  `test_mode2_charge_writes_cutoff_before_mode`: dispatch with
  `mode=MAXIMUM_SELF_CONSUMPTION, kind=CHARGE, target_soc_pct=65.0`;
  expect writes = [(u16, 40047, 650), (u16, 40031, 2)].
- Keep `test_discharge_writes_cap_before_mode` unchanged.
- Rename `test_self_consume_writes_only_mode` →
  `test_self_consume_writes_cutoff_and_mode`. Expect two writes:
  cutoff then mode.
- New `test_mode3_charge_still_writes_cap_before_mode` — regression
  guard on the grid-charge path (u32 → 40032, u16 → 40031).
- New `test_cutoff_failure_aborts_before_mode_write`: cutoff write
  returns False → mode write NOT attempted; function returns False.

### 2.3 New file `tests/test_snapshot_adapter.py`

- `test_mode2_charge_maps_to_charge_pv`
- `test_mode2_self_consume_maps_to_self_consume`
- `test_historical_mode4_still_maps_to_charge_pv` (regression guard for
  pre-§3.3 snapshot replay)
- `test_mode3_still_maps_to_charge_grid`

### 2.4 Update `tests/test_service_lp.py`

Any mock `state` passed to `_run_lp` or `_solve_args` needs
`state.soc_pct = 50.0` (not a bare `MagicMock()`). Grep for
`state = MagicMock` in the file and add the attribute.

### 2.5 Watcher regression

- `test_mode2_charge_over_cap_skipped_when_cap_zero` — verifies the
  §1.8 fix: a mode-2 CHARGE dispatch with `cap_kw=0` and measured
  charging at 3 kW should NOT raise WRONG_DIRECTION or OVER_CAP.

## Phase 3 — Verification

Replay isn't available locally (no snapshot archive). Pre-deploy
confidence:

1. `uv run pytest tests/ -q` — full suite green. Expected change:
   ~same net (~295) as existing tests migrate 1-for-1 and ~5 new unit
   tests land alongside.
2. `python -m optimiser.smoke` — end-to-end smoke runs cleanly.
3. Probe pass criteria from Phase 0.

**Post-deploy live-log checks** (first 60 min after deploy):

| Signal | Where | Expected | Rollback trigger |
|---|---|---|---|
| `apply_lp_dispatch` success rate | service logs | ~100% | any run of ≥3 failures |
| `Setting charge cutoff SOC` INFO line | service logs | once per tick | missing for > 2 ticks |
| `BREAKER_LATCHED` events | `events.ndjson` | zero | any occurrence |
| `state.grid_power_kw` during charge slot | telemetry | ≈ 0 | > 0.5 kW sustained |
| `state.remote_ems_mode` readback | telemetry | = last commanded | any drift |
| Watcher `WRONG_DIRECTION` / `OVER_CAP` events | events.ndjson | zero for mode-2 CHARGE | any → §1.8 fix incomplete |

Rollback path: revert Commit 2 in git. Commit 1 (probe) stays.

## Risks & mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Firmware rejects frequent 40047 rewrites | Med | Probe 1 verifies; guard if fail |
| Cutoff-below-current causes discharge | High | Probe 2 verifies; clamp if fail |
| Mid-slot PV windfall exceeds forecast → would exceed cutoff | Low | Inverter stops at cutoff; excess diverts to export (40038 cap), then curtails. Correct behaviour by design. |
| Mid-slot PV shortfall → SOC doesn't reach target | Low | Mode 2 can't grid-charge; battery stops early; next tick (60 s) re-plans with fresh state. No grid draw ever. |
| Snapshot adapter regression for pre-§3.3 historical data | Med | `test_historical_mode4_still_maps_to_charge_pv` + keep `_MODE_TO_ACTION` entry |
| Fallback inherits stale tight cutoff (e.g. 70% from last LP tick) | Low | §1.4/§1.5 resets 40047 to standard ceiling on both fallback paths (service-side `set_fallback` and watchdog). Even without the reset, mode-2 physics bound the hazard (no grid-charge possible). |
| Watcher false OVER_CAP on mode-2 CHARGE (cap_kw=0) | High | §1.8 fix + explicit test |
| Idle tick writes cutoff = current_soc, pinning SOC | Low | Bounded to 60 s per idle tick; next tick re-evaluates |
| Mode-3 edge case: LP plans small positive grid_to_battery but mostly PV | Low | Fine — mode 3 execution is safe; LP is already choosing this. Worst case: slight over-charge from grid if PV surges mid-slot. 60 s bounded. |

## Critical files

| Purpose | Path | Commit |
|---|---|---|
| Probe script (new) | `src/optimiser/probe_charge_cutoff.py` | 1 |
| Slot decision + solver plumbing | `src/optimiser/lp/result.py` | 2 |
| Solver populates new field | `src/optimiser/lp/solver.py` | 2 |
| Dispatch rewrite | `src/optimiser/lp/dispatch.py` | 2 |
| Controller + write path + fallback-resets-cutoff | `src/optimiser/clients/sigenergy.py` | 2 |
| Watchdog: reset cutoff in `_trigger_fallback` + env var | `src/optimiser/watchdog.py` | 2 |
| Snapshot adapter disambiguation | `src/optimiser/lp/snapshot_adapter.py` | 2 |
| Service call site | `src/optimiser/service.py` | 2 |
| Test migrations | `tests/test_lp_integration.py`, `tests/test_sigenergy_apply_dispatch.py`, `tests/test_service_lp.py`, `tests/test_watchdog.py` | 2 |
| New test file | `tests/test_snapshot_adapter.py` | 2 |
| Mode-doc update | `SIGENERGY-MODES.md` §4 | 1 (findings), 2 (final) |
| Legacy `apply()` removal | `src/optimiser/clients/sigenergy.py` | 3 (optional) |

## Reused utilities (no new helpers)

- `_write_u16`, `_write_u32` in `clients/sigenergy.py`
- `REG_CHARGE_CUTOFF_SOC`, `REG_REMOTE_EMS_CONTROL_MODE` already defined
- `DEADBAND_KW` in `lp/dispatch.py`
- `PV_PRODUCING_THRESHOLD_KW` in `lp/dispatch.py` (mode 5 vs 6,
  unchanged by §3.3)
- Probe harness: `_sample_loop`, `_summarise`, `Sample`,
  `BASELINE_DURATION_S`, `DEFAULT_CONFIG_PATH` from `probe_mode5.py`
- `assert_battery_soc_limits()` — stays as initial-ceiling writer

## Open questions to resolve before or during Commit 2

1. **Probe-gate strictness.** If Probe 1 passes but ≤1% of rewrites
   return False (transient Modbus jitter), is that pass or fail?
   Recommend: treat as pass if failure rate < 1 in 100 writes; add
   logging + a warning event on each failure so it's visible.
2. **Idle cutoff value.** Writing `current_soc_pct` exactly means the
   boundary-equal case (Probe 3) needs to be bulletproof. If Probe 3
   shows any oscillation at equality, use `current_soc_pct + 0.1` (one
   raw unit above) as the safe hold value.
3. **`PlannerOutput.charge_limit_kw` semantics.** Under §3.3 this field
   reports 0 for mode-2 CHARGE. Downstream readers (any dashboards or
   analytics) that rely on it as "actual charge rate" will be wrong.
   Grep for consumers after Commit 2 and decide whether to deprecate
   the field or repurpose as `target_soc_delta_kw`. Deferred — not
   load-bearing for §3.3 correctness.
4. **Exit criterion for §3.3.** One week of production with zero
   BREAKER_LATCHED events attributable to dispatch, zero grid draw
   during charge slots, zero watcher false-positives. Then consider
   it landed.
