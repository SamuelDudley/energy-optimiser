"""Calibrate Amber's `advancedPrice.{low,predicted,high}` bands against
realised wholesale prices.

Joins ForecastInterval rows in `price_forecast_log` to their eventual
ActualInterval row (same `interval_start`) and computes per channel:

    - **Hit-rate**: how often `realised ∈ [low, high]`. ~66% would mark
      a 1σ-ish band; ~95% a 2σ; <50% suggests the band is poorly
      calibrated and stochastic scenarios built from it would add
      complexity without robustness gain.
    - **MAE of `predicted`**: mean absolute error of Amber's central
      forecast against the realised wholesale price. Compared against
      the LP's wear cost (2.5 c/kWh round-trip) tells us whether the
      forecast noise is large enough to matter for slot-0 decisions.
    - **Asymmetric breach rate**: realised-above-`high` vs realised-
      below-`low`. A persistent skew indicates Amber's band is
      systematically biased — useful for deciding whether to clip the
      band before turning it into LP scenarios.

Stratified by **lookahead bucket** (0–6 h, 6–12 h, 12–24 h, 24–36 h)
because forecast accuracy degrades non-linearly with horizon: the LP
is most exposed to errors in the 0–6 h band where most slot-0
decisions are made.

Also reports the **cross-channel residual correlation**: are import
and export forecast errors moving together (NEM-coupled) or
independently? Tells us whether `CROSS` mode (3×3 import × export grid)
is over-hedging by including physically-implausible combinations
that an empirically-correlated SHARED mode would skip.

This module is pure analysis — no LP, no inverter, no service state.
Runs against a snapshot of `telemetry.duckdb` taken via the snapshot-
and-query pattern in CLAUDE.md (the live service holds the lock).

Usage
-----
    python -m optimiser.analysis.price_band_calibration \\
        --db /tmp/telemetry-snapshot.duckdb \\
        --since 2026-04-28T00:00:00Z

Output is a markdown report on stdout. Pipe to a file for archiving.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

# Lookahead buckets — hours between fetch and interval_start. Tuples
# are (label, lower_inclusive, upper_exclusive).
LOOKAHEAD_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("0-6h",  0.0,  6.0),
    ("6-12h", 6.0, 12.0),
    ("12-24h", 12.0, 24.0),
    ("24-36h", 24.0, 36.0),
)


@dataclass(frozen=True, slots=True)
class CalibrationStats:
    """Per-channel, per-lookahead-bucket calibration metrics."""

    channel: str  # "import" | "export"
    bucket: str
    n: int
    mae_predicted: float | None  # cents/kWh
    within_band_pct: float | None  # 0.0–100.0
    above_high_pct: float | None  # asymmetric overshoot
    below_low_pct: float | None  # asymmetric undershoot

    def format_row(self) -> str:
        def _f(x: float | None, *, suffix: str = "") -> str:
            return "—" if x is None else f"{x:6.2f}{suffix}"

        return (
            f"| {self.channel:6s} | {self.bucket:6s} | {self.n:5d} | "
            f"{_f(self.mae_predicted, suffix=' c/kWh')} | "
            f"{_f(self.within_band_pct, suffix='%')} | "
            f"{_f(self.above_high_pct, suffix='%')} | "
            f"{_f(self.below_low_pct, suffix='%')} |"
        )


@dataclass(frozen=True, slots=True)
class CrossChannelCorrelation:
    """Pearson correlation between import and export forecast residuals
    (realised − predicted), measured slot-by-slot. Computed only on
    rows where both channels' predicted are populated.

    Interpretation:
      ≈  +1.0   highly NEM-coupled — SHARED mode is the right
                composition; CROSS will over-hedge tail combos.
      ≈   0.0   independent — CROSS is the honest composition.
      ≈  -1.0   anti-correlated (unlikely) — neither composition is
                obviously right; a custom 5-point distribution might
                outperform either.
    """

    n: int
    correlation: float | None  # None when n < 2 or variance is 0


# ── SQL builders ─────────────────────────────────────────────────


def _channel_query(
    channel: str,
    bucket_low_h: float,
    bucket_high_h: float,
    since_iso: str | None,
) -> str:
    """Builds the bucket-stratified calibration query for one channel.

    ``channel`` selects the field set:
        "import" → forecast_low/predicted/high vs per_kwh
        "export" → export_forecast_low/predicted/high vs export_per_kwh

    The forecast row chosen for each interval is the *earliest* fetch
    falling inside the bucket — closer to "what would the LP have seen
    when planning slot 0 at that lookahead?" than an averaged value.
    """
    if channel == "import":
        f_low, f_pred, f_high, realised_field = (
            "forecast_low", "forecast_predicted", "forecast_high", "per_kwh",
        )
    elif channel == "export":
        f_low, f_pred, f_high, realised_field = (
            "export_forecast_low", "export_forecast_predicted",
            "export_forecast_high", "export_per_kwh",
        )
    else:
        raise ValueError(f"unknown channel: {channel}")

    since_clause = (
        f"AND interval_start >= TIMESTAMPTZ '{since_iso}'"
        if since_iso else ""
    )

    # Two-step: pick one forecast row per (interval_start, bucket) — the
    # earliest fetch within the lookahead range — then join to the
    # ActualInterval realised price. Forecasts where predicted is NULL
    # get excluded; settled intervals don't carry advancedPrice and
    # can't contribute calibration evidence.
    return f"""
        WITH forecasts_in_bucket AS (
            SELECT
                interval_start,
                {f_low}    AS f_low,
                {f_pred}   AS f_pred,
                {f_high}   AS f_high,
                fetched_at,
                ROW_NUMBER() OVER (
                    PARTITION BY interval_start
                    ORDER BY fetched_at ASC
                ) AS rn
            FROM price_forecast_log
            WHERE interval_type = 'ForecastInterval'
              AND {f_pred} IS NOT NULL
              AND {f_low}  IS NOT NULL
              AND {f_high} IS NOT NULL
              AND date_diff('second', fetched_at, interval_start) >= {int(bucket_low_h * 3600)}
              AND date_diff('second', fetched_at, interval_start) <  {int(bucket_high_h * 3600)}
              {since_clause}
        ),
        realised AS (
            SELECT
                interval_start,
                {realised_field} AS realised
            FROM price_forecast_log
            WHERE interval_type = 'ActualInterval'
              AND {realised_field} IS NOT NULL
              {since_clause}
        )
        SELECT
            f.f_low, f.f_pred, f.f_high, r.realised
        FROM forecasts_in_bucket f
        JOIN realised r USING (interval_start)
        WHERE f.rn = 1
    """


def _correlation_query(since_iso: str | None) -> str:
    """Pulls aligned (interval_start, predicted, realised) pairs for
    BOTH channels — same forecast row constraint — for cross-channel
    residual correlation."""
    since_clause = (
        f"AND interval_start >= TIMESTAMPTZ '{since_iso}'"
        if since_iso else ""
    )
    return f"""
        WITH per_interval AS (
            SELECT
                interval_start,
                FIRST(forecast_predicted        ORDER BY fetched_at ASC) AS imp_pred,
                FIRST(export_forecast_predicted ORDER BY fetched_at ASC) AS exp_pred
            FROM price_forecast_log
            WHERE interval_type = 'ForecastInterval'
              AND forecast_predicted        IS NOT NULL
              AND export_forecast_predicted IS NOT NULL
              {since_clause}
            GROUP BY interval_start
        ),
        realised AS (
            SELECT
                interval_start,
                per_kwh        AS imp_real,
                export_per_kwh AS exp_real
            FROM price_forecast_log
            WHERE interval_type = 'ActualInterval'
              AND per_kwh        IS NOT NULL
              AND export_per_kwh IS NOT NULL
              {since_clause}
        )
        SELECT
            imp_real - imp_pred AS imp_residual,
            exp_real - exp_pred AS exp_residual
        FROM per_interval JOIN realised USING (interval_start)
    """


# ── Computation ──────────────────────────────────────────────────


def compute_calibration(
    conn: duckdb.DuckDBPyConnection,
    *,
    since: datetime | None = None,
) -> tuple[list[CalibrationStats], CrossChannelCorrelation]:
    """Run the calibration queries and assemble the stats list."""
    since_iso = since.astimezone(UTC).isoformat() if since else None

    rows: list[CalibrationStats] = []
    for channel in ("import", "export"):
        for label, low_h, high_h in LOOKAHEAD_BUCKETS:
            sql = _channel_query(channel, low_h, high_h, since_iso)
            results = conn.execute(sql).fetchall()
            rows.append(_stats_from_rows(channel, label, results))

    corr_results = conn.execute(_correlation_query(since_iso)).fetchall()
    correlation = _correlation_from_rows(corr_results)

    return rows, correlation


def _stats_from_rows(
    channel: str, bucket: str, rows: list[tuple]
) -> CalibrationStats:
    n = len(rows)
    if n == 0:
        return CalibrationStats(
            channel=channel, bucket=bucket, n=0,
            mae_predicted=None, within_band_pct=None,
            above_high_pct=None, below_low_pct=None,
        )
    abs_err_sum = 0.0
    in_band = 0
    above_high = 0
    below_low = 0
    for f_low, f_pred, f_high, realised in rows:
        abs_err_sum += abs(realised - f_pred)
        if realised > f_high:
            above_high += 1
        elif realised < f_low:
            below_low += 1
        else:
            in_band += 1
    return CalibrationStats(
        channel=channel,
        bucket=bucket,
        n=n,
        mae_predicted=abs_err_sum / n,
        within_band_pct=100.0 * in_band / n,
        above_high_pct=100.0 * above_high / n,
        below_low_pct=100.0 * below_low / n,
    )


def _correlation_from_rows(rows: list[tuple]) -> CrossChannelCorrelation:
    n = len(rows)
    if n < 2:
        return CrossChannelCorrelation(n=n, correlation=None)

    imp_residuals = [r[0] for r in rows]
    exp_residuals = [r[1] for r in rows]
    imp_mean = sum(imp_residuals) / n
    exp_mean = sum(exp_residuals) / n
    cov = sum(
        (i - imp_mean) * (e - exp_mean)
        for i, e in zip(imp_residuals, exp_residuals, strict=True)
    ) / n
    imp_var = sum((i - imp_mean) ** 2 for i in imp_residuals) / n
    exp_var = sum((e - exp_mean) ** 2 for e in exp_residuals) / n
    if imp_var == 0.0 or exp_var == 0.0:
        return CrossChannelCorrelation(n=n, correlation=None)
    correlation = cov / ((imp_var ** 0.5) * (exp_var ** 0.5))
    return CrossChannelCorrelation(n=n, correlation=correlation)


# ── Reporting ────────────────────────────────────────────────────


def render_report(
    stats: list[CalibrationStats],
    correlation: CrossChannelCorrelation,
    *,
    since: datetime | None = None,
) -> str:
    """Format calibration results as a markdown report.

    Stable structure: title, query window, calibration table, cross-
    channel correlation, and a one-paragraph interpretation guide.
    Stable enough that an operator running this weekly can diff
    successive reports without parsing them.
    """
    since_str = since.astimezone(UTC).isoformat() if since else "(all data)"
    lines = [
        "# Amber price-band calibration report",
        "",
        f"_Window: from {since_str}_",
        "",
        "## Per-channel calibration",
        "",
        "| chan   | bucket | n     | MAE(predicted) | in-band  | above high | below low |",
        "|--------|--------|-------|----------------|----------|------------|-----------|",
        *[s.format_row() for s in stats],
        "",
        "## Cross-channel residual correlation",
        "",
        f"- n  = {correlation.n}",
        (
            f"- ρ  = {correlation.correlation:.3f}"
            if correlation.correlation is not None
            else "- ρ  = (insufficient data — variance zero or n<2)"
        ),
        "",
        "## Interpretation",
        "",
        "- **in-band ≈ 66%**: ~1σ band; ~95% would be ~2σ. Below 50% the band is",
        "  poorly calibrated and stochastic scenarios from it add complexity",
        "  without robustness gain.",
        "- **MAE(predicted) > wear cost (2.5 c/kWh)**: forecast noise is large",
        "  enough to flip slot-0 decisions; price scenarios are likely worth it.",
        "- **above-high vs below-low skew**: persistent imbalance ⇒ Amber's",
        "  band is biased; clip before turning it into scenarios.",
        "- **ρ ≈ +1**: import + export errors move together (NEM-coupled) ⇒",
        "  prefer SHARED mode over CROSS.",
        "- **ρ ≈ 0**: errors independent ⇒ CROSS is the honest composition.",
    ]
    return "\n".join(lines) + "\n"


# ── CLI ──────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="optimiser.analysis.price_band_calibration",
        description=(
            "Run Amber price-band calibration against the local "
            "price_forecast_log table. Read-only."
        ),
    )
    p.add_argument(
        "--db",
        type=Path,
        default=Path("/tmp/telemetry-snapshot.duckdb"),
        help=(
            "Path to a snapshot of telemetry.duckdb. Use the snapshot-"
            "and-query pattern from CLAUDE.md to copy the live DB "
            "before running this; the running service holds the file "
            "lock. Default: /tmp/telemetry-snapshot.duckdb"
        ),
    )
    p.add_argument(
        "--since",
        type=str,
        default=None,
        help=(
            "ISO datetime — restrict the analysis to forecasts and "
            "actuals at or after this time. Useful to filter out the "
            "pre-2026-04-28 rows that have NULL on the export-side "
            "advancedPrice fields."
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    since = (
        datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        if args.since else None
    )
    if not args.db.exists():
        print(
            f"error: db not found at {args.db}. "
            "Snapshot the live telemetry.duckdb (and its .wal sidecar) "
            "first — see CLAUDE.md.",
            file=sys.stderr,
        )
        return 2
    with duckdb.connect(str(args.db), read_only=True) as conn:
        stats, correlation = compute_calibration(conn, since=since)
    print(render_report(stats, correlation, since=since))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
