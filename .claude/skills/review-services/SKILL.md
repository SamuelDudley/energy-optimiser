---
name: review-services
description: Read-only health check of the energy-optimiser main container and watchdog sidecar running locally via Docker. Use when the user types /review-services or asks for a service health audit, state machine / circuit breaker / fallback status, or a recent-incidents summary.
---

# review-services

Read-only audit of both Docker containers on this host. Never restart anything, never write to the DB, never touch Modbus. Output a punch list — done vs. issues.

## What to check

Run these in parallel where possible, then summarise.

1. **Containers up.** `docker ps --format '{{.Names}}\t{{.Status}}\t{{.RestartCount}}' | grep energy-optimiser`. Both `energy-optimiser` and `energy-optimiser-watchdog` must be `Up`. Flag any restarts in the last 24h (`docker inspect <name> --format '{{.State.StartedAt}} {{.RestartCount}}'`).

2. **Heartbeat freshness.** `docker exec energy-optimiser stat -c '%Y' /var/lib/energy-optimiser/heartbeat` → compare to `date +%s`. Age > 90s means the watchdog should have fired fallback. Report age in seconds.

3. **Watchdog incidents (last 24h).** `docker logs --since 24h energy-optimiser-watchdog 2>&1`. Count:
   - `firing fallback` events
   - `fallback write raised` errors
   - `re-arming` (recovery) events
   If firings > 0, report timestamps and whether recovery followed each one.

4. **Main service tick health.** `docker logs --since 1h energy-optimiser 2>&1 | grep -c tick_complete`. Expect ~60 ticks/hour. Flag anything under 55 or over 65 as anomalous.

5. **State machine transitions (last 24h).** `docker logs --since 24h energy-optimiser 2>&1 | grep -E 'state_transition|DEGRADED|FALLBACK'`. List transitions with timestamps. Anything other than ACTIVE for >5 min sustained is a concern.

6. **LP solve distribution (last 1h).** Parse `lp_solve_complete` events from `docker logs --since 1h energy-optimiser`. Report counts by status (optimal / infeasible / timeout / error) and p50/p95 solve_ms. Stochastic solve budget is 10s — flag p95 > 5000ms.

7. **Circuit breaker latches (last 24h).** `docker logs --since 24h energy-optimiser 2>&1 | grep -E 'circuit_breaker|breaker_latch'`. Any latch means the LP has been falling back — report count and timestamps.

8. **Modbus write errors (last 24h).** `docker logs --since 24h energy-optimiser 2>&1 | grep -iE 'modbus.*error|write.*fail|Connection'`. Report count; sample up to 3 distinct messages.

9. **Last action + price.** Most recent `tick_complete`: report `soc`, `action`, `price_ckwh`, `state`, `reason`. `docker logs --tail 200 energy-optimiser 2>&1 | grep tick_complete | tail -1`.

## Output format

Terse punch list. Lead with PASS/WARN/FAIL per section. Example:

```
SERVICES REVIEW — 2026-04-22 12:45 UTC
PASS  Containers: both up (optimiser 8m, watchdog 8m, 0 restarts)
PASS  Heartbeat: 0.3s old
WARN  Watchdog: 1 fallback fire at 12:03:31 (recovered 30s later, stale heartbeat during cold start)
PASS  Tick rate: 60/hr
PASS  State: ACTIVE throughout
PASS  LP: 60/60 optimal, p50=250ms, p95=380ms
PASS  Circuit breaker: no latches
PASS  Modbus: no errors
Last tick: soc=53.2%, discharge_ess, 22.4c/kWh, state=active
```

Keep it under 20 lines. Don't dump log excerpts unless something is WARN/FAIL.

## Don't

- Don't restart containers or modify anything.
- Don't open the DuckDB file (that's `/review-db`).
- Don't run a replay solve (that's `/review-replay`).
- Don't read live config unless needed to interpret a check.
