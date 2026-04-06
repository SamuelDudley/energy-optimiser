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

# L0 defaults
DEFAULT_OCCUPIED_KW = 2.0
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


def build_load_profile(
    store: TelemetryStore,
    outdoor_temp_c: float | None = None,
    occupied: bool = True,
    timestamp: datetime | None = None,
) -> LoadProfile:
    """Build a load profile with fallback chain.

    Fallback order (most specific → least specific):
    1. slot × temp_bucket × occupied × day_type
    2. slot × temp_bucket × occupied
    3. slot × occupied
    4. slot (flat average)
    5. constant default (L0)
    """
    maturity = assess_maturity(store)

    if maturity == 0:
        # Cold start — no data
        kw = DEFAULT_OCCUPIED_KW if occupied else DEFAULT_UNOCCUPIED_KW
        return LoadProfile(
            slots=[kw] * 48,
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
                slots=slots,
                maturity_level=level,
                context=context,
            )

    # Ultimate fallback
    kw = DEFAULT_OCCUPIED_KW if occupied else DEFAULT_UNOCCUPIED_KW
    return LoadProfile(
        slots=[kw] * 48,
        maturity_level=0,
        context="L0 default (fallback)",
    )
