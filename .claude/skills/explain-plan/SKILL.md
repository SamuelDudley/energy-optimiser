---
name: explain-plan
description: Read-only explanation of why the energy optimiser is doing what it's doing right now. Use when the user types /explain-plan or asks "why is it exporting / charging / discharging / idling / not charging / still importing" and similar "why is it doing X (especially at apparently-unfavourable prices)" questions. Grounds the answer in the current TickSnapshot — measured state + LP slot-0 plan + near-term prices — rather than guessing from the code.
---

# explain-plan

Read-only diagnostic that answers "why is the system doing X right now?" by cross-referencing measured state, the LP's slot-0 plan, and the near-term price forecast from the current `TickSnapshot`.

Never writes to the inverter, DB, or container. Produces a concise written explanation with a verdict (benign / bug-worthy) and the evidence that supports it.

## Source of truth

Two objects, fetched together. Prefer the HTTP API; fall back to docker if the API isn't reachable.

**1. The latest `TickSnapshot` (mandatory) — `/plan/current`:**
```bash
curl -s -H "Authorization: Bearer $EO_API_TOKEN" http://localhost:8090/plan/current > /tmp/plan.json
```

**2. The live 5-min price row covering "now" (recommended) — `/price_forecast_log`:**
```bash
SINCE=$(date -u -d '60 minutes ago' +%Y-%m-%dT%H:%M:%SZ)   # Z form; '+00:00' would URL-decode to a space
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
curl -s -H "Authorization: Bearer $EO_API_TOKEN" \
  "http://localhost:8090/price_forecast_log?since=${SINCE}&limit=1000" \
  | jq --arg now "$NOW" '
      [.rows[]
        | select(.resolution == 5
            and .interval_start <= $now
            and .interval_end   >  $now)]
      | sort_by(.fetched_at) | last
      | {interval_start, interval_end,
         import_per_kwh: .per_kwh, export_per_kwh,
         fetched_at, interval_type, is_locked}
    ' > /tmp/price5.json
```

Notes:
- The endpoint returns `{table, count, rows}`, not a bare array — always `.rows[]`.
- Use `Z` (UTC) for timestamps. `+00:00` survives `--iso-8601=seconds` but the literal `+` is URL-decoded to a space and the API rejects it.
- The Amber 5-min poll cadence isn't every minute — the 5-min table only repopulates when Amber publishes a new tick. A 60-min window is wide enough to always find the row covering "now"; a 10-min window may miss it.
- Filter on `interval_start ≤ now < interval_end`, not "last fetched" — the most recent fetch usually returns a forecast row 20+ min in the future, which is not what you want.

Why both are needed: `TickSnapshot.price_forecast` is the **merged price array the LP solved against** — `service.py:366` builds it as `list(prices_5min) + list(prices_30min)`, so 5-min entries (current + ~30 min ahead) come first, then 30-min entries cover the rest of the horizon. `_price_at`'s linear scan finds the 5-min entry first within its coverage window, then falls through to 30-min. So the snapshot already carries 5-min granularity for the near term — but only as far ahead as Amber's 5-min API returned this tick. The standalone `/price_forecast_log` query is still worth running because: (a) it exposes `interval_type` and `is_locked`, which aren't on `PriceInterval`; (b) it's a sanity check that the 5-min data actually made it into the snapshot; (c) if the snapshot's first interval is 30-min wide, the 5-min array was empty this tick (Amber polling glitch, fallback) — that's a finding worth flagging.

`EO_API_TOKEN` is in the deployment `.env` (see `docker-compose.yml`). Read from `/home/dudley/code/energy-optimiser/.env` if not in the user's shell.

**Fallback — docker** (gives you the snapshot only; 5-min prices live in DuckDB which the running service has locked, so don't try to read them via docker):
```bash
docker exec energy-optimiser bash -c \
  "zcat /var/lib/energy-optimiser/snapshots/$(date -u +%Y-%m-%d).ndjson.gz | tail -1" > /tmp/plan.json
```

If both fail, stop and report: the service is down or the API is misconfigured — this skill cannot run without a snapshot.

## Fields to extract

Always pull these first and print them in the explanation (even if one isn't directly relevant — it's cheap context for the reader).

```bash
jq '{
  ts: .timestamp,
  measured: {
    pv_kw:       .system_state.pv_power_kw,
    battery_kw:  .system_state.battery_power_kw,
    grid_kw:     .system_state.grid_power_kw,
    house_kw:    .system_state.house_load_kw,
    soc_pct:     .system_state.soc_pct
  },
  slot0: .lp_solution.slot_0,
  dispatch: .lp_dispatch,
  output: .output,
  active_modes: .active_modes,
  # Slice the 5-min head of the merged price_forecast. Spacing tells you
  # whether the 5-min array was populated this tick.
  price_head: [.price_forecast[0:9][] | {
    start, end,
    import_per_kwh,
    export_per_kwh,
    is_locked,
    span_min: (((.end | sub("\\+00:00$"; "Z") | fromdateiso8601) -
                (.start | sub("\\+00:00$"; "Z") | fromdateiso8601)) / 60)
  }],
  price_tail_first_30min: ([.price_forecast[]
                             | select((((.end | sub("\\+00:00$"; "Z") | fromdateiso8601) -
                                        (.start | sub("\\+00:00$"; "Z") | fromdateiso8601)) / 60) >= 30)
                            ] | first)
}' /tmp/plan.json

# Live 5-min price (what the Amber app shows; what the stale-price guard uses)
jq '{interval_start, interval_end, import_per_kwh, export_per_kwh, fetched_at, interval_type, is_locked}' /tmp/price5.json
```

Show in the evidence:
- The **slot-0 covering price** (snapshot `price_forecast[0]`) — this is what the LP's slot-0 export-revenue term used.
- The **live 5-min row** from `/price_forecast_log` — this is what the Amber UI is rendering and what the stale-price guard checks.
- The first **30-min entry** in the snapshot's `price_forecast` — useful when the user is asking about behaviour out beyond the 5-min coverage window (e.g. "why is it planning to discharge at 6pm?").

Reading the merged array:
- Entries with `span_min == 5` are 5-min slots (typically the first 6–9 entries, covering current + ~30 min ahead).
- Entries with `span_min == 30` are 30-min slots covering the rest of the horizon.
- If `price_head[0].span_min == 30`, the 5-min array was empty this tick — flag it. The LP is operating with coarser-than-usual price granularity; this can happen during Amber polling glitches or Amber-side outages.
- A sign flip between the 5-min slot covering "now" and the 30-min slot that contains it is normal near a price boundary. Quote both and note the direction.

Sign conventions (from `types.py` / client):
- `battery_kw > 0` = charging, `< 0` = discharging
- `grid_kw > 0` = importing from grid, `< 0` = exporting to grid
- `price.export_per_kwh` is positive = revenue to customer, negative = you pay to export. Same convention applies to the 5-min row from `/price_forecast_log`.

Sanity-check the snapshot timestamp: if `ts` is more than ~90s old, the service may have stopped ticking and this skill is reporting stale data. Flag and run `/review-services` instead.

Sanity-check the 5-min row's `fetched_at`: if it's older than ~6 minutes, the stale-price guard may be active (`EXPORT_BLOCKED_STALE_PRICE` event), which forces the export cap to 0 regardless of the LP's plan — that alone can explain "why isn't it exporting?".

## Reasoning knowledge

These are the facts the explanation must draw on. Every time you cite one, also cite the code it's enforced in — it gives the user a trail.

### 1. Costs in the LP objective (`lp/formulation.py`, `lp/constants.py`)

| Term                       | Sign / magnitude                                  | Constant / source                                    |
|----------------------------|---------------------------------------------------|------------------------------------------------------|
| Grid import                | `+grid_import * import_price`                     | `import_price = forecast_predicted or import_per_kwh`|
| Grid export revenue        | `−grid_export * export_per_kwh` (revenue)         | `price.export_per_kwh` from the merged `prices_planning` (5-min for the head, 30-min for the tail; sign flipped at Amber client) |
| Battery throughput wear    | `+(charge + discharge) * 2.5 c/kWh`               | `WEAR_COST_PER_KWH`                                  |
| PV curtail penalty         | `+pv_curtailed * 1.0 c/kWh`                       | `PV_CURTAIL_PENALTY_PER_KWH`                         |
| SOC out-of-band penalty    | `+(over + under + terminal) * 1e4`                | `SOC_BOUND_PENALTY` (regulariser, not real money; solver subtracts it from reported cost) |

Break-even guide (1 kWh round-trip, 90% RTE):
`required_spread ≈ (2 × wear + import × (1 − 0.9)) / 0.9`. At wear=2.5 and 20¢ import, break-even spread ≈ 7.8 ¢/kWh.

### 2. Why export can happen at apparently-zero export price

Two distinct, legitimate reasons before calling it a bug:

- **Curtail penalty > export revenue at 0¢.** Throwing 1 kWh of PV away costs 1¢; exporting it at 0¢ costs nothing. LP picks export. This is the `PV_CURTAIL_PENALTY_PER_KWH` mechanism, deliberate (see the constant's docstring in `lp/constants.py`).
- **"0¢" in the UI is usually rounded.** The real `export_per_kwh` in the snapshot often sits at e.g. `0.0162` — strictly positive. Report the precise value from the snapshot; do not repeat the UI's rounding.

What it is **not**: the LP will not discharge the battery to export at 0¢. Battery throughput carries 2.5 ¢/kWh of wear, so that would be loss-making. If `slot0.grid_export_kw > slot0.pv_to_export_kw` — i.e. the battery is contributing to export — at a non-negative export price, **that** is bug-worthy and should be escalated, not explained away.

### 3. Export cap semantics (`lp/solver.py::_extract_solution`)

`output.grid_export_limit_kw` is written to register 40038 and is a **ceiling, not a setpoint**. Rule: if *any* stochastic scenario's `grid_export[0]` is ≥ `NUMERIC_EPS`, the cap opens fully to `battery_config.export_limit_kw` (typically 5.0). Otherwise it's pinned to 0. So seeing `grid_export_limit_kw = 5.0` does not mean "the LP wants to export 5 kW" — it means "at least one scenario plans some export, so don't throttle the MPPT".

### 4. Stale-price guard (`service._resolve_export_limit_kw`)

If the cached 5-min Amber price is older than `EXPORT_PRICE_STALE_THRESHOLD`, the LP's intended export cap is forced to 0 regardless of what the LP planned. If the user is asking "why isn't it exporting when I can see PV surplus?", check logs for `EXPORT_BLOCKED_STALE_PRICE`.

### 5. Dispatch mapping (`lp/dispatch.py::dispatch_from_slot`)

Interpret `lp_dispatch.mode` together with `slot0.battery_kw` and `slot0.grid_to_battery_kw`:

| `battery_kw`            | Extra condition                                | Mode (RemoteEMSControlMode) | Cap (40032/40034)          |
|-------------------------|------------------------------------------------|-----------------------------|----------------------------|
| \|·\| < 0.1 (deadband)  | —                                              | 2 (MAX_SELF_CONSUMPTION)    | 0 (idle)                   |
| > 0                     | `grid_to_battery > pv_to_battery`              | 3 (COMMAND_CHARGING_GRID_FIRST) | LP charge rate          |
| > 0                     | else (PV-dominant)                             | 2 (MAX_SELF_CONSUMPTION)    | LP charge rate (adaptive)  |
| < 0                     | measured PV > 0.2 kW                           | 5 (COMMAND_DISCHARGING_PV_FIRST) | `max_discharge_kw`     |
| < 0                     | measured PV ≤ 0.2 kW                           | 6 (COMMAND_DISCHARGING_ESS_FIRST) | `max_discharge_kw`    |

**Mode 6 zeroes PV** (verified on hardware) — if you see mode 6 with visible sun, that's why the PV reading dropped. Note also: mode 2 PV-dominant charge uses adaptive trim on reg 40032 (see `clients/sigenergy.py::_apply_mode2_adaptive_charge`), so measured charge rate may legitimately exceed `cap_kw`.

Discharge cap = physical max on purpose, so load transients stay on battery. Don't confuse `dispatch.cap_kw` on discharge (= `max_discharge_kw`) with the LP's signed intent (kept on `signed_intent_kw`).

### 6. User-invoked strategy modes (`active_modes` on the snapshot)

The user can temporarily impose hard constraints on the LP via `/modes/buy` or `/modes/conserve` (see `optimiser/modes.py`). When active, each entry has `kind` (`"buy"` or `"conserve"`), `end_at` (UTC ISO timestamp), and a `params` dict. The active set at solve time is recorded on the snapshot as `active_modes` — `[]` when none are active.

- **`buy`** — sets an import-price ceiling: while active, the LP may charge the battery from grid only when `import_per_kwh ≤ params.ceiling_c_per_kwh`, and battery export is hard-blocked. Use case: user knows a cheap window is coming and wants to overrule wear-cost gating.
- **`conserve`** — blocks battery export below a SOC floor: while active, the LP cannot discharge the battery below `params.floor_pct`. Use case: user expects an outage / hot evening and wants reserve held back.

If `active_modes` is non-empty, **surface a one-line summary per mode at the top of the Evidence section** before the usual measured/plan/price block — e.g. "Buy mode active until 15:30 UTC, ceiling 12 c/kWh" or "Conserve mode active until 19:00 UTC, floor 60%". These are user-imposed overrides, not LP heuristics — if the LP's slot-0 choice looks counter-intuitive given prices, an active mode is the first thing to point at ("LP would normally export here, but conserve mode blocks battery → export below 60% SOC").

### 7. Time zones

All snapshot/price timestamps are UTC. If the user refers to a local-clock event ("at 10am"), convert using `time_utils`. Canberra is UTC+10 (AEST) in winter, UTC+11 (AEDT) in summer; being an hour off here can make an explanation wrong. 30-min NEM intervals align to `:00` and `:30` wall-clock in UTC.

## How to answer

Work through this checklist, out loud if it helps, but keep the final written answer tight.

1. **Are any user-invoked modes active?** Check `active_modes`. If non-empty, lead the explanation with them — they are hard constraints overriding the LP's normal cost optimisation, and a counterintuitive slot-0 choice may be entirely explained by an active mode (see section 6).
2. **What is the system actually doing?** Read `measured.battery_kw` and `measured.grid_kw`. Describe in one sentence ("battery is charging at 3.5 kW, grid is exporting 4.3 kW").
3. **What did the LP plan for this slot?** Read `slot0.battery_kw`, `slot0.grid_export_kw`, `slot0.pv_to_export_kw`, `slot0.pv_to_battery_kw`, `slot0.pv_to_house_kw`. Do the measured values roughly match the plan? If not, call out the deviation.
4. **What is the LP optimising against right now?** List the next 2–4 intervals of `import_per_kwh` and `export_per_kwh`. Look for a flip (positive → negative or vice versa) in the near future that motivates the current action.
5. **Which cost term dominates the slot-0 decision?** Cross-reference section 2 above. Typical shapes:
   - Export > 0 while export price ≈ 0 and `pv_to_export == grid_export` → curtail-penalty mechanism, benign.
   - Battery charging from grid → `grid_to_battery > 0`, import price currently low, discharge planned at a later expensive slot (check `forward_trajectory` if you need to prove the arbitrage).
   - Battery idling (deadband) while export cap = 5 kW → mode 2 cascade routing surplus PV to export; battery is preserving wear.
   - Mode 6 + no PV reading → expected (mode 6 zeroes PV).
6. **Is it bug-worthy?** Apply the red-flag checklist in the next section. If any trigger, say so and recommend `/review-services` or a deeper look.

## Red flags (escalate, don't just explain)

- **Battery discharging into export at non-negative export price**: `slot0.grid_export_kw > slot0.pv_to_export_kw + 0.05` while `price.export_per_kwh ≥ 0`. Wear cost is 2.5 ¢/kWh one-way; no non-negative export price covers it.
- **Snapshot timestamp stale > 90 s**: service may not be ticking. Run `/review-services`.
- **`output.reason` starts with `fallback:` or `lp fallback:`**: circuit breaker latched, LP is not driving. Say so up front.
- **Measured vs plan diverges heavily** (> 1 kW on battery, > 2 kW on grid) with no obvious physical cause (load transient, PV cloud transition): flag as possibly a modbus/control layer issue.
- **`EXPORT_BLOCKED_STALE_PRICE` events in recent logs**: grep `docker logs --since 15m energy-optimiser 2>&1 | grep -i stale` if export was expected but didn't happen.

## Output format

Two short sections, no headings deeper than `##`. Target under 30 lines of text (tables are fine).

```
## Verdict

<one-line judgment: benign / expected-given-prices / bug-worthy-because-X>

## Evidence

- Measured: pv=..., battery=..., grid=..., soc=..., house=...
- LP plan (slot 0): battery_kw=..., grid_export=..., pv_to_export=...
- Export price now: X.XXX ¢/kWh (UI may round to 0)
- Next intervals: ... (show the flip if one is coming)
- Cost term in play: <curtail penalty | arbitrage | wear-cost avoidance | stale-price guard | ...>
- Code reference: <file:line>
```

If bug-worthy, add a third section `## Recommended next step` with one concrete action (e.g. "run /review-services", "grep logs for X", "file a bug — battery→export at non-negative price is never expected").

## Don't

- Don't write to the inverter, DB, or filesystem outside `/tmp`.
- Don't trust the UI's rounded prices — always quote the precise `export_per_kwh` / `import_per_kwh` from the snapshot.
- Don't explain a bug-worthy case away. If battery is discharging to export at non-negative price, say so.
- Don't pull historical snapshots unless the user explicitly asks about a past event. This skill answers "why now".
- Don't run a replay — that's `/review-replay`.
- Don't recommend fixes unless asked. Lead with explanation; offer next steps only if the verdict is bug-worthy.
