"""CLI for the terminal-value training-data generator.

Usage:
    uv run python -m optimiser.terminal_value_data_cli \\
        --snapshots '/var/lib/energy-optimiser/snapshots/2026-*.ndjson.gz' \\
        --config config.toml \\
        --out tv-training.ndjson.gz \\
        --cadence-minutes 30 \\
        --horizon-hours 24 \\
        --starting-socs 15,25,35,45,55,65,75,85
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .terminal_value_data import generate_rows, write_ndjson


def _parse_socs(raw: str) -> list[float]:
    return [float(x) for x in raw.split(",") if x.strip()]


def _parse_iso(s: str | None) -> datetime | None:
    if s is None:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshots", "-s", required=True,
                   help="Glob for snapshot files (NDJSON or NDJSON.gz).")
    p.add_argument("--config", "-c", required=True,
                   help="config.toml with the deployed BatteryConfig.")
    p.add_argument("--out", "-o", required=True,
                   help="Output NDJSON(.gz) file.")
    p.add_argument("--starting-socs",
                   default="15,25,35,45,55,65,75,85",
                   help="Comma-separated SOC%% to grid-search (default 8 values).")
    p.add_argument("--cadence-minutes", type=int, default=30,
                   help="Gap between anchor ticks (default 30).")
    p.add_argument("--horizon-hours", type=float, default=24.0,
                   help="Forward look for features + label window (default 24).")
    p.add_argument("--start-ts", default=None,
                   help="ISO-8601 lower bound on anchor tick (default: archive start).")
    p.add_argument("--end-ts", default=None,
                   help="ISO-8601 upper bound on anchor tick (default: archive end − horizon).")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = load_config(args.config)
    socs = _parse_socs(args.starting_socs)
    if not socs:
        print("--starting-socs is empty", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    rows = generate_rows(
        snapshots=args.snapshots,
        starting_socs=socs,
        battery_config=cfg.battery,
        cadence_minutes=args.cadence_minutes,
        horizon_hours=args.horizon_hours,
        start_ts=_parse_iso(args.start_ts),
        end_ts=_parse_iso(args.end_ts),
        progress=args.verbose,
    )
    n = write_ndjson(rows, out)
    print(f"Wrote {n} rows to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
