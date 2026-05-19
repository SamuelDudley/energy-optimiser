"""Load profiler with maturity levels and fallback chain.

Builds typical load curves from historical telemetry, bucketed by
temperature, occupancy, and day type. Falls back through progressively
broader queries when specific buckets have insufficient samples.
"""

from __future__ import annotations

import logging
from datetime import datetime

from .logging_utils import emit
from .store import TelemetryStore
from .time_utils import utc_to_local
from .types import EventType, LoadProfile

logger = logging.getLogger(__name__)

# L0 defaults.
#
# OCCUPIED was originally 2.0 kW — too high for this site's actual
# baseload. 48 h of telemetry at install showed weighted hour-of-day
# averages of 0.25-1.8 kW (morning HW-HP peak pulls the middle up;
# evening runs ~1.3 kW; overnight ~0.4 kW). 1.0 kW is a better ballpark
# for a small 2-3 person household with a heat-pump HWS. Only used at
# maturity level 0 (< 7 days of data); L1+ uses real historical data
# from the telemetry table, so this default sunsets automatically.
DEFAULT_OCCUPIED_KW = 1.0
DEFAULT_UNOCCUPIED_KW = 0.5


def assess_maturity(store: TelemetryStore) -> int:
    """Determine data maturity level from telemetry."""
    span_days = store.get_data_span_days()
    valid_rows = store.get_valid_load_rows()
    temp_buckets = store.get_temp_buckets_seen()

    if span_days < 7 or valid_rows < 1000:
        return 0
    if span_days < 30:
        return 1
    if span_days < 90 or temp_buckets < 3:
        return 2
    return 3


def temp_to_bucket(temp_c: float | None) -> str | None:
    """Convert a temperature to a bucket name."""
    if temp_c is None:
        return None
    if temp_c < 10:
        return "cold"
    if temp_c < 20:
        return "mild"
    if temp_c < 30:
        return "warm"
    return "hot"


def smooth_slots(slots: list[float], window: int) -> list[float]:
    """Apply a circular centred boxcar smoother to a 48-slot day profile.

    A sharp peak in any single slot — typically an artefact of
    averaging a rare event (kettle, oven, AC burst) that happened to
    land in that bin in the historical window — propagates into the LP
    as a reserve obligation every day. Smoothing across `window`
    neighbouring slots spreads that mass evenly, which models the real
    uncertainty about *when* such an event will recur. The smoother
    wraps around the day boundary because the profile represents a
    circular 24h cycle.

    Energy-preserving: `sum(smoothed) == sum(slots)` to floating-point
    tolerance, so the LP's expected daily consumption baseline doesn't
    drift as the smoothing window changes.

    `window` must be odd (centred) and >= 1. `window=1` is the
    identity.
    """
    if window <= 1:
        return list(slots)
    if window % 2 == 0:
        raise ValueError(f"smoothing window must be odd, got {window}")
    n = len(slots)
    if window > n:
        raise ValueError(f"smoothing window {window} exceeds slot count {n}")
    half = window // 2
    out: list[float] = []
    for i in range(n):
        total = 0.0
        for offset in range(-half, half + 1):
            total += slots[(i + offset) % n]
        out.append(total / window)
    return out


def build_load_profile(
    store: TelemetryStore,
    outdoor_temp_c: float | None = None,
    occupied: bool = True,
    timestamp: datetime | None = None,
    statistic: str = "mean",
    smoothing_slots: int = 1,
) -> LoadProfile:
    """Build a load profile with fallback chain.

    Fallback order (most specific → least specific):
    1. slot × temp_bucket × occupied × day_type
    2. slot × temp_bucket × occupied
    3. slot × occupied
    4. slot (flat average)
    5. constant default (L0)

    `statistic` ("mean" | "median") controls how the per-slot
    aggregation reduces ~90 days of telemetry into one value per slot;
    "median" is robust to single high-load days that would otherwise
    lift the LP's expected baseline.

    `smoothing_slots` applies a circular boxcar to the assembled
    profile — `1` is the identity, larger odd values redistribute peak
    mass across neighbouring slots. See `smooth_slots`.
    """
    # Validate eagerly so cold-start / fallback paths fail the same way.
    if statistic not in {"mean", "median"}:
        raise ValueError(f"unknown statistic {statistic!r}; expected 'mean' or 'median'")

    maturity = assess_maturity(store)

    if maturity == 0:
        # Cold start — no data
        kw = DEFAULT_OCCUPIED_KW if occupied else DEFAULT_UNOCCUPIED_KW
        return LoadProfile(
            slots=smooth_slots([kw] * 48, smoothing_slots),
            maturity_level=0,
            context="L0 default",
        )

    # Determine context
    temp_bucket = temp_to_bucket(outdoor_temp_c)
    is_weekday = True
    if timestamp:
        local = utc_to_local(timestamp)
        is_weekday = local.weekday() < 5

    # Try each fallback level
    fallbacks = [
        (
            temp_bucket,
            occupied,
            is_weekday,
            f"{temp_bucket}+{'occ' if occupied else 'away'}+{'wd' if is_weekday else 'we'}",
        ),
        (temp_bucket, occupied, None, f"{temp_bucket}+{'occ' if occupied else 'away'}"),
        (None, occupied, None, f"{'occ' if occupied else 'away'}"),
        (None, None, None, "flat_avg"),
    ]

    for tb, occ, wd, context in fallbacks:
        slots = store.get_load_profile_slots(
            temp_bucket=tb,
            occupied=occ,
            weekday=wd,
            as_of=timestamp,
            statistic=statistic,
        )
        if slots is not None:
            level = maturity
            if tb is None and occ is not None:
                level = min(maturity, 1)
            if occ is None:
                level = min(maturity, 1)
            if context != fallbacks[0][3]:
                emit(
                    EventType.PLANNER_FALLBACK,
                    {
                        "requested": fallbacks[0][3],
                        "actual": context,
                    },
                )
            return LoadProfile(
                slots=smooth_slots(slots, smoothing_slots),
                maturity_level=level,
                context=context,
            )

    # Ultimate fallback
    kw = DEFAULT_OCCUPIED_KW if occupied else DEFAULT_UNOCCUPIED_KW
    return LoadProfile(
        slots=smooth_slots([kw] * 48, smoothing_slots),
        maturity_level=0,
        context="L0 default (fallback)",
    )
