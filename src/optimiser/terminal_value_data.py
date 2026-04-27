"""Terminal-value training-data generator.

Produces (features, label) rows for fitting a PV-aware terminal value
function `V(SOC, state)` that would replace the constant
`TERMINAL_SOC_FLOOR_PCT = 20.0` in lp/constants.py.

For each anchor tick T at a chosen cadence over the snapshot archive,
and for each candidate starting SOC:

  features = forward-looking summaries computed from the snapshot at T
             (next-24h Solcast P10/P50 PV, house-load forecast,
             min/max import/export prices)
  label    = realised cost from running the closed-loop simulator
             from T → T+horizon with `initial_soc_pct = candidate`,
             evolving SOC under the realised (post-modifier=identity)
             prices/PV/load.

One NDJSON row per (T, starting_soc). The labels are counterfactual:
each candidate starting SOC produces a different SOC trajectory and a
different realised cost, even though the world (prices, PV, load)
is the same.

Why this exists: the operational SOC floor is a hard 15%, applied
every slot. The TERMINAL floor is a separate per-tick constraint at
the LAST slot of the LP horizon — currently 20% as a constant.
A PV-aware function `V` would let the LP end the horizon LOWER on
days where the next-24h PV forecast is strong (cheap to refill) and
HIGHER on days where it's weak — capturing more arbitrage upside
without sacrificing the safety margin into expensive unpriced tails.

This module just generates the training data. Fitting V (likely a
PWL-in-SOC form so it embeds cheaply in the LP objective) is a later
step. Validation is via the same `simulate.py` machinery.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import logging
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import BatteryConfig
from .simulate import (
    _load_indexed_snapshots,
    _nearest_snapshot,
    simulate,
)
from .types import LoadProfile, PriceInterval, PVForecast, TickSnapshot

logger = logging.getLogger(__name__)

# NEM is always UTC+10. Local Canberra time is UTC+10 (AEST) in winter,
# UTC+11 (AEDT) in summer — but we want a stable feature, not a
# DST-jumpy one. Use NEM time for hour-of-day feature.
_NEM_OFFSET = timedelta(hours=10)


# ── Row schema ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TrainingRow:
    """One (features, label) row.

    Features are forward-looking from anchor `timestamp` over a 24h
    (or `horizon_hours`) window. Label is realised cost from running
    the closed-loop simulator over the same window starting at
    `soc_pct_terminal`.
    """

    # Anchor + candidate SOC
    timestamp: str                          # ISO-8601 UTC
    soc_pct_terminal: float                 # candidate starting SOC at T

    # Time-of-day / season features (NEM time, DST-stable)
    hour_of_day_nem: float                  # 0.0–24.0
    day_of_week_nem: int                    # 0=Mon, 6=Sun
    month: int                              # 1–12

    # Forward-looking PV (kWh integrated over horizon)
    horizon_pv_p50_kwh: float
    horizon_pv_p10_kwh: float
    horizon_pv_p90_kwh: float

    # Forward-looking house load
    horizon_house_load_kwh: float

    # Price extremes over horizon (cents/kWh)
    horizon_min_import_c: float
    horizon_max_import_c: float
    horizon_mean_import_c: float
    horizon_max_export_c: float
    horizon_mean_export_c: float

    # Label
    realised_cost_cents: float

    # Provenance — refuse to mix versions when the schema drifts
    snapshot_version: str
    horizon_hours: float
    cadence_minutes: float
    sim_solve_failures: int
    sim_n_steps: int


# ── Feature extraction ───────────────────────────────────────────


def _slot_kwh(forecast: PVForecast, horizon_end: datetime, key: str) -> float:
    """Return the kWh contribution of one PV forecast slot inside the
    [start, horizon_end) window. Forecast slot extends [start, end);
    we clip at horizon_end and use the slot's per-slot kW (treated as
    average power over the slot)."""
    end = min(forecast.end, horizon_end)
    if end <= forecast.start:
        return 0.0
    duration_h = (end - forecast.start).total_seconds() / 3600.0
    kw = getattr(forecast, key)
    return kw * duration_h


def _price_kwh_window(
    prices: list[PriceInterval],
    anchor: datetime,
    horizon_end: datetime,
) -> tuple[float, float, float, float, float]:
    """Compute (min_import, max_import, mean_import, max_export, mean_export)
    over the horizon. Means are duration-weighted across slots that
    overlap the window. Returns (NaN, ...) sentinels if no overlap."""
    imports: list[tuple[float, float]] = []  # (price, weight_h)
    exports: list[tuple[float, float]] = []
    for p in prices:
        start = max(p.start, anchor)
        end = min(p.end, horizon_end)
        if end <= start:
            continue
        h = (end - start).total_seconds() / 3600.0
        imports.append((p.import_per_kwh, h))
        exports.append((p.export_per_kwh, h))
    if not imports:
        nan = float("nan")
        return nan, nan, nan, nan, nan
    total_h = sum(h for _, h in imports)
    min_imp = min(p for p, _ in imports)
    max_imp = max(p for p, _ in imports)
    mean_imp = sum(p * h for p, h in imports) / total_h
    max_exp = max(p for p, _ in exports)
    mean_exp = sum(p * h for p, h in exports) / total_h
    return min_imp, max_imp, mean_imp, max_exp, mean_exp


def _load_profile_kwh(
    profile: LoadProfile,
    anchor: datetime,
    horizon_end: datetime,
) -> float:
    """Sum the load profile over the horizon. LoadProfile slots are
    30-min averages aligned to the half-hour boundary at or before
    `anchor`. We sum contributions at slot resolution.

    Falls back to a simple "1 kW × horizon" if no profile available —
    the caller can ignore rows where the profile was missing."""
    if not profile or not profile.slots:
        # Fixture-quality fallback. House load ~1 kW continuous is a
        # decent prior for a Canberra household in shoulder season.
        return 1.0 * (horizon_end - anchor).total_seconds() / 3600.0
    # Slot 0 starts at the half-hour boundary preceding anchor.
    slot_dt = timedelta(minutes=30)
    slot_start = anchor.replace(minute=(anchor.minute // 30) * 30, second=0, microsecond=0)
    total_kwh = 0.0
    cursor = slot_start
    i = 0
    while cursor < horizon_end and i < len(profile.slots):
        slot_end = cursor + slot_dt
        overlap_start = max(cursor, anchor)
        overlap_end = min(slot_end, horizon_end)
        if overlap_end > overlap_start:
            h = (overlap_end - overlap_start).total_seconds() / 3600.0
            total_kwh += profile.slots[i] * h
        cursor = slot_end
        i += 1
    return total_kwh


def _extract_features(
    snap: TickSnapshot,
    anchor: datetime,
    horizon_hours: float,
) -> dict:
    """Pull forward-looking features from a snapshot taken at `anchor`."""
    horizon_end = anchor + timedelta(hours=horizon_hours)

    # PV — Solcast P10/P50/P90 over horizon
    pv_p10_kwh = 0.0
    pv_p50_kwh = 0.0
    pv_p90_kwh = 0.0
    for f in snap.pv_forecast or []:
        if f.end <= anchor or f.start >= horizon_end:
            continue
        f_clip_start = max(f.start, anchor)
        f_clip_end = min(f.end, horizon_end)
        h = (f_clip_end - f_clip_start).total_seconds() / 3600.0
        pv_p10_kwh += f.pv_estimate10_kw * h
        pv_p50_kwh += f.pv_estimate_kw * h
        pv_p90_kwh += f.pv_estimate90_kw * h

    # Prices — min/max/mean over horizon
    min_imp, max_imp, mean_imp, max_exp, mean_exp = _price_kwh_window(
        snap.price_forecast, anchor, horizon_end
    )

    # House load — integrate the load profile
    house_kwh = _load_profile_kwh(snap.load_profile, anchor, horizon_end)

    # NEM time of day for stable hour/dow features
    nem = anchor + _NEM_OFFSET
    hour_of_day = nem.hour + nem.minute / 60.0

    return {
        "hour_of_day_nem": hour_of_day,
        "day_of_week_nem": nem.weekday(),
        "month": anchor.month,
        "horizon_pv_p50_kwh": pv_p50_kwh,
        "horizon_pv_p10_kwh": pv_p10_kwh,
        "horizon_pv_p90_kwh": pv_p90_kwh,
        "horizon_house_load_kwh": house_kwh,
        "horizon_min_import_c": min_imp,
        "horizon_max_import_c": max_imp,
        "horizon_mean_import_c": mean_imp,
        "horizon_max_export_c": max_exp,
        "horizon_mean_export_c": mean_exp,
    }


# ── Row generation ───────────────────────────────────────────────


def generate_rows(
    *,
    snapshots: str | list[Path],
    starting_socs: list[float],
    battery_config: BatteryConfig,
    cadence_minutes: int = 30,
    horizon_hours: float = 24.0,
    start_ts: datetime | None = None,
    end_ts: datetime | None = None,
    progress: bool = False,
) -> Iterator[TrainingRow]:
    """Walk anchor ticks at `cadence_minutes` cadence; for each, run
    one closed-loop simulation per candidate `soc` in `starting_socs`
    and emit a `TrainingRow`.

    Args:
        snapshots: glob string or explicit list of NDJSON(.gz) paths.
        starting_socs: candidate terminal SOCs to evaluate (% units).
        battery_config: BatteryConfig for the LP solver.
        cadence_minutes: gap between anchor ticks. 30 min keeps the
            row count manageable and is finer than the half-hour
            price grid.
        horizon_hours: how far forward to look for both features and
            label window. Default 24h matches a real terminal-value
            decision (the LP plans 48h, but the terminal slot is at
            ~hour 36–48; 24h beyond the anchor is a good proxy for
            "what's in the unpriced tail").
        start_ts, end_ts: optional bounds. Default: full range minus
            horizon (anchor must have horizon_hours of data after it).
    """
    if isinstance(snapshots, str):
        base = Path(snapshots).parent
        pattern = Path(snapshots).name
        paths = sorted(base.glob(pattern))
    else:
        paths = list(snapshots)
    if not paths:
        raise ValueError(f"No snapshot files matched {snapshots!r}")

    # Load the index ONCE and reuse across every simulate() call. The
    # archive can be ~30 MB across many days; re-parsing per anchor ×
    # SOC blew dominant runtime in the smoke test.
    index = _load_indexed_snapshots(paths)
    if not index:
        raise ValueError(f"No snapshots loaded from {paths}")
    sorted_ts = sorted(index.keys())
    archive_start = sorted_ts[0]
    archive_end = sorted_ts[-1]

    horizon = timedelta(hours=horizon_hours)
    cadence = timedelta(minutes=cadence_minutes)

    walk_start = max(start_ts or archive_start, archive_start)
    walk_end = min(end_ts or archive_end, archive_end - horizon)

    n_emitted = 0
    n_anchors = 0
    t = walk_start
    while t <= walk_end:
        snap = _nearest_snapshot(index, t)
        if snap is None:
            t += cadence
            continue
        if not snap.price_forecast:
            t += cadence
            continue
        features = _extract_features(snap, t, horizon_hours)
        n_anchors += 1
        for soc in starting_socs:
            result = simulate(
                snapshot_index=index,
                battery_config=battery_config,
                initial_soc_pct=soc,
                start_ts=t,
                end_ts=t + horizon,
            )
            row = TrainingRow(
                timestamp=t.astimezone(timezone.utc).isoformat(),
                soc_pct_terminal=soc,
                **features,
                realised_cost_cents=round(result.total_cost_cents, 4),
                snapshot_version=snap.version,
                horizon_hours=horizon_hours,
                cadence_minutes=cadence_minutes,
                sim_solve_failures=result.n_solve_failures,
                sim_n_steps=len(result.steps),
            )
            yield row
            n_emitted += 1
            if progress:
                logger.info(
                    "TV row %d (anchor=%s, soc=%.1f) cost=%.1fc fails=%d",
                    n_emitted,
                    t.isoformat(),
                    soc,
                    result.total_cost_cents,
                    result.n_solve_failures,
                )
        t += cadence

    if progress:
        logger.info("Done: %d anchors × %d SOCs = %d rows",
                    n_anchors, len(starting_socs), n_emitted)


# ── Output writers ───────────────────────────────────────────────


def write_ndjson(rows: Iterator[TrainingRow], path: Path) -> int:
    """Stream rows to NDJSON (or NDJSON.gz). Returns row count."""
    opener = gzip.open if path.suffix == ".gz" else open
    n = 0
    with opener(path, "wt", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(asdict(row)) + "\n")
            n += 1
    return n
