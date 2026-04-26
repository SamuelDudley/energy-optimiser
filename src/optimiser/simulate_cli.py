"""CLI for the closed-loop simulator. See `simulate.py` for the model.

Usage:
    # Replay one day on current production config
    python -m optimiser.simulate_cli \
        --snapshots '/var/lib/energy-optimiser/snapshots/2026-04-25.ndjson.gz' \
        --config config.toml

    # Compare candidate scenario weights against history
    python -m optimiser.simulate_cli \
        --snapshots '/var/lib/energy-optimiser/snapshots/2026-04-2*.ndjson.gz' \
        --config config.toml \
        --weights p10=0.40,p50=0.40,p90=0.20 \
        --label "p10-heavy"

    # Adverse scenario: PV under-delivers by 50%
    python -m optimiser.simulate_cli \
        --snapshots '...' --config config.toml \
        --pv-actual-mult 0.5 --label "cloudy-bust"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .simulate import ScenarioModifier, simulate


def _parse_weights(s: str | None) -> dict[str, float] | None:
    if not s:
        return None
    out: dict[str, float] = {}
    for kv in s.split(","):
        k, v = kv.split("=")
        out[k.strip()] = float(v.strip())
    return out


def main() -> None:
    p = argparse.ArgumentParser(
        description="Closed-loop multi-tick simulator for LP candidate evaluation",
    )
    p.add_argument("--snapshots", "-s", required=True)
    p.add_argument("--config", "-c", required=True)
    p.add_argument("--label", default="run")
    p.add_argument(
        "--initial-soc",
        type=float,
        default=None,
        help="Override starting SOC (default: first snapshot's SOC)",
    )
    p.add_argument(
        "--weights",
        default=None,
        help="Comma-sep stochastic scenario weights, e.g. 'p10=0.40,p50=0.40,p90=0.20'",
    )
    p.add_argument(
        "--wear-cost",
        type=float,
        default=None,
        help="Override one-way wear cost per kWh (default: lp.constants.WEAR_COST_PER_KWH)",
    )
    p.add_argument(
        "--floor",
        type=float,
        default=None,
        help="Override soc_floor_pct (default: from config)",
    )
    p.add_argument(
        "--ceiling",
        type=float,
        default=None,
        help="Override soc_ceiling_pct (default: from config)",
    )
    # Scenario modifiers — perturb the historical record
    p.add_argument("--pv-forecast-mult", type=float, default=1.0)
    p.add_argument("--pv-actual-mult", type=float, default=1.0)
    p.add_argument("--import-price-mult", type=float, default=1.0)
    p.add_argument("--export-price-mult", type=float, default=1.0)
    p.add_argument(
        "--output",
        "-o",
        default=None,
        help="Optional NDJSON dump of every step",
    )
    args = p.parse_args()

    cfg = load_config(args.config)
    bcfg = cfg.battery
    if args.floor is not None or args.ceiling is not None:
        import dataclasses as _dc
        overrides: dict = {}
        if args.floor is not None:
            overrides["soc_floor_pct"] = args.floor
        if args.ceiling is not None:
            overrides["soc_ceiling_pct"] = args.ceiling
        bcfg = _dc.replace(bcfg, **overrides)
    weights = _parse_weights(args.weights)
    mod = ScenarioModifier(
        pv_forecast_multiplier=args.pv_forecast_mult,
        actual_pv_multiplier=args.pv_actual_mult,
        import_price_multiplier=args.import_price_mult,
        export_price_multiplier=args.export_price_mult,
        name=args.label,
    )

    print(
        f"=== {args.label} ===  weights={weights or 'default'}  "
        f"pv_fcst×{args.pv_forecast_mult}  pv_act×{args.pv_actual_mult}  "
        f"ip×{args.import_price_mult}  ep×{args.export_price_mult}",
        file=sys.stderr,
    )

    def progress(done: int, total: int) -> None:
        print(f"  {done}/{total} steps  ({done/total*100:.0f}%)", end="\r", file=sys.stderr)

    result = simulate(
        snapshots=args.snapshots,
        battery_config=bcfg,
        scenario_weights=weights,
        wear_cost_per_kwh=args.wear_cost,
        modifier=mod,
        initial_soc_pct=args.initial_soc,
        progress=progress,
    )
    print(file=sys.stderr)

    if args.output:
        with open(args.output, "w") as f:
            for step in result.steps:
                f.write(
                    json.dumps(
                        {
                            "ts": step.ts.isoformat(),
                            "soc_start": round(step.soc_pct_start, 2),
                            "soc_end": round(step.soc_pct_end, 2),
                            "bat_kw": round(step.bat_kw, 3),
                            "grid_in_kw": round(step.grid_import_kw, 3),
                            "grid_out_kw": round(step.grid_export_kw, 3),
                            "pv_kw": round(step.pv_actual_kw, 3),
                            "house_kw": round(step.house_load_kw, 3),
                            "ip": round(step.import_price, 2),
                            "ep": round(step.export_price, 2),
                            "cost_c": round(step.cost_cents, 2),
                            "dispatch": step.dispatch_kind,
                            "solve_status": step.solve_status,
                        }
                    )
                    + "\n"
                )

    print(json.dumps({"label": args.label, **result.summary()}, indent=2))


if __name__ == "__main__":
    main()
