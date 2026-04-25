"""Stale-price guard on the export limit.

If the cached 5-min Amber price is older than
``EXPORT_PRICE_STALE_THRESHOLD``, the service must clamp the export limit to
0 regardless of what the LP planned. Wholesale feed-in prices can flip sign
within minutes during solar-glut windows, so once we lose recency we choose
"miss revenue" over "pay to export while blind"."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from optimiser.service import EXPORT_PRICE_STALE_THRESHOLD, Service
from optimiser.types import EventType


def _stub_service(prices_5min_age: timedelta | None) -> Service:
    """Build a Service with only the bits the guard touches."""
    svc = Service.__new__(Service)
    svc._amber = MagicMock()
    svc._amber.prices_5min_age = prices_5min_age
    svc._sigenergy = MagicMock()
    svc._sigenergy.set_export_limit_kw = AsyncMock(return_value=True)
    svc._sigenergy.set_fallback = AsyncMock(return_value=True)
    return svc


class TestResolveExportLimit:
    def test_passes_through_when_prices_are_fresh(self) -> None:
        svc = _stub_service(prices_5min_age=timedelta(seconds=30))
        assert svc._resolve_export_limit_kw(5.0, "tick-1") == 5.0

    def test_passes_through_at_threshold_boundary(self) -> None:
        svc = _stub_service(prices_5min_age=EXPORT_PRICE_STALE_THRESHOLD)
        # exactly at the threshold is still fresh
        assert svc._resolve_export_limit_kw(5.0, "tick-1") == 5.0

    def test_clamps_to_zero_when_prices_stale(self) -> None:
        svc = _stub_service(
            prices_5min_age=EXPORT_PRICE_STALE_THRESHOLD + timedelta(seconds=1)
        )
        assert svc._resolve_export_limit_kw(5.0, "tick-1") == 0.0

    def test_clamps_to_zero_when_no_prices_ever_fetched(self) -> None:
        # prices_5min_age is None when the cache has never been populated —
        # we should treat that the same as stale (we have no idea what the
        # current export price is).
        svc = _stub_service(prices_5min_age=None)
        assert svc._resolve_export_limit_kw(5.0, "tick-1") == 0.0

    def test_zero_limit_is_left_alone(self) -> None:
        # The LP itself decided not to export. Stale-or-not, we honour 0.
        svc = _stub_service(prices_5min_age=timedelta(hours=1))
        assert svc._resolve_export_limit_kw(0.0, "tick-1") == 0.0

    def test_none_limit_is_left_alone(self) -> None:
        # Some LP paths set grid_export_limit_kw=None to mean "don't touch".
        # We must not pretend 0 was requested in that case.
        svc = _stub_service(prices_5min_age=timedelta(hours=1))
        assert svc._resolve_export_limit_kw(None, "tick-1") is None

    def test_emits_event_when_clamping(self, capsys: pytest.CaptureFixture) -> None:
        svc = _stub_service(
            prices_5min_age=EXPORT_PRICE_STALE_THRESHOLD + timedelta(minutes=10)
        )
        svc._resolve_export_limit_kw(5.0, "tick-abc")
        out = capsys.readouterr().out
        assert EventType.EXPORT_BLOCKED_STALE_PRICE.value in out
        assert "tick-abc" in out
        assert "5.0" in out  # the LP's intended limit is recorded

    def test_no_event_when_passing_through(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        svc = _stub_service(prices_5min_age=timedelta(seconds=30))
        svc._resolve_export_limit_kw(5.0, "tick-abc")
        out = capsys.readouterr().out
        assert EventType.EXPORT_BLOCKED_STALE_PRICE.value not in out
