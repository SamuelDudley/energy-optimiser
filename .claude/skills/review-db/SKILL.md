---
name: review-db
description: Read-only sanity audit of the energy-optimiser DuckDB telemetry database. Use when the user types /review-db or asks to check data freshness, null rates, 5-minute gaps, table row counts, or load profile health. Follows the snapshot-and-query pattern because the running service holds the DB file lock.
---

# review-db

Read-only sanity check of `telemetry.duckdb`. The running service holds a file lock, so we MUST copy the DB aside inside the container before opening it (per CLAUDE.md). All queries run read-only against the copy.

## Pattern

Run the whole check in a single `docker exec` via heredoc so the copy stays inside the container and is discarded when the exec exits. Use the container's Python — it has duckdb + pytz already.

```bash
docker exec -i energy-optimiser bash -s <<'SH'
set -e
cp /var/lib/energy-optimiser/telemetry.duckdb /tmp/tele.duckdb
python - <<'PY'
import duckdb
con = duckdb.connect("/tmp/tele.duckdb", read_only=True)
# ... queries below ...
PY
SH
```

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

6. **EMS mode distribution (last 24h).** `SELECT ems_mode, COUNT(*) ...`. Mode 2 = SELF_CONSUME (fallback). If mode 2 > 10% and no known fallback incident, it's a concern.

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
Actions (24h): discharge_ess 612, charge_pv 198, idle 150, charge_grid 20
EMS modes (24h): 3=612, 4=198, 6=150, 2=20 (10% fallback — see services review)
PASS  Load profile: hot_water 240 rows, latest 1m ago
PV MAE: 0.42kW over 287 matched rows
DB size: 780K
```

Under 20 lines. Only show detail for WARN/FAIL sections.

## Don't

- Don't open the live DB directly. Always copy aside first.
- Don't write to the DB. `read_only=True` on every connect.
- Don't guess at schema — use the columns listed here or `DESCRIBE <table>`.
- Don't recommend fixes unless the user asks. This is an audit.
