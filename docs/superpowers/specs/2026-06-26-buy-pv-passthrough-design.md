# Keep PV generating during grid-charge (mode 4) + buy "take it all"

**Date:** 2026-06-26
**Status:** design — pending review
**Worktree/branch:** `worktree-buy-pv-passthrough`

## Problem

When the system imports grid power to charge the battery, it curtails PV
generation. Observed by the operator; the app exposes inverter modes that
keep PV generating *while* importing, so the hardware supports it.

## Evidence (grounded in code, as of `968db10`)

- **LP already plans concurrent grid + PV charge.** `bat_charge_grid` is
  bounded `[0, max_ac_charge_kw]` (10 kW) and `bat_charge_pv`
  `[0, max_dc_charge_kw]` (13 kW) as separate variables in the slot
  energy balance (`lp/formulation.py:391-396, 522`). The LP is *not* the
  source of curtailment.
- **Dispatch curtails PV.** `lp/dispatch.py:195-204`: when charging and
  `grid_to_battery > pv_to_battery + 0.05`, it emits **mode 3**
  (`COMMAND_CHARGING_GRID_FIRST`). Per the operator, mode 3 curtails PV on
  this hardware. (`SIGENERGY-MODES.md` notes mode 3 was *Not probed*.)
- **Mode 4 keeps PV on while importing — probe-verified.** 2026-04-23
  probe: cap 13 kW, PV held at 9.3 kW while grid imported 3.36 kW into the
  battery (`SIGENERGY-MODES.md` §"Mode 4"). Mode 4 = PV-first, grid tops
  up to the cap (40032). The cap is a *target*, not a ceiling.
- **Buy mode** (`lp/formulation.py:648-671, 774`): below the price ceiling
  `bat_charge_grid` is uncapped (only the 10 kW physical bound), wear is
  zeroed, and a 1e3-per-% end-of-window-SOC reward drives SOC to the
  cutoff/ceiling. Above the ceiling `bat_charge_grid[t]=0`. So buy already
  charges hard; the ceiling self-enforces at dispatch (above ceiling →
  grid charge is 0 → slot is PV-dominant/idle → mode 2, never the
  grid-charge branch).
- **Interface availability:** `dispatch_from_slot` is called at
  `service.py:1186` inside `_run_lp`, which already holds `_mode_manager`
  and computes mode overrides (`service.py:1117-1119`). Buy-active state is
  in scope to plumb through.

## Design

Two parts, both at the **dispatch** layer. No LP-formulation change.

### Part 1 — All grid-charging keeps PV on (mode 3 → mode 4)

In `dispatch_from_slot`, the grid-dominant charge branch
(`grid_to_battery > pv_to_battery + MODE_SWITCH_HYSTERESIS_KW`):

- **PV producing** (`pv_signal_kw > PV_PRODUCING_THRESHOLD_KW`, reusing the
  same `measured_pv_kw`-with-LP-fallback signal the discharge path already
  uses): emit **mode 4** (`COMMAND_CHARGING_PV_FIRST`), cap = `battery_kw`
  (the LP's planned total). PV is consumed first; grid tops up to the
  total. Mirrors the existing mode 5/6 discharge gating on live PV.
- **PV ≈ 0** (night/heavy cloud): keep **mode 3**, cap = `battery_kw`.
  Mode 3 is the documented grid-charge path and mode 4 with no PV is
  untested; nothing to keep on anyway.

### Part 2 — Buy mode "take it all"

Add `buy_active: bool = False` to `dispatch_from_slot`, plumbed from the
tick's mode overrides. In the mode-4 grid-charge branch, when
`buy_active`:

- cap = `max_ac_charge_kw + max_dc_charge_kw` (≈ 23 kW) instead of
  `battery_kw`.

Because grid is physically AC-capped at 10 kW, a 23 kW target makes the
inverter pull its full 10 kW grid **plus** all available PV every tick.
This is the PV-style "soak it all" applied to grid+PV, overriding any
intra-window rate the LP would otherwise plan. Bounded by:

- the price **ceiling** (above it the LP zeroes grid charge → not this
  branch), and
- the **SOC cutoff** auto-exit (`ModeManager.prune_soc_reached`, called
  each tick on measured SOC) plus the hardware SOC ceiling (reg 40047,
  pinned at startup).

Each tick the LP re-plans against the measured (higher) SOC — the
"take it all, re-plan next tick" model, identical in spirit to the
mode-2 PV adaptive-trim path.

### What does NOT change

- LP formulation (already models concurrent grid+PV charge).
- PV-dominant charge path (mode 2 adaptive trim).
- Discharge paths (mode 5/6).
- Fallback (mode 2 + relays off).
- Buy mode's ceiling / no-battery-export / SOC-cutoff constraints.

## Verification & error handling

- `signed_intent_kw = battery_kw` (positive) — the watcher's post-write
  direction check (reg 30037) still validates "charging".
- `verify_battery_response` (CHARGE): with buy cap ≈ 23 kW the measured
  charge (≤ ~21 kW physical) never trips the `OVER_CAP` check; for
  general mode-4 (cap = `battery_kw`) measured ≈ cap. Confirm the 1.05
  overshoot tolerance covers transient PV surge during implementation.
- `measured_pv_kw is None` (replay/tests): fall back to the LP's planned
  PV flows (same pattern as the discharge branch, `dispatch.py:214-220`).

## Hardware verification (REQUIRED before deploy)

Mode behaviour is hardware/firmware-specific and `SIGENERGY-MODES.md`
mandates re-probing. Two unproven assumptions:

1. Mode 3 curtails PV during grid-charge (operator-observed, not probed).
2. Mode 4 with a high cap pulls full grid **and** keeps full PV (probed
   only at SOC 77 %, cap 13 kW — not under a "grid-charge while PV high"
   regime).

Plan: run a probe (pattern: `probe_mode4.py`) confirming both on current
firmware; update `SIGENERGY-MODES.md` (mode 3 section + a "grid-charge
with PV" subsection + the mode-selection summary table). Probe preflight:
PV ≥ 6 kW, SOC ∈ [25, 85]; ~3 min service downtime.

## Testing

- Dispatch unit tests (`tests/`): grid-dominant + PV producing,
  `buy_active=False` → mode 4, cap = `battery_kw`; `buy_active=True` →
  mode 4, cap = `max_ac + max_dc`; PV ≈ 0 → mode 3; PV-dominant → mode 2
  (unchanged); `measured_pv_kw=None` → LP-flow fallback.
- Verification test: mode-4 charge measured response not flagged.
- Full suite green.

## Out of scope

- Changing buy mode's ceiling / cutoff / window semantics.
- LP intra-window rate optimization (deliberately bypassed in buy by
  Part 2).
- The unrelated 2026-06-25 snapshot gzip-corruption bug.

## Risks

- **Mode 3/4 firmware behaviour differs from assumption** → mitigated by
  the required probe before deploy.
- **Mode 4 grid import on PV droop** — a documented hazard for
  *export-first* use, but here grid import is the *intended* behaviour, so
  it is not a hazard for grid-charging.
