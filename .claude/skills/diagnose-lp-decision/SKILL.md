---
name: diagnose-lp-decision
description: Read-only post-mortem of a single historical LP tick — explains *why* the LP made a counterintuitive slot-0 decision (idled a high-priced slot, charged at peak, exported at zero, etc.). Use when the user types /diagnose-lp-decision or asks "why did the LP do X at HH:MM yesterday/last week", "why did it skip the peak", "why did it discharge into a cheap slot". Different from /explain-plan, which answers "right now". This skill replays one historical tick, runs sensitivity sweeps, and decomposes the LP's choice across stochastic scenarios to surface which one is binding via non-anticipativity.
---

# diagnose-lp-decision

A surprising LP decision usually has one of three causes:

1. **Numerical / transient** — solver picked an arbitrary vertex of a degenerate polyhedron. Rule out by reproducing.
2. **Stochastic hedge** — slot 0 is tied across P10/P50/P90 by non-anticipativity, and the cheapest scenario's plan dominates the choice via its expected weight. The LP is correctly trading a tiny here-and-now loss for a hedge against a worse outcome under another scenario. Read like a bug, behaves like a feature.
3. **Genuine bug** — formulation error, wrong price merge, terminal-SOC pinned wrong, mis-mapped register. Rare but worth catching.

This skill walks through cases 1 → 2 → 3 in order, escalating only when the prior is ruled out. It produces a verdict: **close as understood / file as KNOWN-ISSUES / needs deeper work**.

Worked example: `INVESTIGATION-evening-slot-skip.md` (2026-04-25 18:35 idle of the highest-priced slot — turned out to be a p10-scenario hedge against tomorrow's 9.94 c/kWh slot).

## Inputs the user gives you

- A timestamp (UTC, ISO-prefix granularity — e.g. `2026-04-25T08:35`)
- Optionally, the action that surprised them ("idled", "discharged at <c", "charged from grid")
- Optionally, the slot they expected to be different ("should have discharged at 18:35 local")

If the user gives a *local* time, convert to UTC before doing anything else (Canberra is UTC+10 winter / +11 summer; see `time_utils.py`). Snapshots and prices are UTC.

## Tools at your disposal

This skill uses three CLIs, each read-only:

- **`replay_cli`** with `--filter-timestamp` / `--override-soc` flags — one-tick replay against a candidate config; reproduces the historical solve (or doesn't, which is itself a finding). Use for SOC sensitivity sweeps.
- **`optimiser.lp.diagnose_cli`** — single-tick deep introspection: slot-0 vars across all compound scenarios (3 under default POINT mode; 9 under SHARED, 27 under CROSS — see `lp/scenarios.py`), objective decomposition, forward trajectory, and an optional `--force-bat-net` counterfactual that surfaces *which scenario's future slot is binding* the slot-0 choice.
- **`jq`** against the snapshot file directly — for the original LP's `forward_trajectory` (already in the snapshot, no re-solve needed) and price-forecast inspection.

The DuckDB telemetry file is locked by the running service. Don't touch it. Snapshots are NDJSON.gz, append-only, safe to read concurrently.

## Step 1 — Get the snapshot to a host-readable path

Run replay/diagnose **inside the container** (`docker exec energy-optimiser …`), reading snapshots in-place from the container's volume — same pattern as `/review-replay`. Copy snapshots to `/tmp` on the host only when you need `jq` from the host shell.

> **Note**: the deployed image has no `jq`. When you need to inspect a small JSON file produced by a `docker exec` command (e.g. replay output), `docker cp` it back to the host first and run `jq` there. The pattern is `docker cp energy-optimiser:/tmp/repro.ndjson /tmp/ && jq … /tmp/repro.ndjson`.

```bash
DAY="2026-04-25"  # date of the tick the user is asking about
docker cp energy-optimiser:/var/lib/energy-optimiser/snapshots/${DAY}.ndjson.gz /tmp/
```

If the user is asking about *today*, the file rotates daily and is gzipped append-mode — `zcat` + `tail` works on it even mid-write. If `docker cp` complains about a moving target, re-run.

The live config is bind-mounted at `/etc/energy-optimiser/config.toml`; pass that path on `docker exec` invocations so the diagnosis matches what the running service was using.

## Step 2 — Confirm the tick exists and capture the surprise

```bash
TS_PREFIX="2026-04-25T08:35"   # UTC ISO prefix; minute-level is enough
zcat /tmp/${DAY}.ndjson.gz \
  | jq -c --arg p "$TS_PREFIX" 'select(.timestamp | startswith($p))' \
  > /tmp/tick.ndjson
wc -l /tmp/tick.ndjson   # should be 1; if 0, widen the prefix
```

Pull the slot-0 view + the next 5–10 future slots in the original solve:

```bash
jq -c '{
  ts: .timestamp,
  soc: .system_state.soc_pct,
  slot_0: .lp_solution.slot_0,
  output: .output,
  next_slots: [.lp_solution.forward_trajectory[0:8][] | {
    slot: .slot_start[11:16],
    bat: .battery_kw,
    ge: .grid_export_kw,
    soc: .soc_pct_end
  }],
  prices_head: [.price_forecast[0:6][] | {
    s: .start[11:16], e: .end[11:16],
    imp: .import_per_kwh, exp: .export_per_kwh,
    pred: .forecast_predicted, locked: .is_locked
  }]
}' /tmp/tick.ndjson
```

Check:
- Does `slot_0.battery_kw` match what the user described? If not — check the field unit conventions: `battery_kw < 0` is discharge.
- Is `output.reason` a `fallback:` / `lp fallback:` / `circuit_breaker:` prefix? If yes — the LP wasn't driving this tick. Stop and report; this skill doesn't diagnose fallback ticks.

Also pull a few neighbouring ticks (±10 min) for comparison:

```bash
zcat /tmp/${DAY}.ndjson.gz \
  | jq -c 'select(.timestamp[11:16] | IN("08:25","08:30","08:35","08:40","08:45"))
           | {ts: .timestamp[11:16], soc: .system_state.soc_pct,
              slot0_bat: .lp_solution.slot_0.battery_kw,
              slot0_ge: .lp_solution.slot_0.grid_export_kw}'
```

If the neighbours all picked the same action and only the target tick differs — that's the puzzle. If neighbours also did what the user finds surprising, the issue isn't tick-specific; widen the question before continuing.

## Step 3 — Reproduce via replay (rules out H1: numerical / transient)

```bash
docker exec energy-optimiser python -m optimiser.replay_cli \
  --snapshots /var/lib/energy-optimiser/snapshots/${DAY}.ndjson.gz \
  --config /etc/energy-optimiser/config.toml \
  --filter-timestamp "$TS_PREFIX" \
  -o /tmp/repro.ndjson 2>&1 | tail -5
docker cp energy-optimiser:/tmp/repro.ndjson /tmp/repro.ndjson
jq -c '{orig: .original, cand: .candidate, orig_reason: .original_reason, cand_reason: .candidate_reason, delta: .delta_cents}' /tmp/repro.ndjson
```

Outcomes:
- `cand == orig` and reasons line up → **deterministic; H1 ruled out.** Move to step 4.
- `cand != orig` → divergence between historical LP and replay. **This is significant.** Reasons it can happen:
  - Config drift (`/etc/energy-optimiser/config.toml` differs from what the running service had at the time). Check `git log -- config.toml` near the tick's timestamp.
  - The replay reconstruction is dropping a field (e.g. `forecast_predicted` was dropped pre-2026-04-25 — that bug is fixed, but if you find another, it's a real find).
  - A code change altered LP behaviour between then and now. Check `git log src/optimiser/lp/` since the tick.
  - **Stop and report this finding.** Don't continue assuming the rest of the diagnosis is valid.

## Step 4 — SOC sensitivity (rules out H4: edge-of-numerical-cliff)

Sweep the initial SOC ±5% around the historical value to see if the choice flips on a numerical knife-edge:

```bash
SOC_HIST=$(jq -r '.system_state.soc_pct' /tmp/tick.ndjson)
echo "Historical SOC: $SOC_HIST"

for soc in $(python3 -c "h=$SOC_HIST; print(' '.join(f'{h+d:.1f}' for d in [-5, -2, -0.5, -0.1, 0, +0.1, +0.5, +2, +5, +10]))"); do
  docker exec energy-optimiser python -m optimiser.replay_cli \
    --snapshots /var/lib/energy-optimiser/snapshots/${DAY}.ndjson.gz \
    --config /etc/energy-optimiser/config.toml \
    --filter-timestamp "$TS_PREFIX" \
    --override-soc $soc \
    -o /tmp/sweep.ndjson 2>/dev/null >/dev/null
  docker cp energy-optimiser:/tmp/sweep.ndjson /tmp/sweep.ndjson 2>/dev/null
  echo "soc=$soc  $(jq -r '.candidate + "  " + (.candidate_reason | split("scenarios]:")[1] // "")' /tmp/sweep.ndjson)"
done
```

Read the output:
- Decision is the same across ±5% → robust; SOC isn't the lever. Move to step 5.
- Decision flips inside ±0.5% → numerical knife-edge. Worth flagging in the report but probably still understood by step 5.
- Decision flips somewhere reasonable (e.g. at SOC + 4% → switches from idle to discharge) → tells you the *budget margin*. The LP saw N kWh of headroom; one more % of SOC was enough to relax a binding constraint. Useful context for step 5.

## Step 5 — Deep LP introspection (H2: stochastic hedge)

This is where most "the LP did something weird" cases land. Run the diagnostic CLI with the counterfactual flag set to whatever the user *expected* the LP to do:

```bash
# What the user expected, expressed as slot-0 net battery kW (charge − discharge,
# signed; − = discharge, + = charge):
#   Full discharge at 5 kW export limit + ~1 kW house = 6 kW out → net = −6
#   Full grid charge at 10 kW                                  → net = +10
#   Idle / load-follow only                                    → net = ~0 (use −1 if house ≈ 1 kW)
EXPECTED_NET="-6.0"

docker exec energy-optimiser python -m optimiser.lp.diagnose_cli \
  --snapshot /var/lib/energy-optimiser/snapshots/${DAY}.ndjson.gz \
  --timestamp "$TS_PREFIX" \
  --config /etc/energy-optimiser/config.toml \
  --force-bat-net "$EXPECTED_NET"
```

The output has six sections; read them in order:

1. **Tick header** — confirms inputs (SOC, battery config, price/PV counts).
2. **Natural solve summary** — status, total objective in cents, base scenario.
3. **Slot 0 across scenarios** — three rows. Look for: are all three picking the same `bat_net`? They must (non-anti). Are the per-source decompositions (`bcg`/`bcp`/`bd`) different? They can be — only the net is tied.
4. **Slot-0 objective contribution (base × weight)** — the dollar terms for slot 0. Useful for "where is the cost coming from?" sanity.
5. **Forward trajectory** — first ~24 slots; spot which ones are full discharge / idle / charge in the natural plan.
6. **Counterfactual** — the key section. `Δ_vs_natural` is the cost of forcing the user's expected decision. If it's tiny (< 0.05 c) the LP is on a knife-edge and the natural-vs-expected choice is essentially indifferent. If it's larger, the LP genuinely prefers its choice by that much.

   Then per-scenario diff: for each scenario, the slots where the LP shifted activity to absorb the forced slot 0. **The slot with the largest |Δbd| in the lowest-weight scenario is usually the one driving the LP's natural decision.** Cite its price (`ep`/`ip`) — that's the alternative the LP was saving SOC for.

### How to read the counterfactual

Pattern A — **stochastic hedge**:
- `Δ_vs_natural` is small (< 0.5c).
- One scenario's diff shows a single high-priced future slot being given up. The other two scenarios shift to a moderately-priced slot today.
- The high-priced future slot's `ep` is *higher* than slot 0's `ep`, and it's in the pessimistic scenario (typically p10 — low PV → tight SOC).
- **Verdict: H2. Close as understood.** The LP is risk-averse against the hedge scenario.

Pattern B — **late-horizon constraint**:
- `Δ_vs_natural` is small.
- Diff shows shifts deep in the horizon (e.g. 30+ hours out), often with `slack > 0` on the terminal SOC.
- The LP is constrained by its terminal-floor requirement and the natural solve threads the needle differently than the user expected.
- **Verdict: H2-adjacent. Close as understood.** Note in the report that the terminal-floor pressure is what's binding; consider whether `TERMINAL_SOC_FLOOR_PCT` is well-calibrated.

Pattern C — **degenerate-objective**:
- `Δ_vs_natural` is essentially zero (< 0.01c).
- Diff shows shifts among slots with near-identical prices.
- **Verdict: H1. Close as understood, but flag if it happens repeatedly.** Could be addressed by adding tie-break penalties (cf. `EXPORT_TIE_BREAK_PENALTY_PER_KWH`).

Pattern D — **suspicious**:
- `Δ_vs_natural` is *negative* (forced is cheaper than natural — LP picked a worse solution).
- Or: an obvious arbitrage exists in the diff (e.g. charge slot at 10c spot + discharge slot at 30c spot, but the LP didn't take it).
- Or: scenarios disagree on slot-0 net (which would mean the non-anti tie isn't being applied — formulation bug).
- **Verdict: H3. Needs deeper work.** Don't try to fix in this session — file as KNOWN-ISSUES with the diagnose_cli output attached. Possible causes worth checking before filing: solver hit `timeLimit` (check status), or the LP returned `infeasible` and replay defaulted to SELF_CONSUME.

## Step 6 — Report

Target ~30 lines. Three sections:

```
## Verdict
<one line: close as understood / KNOWN-ISSUES candidate / needs deeper work>
<one line on the *cause*, e.g. "p10 hedge for tomorrow's 9.94c slot via non-anti tie">

## Evidence
- Tick: <ts UTC>, SOC=<pct>%, slot-0 plan: <bat>kW (<idle|discharge|charge>)
- Reproduced via replay: <yes/no>; <delta if cand != orig>
- SOC sensitivity: <"flips at SOC+X%" or "robust ±5%">
- Counterfactual cost: forcing <expected> changes objective by <Δ>c
- Binding scenario / slot: <pX> @ <future ts> ep=<c>/kWh ip=<c>/kWh
- Real-money impact: ~<X>c per occurrence

## Recommendation
<close / file / escalate, plus one concrete next step if applicable>
```

If the verdict is **close as understood**, that's enough — don't recommend a code change.

If the verdict is **file as KNOWN-ISSUES**, propose a one-line entry describing the situation; let the user actually edit `KNOWN-ISSUES.md`.

If the verdict is **needs deeper work**, name what's unexplained and what one specific extra piece of evidence would resolve it (e.g. "dump the LP matrix for slot 0 and inspect the basis", "run with `--force-bat-net` at three more values to find the cost frontier"). Don't go on a fishing expedition.

## Don't

- Don't attempt to fix the LP. This is read-only diagnosis.
- Don't restart the service or touch the live config / DuckDB.
- Don't extend the horizon, change scenario weights, or run the live solver against patched inputs to "see what happens" — that's an LP tuning task, not a diagnosis. Use `replay_cli` against a separate candidate config if the user genuinely wants to test a tuning change.
- Don't pretend a knife-edge decision is meaningful. If the counterfactual cost is < 0.05c, say so; the LP is essentially indifferent and the choice it made is not signal.
- Don't quote prices to fewer decimal places than the snapshot has. The doc / UI rounds to 2 dp; the LP solves on the full precision. A 0.04c difference can be the whole story.
- Don't extrapolate from one tick. If the user says "this happened twice yesterday", the second occurrence may have a different cause; spot-check.
