"""Tests for time handling — covers spec §6.8 and §9.10."""

from __future__ import annotations

from datetime import datetime

from optimiser.time_utils import (
    CANBERRA_TZ,
    NEM_TZ,
    UTC,
    local_hour_to_utc_hour,
    nem_to_local,
    nem_to_utc,
    parse_iso,
    slot_index,
    snap_to_interval,
)


class TestNEMConversions:
    """NEM time is always UTC+10. Canberra observes DST."""

    def test_nem_to_utc(self) -> None:
        # NEM 17:30 = UTC 07:30
        nem = datetime(2026, 4, 3, 17, 30, 0, tzinfo=NEM_TZ)
        utc = nem_to_utc(nem)
        assert utc.hour == 7
        assert utc.minute == 30
        assert utc.tzinfo == UTC

    def test_nem_to_local_winter(self) -> None:
        # Winter (AEST = UTC+10, same as NEM): NEM 17:00 = local 17:00
        nem = datetime(2026, 7, 1, 17, 0, 0, tzinfo=NEM_TZ)
        local = nem_to_local(nem)
        assert local.hour == 17

    def test_nem_to_local_summer(self) -> None:
        # Summer (AEDT = UTC+11): NEM 17:00 = local 18:00
        nem = datetime(2026, 1, 15, 17, 0, 0, tzinfo=NEM_TZ)
        local = nem_to_local(nem)
        assert local.hour == 18


class TestLocalHourConversion:
    """Evening reserve hours are in local time."""

    def test_winter_local_17_to_utc(self) -> None:
        # Winter: 17:00 AEST = 07:00 UTC
        ref = datetime(2026, 7, 1, 12, 0, tzinfo=CANBERRA_TZ)
        utc_hour = local_hour_to_utc_hour(17, ref)
        assert utc_hour == 7

    def test_summer_local_17_to_utc(self) -> None:
        # Summer: 17:00 AEDT = 06:00 UTC
        ref = datetime(2026, 1, 15, 12, 0, tzinfo=CANBERRA_TZ)
        utc_hour = local_hour_to_utc_hour(17, ref)
        assert utc_hour == 6


class TestParseISO:
    """ISO 8601 parsing to UTC."""

    def test_parse_with_offset(self) -> None:
        dt = parse_iso("2026-04-03T17:30:00+10:00")
        assert dt.tzinfo == UTC
        assert dt.hour == 7

    def test_parse_utc_z(self) -> None:
        dt = parse_iso("2026-04-03T07:30:00Z")
        assert dt.hour == 7

    def test_parse_naive_assumes_utc(self) -> None:
        dt = parse_iso("2026-04-03T07:30:00")
        assert dt.tzinfo == UTC


class TestSnapToInterval:
    def test_snap_5min(self) -> None:
        dt = datetime(2026, 4, 3, 7, 23, 45, tzinfo=UTC)
        snapped = snap_to_interval(dt, 5)
        assert snapped.minute == 20
        assert snapped.second == 0

    def test_snap_30min(self) -> None:
        dt = datetime(2026, 4, 3, 7, 45, 0, tzinfo=UTC)
        snapped = snap_to_interval(dt, 30)
        assert snapped.minute == 30


class TestSlotIndex:
    def test_midnight(self) -> None:
        # July 1 is AEST (UTC+10): UTC 14:00 = AEST 00:00
        dt = datetime(2026, 7, 1, 14, 0, 0, tzinfo=UTC)
        assert slot_index(dt) == 0

    def test_noon(self) -> None:
        # July 1 is AEST (UTC+10): UTC 02:00 = AEST 12:00
        dt = datetime(2026, 7, 1, 2, 0, 0, tzinfo=UTC)
        assert slot_index(dt) == 24
