# User strategy modes — design

**Date:** 2026-05-19
**Status:** Draft for review
**Related:** `lp/formulation.py`, `service.py`, `api/handlers/dashboard.py`, KNOWN-ISSUES #24

## Problem

The LP optimises against price + PV forecasts. When those forecasts are wrong, the user has visibility the LP doesn't (sky, BOM nowcast, planned high-load event) and needs to **inject information** rather than wait for the next Solcast refresh to correct it.

Concrete case that motivated this work (2026-05-19): overnight, the LP discharged the battery against an optimistic morning PV forecast. Reality: zero solar today. Evening load (resistance heater + cooktop) is now exposed to peak import. The user needs to **manually buy energy** this morning at cheap rates to ride the peak.

The existing Sigenergy app has an override that charges blindly through 5-min price spikes. That's the lower bar this design must clear: **smart override that skips spikes without micro-management**.

A symmetric situation exists for export: an inbound event (storm forecast, planned high evening load tonight) where the user wants to **stop selling cheap** to preserve stored energy for the event.

## Design summary

Two user-invoked modes, both expressed as **time-bounded LP-side constraints**:

| Mode | User specifies | LP behaviour during window |
|---|---|---|
| **Buy** | end-time `T`, max import price `ceiling` (c/kWh) | Battery cannot import-charge at slots where `ip > ceiling`. Wear cost on import-charge set to 0 during window. Battery cannot export. |
| **Conserve** | end-time `T`, min export price `floor` (c/kWh) | Battery cannot export at slots where `ep < floor`. PV→grid behaviour unchanged (existing `export_cap` still applies). |

Both modes are **additive constraints**. The LP keeps doing its normal cost-min job inside the constraint set. No parallel control path, no new dispatch logic — the existing `dispatch_from_slot` reads `slot_0` as today.

They **compose cleanly**: buy mode forbids `bat_charge_grid` above the ceiling; conserve mode forbids battery contribution to `grid_export` below the floor; neither touches the other's variables.

## Why this clears the Sigenergy-app bar

The Sigenergy override pays whatever the next 5 min costs. Approach A:

1. **Hard refuses** any slot above the ceiling — that's the spike skip.
2. **Below the ceiling**, the LP's existing cost-minimisation picks the cheapest slots first. A 9c slot will always be picked before a 13c one. A 3c spike inside a band of mostly-cheap slots is dominated and never selected for a typical fill.
3. With 10 kW AC charge rate, **a single missed 5-min slot ≈ 2.5% SOC**. Misses are cheap; the user can afford a picky ceiling.

Net: the user sets two knobs once and walks away.

## LP formulation changes

Located in `lp/formulation.py::build_stochastic_lp` (and via it, `_add_scenario_to_problem`).

A new optional argument `mode_overrides: ModeOverrides | None` flows in via `solve_stochastic(...)`. It carries the active modes' parameters and the slot grid is checked against each window.

### Buy mode constraints, per scenario, per slot `t` in `[buy.start, buy.end]`

```
# Hard ceiling on import-charge:
if ip[t] > buy.ceiling:
    bat_charge_grid[t] == 0    # explicit equality constraint

# No battery export during the window (preserve what was bought):
#   bat-to-export = grid_export[t] - pv_to_export[t]
grid_export[t] <= pv_to_export[t]

# Wear cost discount: the wear penalty on bat_charge_grid[t] (the
# one-way charge component of round-trip wear) is zeroed in the
# objective for in-window slots. Moot above the ceiling because
# bat_charge_grid[t] is already pinned to 0 there. The discharge-
# side wear term is unchanged.
```

PV-sourced export (`pv_to_export`) is **not** blocked — that's controlled by the existing `export_cap` and remains free to flow.

### Conserve mode constraints, per scenario, per slot `t` in `[conserve.start, conserve.end]`

```
# No battery contribution to export at sub-floor prices:
if ep[t] < conserve.floor:
    grid_export[t] <= pv_to_export[t]
```

That single constraint is all conserve mode needs. Charging behaviour is unaffected.

### Non-anticipativity

Both modes' constraints apply to slot 0 if the current time falls inside an active window. Slot 0 is tied across scenarios by the existing non-anticipativity machinery, so per-scenario constraint identity is preserved.

## Activation surface — dashboard

A new "Modes" section on the dashboard renders two cards:

```
┌─ Buy mode ──────────────────────┐  ┌─ Conserve mode ─────────────────┐
│ Inactive                        │  │ Inactive                        │
│ [ Activate ]                    │  │ [ Activate ]                    │
└─────────────────────────────────┘  └─────────────────────────────────┘
```

When active:

```
┌─ Buy mode — active ─────────────┐
│ Ends in 1h 23m (15:30)          │
│ Ceiling: ≤ 12.0 c/kWh           │
│ Activated 14:07 via dashboard   │
│ [ Cancel ]                      │
└─────────────────────────────────┘
```

"Activate" opens a panel asking for:
- Duration preset (15 min / 30 min / 1 h / 2 h / 4 h) or custom end-time, with a max of `now + 24h`
- Threshold (ceiling for buy, floor for conserve). Last-used value pre-filled.

## API surface

New module `api/handlers/modes.py`. Endpoints:

| Method + path | Body | Behaviour |
|---|---|---|
| `GET /modes` | — | Returns active modes with parameters + remaining time |
| `POST /modes/buy` | `{end_at, ceiling_c_per_kwh}` | Activate buy (replaces any existing buy mode) |
| `POST /modes/conserve` | `{end_at, floor_c_per_kwh}` | Activate conserve (replaces any existing conserve mode) |
| `DELETE /modes/buy` | — | Cancel buy mode early |
| `DELETE /modes/conserve` | — | Cancel conserve mode early |

Validation on POST:
- `end_at` must be ISO-8601 UTC, strictly in the future, ≤ `now + 24h`.
- Threshold must be in `[0, 100]` c/kWh.
- Reject 400 with structured error on invalid input.

The current set is also folded into `GET /dashboard/config` so the dashboard front-end can paint mode state on its existing config-poll path.

## Runtime representation

New module `src/optimiser/modes.py`:

- `ActiveMode` frozen dataclass: `kind: Literal["buy", "conserve"]`, `end_at: datetime` (UTC), `params: dict`, `activated_at: datetime`, `source: str`.
- `ModeManager` holds the active set in memory, persists to a JSON file, exposes:
  - `activate(kind, end_at, params, source) -> ActiveMode`
  - `cancel(kind) -> None`
  - `active() -> list[ActiveMode]`  (drops expired entries lazily)
  - `to_overrides(slots: list[datetime]) -> ModeOverrides`  (slot-aligned view consumed by the LP)
- `ModeOverrides` frozen dataclass passed into `solve_stochastic` — pre-computes per-slot booleans (`buy_active_at[t]`, `conserve_active_at[t]`) + the ceiling/floor values so the LP loop has cheap per-slot lookups.

`Service` owns one `ModeManager`. Each tick, `_run_lp` calls `mode_manager.to_overrides(slot_grid)` and forwards it to `solve_stochastic`.

## Persistence

Single JSON file at `<state_dir>/active_modes.json` (next to the existing heartbeat path). Written synchronously on every state change. On `Service.start()`, load; drop any entry whose `end_at` is in the past; emit `MODE_EXPIRED` for those.

JSON over DuckDB because: ~2 rows, easy to inspect with `cat`, no schema migrations, no lock contention with the running service.

## Composition and edge cases

- **Both modes active simultaneously:** allowed; constraints are additive (no semantic conflict — buy constrains `bat_charge_grid` and bat-export, conserve constrains bat-export at a different threshold; for overlapping slots, the tighter export rule wins implicitly).
- **Same mode re-activated while active:** the POST replaces the existing entry (no stacking). Frontend should warn before overwrite.
- **Window expiry during a tick:** `to_overrides` is computed at tick start; a window expiring mid-tick is honoured next tick. Emit `MODE_EXPIRED` event from `ModeManager.active()` when it drops the entry.
- **Service restart during active window:** state restored from JSON; modes resume; LP picks them up on first tick.
- **Fallback / circuit breaker:** when the LP is bypassed (fallback active, paranoid writes in force), modes are inert by construction — no LP solve means no constraint application. This is the correct behaviour: safety overrides user intent.
- **Buy + zero in-window slots below ceiling:** LP charges nothing. That's the user's stated intent (cap = pain threshold). Dashboard should surface this ("0 slots under ceiling in your window") on activate.
- **Conserve + battery already at floor SOC:** no-op for charge-side; conserve only constrains discharge-to-grid.

## Observability

- New `EventType.MODE_ACTIVATED` `{kind, params, source, activated_by}`
- New `EventType.MODE_EXPIRED` `{kind, reason}` — reason ∈ `{"window_ended", "user_cancelled", "service_started_after_end_at"}`
- `TickSnapshot.active_modes: list[ActiveMode]` so `/explain-plan` and replay can see what constraints were in force.
- `/explain-plan` extended to print a one-line summary when modes are active ("Buy mode active until 15:30, ceiling 12c").
- Replay (`replay_cli.py`) accepts `--respect-modes` (default on) so historical re-solves match what the LP actually saw.

## Testing

Unit tests in `tests/test_modes.py` + `tests/test_lp_modes.py`:

1. `ModeOverrides` slot-alignment — a mode active 13:00–14:00 yields `buy_active_at[t]=True` only for slots in that range.
2. LP with buy mode + a synthetic price strip (`[5, 5, 15, 5]`, ceiling=10): `bat_charge_grid[2] == 0`, others > 0.
3. LP with conserve mode + a synthetic export-price strip (`[20, 20, 5, 20]`, floor=15): `grid_export[2] <= pv_to_export[2]`, others can exceed.
4. Composition: both modes active, overlapping window, both constraints honoured.
5. Persistence round-trip: write, restart `ModeManager`, verify reload + auto-drop of expired entries.
6. API validation: past `end_at` rejected; out-of-range threshold rejected; replacing existing mode succeeds.

Integration: existing replay machinery (`replay_cli`) re-solves a historical day with synthetic modes injected — confirms LP output differs in expected directions.

## Out of scope

- **Preset bundles** (Approach C — "eager / smart / patient" buttons). Defer until Approach A has been used in anger and we know what tunings are common.
- **Approach B target-SOC** (window + ceiling + target). The fast charge rate makes the implicit "fill until window ends or battery full" target-of-A sufficient. Revisit if a real situation needs explicit target control.
- **Scheduled/recurring modes** ("every Sunday conserve 6am–6pm"). One-off only.
- **Auto-trigger from external signals** (BOM storm forecast → auto-conserve). Manual only.
- **Systemic forecast-bust hedge** (e.g. Solcast P10/P50/P90 stochastic axis, auto-floor-lift when tomorrow's confidence is low). Separate work; tracked via the `forecast-bust-hedge` memory placeholder. Modes are the band-aid; this would be the fix.
- **Blocking PV→grid export during buy mode.** Existing `export_cap` already curtails at negative prices; blocking solar export wastes generation.
- **CLI / shell access for activation.** Dashboard is sufficient for the urgency-driven use case.

## Open questions for review

1. **Pre-fill defaults on the activate panel:** last-used value, or sensible-default-from-current-prices (e.g. ceiling = median in-window price + 2c)? Last-used is simpler; suggesting based on current prices is more helpful for first-time use.
2. **Max window length cap (24h):** is that long enough? Could imagine a 48h conserve mode for a multi-day storm front.
3. **Should buy mode forbid battery → house discharge during its window?** Currently the design lets the battery serve house load as normal — bought energy is preserved against export, but house pulls naturally. Alternative: forbid all battery discharge during buy window so SOC monotonically rises. Default position: no extra restriction; let the LP arbitrage normally.
4. **Conserve floor at `0` c/kWh:** does that mean "any positive export price"? The constraint is `grid_export ≤ pv_to_export if ep < floor`. With floor=0, only negative-ep slots are constrained, which is what `export_cap` already handles. A floor of 0 is therefore effectively a no-op — should the API reject it, or accept it as a documented no-op?

## Implementation order (rough)

1. `modes.py` (data classes, `ModeManager`, persistence) + tests
2. `lp/formulation.py` — accept `ModeOverrides`, add per-slot constraints + zero-out wear on `bat_charge_grid` inside buy windows
3. `solve_stochastic` plumb-through; `service.py::_run_lp` wires it
4. API handlers + validation
5. Dashboard frontend (cards + activate panel)
6. Observability (events + snapshot field + explain-plan extension)
7. Replay flag

Each step ships with tests. No big-bang merge.
