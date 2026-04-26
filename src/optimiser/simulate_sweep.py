"""Sweep candidate LP configs × adverse scenarios. Prints a comparison
table so we can pick the most-robust fix for the "LP treats PV refill
as free" failure mode (KNOWN-ISSUES / task #6).

Each cell of the table is the realised total cost (AUD) of running a
candidate config across one scenario. Lower is better. The "worst-
case" column is the max across scenarios — that's the headline metric
for picking a robust fix (we want to minimise it).

Run:
    uv run python -m optimiser.simulate_sweep \
        --snapshots '/var/lib/energy-optimiser/snapshots/2026-04-25.ndjson.gz' \
        --config config.toml
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from .config import load_config
from .simulate import ScenarioModifier, simulate


# ── Adverse scenario library ─────────────────────────────────────


SCENARIOS: list[ScenarioModifier] = [
    ScenarioModifier(name="history"),  # baseline — historical record unchanged
    # The classic LP failure mode: forecast looks normal but the realised
    # PV under-delivers (cloudy day, forecast bust).
    ScenarioModifier(
        name="pv-bust-50",
        pv_forecast_multiplier=1.0,
        actual_pv_multiplier=0.5,
    ),
    # Heavier bust — overcast day.
    ScenarioModifier(
        name="pv-bust-30",
        pv_forecast_multiplier=1.0,
        actual_pv_multiplier=0.3,
    ),
    # LP's forecast inflated above reality (Solcast bias).
    ScenarioModifier(
        name="forecast-too-rosy",
        pv_forecast_multiplier=1.5,
        actual_pv_multiplier=1.0,
    ),
    # Bonanza: lots of PV + better export. Tests whether candidates
    # aren't TOO conservative (leaving money on the table on sunny days).
    ScenarioModifier(
        name="pv-bonanza",
        pv_forecast_multiplier=1.2,
        actual_pv_multiplier=1.2,
        export_price_multiplier=1.5,
    ),
]


# ── Candidate configs ────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Candidate:
    name: str
    weights: dict[str, float] | None = None
    wear_cost_per_kwh: float | None = None  # None → default 2.5

    def __post_init__(self) -> None:
        if self.weights and abs(sum(self.weights.values()) - 1.0) > 1e-3:
            raise ValueError(f"{self.name}: weights don't sum to 1.0")


CANDIDATES: list[Candidate] = [
    Candidate(name="prod-current"),  # baseline: P10:0.20/P50:0.60/P90:0.20, wear=2.5c
    # Hypothesis A: lift P10 weight so the LP plans more conservatively
    # against worst-case PV. Reduces aggressive discharge under
    # forecast-rosy days.
    Candidate(
        name="p10-heavy",
        weights={"p10": 0.40, "p50": 0.40, "p90": 0.20},
    ),
    Candidate(
        name="p10-dominant",
        weights={"p10": 0.60, "p50": 0.30, "p90": 0.10},
    ),
    # Hypothesis B: raise wear cost. CLAUDE.md decision-log notes the
    # "true" wear is closer to 10c/kWh; current 2.5c is "pragmatic
    # middle ground". Lift it to suppress marginal cycles below
    # ~7-8c spread (roughly evening Amber peak break-even).
    Candidate(
        name="wear-5c",
        wear_cost_per_kwh=5.0,
    ),
    Candidate(
        name="wear-7c",
        wear_cost_per_kwh=7.0,
    ),
    # Hypothesis C: combo — both lift P10 AND raise wear. Belt and
    # braces. Cost: less aggressive on good days.
    Candidate(
        name="conservative-combo",
        weights={"p10": 0.40, "p50": 0.40, "p90": 0.20},
        wear_cost_per_kwh=5.0,
    ),
]


# ── Runner ───────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--snapshots", "-s", required=True)
    p.add_argument("--config", "-c", required=True)
    p.add_argument(
        "--initial-soc",
        type=float,
        default=None,
        help="Override starting SOC across all sweep runs",
    )
    p.add_argument(
        "--out",
        default=None,
        help="NDJSON of full result list (one row per (scenario, candidate))",
    )
    args = p.parse_args()

    cfg = load_config(args.config)
    bcfg = cfg.battery

    # 6 candidates × 6 scenarios = 36 sims at ~2-3 min each on a single
    # day — about 2 min total. Quick enough for interactive use.
    print(
        f"Running {len(CANDIDATES)} candidates × {len(SCENARIOS)} scenarios "
        f"= {len(CANDIDATES) * len(SCENARIOS)} simulations",
        file=sys.stderr,
    )

    rows: list[dict] = []
    for cand in CANDIDATES:
        for scen in SCENARIOS:
            print(f"  {cand.name} × {scen.name} ...", end="", file=sys.stderr, flush=True)
            r = simulate(
                snapshots=args.snapshots,
                battery_config=bcfg,
                scenario_weights=cand.weights,
                wear_cost_per_kwh=cand.wear_cost_per_kwh,
                modifier=scen,
                initial_soc_pct=args.initial_soc,
            )
            row = {
                "candidate": cand.name,
                "scenario": scen.name,
                **r.summary(),
            }
            rows.append(row)
            print(
                f" cost=${row['total_cost_aud']:+7.2f} "
                f"min_soc={row['min_soc_pct']:5.1f} "
                f"discharged={row['kwh_discharged_battery']:5.1f}kWh "
                f"failures={row['solve_failures']}",
                file=sys.stderr,
            )

    if args.out:
        with open(args.out, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    # Pivot table: rows = candidate, cols = scenario, cell = cost in AUD
    print("\n=== Realised cost (AUD) — lower is better ===\n")
    scen_names = [s.name for s in SCENARIOS]
    header = f"{'candidate':<22}" + "".join(f"{n:>14}" for n in scen_names) + "       worst"
    print(header)
    print("-" * len(header))
    by_cand_scen = {(r["candidate"], r["scenario"]): r["total_cost_aud"] for r in rows}
    for cand in CANDIDATES:
        cells: list[float] = [by_cand_scen[(cand.name, s.name)] for s in SCENARIOS]
        worst = max(cells)
        cell_strs = "".join(f"{c:>+14.2f}" for c in cells)
        print(f"{cand.name:<22}{cell_strs}   {worst:>+8.2f}")

    print("\n=== Min realised SOC across the run ===\n")
    print(header)
    print("-" * len(header))
    by_cand_scen_soc = {(r["candidate"], r["scenario"]): r["min_soc_pct"] for r in rows}
    for cand in CANDIDATES:
        cells = [by_cand_scen_soc[(cand.name, s.name)] for s in SCENARIOS]
        worst = min(cells)
        cell_strs = "".join(f"{c:>14.2f}" for c in cells)
        print(f"{cand.name:<22}{cell_strs}   {worst:>8.2f}")


if __name__ == "__main__":
    main()
