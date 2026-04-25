# Plan: two-phase adaptive mode-2 dispatch (bug #2)

## Context

§3.3 (shipped 2026-04-24, commit `acea3f5`) replaced mode 4
(`COMMAND_CHARGING_PV_FIRST`) with mode 2 + dynamic `charge_cut_off_soc`
for the "charge from PV up to a target SOC" dispatch path. That removed
the grid-draw hazard mode 4 had (cap is a target, not a ceiling) but
exposed a follow-on issue in the cascade.

Mode 2's cascade is sequential:

```
PV → house load → battery (up to cutoff and 40032 cap) → export → curtail
```

So with `40032 = max_dc_charge_kw` (uncapped — the §3.3 fix for stale
caps throttling battery DC), every available kW of surplus PV goes to
the battery first. Export only flows once the battery cascade is
saturated (cutoff reached or 40032 cap hit). On hardware we observe a
flip-flop:

- Battery charging at 9 kW, export = 0
- Battery at cutoff → export = 5 kW, charge = 0

We are leaving money on the table during the daytime window where a
mixed split (e.g. 4 kW charge + 5 kW export) would clear both wear
cost and earn export revenue. The LP doesn't see this — it can plan a
mixed slot, but the cascade enforced by the inverter writes "battery
OR export" sequentially.

**Why we can't just trust the LP plan.** Two related blockers:

1. **No independent measure of true PV potential.** Solcast forecast is
   the only independent signal; live `pv_power_kw` (reg 30035) reports
   what's flowing right now, not the MPP available. If we cap 40032 at
   the LP's planned charge rate (e.g. 4 kW) and conditions are better
   than forecast, the cascade silently curtails because it can't sink
   the surplus anywhere. We lose the upside without ever knowing.
2. **The LP doesn't value being full.** Confirmed by re-reading the
   objective in `formulation.py` — pure economic + wear, no SOC reward.
   So if priced horizon need is "just enough", the LP won't plan a
   mixed slot at all on a sunny midday. We need a dispatch-layer
   strategy that exploits real-time conditions even when the LP plan is
   conservative.

**The proposed fix (user idea).** Make mode-2 dispatch a two-phase
operation per tick:

- **Phase A — measure (~5 s):** write `40032 = max_dc_charge_kw`,
  cutoff = LP target, export = 0. Battery soaks all surplus PV at the
  physical max so we read true MPP from `pv_power_kw`.
- **Phase B — split (rest of tick):** trim
  `40032 = max(LP_rate, max(0, surplus_A − export_cap))`. Export at the
  DNSP cap. Cascade now naturally splits between battery and grid
  without curtail.

Where `surplus_A = max(0, phase_A_pv_kw − phase_A_house_load_kw)`.

The probe (`probe_two_phase.py`, landed alongside this plan) validates
the split-and-curtail behaviour empirically before any production code
or tests are written. **This plan is conditional on the probe passing.**

---

## Files to modify

### 1. `src/optimiser/clients/sigenergy.py`

Refactor `apply_lp_dispatch` to split on mode and add an adaptive
mode-2 branch.

- **Add module constants** near the top (after the existing register
  block):
  ```python
  # Two-phase mode-2 dispatch (bug #2 / PLAN-MODE2-ADAPTIVE.md):
  # Phase A pins 40032 = max so we can read true MPP, then Phase B
  # trims so battery + export split rather than cascade-saturate.
  MODE2_PROBE_SECONDS: float = 5.0
  # Trim safety floor: never trim below the LP's intended charge rate,
  # even if measured surplus says we could. Protects against
  # transient PV droops during phase A inflating the trim-down.
  MODE2_TRIM_FLOOR_HEADROOM_KW: float = 0.5
  ```
- **`apply_lp_dispatch`** — split branches:
  ```python
  if mode == RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION:
      return await self._apply_mode2_adaptive(dispatch, export_cap_kw)
  return await self._apply_static_dispatch(dispatch)
  ```
  The existing mode-3/5/6 logic stays as `_apply_static_dispatch`
  (rename, no behavioural change).
- **New: `_apply_mode2_adaptive`**
  ```python
  async def _apply_mode2_adaptive(
      self, dispatch: LPDispatch, export_cap_kw: float
  ) -> bool:
      """Two-phase: measure full PV, then trim 40032 so battery and
      export split. See PLAN-MODE2-ADAPTIVE.md for the rationale."""
      # Phase A: write 40032=max, cutoff=target, mode=2 (export already
      # written by service.py path before dispatch).
      max_charge_raw = int(round(self._battery.max_dc_charge_kw * 1000))
      if not await self._write_u32(REG_ESS_MAX_CHARGING_LIMIT, max_charge_raw):
          return False
      if dispatch.target_soc_pct is None:
          logger.warning("mode 2 dispatch missing target_soc_pct; "
                         "leaving cutoff (40047) untouched")
      else:
          cutoff_raw = max(0, min(1000, int(round(dispatch.target_soc_pct * 10))))
          if not await self._write_u16(REG_CHARGE_CUTOFF_SOC, cutoff_raw):
              return False
      if not await self._write_u16(
          REG_REMOTE_EMS_CONTROL_MODE,
          RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION.value,
      ):
          return False

      # Phase A measure window
      await asyncio.sleep(MODE2_PROBE_SECONDS)
      state = await self.read_state()
      if state is None or state.pv_power_kw is None or state.house_load_kw is None:
          # Telemetry blind — leave Phase-A state in force (uncapped
          # charge, export at LP cap). Equivalent to today's behaviour.
          logger.warning("mode 2 phase-A telemetry None; staying uncapped")
          return True
      surplus_a = max(0.0, state.pv_power_kw - state.house_load_kw)

      # Phase B trim. LP_rate floor protects against transient PV droop
      # during the 5 s window collapsing the trim to zero.
      lp_rate = abs(dispatch.signed_intent_kw) if dispatch.signed_intent_kw else 0.0
      trim_kw = max(
          lp_rate,
          max(0.0, surplus_a - export_cap_kw) - MODE2_TRIM_FLOOR_HEADROOM_KW,
      )
      trim_kw = min(trim_kw, self._battery.max_dc_charge_kw)
      trim_raw = int(round(trim_kw * 1000))
      logger.info(
          "Mode-2 adaptive: phaseA pv=%.2fkW load=%.2fkW surplus=%.2fkW "
          "→ phaseB trim=%.2fkW (lp_rate=%.2fkW, export_cap=%.2fkW)",
          state.pv_power_kw, state.house_load_kw, surplus_a,
          trim_kw, lp_rate, export_cap_kw,
      )
      return await self._write_u32(REG_ESS_MAX_CHARGING_LIMIT, trim_raw)
  ```
- **`apply_lp_dispatch` signature change**: takes `export_cap_kw: float`
  as a new positional kwarg. Caller (`service.py`) must pass it in. This
  also means service must reorder writes — see §2.

- **`set_fallback`** — leave alone. Already writes `40032 = max` so
  fallback inherits Phase-A semantics (uncapped charge, mode 2). No
  adaptive trim in fallback by design — paranoid path stays simple.

### 2. `src/optimiser/service.py`

Reorder so the export cap is in force when `apply_lp_dispatch` starts
its Phase-A read, and pass it through.

- **Lines 442-445 (apply export limit block)** — move BEFORE the
  `apply_lp_dispatch` call. Current order:
  ```python
  await self._sigenergy.apply_lp_dispatch(dispatch)  # currently first
  ...
  if output.grid_export_limit_kw is not None:        # currently second
      await self._sigenergy.set_export_limit_kw(output.grid_export_limit_kw)
  ```
  New order:
  ```python
  if output.grid_export_limit_kw is not None:
      await self._sigenergy.set_export_limit_kw(output.grid_export_limit_kw)
      self._last_export_limit_kw = output.grid_export_limit_kw
  await self._sigenergy.apply_lp_dispatch(
      dispatch,
      export_cap_kw=output.grid_export_limit_kw or 0.0,
  )
  ```
- This reorder is safe: export-cap writes are idempotent and don't
  affect mode/charge/discharge legs; the inverter's previous mode keeps
  honouring whatever export cap is in force until `apply_lp_dispatch`
  swaps mode.

- **Tick budget**: 60 s → 5 s of that is now Phase-A sleep inside
  dispatch. Other tick work (telemetry write, profile update, snapshot)
  must still fit. Spot-check `tick_complete` event durations after
  deploy — should still be well under 60 s.

- **`_last_export_limit_kw` snapshot caching**: already used by
  `service.py:633` for the snapshot adapter. Move just needs the cache
  update before dispatch (line 445 already does it; trivial).

### 3. `src/optimiser/lp/dispatch.py`

No formulation changes. The mapping is unchanged:
`bat>0, pv ≥ grid → mode 2 + cutoff = soc_end`. The dispatch
two-phase split happens entirely inside `clients/sigenergy.py`.

But: `signed_intent_kw` becomes load-bearing in a new way. Today
it's "advisory for the watcher's direction check"; under this plan
it's also the trim floor. Add a comment at `dispatch_from_slot` where
`signed_intent_kw` is set, noting both consumers:

```python
# signed_intent_kw consumers:
#   1. lp/watcher.py — direction check during verify
#   2. clients/sigenergy.py — trim floor for mode-2 adaptive dispatch
#      (LP rate is the minimum we'll throttle 40032 down to even if
#      measured surplus is lower)
```

### 4. `src/optimiser/lp/runtime.py` / `lp/dispatch.py` (LPDispatch dataclass)

Verify `LPDispatch` already carries `signed_intent_kw` (it does, from
§3.3). No change.

### 5. `tests/test_sigenergy_apply_dispatch.py`

Existing `TestMode2Charge` and `TestMode2Idle` assume single-phase
writes. Update:

- **Existing mode-2 tests**: parametrise to assert the Phase-A write
  sequence (40032=max, cutoff, mode), then patch `read_state` to
  return a synthetic state, then assert the Phase-B trim write.
- **New: `TestMode2Adaptive`**:
  - `test_phase_a_writes_max_charge_then_cutoff_then_mode` — covers
    write order; previously the §3.3 invariant was "aux before mode",
    now extends to "max-charge before cutoff before mode".
  - `test_phase_b_trims_to_surplus_minus_export_cap` — patch
    `read_state` to return `pv=9, load=1`, `export_cap=5`; assert
    trim write of `(9-1) - 5 = 3 kW` (minus headroom = 2.5 kW raw 2500).
  - `test_phase_b_uses_lp_rate_floor_when_surplus_low` — patch state
    with `pv=2, load=1`, `signed_intent_kw=4` (i.e. LP wanted 4 kW
    charge); assert trim = 4 kW (LP rate dominates).
  - `test_phase_b_clamped_to_max_dc_charge` — surplus_A absurdly
    high; assert trim ≤ `max_dc_charge_kw`.
  - `test_phase_a_telemetry_failure_leaves_uncapped` — `read_state`
    returns None; assert no Phase-B write happens, `apply_lp_dispatch`
    still returns True (Phase-A state is safe).
  - `test_export_cap_zero_skips_phase_b_export_term` — covers the
    case where service passes `export_cap_kw=0` (price negative); trim
    formula still works (surplus all goes to battery).
- Use `freezegun` or `monkeypatch.setattr(asyncio, "sleep", ...)` to
  stub the 5 s probe wait; tests must stay fast.

### 6. `tests/test_service_lp.py`

The test that wires `apply_lp_dispatch` end-to-end (search for
`apply_lp_dispatch` calls) needs the new `export_cap_kw` kwarg in
mock assertions. Likely 2-3 spots.

### 7. `KNOWN-ISSUES.md`

Add an entry or extend the bug #2 entry to record:
- Symptom (battery OR export flip-flop)
- Root cause (mode 2 cascade is sequential)
- Fix (two-phase adaptive dispatch)
- Verification: `probe_two_phase.py` results + replay window

---

## Out of scope

- **Adaptive dispatch in fallback** — `set_fallback` stays single-shot
  (mode 2, 40032=max, export=DNSP). Fallback's job is "deterministic
  safe state under unknown conditions"; adding a 5 s probe to fallback
  trades determinism for marginal gain.
- **Adaptive dispatch in modes 3/5/6** — all three are CAP modes (the
  cap IS the ceiling, not a sequential cascade) so the bug doesn't
  apply. Keep `_apply_static_dispatch` simple.
- **LP formulation changes** — the LP already plans mixed slots; this
  plan is purely a dispatch-layer fix to make hardware honour them.
- **Solcast vs measured PV logging** — discussed in conversation as a
  "would be nice"; defer to a separate small PR. The probe NDJSON is
  enough to validate the formula now.
- **Multiple probe rounds per tick** — could iterate trim every 5 s as
  PV changes. Defer; one read per minute is enough given Solcast
  stability and the size of intra-tick PV swings.
- **Probe duration tuning** — 5 s is a guess based on inverter
  cascade settling in 2-3 s in earlier probes. If empirics show 3 s is
  enough, drop the constant; this plan locks in the conservative value.

---

## Verification

### 1. Probe (gates the plan)

```bash
docker compose build optimiser
docker compose stop optimiser
docker run --rm --network host \
  -v energy-optimiser_optimiser-data:/var/lib/energy-optimiser \
  -v $(pwd)/config.toml:/etc/energy-optimiser/config.toml:ro \
  energy-optimiser-optimiser python -m optimiser.probe_two_phase
docker compose start optimiser
```

Acceptance gates (all three must PASS):
- **P1 perfect trim** — split matches formula within ±0.5 kW; PV
  unchanged from Phase A.
- **P2 under-trim (+2 kW)** — same split + headroom; battery cap not
  binding, export at DNSP, no curtail.
- **P3 over-trim (−2 kW)** — battery + export = phase-A surplus − 2;
  PV drops by ~2 kW (curtail). Confirms safety: when 40032 < ideal,
  PV curtails (no weird grid behaviour).

If any sub-probe fails, **stop and re-discuss**. The cascade may
behave differently than predicted under specific conditions (e.g. SOC
near ceiling, time-of-day MPPT quirks). Probe NDJSON dump at
`/var/lib/energy-optimiser/probe_two_phase.ndjson` for offline analysis.

### 2. Unit tests

```bash
uv run pytest tests/test_sigenergy_apply_dispatch.py tests/test_service_lp.py -v
uv run pytest tests/ -q  # full suite must stay green
```

Stub `asyncio.sleep` so tests don't actually wait 5 s per case.

### 3. Replay against last 30 days

```bash
python -m optimiser.replay_cli \
  -s '/var/lib/energy-optimiser/snapshots/2026-03-*.ndjson.gz' \
  -s '/var/lib/energy-optimiser/snapshots/2026-04-*.ndjson.gz' \
  -c config.toml \
  -o /tmp/two-phase-replay.ndjson \
  -v
```

**Caveat:** replay is BLIND to the dispatch-layer behaviour. The
replay engine re-solves the LP with the same inputs; it cannot
simulate the cascade-vs-trim difference because the snapshot is
recorded post-cascade. Replay will show `delta_cents ≈ 0` for this
change. **This is expected — replay does not validate this plan.**
The validation comes from (1) the probe and (2) post-deploy
observation.

### 4. Post-deploy observation (load-bearing)

After deploy, monitor for at least 2 sunny days:

- **Tail `tick_complete` events** — log lines emitted by
  `_apply_mode2_adaptive` should show `phaseA pv=… surplus=… → trim=…`
  lines whenever LP plans a mode-2 charge slot.
- **Telemetry query** (DuckDB):
  ```sql
  SELECT
    DATE_TRUNC('hour', ts) AS hr,
    AVG(pv_power_kw) AS pv,
    AVG(battery_power_kw) AS bat,
    AVG(grid_power_kw) AS grid,
    SUM(CASE WHEN battery_power_kw > 0.5 AND grid_power_kw < -0.5 THEN 1 ELSE 0 END) AS mixed_slots,
    SUM(CASE WHEN battery_power_kw > 0.5 AND grid_power_kw > -0.5 THEN 1 ELSE 0 END) AS charge_only,
    SUM(CASE WHEN battery_power_kw < 0.5 AND grid_power_kw < -0.5 THEN 1 ELSE 0 END) AS export_only
  FROM telemetry
  WHERE ts >= NOW() - INTERVAL 7 DAY
    AND pv_power_kw > 5
  GROUP BY hr ORDER BY hr;
  ```
  Expect `mixed_slots` count to rise vs the pre-deploy baseline,
  with `charge_only` / `export_only` falling proportionally.
- **No fallback regressions** — `lp_error`, `breaker_latched`,
  `dispatch_failed` event counts should be unchanged.

### 5. Rollback

The change is contained to `apply_lp_dispatch`. Revert the commit and
redeploy if post-deploy metrics regress; no schema or data migration
to undo.

---

## Critical files reference

| Purpose | File | Line(s) |
|---|---|---|
| Add module constants | `src/optimiser/clients/sigenergy.py` | top of file (after register block) |
| Split apply_lp_dispatch on mode | `src/optimiser/clients/sigenergy.py` | 869 |
| Rename existing branch → `_apply_static_dispatch` | `src/optimiser/clients/sigenergy.py` | 936-960 |
| New `_apply_mode2_adaptive` | `src/optimiser/clients/sigenergy.py` | new method |
| Reorder set_export before apply_dispatch | `src/optimiser/service.py` | 442-445 |
| Add `signed_intent_kw` consumer comment | `src/optimiser/lp/dispatch.py` | wherever signed_intent_kw is set |
| Adaptive tests | `tests/test_sigenergy_apply_dispatch.py` | new `TestMode2Adaptive` class |
| Update apply_lp_dispatch call sites | `tests/test_service_lp.py` | grep for `apply_lp_dispatch` |

## Reused utilities (no new helpers)

- `_write_u16`, `_write_u32` — existing low-level Modbus writers.
- `read_state` — existing telemetry read; already returns `pv_power_kw`
  and `house_load_kw` (nulled if grid sensor offline).
- `MODE2_PROBE_SECONDS`, `MODE2_TRIM_FLOOR_HEADROOM_KW` — new module
  constants in `clients/sigenergy.py`.
- `dispatch.signed_intent_kw` — already populated by `dispatch_from_slot`
  for the watcher path; reused as trim floor.
- Probe: `probe_two_phase.py` — already landed; gates this plan.
