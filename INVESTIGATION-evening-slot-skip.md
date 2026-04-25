# Investigation: LP skipped a high-priced 5-min slot at 18:35 (2026-04-25)

## TL;DR

During the 2026-04-25 evening peak, the LP's tick at **18:35:00 local (08:35:00 UTC)** decided to *idle* the battery for slot 18:35–18:40, even though that slot had the **highest** 5-min export price among the next dozen slots (+8.08 c/kWh). All neighbouring ticks (18:25, 18:30, 18:40, 18:45) thought slot 18:35 should be a full discharge. So one specific solve, on one specific tick, made an apparently-suboptimal call for one specific slot. Cost is small (≤0.1 c) but it suggests a corner case in the LP — possibly numerical, possibly a scenario-weight quirk, possibly a constraint interaction we haven't seen before.

## Reproduction handle

- Snapshot file: `/var/lib/energy-optimiser/snapshots/2026-04-25.ndjson.gz`
- Affected tick: timestamp `2026-04-25T08:35:00.x+00:00`
- Adjacent ticks for comparison: `08:25, 08:30, 08:40, 08:45`
- Service version at the time: see `version` field in the snapshot

```bash
# Quickest pull of the relevant frames
docker cp energy-optimiser:/var/lib/energy-optimiser/snapshots/2026-04-25.ndjson.gz /tmp/
zcat /tmp/2026-04-25.ndjson.gz | jq -c 'select(.timestamp[11:16] | IN("08:25","08:30","08:35","08:40","08:45"))' \
  > /tmp/skip_anomaly.ndjson
```

## What the data shows

### Each tick's plan for slot 18:35

| Tick | View of slot 18:35 (5-min) | View of slot 18:35 (30-min) | Plan for slot 18:35 |
|---|---|---|---|
| 08:25 | +7.83 c/kWh | +7.86 | bat=−6.00, exp=5.00 |
| 08:30 | +7.90 c/kWh | +8.01 | bat=−6.00, exp=5.00 |
| **08:35** | **+8.08 c/kWh** | **+7.90** | **bat=−1.00, exp=0.00 ← skipped** |
| 08:40 | +8.08 (now in past) | +7.76 | (slot in past) |
| 08:45 | +8.08 (now in past) | +7.76 | (slot in past) |

### Each tick's plan for the surrounding slots (08:35 view)

```
slot     plan              5-min ep    30-min ep
18:35    bat=-1, exp=0     +8.08       +7.90    ← idle, but highest priced
18:40    bat=-6, exp=5     +7.96       +7.90
18:45    bat=-6, exp=5     +8.04       +7.90
18:50    bat=-6, exp=5     +8.04       +7.90
18:55    bat=-6, exp=5     +8.04       +7.90
19:00…   bat=-6, exp=5     +7.83       +7.83
```

The 08:35 LP solve picked the **most expensive 5-min slot in the next half-hour** to skip. Every other slot through 19:25 got full discharge. The price spread between the chosen slots and the skipped slot is small (~0.04–0.25 c/kWh) but the *direction* is wrong — skipping should pick the cheapest, not the most expensive.

### LP cost trajectory across ticks

```
08:25  cost = -46.6 c
08:30  cost = -47.3 c (best — full SOC, full slate of discharge slots)
08:35  cost = -45.7 c (loses 1.6 c vs 08:30 — partly from less SOC, partly from this skip)
08:40  cost = -38.7 c
08:45  cost = -36.7 c
```

### What actually happened in the inverter

The dispatch at 08:35 wrote `export_cap=0` and the cascade respected it: measured battery −0.49 kW (load-follow only), grid 0.00 kW. Slot 18:35 yielded ~0 c of export revenue when the LP could have got ~0.4 kWh × +8.08 c/kWh ≈ 3.4 c.

Realised loss vs. an alternative-universe "discharge instead" choice: ~3.4 c gross export revenue forgone, partly offset because that 0.4 kWh stayed in the battery and got discharged at later +7.83-c/kWh slots instead — net realised loss ≈ (8.08 − 7.83) × 0.4 ≈ **0.1 c**. Trivially small in dollars, large as a "is this LP behaving correctly" question.

## Hypotheses to test

### H1 — Numerical degeneracy in HiGHS

The objective at 08:35 has many near-equally-good solutions (lots of slots at very similar prices). HiGHS picks one arbitrarily, and small numerical differences in starting state push it to a different basis than the surrounding ticks. The chosen vertex of the LP polyhedron happens to skip slot 18:35.

**How to test:**
- Replay slot 08:35 with a slightly different initial SOC (±0.1%) and see if the choice flips
- Try an alternative solver (CBC) and compare
- Check whether `EXPORT_TIE_BREAK_PENALTY_PER_KWH` or similar penalties produce LP coefficient values close enough to the price differences (~0.04 c/kWh) to influence the basis

### H2 — Scenario-weight interaction

The LP solves over 3 PV scenarios (P10/P50/P90). At night PV is 0 so all scenarios should be identical, but if the load profile differs across scenarios *and* slot 18:35 is on a constraint boundary in one of them, the mix could push the choice. Verify by inspecting `LPSolution.reason` and any per-scenario diagnostics.

**How to test:**
- Force scenarios to identical inputs and re-solve; if the skip vanishes, H2 is the cause
- Check `pv_forecast` values for slot 18:35 across all three scenarios

### H3 — Discharge ramp / mode-switch constraint

If the LP has a constraint that penalises mode flips between consecutive slots (it shouldn't, but maybe a hidden one), skipping a slot to "land" on a longer run of identical decisions could be cheaper. Slot 18:35 might fall on the wrong side of such a constraint.

**How to test:**
- Read `lp/formulation.py` for any per-slot-transition costs
- Check whether the choice is sensitive to whether 18:30 was idle or discharge

### H4 — Initial-state mismatch

The LP starts each solve at the measured SOC. If the SOC at 08:35:00 fell on a numerical discontinuity (e.g. the precise edge of a piecewise-linear constraint), the LP could land on a different basis. Look at `system_state.soc_pct` at 08:35:00 and compare with surrounding ticks.

**How to test:**
- Replay 08:35 with the SOC from 08:30 and 08:40 and see which way the choice goes

### H5 — Stochastic price scenarios (not currently used) — NOT this

For now the LP treats prices deterministically (KNOWN-ISSUES #24). Ruled out unless that ships before the next investigation.

## Replay recipe

```bash
# Single-tick replay against the affected snapshot
python -m optimiser.replay_cli \
  --snapshots /tmp/skip_anomaly.ndjson \
  --config config.toml \
  --filter-timestamp '2026-04-25T08:35' \
  -v

# Sweep initial SOC to test H4
for soc in 75.3 75.5 75.7 76.0; do
  python -m optimiser.replay_cli \
    --snapshots /tmp/skip_anomaly.ndjson \
    --config config.toml \
    --override-soc $soc \
    --filter-timestamp '2026-04-25T08:35' \
    -v
done
```

(The `--override-soc` flag may not exist yet — add to `replay_cli.py` as part of the investigation if so.)

## What to look at first

1. Print the LP's full objective contribution per slot for 18:35 — see exactly how `bat=−1, exp=0` beats `bat=−6, exp=5`. The objective coefficients should make the answer obvious.
2. Compare the LP basis at 08:30, 08:35, 08:40 — if the basis changes between 08:30 and 08:35 in a way that's not driven by SOC or prices, it's H1.
3. Look at `pv_curtail` and `pv_to_battery` for slot 18:35 in the 08:35 solve. At night these should be zero — if they're not, the formulation has a leak.

## Cost vs. benefit of digging deeper

- **Real-money impact:** ≤0.1 c per occurrence; observed once in 4 hours of evening peak. If it happens at most a few times per day, < 1 c/day.
- **Confidence impact:** larger. If we don't understand *why*, we can't rule out the same pathology firing in higher-stakes situations (e.g. peak-shaving slots where the LP needs to commit a specific charge schedule). Worth resolving.

Recommended depth: 2–4 hours to test H1 and H4. If those don't explain it, escalate to a single-slot LP build trace (write the formulation matrix to a file and inspect the objective row) before going further.

## Status

- [ ] Pull single-tick replay capability
- [ ] Test H1 (solver numerical sensitivity)
- [ ] Test H4 (SOC sensitivity)
- [ ] Inspect objective contributions for slot 18:35
- [ ] Decide whether to file as KNOWN-ISSUES item or close as understood

## Cross-references

- Snapshot review writeup in this conversation (search for "1:36pm and 1:42pm" and "evening peak")
- `lp/formulation.py` — solver inputs
- `lp/constants.py` — `EXPORT_TIE_BREAK_PENALTY_PER_KWH`, scenario weights
- `KNOWN-ISSUES.md #22` — load uncertainty (related but different)
- `CLAUDE.md` decision-log entry on 5-min/30-min price merge
