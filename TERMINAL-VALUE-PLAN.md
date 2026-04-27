# Terminal-value function — plan document

Status: **Parked, awaiting data accumulation (~60 days from 2026-04-27).**

This doc describes what's needed to replace the constant
`TERMINAL_SOC_FLOOR_PCT = 20.0` (lp/constants.py) with a fitted
`V(SOC, state)` — the "PV-aware terminal value function" the constants
comment alludes to. The data-generation tool is already built and
shipping rows; the fit + LP integration come once 60 days of training
rows have accumulated.

Cross-references:
- `src/optimiser/terminal_value_data.py` — counterfactual row generator (shipped).
- `src/optimiser/terminal_value_data_cli.py` — driver CLI (shipped).
- `src/optimiser/lp/constants.py::TERMINAL_SOC_FLOOR_PCT` — the scalar V replaces.
- `src/optimiser/lp/constants.py::terminal_soc_floor_pct` — staged hour-of-day function (committed, not wired). Parked in favour of the proper V; left in source as documentation of the heuristic shape.
- `src/optimiser/lp/formulation.py:489` — current call site of the constant.

---

## 1. Why a function instead of a constant

The current LP terminal floor is 20% — applied at the last slot of the
LP horizon (~36h out, capped at the 48h `HORIZON_HOURS`). Every
constraint at every other slot is governed by the *operational* per-slot
floor (`battery_config.soc_floor_pct`, default 15%). The terminal floor
exists to reserve energy past the unpriced tail beyond Amber's forecast
window.

A constant ignores the question "*at what time-of-day does the terminal
slot land*". 20% at 13:00 NEM (PV peak — battery refills within an
hour) is over-conservative; 20% at 06:00 NEM (just before the morning
peak, no PV) is under-conservative. Different terminal hours need
different reserves.

The hour-of-day curve in `terminal_soc_floor_pct()` captures the
gross shape of this dependence by hand. A fitted V captures it
empirically *and* conditions on more than just hour — most importantly
on the actual next-24h PV forecast, which the hour heuristic cannot.

Expected benefit (rough order of magnitude, to be sharpened by the fit
itself):
- Sunny-tomorrow days: lower terminal floor → more peak shaving today
  → ~$0.30–0.80/day extra evening-peak revenue.
- Cloudy-tomorrow / forecast-bust days: higher terminal floor → less
  exposure to morning-peak retail imports → ~$1–2/day worst-case
  loss avoided.
- Net expectation: a few hundred dollars/year, plus a clearer
  separation between "safety" (per-slot floor, 15%, hard) and
  "value" (terminal floor, V, soft, learned).

## 2. What V looks like

`V(SOC, features) = scalar value (cents)` interpreted as the *expected
24h cost from terminating with `SOC` under the world summarised by
`features`*. The LP integrates V as a **negative term in the objective**
at the last slot:

```
minimize  Σ_t  cost_t (...)   +   V(soc_pct[T_final], features_at_T_final)
```

Higher V = higher cost (we *want* to minimise). Because V is a function
of the LP's decision variable `soc_pct[T_final]`, it must be embeddable
as a continuous LP construct — that's the form constraint.

### Form: piecewise-linear in SOC

V must be **piecewise-linear in SOC** so it embeds as auxiliary slack
variables + linear constraints (HiGHS handles this trivially). The
features enter as coefficients of the PWL pieces — the function is
linear in feature-weighted breakpoints.

Concretely, with K breakpoints `s_1 < s_2 < ... < s_K`:

```
V(SOC, x) = max_k [ a_k(x) - b_k(x) · SOC ]
```

where each `a_k`, `b_k` is a *linear function of features*
(e.g. `b_k(x) = β₀ᵏ + β₁ᵏ · pv_p10 + β₂ᵏ · max_import + ...`).

Five breakpoints over [10%, 50%] (e.g. 10, 18, 25, 35, 50) give the
function enough flexibility to model "steeply expensive below 20%,
gently expensive between 20-30%, flat above 35%" without overfitting.
Refine after first fit.

### Anti-form: tree models, neural nets

XGBoost / LightGBM / NN would be more flexible and probably learn a
better V on raw features, but **none embed cleanly into the LP**.
You'd have to read out a PWL approximation per-tick (per-LP-solve)
or constrain the LP to a discrete SOC grid. Both are uglier than just
fitting a PWL form directly. Trees/NNs are reasonable for *exploring*
the data offline (does V plausibly depend on `next_24h_pv_p10`? does
it interact with `month`?) but the deployed V should be PWL.

## 3. Training data

### Schema (already shipped)

Each row in `terminal_value_data.TrainingRow` has:

**Anchor:**
- `timestamp` — anchor time T (ISO UTC)
- `soc_pct_terminal` — candidate starting SOC at T (the X in V)

**Forward-looking features:**
- `hour_of_day_nem` — 0–24, NEM time (DST-stable)
- `day_of_week_nem` — 0=Mon, 6=Sun
- `month` — 1–12 (seasonal regime proxy)
- `horizon_pv_p10_kwh`, `horizon_pv_p50_kwh`, `horizon_pv_p90_kwh` —
  Solcast over [T, T+24h)
- `horizon_house_load_kwh` — load profile integrated over horizon
- `horizon_min_import_c`, `horizon_max_import_c`, `horizon_mean_import_c`
- `horizon_max_export_c`, `horizon_mean_export_c`

**Label:**
- `realised_cost_cents` — closed-loop simulator cost from T → T+horizon
  starting at `soc_pct_terminal`, against the realised
  prices/PV/load that *actually happened* in the snapshot stream.

**Provenance:**
- `snapshot_version`, `horizon_hours`, `cadence_minutes`,
  `sim_solve_failures`, `sim_n_steps`.

### Coverage requirements

Minimum **~60 days** with explicit diversity along three axes:
- **Seasonal**: at least one full month with low PV (winter) and one
  with high PV (autumn) — Canberra's PV regime shifts substantially
  Jan vs Jul. April→Jun captures the autumn-to-winter transition.
- **Forecast quality**: days where Solcast P50 was accurate AND days
  where it busted (PV under-delivered or over-delivered). The model
  needs to learn that lower P10 means more cushion needed.
- **Price regime**: typical days AND price-event days (negative
  spikes, evening-peak >40c). If the training set is all "boring
  20c" days, V won't generalise.

Sample size at default cadence (30 min) × 8 SOCs × 60 days × 48
anchors/day = ~23k rows. With seasonal repeats and weather variance
this is plenty for a PWL with ~5 breakpoints × ~10 features = 50
params.

### Generation (already automated)

```bash
# Once 60 days of snapshots have accumulated
uv run python -m optimiser.terminal_value_data_cli \
    --snapshots '/var/lib/energy-optimiser/snapshots/2026-*.ndjson.gz' \
    --config config.toml \
    --out /var/lib/energy-optimiser/tv-training.ndjson.gz \
    --cadence-minutes 30 \
    --horizon-hours 24 \
    --starting-socs 15,25,35,45,55,65,75,85
```

Smoke-tested: 22 rows from 3 days of snapshots in ~8 min wall-clock
(post-index-reuse refactor, commit `09d3cff`). Full 60-day run at the
above cadence ≈ 23k rows × ~20s/row = ~125 hours wall-clock — needs
overnight runs or parallelisation. **Action item: parallelise the
driver loop before kicking off the full run** (sims are independent
across anchors).

## 4. Process

### Phase 1 — Data accumulation (now → +60 days)

- Snapshots are already collected continuously by the running service
  (`/var/lib/energy-optimiser/snapshots/YYYY-MM-DD.ndjson.gz`,
  ~7 MB/day gzipped). No new instrumentation required.
- Verify disk headroom: 60 × 7 MB = ~420 MB. Trivial.
- Lock the snapshot `version` field — current is `0.2.0`. If the
  schema bumps, training rows from before vs after must not be mixed.
  The data-gen tool already records `snapshot_version` per row;
  refuse mixed versions at fit time.

### Phase 2 — Data generation (+60 days, ~1 day wall-clock)

1. Parallelise `terminal_value_data.generate_rows()` — sims are
   independent across (anchor, SOC) pairs. Aim for N-way parallelism
   matching CPU count. Should bring 125h → ~15h on an 8-core box.
2. Run the CLI over the full snapshot archive. Output one
   `tv-training.ndjson.gz` (~few MB).
3. Sanity-check the row distribution: histograms of features +
   labels, check for solver-failure clusters, drop rows where the
   simulator hit fallback mode (would corrupt the label).

### Phase 3 — Fit (interactive, ~hours)

1. Load training rows into a notebook / DuckDB.
2. Exploratory: look at the marginal of `realised_cost_cents` vs
   `soc_pct_terminal` for fixed feature buckets. Confirm the curve
   shape is plausible (monotonic decreasing, levelling off above
   some SOC).
3. Fit a feature-conditioned PWL:
   - Pick breakpoints (start with 5: 10, 20, 30, 40, 50% SOC).
   - Linear regression of `realised_cost_cents` against
     `[1, max(0, s_k − SOC) for k in breakpoints]` cross-products
     with features.
   - Regularise (Lasso or ridge) to avoid overfitting features.
4. Validate cross-day: fit on first 50 days, evaluate on last 10.
   RMSE on held-out should be < ~30c (the typical day-to-day
   variance of cost is ~$1–2).

### Phase 4 — Validate via simulator sweep (~1 day)

1. Embed the fitted V into a fork of the LP. Replace
   `TERMINAL_SOC_FLOOR_PCT` (and the staged hour-of-day function)
   with the V term in the objective.
2. Run `simulate_sweep` across the original adverse-scenario matrix
   (history, pv-bust-50, pv-bust-30, forecast-too-rosy, pv-bonanza)
   on at least 10 representative days (sunny, cloudy, peak-spike,
   negative-export, etc.).
3. Compare daily cost: fitted-V vs constant-20% vs hour-of-day curve.
   The bar to clear:
   - **Must not** lose money on any single scenario.
   - **Should** beat constant-20% on average across normal days
     (sunny-tomorrow → lower terminal SOC → more revenue).
   - **Should** match or beat constant-20% on bust scenarios.
4. Check solver behaviour: V adds slack vars to the objective; verify
   solve times stay under the 500ms typical / 10s max budget.

### Phase 5 — Ship (~1 day)

1. Commit the fitted V coefficients to the repo (probably as a
   small frozen dataclass in `lp/constants.py` or a JSON sidecar
   loaded at startup).
2. Update `lp/formulation.py:489` to call V instead of the
   constant. Add the slack-variable PWL term to the objective.
3. Add property tests asserting the LP terminal slot SOC honours V
   in canonical scenarios (sunny-tomorrow, cloudy-tomorrow, etc.).
4. Deploy via the standard `docker compose build && up -d` flow.
5. Watch the live LP for 1–2 days. Especially look for: terminal-SOC
   regression (LP arriving at terminal much higher/lower than V
   said it should), grid-charging that can't be explained by V's
   trade-off math.
6. Refresh V monthly (re-fit on the latest 60 days). Eventually
   automate.

## 5. Outcomes — what success looks like

### Quantified
- **Net daily-cost improvement** vs constant-20% baseline of $0.30–0.80.
- **Worst-case daily cost** on PV-bust scenarios held within ~$0.50
  of the constant-20% baseline (i.e. V doesn't make us materially
  more exposed in the bad cases — it only captures upside).
- **Solve-time impact** under 50ms additional latency (a single PWL
  term with ~5 breakpoints adds ~10 vars, ~10 constraints to the LP
  — negligible at HiGHS speeds).

### Qualitative
- LP terminal SOC visibly tracks PV forecast: ~15% on sunny-tomorrow
  forecasts, ~30% on cloudy-tomorrow forecasts.
- Operator-readable V: when looking at a tick's plan, the trade-off
  "we're holding 5 kWh extra because P10 PV tomorrow is only 18 kWh"
  is legible from the data, not opaque.
- A V refresh produces a new coefficient table that's interpretable
  vs the previous version (no abrupt sign flips, no breakpoint
  collapse).

### Failure modes — what would tell us we got it wrong

- **V drives the LP to grid-charge overnight** to reach the V-suggested
  terminal SOC. (Means V is over-valuing terminal energy or the LP's
  objective scaling is off. Fix: cap V's contribution magnitude.)
- **V correlates accidentally with non-causal features** (e.g. day-of-
  week, because Sundays in our dataset all happened to be sunny).
  Diagnostic: ablate the feature, refit, see if held-out RMSE moves.
- **PWL form too rigid** — breakpoint placement leaves big residuals
  near the operational floor. Diagnostic: residual plot vs
  `soc_pct_terminal`.
- **Schema drift breaks training** — snapshot `version` changed mid-
  archive and rows from the two regimes don't actually mean the same
  thing. Diagnostic: bucket rows by version, re-fit per version, see
  if coefficients agree.

## 6. Open questions

1. **Cadence of V refresh**: monthly? quarterly? When does seasonal
   drift matter most — autumn/winter shoulder, or summer-peak season?
2. **Do we need separate V for weekday vs weekend?** Different load
   profile (`day_of_week_nem` is already a feature; the PWL might
   pick this up automatically, or we may need explicit interaction
   terms).
3. **Does V need to be aware of the LP's next-tick re-solve?**
   Probably not — the LP plans against terminal value in expectation,
   and re-solving every tick re-equilibrates. But worth a hypothesis-
   test once we have the fit.
4. **Slack penalty calibration** — V will be a soft term, paired
   with a slack penalty for infeasibility. The penalty scaling
   relative to V's gradient matters; too low and V is ignored, too
   high and we're back to a hard constraint.

## 7. What's already done

- ✅ Snapshot pipeline collecting the raw data continuously.
- ✅ `terminal_value_data.py` — counterfactual row generator
  (commit `787e345`).
- ✅ `terminal_value_data_cli.py` — driver CLI (commit `787e345`).
- ✅ Index reuse refactor — runtime-budget unblocked
  (commit `09d3cff`).
- ✅ Hand-calibrated hour-of-day curve as documentation /
  fallback / heuristic (commit `ff81f59`, parked).
- ✅ Smoke test on 3 days × 4 anchors × 2 SOCs (22 rows, 0 solver
  failures) — pipeline shape validated.
- ✅ Hard SOC floor (commit `8893964`) — primary safety mechanism;
  V is purely an *economic* layer on top.

## 8. What's not done — kicking off the V fit, in order

1. **Wait** ~60 days until the snapshot archive has seasonal coverage.
2. **Parallelise** the data-gen driver loop (sims are independent;
   should be a small wrapper around `multiprocessing.Pool`).
3. **Run** the CLI on the full archive overnight.
4. **Fit** the PWL V (notebook / scripted).
5. **Validate** via `simulate_sweep` on 10+ representative days.
6. **Ship** the V via the standard build/deploy path.
7. **Monitor** live behaviour for 48h post-deploy, then decide on
   refresh cadence.

Total wall-clock from "60 days from now" to V live in production:
~3–5 days of focused work.
