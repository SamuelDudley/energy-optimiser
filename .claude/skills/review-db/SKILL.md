---
name: review-db
description: Read-only sanity audit of the energy-optimiser DuckDB telemetry database. Use when the user types /review-db or asks to check data freshness, null rates, 5-minute gaps, table row counts, or load profile health. Follows the snapshot-and-query pattern because the running service holds the DB file lock.
---

# review-db

Read-only sanity check of `telemetry.duckdb`. The running service holds a file lock, so we MUST copy the DB aside inside the container before opening it (per CLAUDE.md). All queries run read-only against the copy.

## Pattern

Run the whole check in a single `docker exec` via heredoc so the copy stays inside the container and is discarded when the exec exits. Use the container's Python — it has duckdb + pytz already.

**CRITICAL: copy the WAL too.** DuckDB checkpoints infrequently (default threshold 16 MB of WAL). Between checkpoints, all recent writes live only in `telemetry.duckdb.wal` — e.g. the main file can be 10+ hours stale while the WAL holds every tick since the last checkpoint. Copying only the main file silently drops this data from the audit. Copy both, then DuckDB replays the WAL in-memory on open.

```bash
docker exec -i energy-optimiser bash -s <<'SH'
set -e
cp /var/lib/energy-optimiser/telemetry.duckdb /tmp/tele.duckdb
# WAL may not exist right after a checkpoint — ignore if absent.
cp /var/lib/energy-optimiser/telemetry.duckdb.wal /tmp/tele.duckdb.wal 2>/dev/null || true
python - <<'PY'
import duckdb
# read_only=True is fine: DuckDB replays the WAL into memory on open.
con = duckdb.connect("/tmp/tele.duckdb", read_only=True)
# ... queries below ...
PY
SH
```

Sanity-check after opening: confirm `MAX(ts)` from `telemetry` is within minutes of `now()`. If it's hours stale, the WAL copy probably failed (or wasn't replayed) — report this before running the rest of the checks, because every downstream metric will be wrong.

## Queries to run

All in one python block. Print each result with a short label so parsing is trivial.

1. **Table inventory.** For each of `telemetry`, `load_telemetry`, `pv_forecast_log`, `price_forecast_log`: row count + min(ts)/max(ts) (use `fetched_at` for price_forecast_log, `period_end` for pv_forecast_log).

2. **Staleness.** For each table, age of latest row vs `now()`. Flag:
   - `telemetry.ts` > 10 min old → FAIL (service should write every 5 min)
   - `load_telemetry.ts` > 10 min old → FAIL
   - `pv_forecast_log.fetched_at` > 2h old → WARN (Solcast is fetched infrequently)
   - `price_forecast_log.fetched_at` > 10 min old → WARN

3. **5-min gaps in telemetry (last 24h).** Expected ~288 rows/day.
   ```sql
   WITH spaced AS (
     SELECT ts, ts - LAG(ts) OVER (ORDER BY ts) AS gap
     FROM telemetry WHERE ts > now() - INTERVAL '24 hours'
   )
   SELECT COUNT(*) AS gap_count, MAX(EXTRACT(EPOCH FROM gap)) AS max_gap_s
   FROM spaced WHERE gap > INTERVAL '10 minutes'
   ```
   Report gap count and max gap. Any gap > 10 min is WARN.

4. **Null rates in telemetry (last 24h).** Key fields: `soc_pct`, `battery_kw`, `pv_kw`, `grid_kw`, `house_load_kw`, `import_price`, `export_price`, `ems_mode`, `planner_action`. Use `100.0*(COUNT(*) - COUNT(col))/NULLIF(COUNT(*),0)` — do NOT use `SUM(CASE WHEN col IS NULL THEN 1 END)`, which returns NULL (not 0) when there are no nulls, poisoning the percentage. Anything > 5% is WARN, > 20% is FAIL. Per CLAUDE.md, nulls are intentional when inputs are suspect — but sustained high null rates mean a sensor is down.

5. **Planner action distribution (last 24h).** `SELECT planner_action, COUNT(*) FROM telemetry WHERE ts > now() - INTERVAL '24h' GROUP BY 1 ORDER BY 2 DESC`. Just list. Useful signal that the LP is actually varying behaviour.

6. **EMS mode distribution (last 24h).** `SELECT ems_mode, COUNT(*) ...`. **This is `plant_ems_work_mode` (reg 30003, `EMSWorkMode` enum) — NOT `RemoteEMSControlMode` (reg 40031).** Values:
   - `0` = MAX_SELF_CONSUMPTION (plant-managed; our control is NOT engaged)
   - `1` = AI_MODE
   - `2` = TOU (time-of-use; plant-managed)
   - `5` = FULL_FEED_IN_TO_GRID
   - `6` = VPP_SCHEDULING
   - `7` = **REMOTE_EMS** — our control is engaged. Expected on >99% of rows during normal operation.
   - `9` = CUSTOM

   Heuristics: `ems_mode = 7` sustained = healthy. Any sustained value ≠ 7 means the inverter is ignoring remote EMS commands — FAIL. Isolated non-7 ticks right after startup/restart are expected (the service enables remote EMS on first tick). Do not confuse this field with the real fallback signal, which is `planner_action = self_consume` combined with `state = fallback` in the services review — those flag *our* LP falling back, independent of what the inverter is doing.

7. **Load profile coverage.** `SELECT load_id, COUNT(*), MAX(ts), MIN(ts) FROM load_telemetry GROUP BY load_id`. For each managed load, report row count and freshness of latest row.

8. **PV forecast vs actual (last 24h, where both present).**
   ```sql
   SELECT
     AVG(ABS(actual_kw - pv_estimate_kw)) AS mae_kw,
     COUNT(*) FILTER (WHERE actual_kw IS NOT NULL) AS matched_rows
   FROM pv_forecast_log
   WHERE period_end > now() - INTERVAL '24 hours'
   ```
   MAE is useful but don't judge it — just report.

9. **DB file size.** `docker exec energy-optimiser du -sh /var/lib/energy-optimiser/telemetry.duckdb`. Report and compare to expected (~1 MB/day of telemetry).

## Output format

Terse. PASS/WARN/FAIL prefix per section. Example:

```
DB REVIEW — 2026-04-22 12:45 UTC
PASS  telemetry:         12,450 rows, latest 2m ago
PASS  load_telemetry:     3,102 rows, latest 1m ago (hot_water)
PASS  pv_forecast_log:      892 rows, latest 38m ago
PASS  price_forecast_log: 5,612 rows, latest 3m ago
PASS  No gaps in last 24h
PASS  Null rates: all < 1%
Actions (24h): discharge_ess 612, self_consume 428, charge_pv 198, charge_grid 20
PASS  EMS mode (24h): 7=1258 (99.8%), 0=2 (startup blips) — remote EMS engaged
PASS  Load profile: hot_water 240 rows, latest 1m ago
PV MAE: 0.42kW over 287 matched rows
DB size: 780K
```

Under 20 lines. Only show detail for WARN/FAIL sections.

## Don't

- Don't open the live DB directly. Always copy aside first.
- Don't copy only `telemetry.duckdb` — you'll miss everything since the last checkpoint. Always copy `telemetry.duckdb.wal` alongside it.
- Don't write to the DB. `read_only=True` on every connect.
- Don't guess at schema — use the columns listed here or `DESCRIBE <table>`.
- Don't recommend fixes unless the user asks. This is an audit.
