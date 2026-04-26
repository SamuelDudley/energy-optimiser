---
name: sim-sweep
description: Closed-loop multi-tick simulator for evaluating LP strategy changes (e.g. wear cost, scenario weights, floor) against historical snapshots. Use when the user types /sim-sweep or asks to A/B a LP config change, stress-test under PV-bust scenarios, or compare a candidate against the deployed default. Differs from /review-replay in that it rolls SOC physics forward across ticks rather than re-solving each historical tick independently.
---

# sim-sweep

Why this exists: `/review-replay` re-solves each historical tick independently against the original SOC. Useful for sanity but blind to compounding effects — early-tick decisions don't shift later state. The 2026-04-25 overnight failure (LP drained the pack from 70% → 0.1% across an evening) is exactly the failure mode `/review-replay` couldn't catch: each individual tick was locally rational; the disaster was the cumulative trajectory.

`simulate.py` (the engine behind this skill) advances SOC physics tick-by-tick under each candidate's plan, so the candidate's *cumulative* decisions diverge from history. Realised cost is then evaluated against actual prices — apples-to-apples comparisons across configs.

## Before you start

The simulator is heavy: each step does a real stochastic LP solve (~150–500 ms). One day = 288 5-min steps = ~2–3 min/sim. A 2-config × 3-scenario sweep on one day = ~15 min. Plan accordingly.

Snapshots needed:
```bash
mkdir -p /tmp/eo-sim
docker cp energy-optimiser:/var/lib/energy-optimiser/snapshots/. /tmp/eo-sim/
```

## One-off sim

The simplest invocation — one config, one scenario, one day:

```bash
uv run python -m optimiser.simulate_cli \
  --snapshots '/tmp/eo-sim/2026-04-25.ndjson.gz' \
  --config config.toml \
  --label baseline
```

Outputs a JSON summary: total cost AUD, kWh imported/exported/cycled, min/max realised SOC.

### Useful overrides

- `--initial-soc 70.0` — override the starting SOC (the default uses the first snapshot's SOC).
- `--wear-cost 5.0` — pin a specific wear cost regardless of the constant. The change shipped in commit `0ab9e44` was validated this way.
- `--floor 15.0 --ceiling 95.0` — override the planning band.
- `--weights 'p10=0.40,p50=0.40,p90=0.20'` — heavier P10 hedge.

### Adverse-scenario stress

Modifiers perturb the snapshot stream before the LP sees it:

- `--pv-actual-mult 0.5` — historical PV under-delivers by 50% (cloudy day vs sunny forecast).
- `--pv-forecast-mult 1.5` — Solcast bias 50% high (LP overcommits).
- `--import-price-mult 2.0` / `--export-price-mult 1.5` — pricing shocks.

The `pv-actual-mult < 1.0` scenario is the canonical "did the LP just bet the safety reserve on a forecast that didn't hold?" test.

## Sweep candidates × scenarios

`simulate_sweep.py` runs a matrix and prints a comparison table. Quick check on one day:

```bash
uv run python -m optimiser.simulate_sweep \
  --snapshots '/tmp/eo-sim/2026-04-25.ndjson.gz' \
  --config config.toml
```

The candidate list lives in `simulate_sweep.py` (`CANDIDATES`) — edit it for one-off A/Bs. Default sweep covers prod-current + p10-heavy + wear-5c + conservative-combo across 5 PV/forecast scenarios.

## When to invoke

Use this skill when the user is:

- Debating an LP constant change ("what if we lift wear cost to 6c?", "should I drop the P50 weight?")
- Evaluating a defensive change against a class of scenarios ("how does the LP behave if Solcast is systematically biased?")
- Validating a fix ("does the wear=5c change actually save money under realistic adverse conditions?")
- Asking "would the LP have done X if Y?" where Y is a config hypothetical

Do NOT use this skill for:

- "Why did the LP do X right now?" → `/explain-plan`
- "Why did the LP do X at HH:MM yesterday?" → `/diagnose-lp-decision`
- "Are snapshots being written correctly?" → `/review-replay`
- "How healthy is the running service?" → `/review-services`

## Headline result format

When reporting, lead with the worst-case column. The whole point of these sims is "what's the cost when the world doesn't cooperate":

```
                                history    pv-bust-50    pv-bust-30    worst
prod (wear=2.5):                 -$1.05      +$1.55       +$2.90      +$2.90
cand-wear5c:                     -$0.27      +$0.30       +$0.85      +$0.85
```

Negative numbers are revenue (we earned). Positive numbers are cost (we paid). The `worst` column is the headline metric — picking a robust config means minimising it.

## Known caveats

- Battery-only sim: managed loads (HW heat pump, etc.) are excluded to isolate battery strategy. If the question is about load coordination, simulate via the live service or write a managed-load-aware variant.
- Physics model is approximate (signed `battery_kw` directly drives SOC delta; real mode-6 dispatch is load-following). Biases cancel for relative comparisons but absolute numbers are ±10% rough.
- The simulator's price stream is the snapshot's *forecast at decision time*, but realised cost uses the LP's slot-0 price. So a price forecast bust isn't independently testable — use `--import-price-mult` / `--export-price-mult` for that.
- LP grid-arb loophole: at `ep > ip` the LP can phantom-arb (simultaneous import + export). Workaround: keep `ep < ip` in any synthetic scenarios. Documented in `KNOWN-ISSUES.md` (High).
