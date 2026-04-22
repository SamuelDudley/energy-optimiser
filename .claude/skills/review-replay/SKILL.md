---
name: review-replay
description: Read-only audit of NDJSON tick snapshots PLUS a full replay solve against the current config. Use when the user types /review-replay or asks to audit snapshot coverage, check replay health, or backtest the current LP config. Always runs the solve (user preference).
---

# review-replay

Two-part check: (1) snapshot inventory + gap detection, (2) a real replay solve against recent snapshots using the current `config.toml`. Read-only — replay only reads snapshots and the config, never touches DuckDB or the live service.

## Part 1: Snapshot inventory

```bash
docker exec energy-optimiser sh -c '
  ls -la /var/lib/energy-optimiser/snapshots/
  echo ---COUNTS---
  for f in /var/lib/energy-optimiser/snapshots/*.ndjson.gz; do
    date=$(basename "$f" .ndjson.gz)
    lines=$(zcat "$f" | wc -l)
    echo "$date $lines"
  done
'
```

Report:
- File count, total size, oldest/newest date
- Any missing calendar days in the last 30 (gap detection)
- Per-day tick counts. Expected ~1440/day (once per minute). Anything < 1400 is WARN — indicates the service was down or the snapshot writer failed.

## Part 2: Always-solve replay

Run `replay_cli` against the last 7 days of snapshots using the current live config (`/etc/energy-optimiser/config.toml` bind-mounted from the repo).

```bash
docker exec energy-optimiser python -m optimiser.replay_cli \
  --snapshots '/var/lib/energy-optimiser/snapshots/*.ndjson.gz' \
  --config /etc/energy-optimiser/config.toml \
  --output /tmp/replay-results.ndjson 2>&1 | tail -30
```

Then analyse the output with DuckDB (no need to copy aside — `/tmp/replay-results.ndjson` is fresh):

Schema of each line in the output: `tick_id, timestamp, original, candidate, delta_cents, original_reason, candidate_reason, solve_status, solve_ms`. Note the columns are `original` / `candidate` (not `*_action`).

```bash
docker exec -i energy-optimiser python - <<'PY'
import duckdb
c = duckdb.connect(":memory:")
q = """
SELECT
  DATE_TRUNC('day', timestamp) AS day,
  COUNT(*) AS ticks,
  COUNT(*) FILTER (WHERE candidate != original) AS changed_actions,
  ROUND(SUM(delta_cents)/100.0, 2) AS delta_aud,
  ROUND(AVG(solve_ms), 0) AS avg_solve_ms,
  ROUND(QUANTILE_CONT(solve_ms, 0.95), 0) AS p95_solve_ms
FROM read_json('/tmp/replay-results.ndjson')
GROUP BY day
ORDER BY day
"""
for row in c.execute(q).fetchall():
    print(row)

# Overall
overall = c.execute("""
SELECT
  COUNT(*),
  ROUND(SUM(delta_cents)/100.0, 2),
  ROUND(AVG(solve_ms), 0),
  ROUND(QUANTILE_CONT(solve_ms, 0.95), 0)
FROM read_json('/tmp/replay-results.ndjson')
""").fetchone()
print("OVERALL ticks=%d delta_aud=%s avg_ms=%s p95_ms=%s" % overall)
PY
```

## Output format

```
REPLAY REVIEW — 2026-04-22 12:45 UTC

Snapshots: 31 files, 2026-03-23 → 2026-04-22, 412 MB total
PASS  No missing days in last 30
PASS  All days ≥ 1400 ticks (min=1438, max=1440)

Replay (last 7d, current config):
  2026-04-16  1440 ticks  12 changed  +$0.08  avg=245ms  p95=410ms
  2026-04-17  1440 ticks   8 changed  +$0.02  avg=250ms  p95=420ms
  ...
  OVERALL:   10080 ticks  68 changed  +$0.31  avg=248ms  p95=418ms

PASS  Replay matches live (delta negligible, as expected replaying current config)
PASS  solve_ms p95 < 5000ms budget
```

Interpretation to include in the report:
- If replay is against the **current** config (same as what the live service ran), delta should be near zero — any deviation points to non-determinism in the solver or snapshot drift. Flag > ±$1/week as suspicious.
- If the user is explicitly testing a **candidate** config (they'll mention it or swap the `--config` flag), large deltas are informative, not errors.
- `p95_solve_ms > 5000` is a perf regression. `p95 > 10000` violates the budget and needs attention.

Keep output under 25 lines.

## Known quirks

- **Today's snapshot is being written live.** `zcat` and gzip readers will emit "invalid compressed data" / "invalid block type" errors on the tail of today's file. `replay_cli` handles this gracefully (counts parseable ticks, ignores the torn tail) — do NOT report it as a failure. Only treat gzip errors on **past-day** files as real data corruption.
- **Delta should be ~$0 for current-config replay.** Non-zero delta here means solver non-determinism (HiGHS sampling tolerance) — note it but don't alarm. Threshold for real concern: > ±$1/week.

## Don't

- Don't run replay without `--output` — we need the NDJSON to summarise.
- Don't touch the live DuckDB — replay reads snapshots only.
- Don't use a candidate config unless the user asked. Default is the live `config.toml`.
- Don't interpret changed_actions as bugs — the stochastic solver samples scenarios, small variation is normal.

## Variants the user may ask for

- "Replay 30 days" → change the snapshot glob to `2026-03-*.ndjson.gz 2026-04-*.ndjson.gz` or similar, and raise delay expectations proportionally.
- "Replay with candidate config X" → user will provide/point to the alternate config; use it instead of the live one.
- "Skip the solve" → defer to `/review-services` + `/review-db` instead; this skill always solves.
