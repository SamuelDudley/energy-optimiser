# Baseload multiplier

**Date:** 2026-08-11
**Status:** approved for implementation

## Purpose

A single config knob that scales the baseload forecast the LP plans
against. Above 1.0 it nudges the planner toward holding more reserve
(guest week, cold snap, distrust of a young profile). Below 1.0 it
plans for an emptier house (vacation mode). It is a manual override:
the operator knows something about the future that the historical
profile cannot.

## Scope

**Baseload only.** The multiplier applies to the `LoadProfile` returned
by `build_load_profile()`, which is non-managed baseload by
construction (the store query subtracts measured managed-load power per
timestamp). Managed loads (hot water heat pump, future EV) are separate
LP decision variables planned exactly and are NOT scaled.

Out of scope, deferred to the conservation-mode design: dashboard
control, auto-expiry ("×0.6 until Aug 24"), per-slot or time-windowed
multipliers.

## Design

### Config

`lp_baseload_multiplier: float = 1.0` on `PlannerConfig`, read from
`[planner]` in `config.toml`.

Validation at parse time: `0.2 <= value <= 3.0`. Out-of-range fails
config load with a message naming the field and the accepted range. A
typo (`13` for `1.3`) must fail startup, not triple the forecast
silently.

### Application

In `service.py`, immediately after `build_load_profile()` returns:

- `slots = [s * m for s in profile.slots]`
- `context = f"{profile.context} x{m:g}"` when `m != 1.0`; context is
  unchanged at the identity.

The profiler is untouched — it remains pure historical truth; the
nudge is planner policy. The scaled profile is what lands in
`TickSnapshot.load_profile`, so replay, `/explain-plan`, and the
dashboard automatically see exactly what the LP saw, and the context
stamp marks nudged plans for later archaeology.

### Occupancy interplay

The multiplier applies uniformly to whichever profile context is
active, including the `away` profile that presence detection selects
when nobody is home. The mechanisms stack deliberately: occupancy
handles "phones absent now", the knob handles "we know next week is
empty". Documented in `config.example.toml`, not special-cased in
code.

## Error handling

- Out-of-range value: `ValueError` at config parse, service does not
  start (same failure shape as other `[planner]` validation).
- Missing key: defaults to 1.0, identity behaviour.

## Testing

1. Config: accepts 1.3; rejects 0.1, 5.0, and negative values with the
   named-field message; missing key defaults to 1.0.
2. Service application: slots scaled, context stamped `x1.3`.
3. Identity: at 1.0 slots and context are unchanged.
4. Managed-load exactness: with a multiplier set, the managed-load plan
   in the LP solution is unchanged versus multiplier 1.0 (pins the
   baseload-only scope).

## Deploy

First deploy is rebuild path (b): this adds a `PlannerConfig` field,
and a bind-mounted `config.toml` naming a field the running image does
not know crash-loops the service (2026-04-29 lesson).

## Validation path

`--load-profile-mult` in the simulator exercises the same lever
closed-loop (added for the load-statistic A/B); no separate sweep is
required for the knob itself since it ships at the identity default.
