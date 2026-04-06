"""Time handling: NEM time, local time, UTC conversions.

NEM time is always UTC+10 (no DST). Canberra is UTC+10 (AEST) in winter
and UTC+11 (AEDT) in summer. All storage is in UTC.
"""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime

CANBERRA_TZ = zoneinfo.ZoneInfo("Australia/Canberra")
NEM_TZ = zoneinfo.ZoneInfo("Australia/Brisbane")  # UTC+10, no DST
UTC = UTC


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_local() -> datetime:
    return datetime.now(CANBERRA_TZ)


def nem_to_utc(nem_dt: datetime) -> datetime:
    """Convert NEM time (UTC+10) to UTC."""
    if nem_dt.tzinfo is None:
        nem_dt = nem_dt.replace(tzinfo=NEM_TZ)
    return nem_dt.astimezone(UTC)


def nem_to_local(nem_dt: datetime) -> datetime:
    """Convert NEM time to Canberra local time."""
    utc = nem_to_utc(nem_dt)
    return utc.astimezone(CANBERRA_TZ)


def local_to_utc(local_dt: datetime) -> datetime:
    """Convert Canberra local time to UTC."""
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=CANBERRA_TZ)
    return local_dt.astimezone(UTC)


def utc_to_local(utc_dt: datetime) -> datetime:
    """Convert UTC to Canberra local time."""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=UTC)
    return utc_dt.astimezone(CANBERRA_TZ)


def local_hour_to_utc_hour(local_hour: int, reference_date: datetime | None = None) -> int:
    """Convert a local hour (e.g. 17 for 5pm) to UTC hour for today.

    This changes with DST — 17:00 AEST = 07:00 UTC, but 17:00 AEDT = 06:00 UTC.
    """
    if reference_date is None:
        reference_date = now_local()
    local_dt = reference_date.replace(hour=local_hour, minute=0, second=0, microsecond=0)
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=CANBERRA_TZ)
    return local_dt.astimezone(UTC).hour


def snap_to_interval(dt: datetime, interval_minutes: int = 5) -> datetime:
    """Snap a datetime to the nearest interval boundary."""
    total_minutes = dt.hour * 60 + dt.minute
    snapped = (total_minutes // interval_minutes) * interval_minutes
    return dt.replace(
        hour=snapped // 60,
        minute=snapped % 60,
        second=0,
        microsecond=0,
    )


def slot_index(dt: datetime) -> int:
    """Convert a datetime to a 30-minute slot index (0–47)."""
    local = utc_to_local(dt) if dt.tzinfo == UTC else dt
    return local.hour * 2 + local.minute // 30


def parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 datetime string to UTC."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
