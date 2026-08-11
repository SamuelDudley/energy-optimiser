# Conserve until cancelled, and the 48h activation fix

**Date:** 2026-08-11
**Status:** approved for implementation

## Purpose

Two changes to user-strategy modes.

1. Fix the 48h duration preset. It fails on activation.
2. Let conserve mode run until the user cancels it.

## Bug: the 48h preset fails

The dashboard computes `end_at = browser_now + 48h`
(`dashboard.js:1828`). The server rejects `end_at > server_now + 48h`
(`handlers/modes.py:35`) with no tolerance. The 48h preset sits
exactly on the limit. When the browser clock runs ahead of the server
clock, the request fails with "end_at must be within 48h of now".
Shorter presets leave slack, so only 48h fails.

### Fix: accept and clamp, with a grace window

The server accepts `end_at` up to `now + 48h + 5 min`. It clamps any
accepted value above `now + 48h` down to `now + 48h`. It rejects
values beyond the grace window. Stored modes never end more than 48h
out. No client change is needed.

## Feature: conserve until cancelled

`ActiveMode.end_at` becomes `datetime | None`. `None` means the mode
runs until the user cancels it.

Rules:

- Conserve mode accepts `end_at: null` at activation.
- Buy mode rejects `end_at: null` with a 400 response. An indefinite
  conserve holds the battery back and costs only patience. An
  indefinite forced buy pulls up to 10 kW from grid until someone
  notices. Buy keeps its mandatory expiry.
- A mode with `end_at = None` never expires. Expiry pruning skips it
  at load and at runtime.
- The LP treats a `None` window as active on every horizon slot.
- Persistence round-trips `None`. An indefinite conserve survives a
  service restart.
- The snapshot record (`ActiveModeRecord.end_at`) and the replay
  reader accept `None`.
- The dashboard conserve dialog gains an "Until I turn it off"
  duration option. The active-mode card shows "until cancelled"
  instead of a countdown.

## Testing

1. Activation with `end_at` at `now + 48h + 2 min`: accepted, stored
   `end_at <= now + 48h`.
2. Activation with `end_at` at `now + 48h + 6 min`: rejected.
3. Conserve with `end_at: null`: accepted. Buy with `end_at: null`:
   rejected with a message naming buy.
4. `ModeManager.active()` far in the future keeps a `None`-expiry
   mode and prunes a dated one.
5. Persist and reload round-trips `end_at = None`.
6. `to_overrides` marks every slot in-window for a `None`-expiry
   conserve.
7. Replay reconstruction of a snapshot with `end_at: null`.

## Deploy

Rebuild path (b): Python model change. The dashboard JS is
bind-mounted and needs no rebuild.
