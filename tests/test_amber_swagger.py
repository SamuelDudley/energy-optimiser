"""Tests for the swagger-v2.1.0-driven extensions to AmberClient.

Covers:
  - advancedPrice.predicted is stored in PriceInterval.forecast_predicted
  - CurrentInterval.estimate flag maps to is_locked
  - Rate limit headers are parsed and exposed
  - Low-remaining emits a rising-edge warning event
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from optimiser.clients.amber import AmberClient
from optimiser.config import AmberConfig


@pytest.fixture
def amber_config() -> AmberConfig:
    return AmberConfig(api_key="test-key", site_id="test-site")


def _mock_response(
    *,
    payload: list[dict],
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Response double that supports .json() and .headers.get()."""
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    resp.headers = headers or {}
    return resp


def _general_interval(
    start: str,
    *,
    interval_type: str = "ForecastInterval",
    per_kwh: float = 20.0,
    advanced: dict | None = None,
    estimate: bool | None = None,
) -> dict:
    obj: dict = {
        "type": interval_type,
        "duration": 30,
        "channelType": "general",
        "startTime": start,
        "endTime": start.replace(":00:00", ":30:00"),
        "perKwh": per_kwh,
        "spotPerKwh": 6.0,
        "renewables": 40.0,
        "spikeStatus": "none",
        "descriptor": "neutral",
    }
    if advanced is not None:
        obj["advancedPrice"] = advanced
    if estimate is not None:
        obj["estimate"] = estimate
    return obj


def _feed_in_interval(
    start: str,
    *,
    interval_type: str = "ForecastInterval",
    per_kwh: float = 5.0,
    advanced: dict | None = None,
    estimate: bool | None = None,
) -> dict:
    obj: dict = {
        "type": interval_type,
        "duration": 30,
        "channelType": "feedIn",
        "startTime": start,
        "endTime": start.replace(":00:00", ":30:00"),
        "perKwh": per_kwh,
        "spotPerKwh": 1.5,
        "renewables": 40.0,
        "spikeStatus": "none",
        "descriptor": "neutral",
    }
    if advanced is not None:
        obj["advancedPrice"] = advanced
    if estimate is not None:
        obj["estimate"] = estimate
    return obj


class TestAdvancedPricePredicted:
    async def test_predicted_captured_when_present(
        self, amber_config: AmberConfig,
    ) -> None:
        """advancedPrice.predicted is Amber's own ML forecast. When
        present, it should land in PriceInterval.forecast_predicted."""
        start = "2026-04-15T00:00:00Z"
        payload = [
            _general_interval(
                start,
                advanced={"low": 8.0, "predicted": 15.5, "high": 25.0},
            ),
            _feed_in_interval(start),
        ]
        client = AmberClient(amber_config)
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=_mock_response(payload=payload))

        prices = await client.get_current_prices()

        assert len(prices) == 1
        assert prices[0].forecast_predicted == pytest.approx(15.5)
        assert prices[0].forecast_low == pytest.approx(8.0)
        assert prices[0].forecast_high == pytest.approx(25.0)

    async def test_predicted_none_when_absent(
        self, amber_config: AmberConfig,
    ) -> None:
        """Past/current intervals don't carry advancedPrice; predicted
        must be None and the LP will fall back to perKwh."""
        start = "2026-04-15T00:00:00Z"
        payload = [
            _general_interval(start, interval_type="ActualInterval"),
            _feed_in_interval(start, interval_type="ActualInterval"),
        ]
        client = AmberClient(amber_config)
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=_mock_response(payload=payload))

        prices = await client.get_current_prices()

        assert prices[0].forecast_predicted is None
        # Same on the export side: feedIn ActualInterval has no
        # advancedPrice, so the LP falls back to export_per_kwh.
        assert prices[0].export_forecast_predicted is None
        assert prices[0].export_forecast_low is None
        assert prices[0].export_forecast_high is None

    async def test_feedin_predicted_captured_and_sign_flipped(
        self, amber_config: AmberConfig,
    ) -> None:
        """advancedPrice on the feedIn channel — verified live 2026-04-28
        — must land in PriceInterval.export_forecast_*. Sign convention
        mirrors export_per_kwh: Amber's ledger sign is flipped to the
        customer perspective (positive = revenue from export).
        """
        start = "2026-04-15T00:00:00Z"
        payload = [
            _general_interval(start),
            # Amber ledger view: negative perKwh means revenue to customer.
            # The advancedPrice block on feedIn follows the same sign.
            _feed_in_interval(
                start,
                per_kwh=-3.85,
                advanced={"low": -2.71, "predicted": -3.95, "high": -5.24},
            ),
        ]
        client = AmberClient(amber_config)
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=_mock_response(payload=payload))

        prices = await client.get_current_prices()

        assert len(prices) == 1
        # Customer-perspective: revenue from export is positive.
        assert prices[0].export_per_kwh == pytest.approx(3.85)
        assert prices[0].export_forecast_predicted == pytest.approx(3.95)
        assert prices[0].export_forecast_low == pytest.approx(2.71)
        assert prices[0].export_forecast_high == pytest.approx(5.24)

    async def test_feedin_advancedprice_independent_of_general(
        self, amber_config: AmberConfig,
    ) -> None:
        """The two channels' advancedPrice blocks are populated
        independently. Both can carry the field, only one can, or neither.
        """
        start = "2026-04-15T00:00:00Z"
        payload = [
            _general_interval(
                start,
                advanced={"low": 8.0, "predicted": 15.5, "high": 25.0},
            ),
            # No advancedPrice on feedIn — should leave export side None.
            _feed_in_interval(start, per_kwh=-2.0),
        ]
        client = AmberClient(amber_config)
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=_mock_response(payload=payload))

        prices = await client.get_current_prices()

        assert prices[0].forecast_predicted == pytest.approx(15.5)
        assert prices[0].export_forecast_predicted is None
        # And the converse — only feedIn carries advancedPrice.
        client2 = AmberClient(amber_config)
        client2._client = MagicMock()
        client2._client.get = AsyncMock(return_value=_mock_response(payload=[
            _general_interval(start),
            _feed_in_interval(
                start,
                per_kwh=-2.0,
                advanced={"low": -1.0, "predicted": -2.5, "high": -4.0},
            ),
        ]))
        prices2 = await client2.get_current_prices()
        assert prices2[0].forecast_predicted is None
        assert prices2[0].export_forecast_predicted == pytest.approx(2.5)


class TestIsLocked:
    async def test_current_interval_with_estimate_false_is_locked(
        self, amber_config: AmberConfig,
    ) -> None:
        start = "2026-04-15T00:00:00Z"
        payload = [
            _general_interval(start, interval_type="CurrentInterval", estimate=False),
            _feed_in_interval(start),
        ]
        client = AmberClient(amber_config)
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=_mock_response(payload=payload))

        prices = await client.get_current_prices()

        assert prices[0].is_locked is True

    async def test_current_interval_with_estimate_true_not_locked(
        self, amber_config: AmberConfig,
    ) -> None:
        start = "2026-04-15T00:00:00Z"
        payload = [
            _general_interval(start, interval_type="CurrentInterval", estimate=True),
            _feed_in_interval(start),
        ]
        client = AmberClient(amber_config)
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=_mock_response(payload=payload))

        prices = await client.get_current_prices()

        assert prices[0].is_locked is False

    async def test_forecast_interval_has_no_is_locked(
        self, amber_config: AmberConfig,
    ) -> None:
        """ForecastInterval doesn't carry `estimate`. is_locked stays None."""
        start = "2026-04-15T00:00:00Z"
        payload = [
            _general_interval(start, interval_type="ForecastInterval"),
            _feed_in_interval(start),
        ]
        client = AmberClient(amber_config)
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=_mock_response(payload=payload))

        prices = await client.get_current_prices()

        assert prices[0].is_locked is None


class TestRateLimitHeaders:
    async def test_headers_parsed(
        self, amber_config: AmberConfig,
    ) -> None:
        start = "2026-04-15T00:00:00Z"
        payload = [_general_interval(start), _feed_in_interval(start)]
        client = AmberClient(amber_config)
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=_mock_response(
            payload=payload,
            headers={
                "RateLimit-Limit": "50",
                "RateLimit-Remaining": "42",
                "RateLimit-Reset": "120",
            },
        ))

        await client.get_current_prices()

        assert client.rate_limit_remaining == 42
        assert client.rate_limit_reset_seconds == 120

    async def test_missing_headers_tolerated(
        self, amber_config: AmberConfig,
    ) -> None:
        """Test mocks (and any non-Amber proxies) may not emit the
        headers. Parsing must not crash."""
        start = "2026-04-15T00:00:00Z"
        payload = [_general_interval(start), _feed_in_interval(start)]
        client = AmberClient(amber_config)
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=_mock_response(
            payload=payload, headers={},
        ))

        await client.get_current_prices()

        assert client.rate_limit_remaining is None

    async def test_low_remaining_emits_warning_once(
        self, amber_config: AmberConfig, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rising-edge: one warning when remaining drops below the
        threshold, none on subsequent calls that stay low."""
        start = "2026-04-15T00:00:00Z"
        payload = [_general_interval(start), _feed_in_interval(start)]
        client = AmberClient(amber_config)
        client._client = MagicMock()
        client._client.get = AsyncMock(return_value=_mock_response(
            payload=payload,
            headers={
                "RateLimit-Limit": "50",
                "RateLimit-Remaining": "2",   # below the threshold
                "RateLimit-Reset": "60",
            },
        ))
        events: list[tuple] = []
        monkeypatch.setattr(
            "optimiser.clients.amber.emit",
            lambda evt_type, payload: events.append((evt_type, payload)),
        )

        for _ in range(3):
            await client.get_current_prices()

        warnings = [e for e in events if "rate limit" in str(e[1].get("message", "")).lower()]
        assert len(warnings) == 1

    async def test_rearms_after_recovery(
        self, amber_config: AmberConfig, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Remaining climbs back → warning re-arms → a later drop warns
        again. Confirms this isn't a one-shot-for-the-lifetime-of-client."""
        start = "2026-04-15T00:00:00Z"
        payload = [_general_interval(start), _feed_in_interval(start)]
        client = AmberClient(amber_config)
        client._client = MagicMock()

        events: list[tuple] = []
        monkeypatch.setattr(
            "optimiser.clients.amber.emit",
            lambda evt_type, payload: events.append((evt_type, payload)),
        )

        # Drop, recover, drop again
        for remaining in ("2", "40", "2"):
            client._client.get = AsyncMock(return_value=_mock_response(
                payload=payload,
                headers={
                    "RateLimit-Limit": "50",
                    "RateLimit-Remaining": remaining,
                    "RateLimit-Reset": "60",
                },
            ))
            await client.get_current_prices()

        warnings = [e for e in events if "rate limit" in str(e[1].get("message", "")).lower()]
        assert len(warnings) == 2
