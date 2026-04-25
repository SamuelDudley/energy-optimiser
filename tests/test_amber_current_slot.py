"""Tests for AmberClient.current_5min_price — time-based slot lookup.

The cached `last_5min_prices` list contains `previous + current + next`
intervals, and may go stale after a poll failure. Callers must select
the slot covering "now" by time, not by index — `[0]` would smuggle in
a previous interval (or, if stale, a slot from minutes ago) and silently
report a wrong "current price".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from optimiser.clients.amber import AmberClient
from optimiser.config import AmberConfig
from optimiser.types import PriceInterval


def _interval(start: datetime, end: datetime, *, exp: float = 0.0) -> PriceInterval:
    return PriceInterval(
        start=start,
        end=end,
        import_per_kwh=20.0,
        export_per_kwh=exp,
        spot_per_kwh=5.0,
        renewables_pct=80.0,
        spike_status="none",
        descriptor="veryLow",
    )


def _client_with_prices(prices: list[PriceInterval]) -> AmberClient:
    c = AmberClient(AmberConfig(api_key="k", site_id="s"))
    c._last_5min_prices = prices
    return c


class TestCurrent5MinPrice:
    def test_returns_interval_covering_now_not_index_zero(self) -> None:
        # Amber returned previous=2 + current + next=2. Index 0 is
        # 10 min ago; index 2 is the current slot.
        base = datetime(2026, 4, 25, 13, 25, tzinfo=UTC)
        slots = [
            _interval(base, base + timedelta(minutes=5), exp=10.0),       # 13:25
            _interval(base + timedelta(minutes=5), base + timedelta(minutes=10), exp=20.0),  # 13:30
            _interval(base + timedelta(minutes=10), base + timedelta(minutes=15), exp=30.0), # 13:35
            _interval(base + timedelta(minutes=15), base + timedelta(minutes=20), exp=40.0), # 13:40
        ]
        client = _client_with_prices(slots)

        # Wall-clock at 13:36 — current slot is 13:35-13:40 (idx 2)
        now = datetime(2026, 4, 25, 13, 36, tzinfo=UTC)
        cur = client.current_5min_price(now)
        assert cur is not None
        assert cur.export_per_kwh == 30.0

    def test_boundary_inclusive_at_start_exclusive_at_end(self) -> None:
        base = datetime(2026, 4, 25, 13, 35, tzinfo=UTC)
        slots = [
            _interval(base, base + timedelta(minutes=5), exp=30.0),
            _interval(base + timedelta(minutes=5), base + timedelta(minutes=10), exp=40.0),
        ]
        client = _client_with_prices(slots)

        # Exactly on start of slot 0 → in slot 0
        assert client.current_5min_price(base) is not None
        assert client.current_5min_price(base).export_per_kwh == 30.0

        # Exactly on end of slot 0 / start of slot 1 → in slot 1
        cur = client.current_5min_price(base + timedelta(minutes=5))
        assert cur is not None
        assert cur.export_per_kwh == 40.0

    def test_returns_none_when_now_past_all_slots(self) -> None:
        # Stale cache scenario: prices end at 13:30, but it's now 13:50
        base = datetime(2026, 4, 25, 13, 25, tzinfo=UTC)
        slots = [
            _interval(base, base + timedelta(minutes=5)),
            _interval(base + timedelta(minutes=5), base + timedelta(minutes=10)),
        ]
        client = _client_with_prices(slots)

        now = datetime(2026, 4, 25, 13, 50, tzinfo=UTC)
        assert client.current_5min_price(now) is None

    def test_returns_none_when_no_cached_prices(self) -> None:
        c = AmberClient(AmberConfig(api_key="k", site_id="s"))
        # Default state — no fetch yet
        now = datetime(2026, 4, 25, 13, 36, tzinfo=UTC)
        assert c.current_5min_price(now) is None
