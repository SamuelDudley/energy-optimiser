"""CLI tool for replaying historical tick snapshots against a candidate LP config.

Usage:
    python -m optimiser.replay_cli --snapshots 'snapshots/2026-03-*.ndjson.gz' --config config.toml
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import load_config
from .replay import load_snapshots, replay, summarise_replay


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay historical ticks against a candidate LP configuration",
    )
    parser.add_argument(
        "--snapshots",
        "-s",
        required=True,
        help="Glob pattern for snapshot files (e.g. 'snapshots/2026-03-*.ndjson.gz')",
    )
    parser.add_argument(
        "--config",
        "-c",
        required=True,
        help="Config file for the candidate LP (battery, managed_loads, etc.)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output file for detailed results (NDJSON). Omit for summary only.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print each tick where the decision changed",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    print(f"Loading snapshots from: {args.snapshots}", file=sys.stderr)
    snapshots = load_snapshots(args.snapshots)

    results = []
    output_file = open(args.output, "w") if args.output else None

    try:
        for result in replay(
            snapshots,
            candidate_battery_config=config.battery,
            candidate_managed_loads=config.managed_loads,
        ):
            results.append(result)

            if output_file:
                output_file.write(
                    json.dumps(
                        {
                            "tick_id": result.tick_id,
                            "timestamp": result.timestamp.isoformat(),
                            "original": result.original_action.value,
                            "candidate": result.candidate_action.value,
                            "delta_cents": round(result.delta_cents, 2),
                            "original_reason": result.original_reason,
                            "candidate_reason": result.candidate_reason,
                            "solve_status": result.candidate_solve_status,
                            "solve_ms": result.candidate_solve_ms,
                        }
                    )
                    + "\n"
                )

            if args.verbose and result.candidate_action != result.original_action:
                print(
                    f"  {result.timestamp.isoformat()} "
                    f"{result.original_action.value:>15} → {result.candidate_action.value:<15} "
                    f"delta={result.delta_cents:+.2f}c  "
                    f"({result.candidate_reason})",
                    file=sys.stderr,
                )
    finally:
        if output_file:
            output_file.close()

    summary = summarise_replay(results)
    print("\n=== Replay Summary ===", file=sys.stderr)
    print(f"  Ticks replayed:    {summary['total_ticks']}", file=sys.stderr)
    if summary["total_ticks"] > 0:
        print(
            f"  Period:            {summary['first_tick']} → {summary['last_tick']}",
            file=sys.stderr,
        )
        print(
            f"  Changed decisions: {summary['changed_decisions']} ({summary['changed_pct']:.1f}%)",
            file=sys.stderr,
        )
        print(f"  Total delta:       ${summary['total_delta_aud']:+.2f} AUD", file=sys.stderr)
        print(f"  Avg per tick:      {summary['avg_delta_per_tick_cents']:+.3f}c", file=sys.stderr)
        failures = summary["candidate_solve_failures"]
        failure_pct = summary["candidate_solve_failure_pct"]
        print(
            f"  Solve failures:    {failures} ({failure_pct:.1f}%)",
            file=sys.stderr,
        )
        print(f"  Avg solve time:    {summary['avg_candidate_solve_ms']:.0f}ms", file=sys.stderr)

    # Print JSON summary to stdout for piping
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
