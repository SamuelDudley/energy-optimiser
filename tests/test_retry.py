"""Tests for HTTP retry policies (issue #4).

Covers:
- 5xx errors are retried with backoff for all clients
- 429 is retried for Solcast but NOT for Amber (would re-trigger limit)
- 4xx (auth) errors are NOT retried
- Network errors are retried
- BOM client sets a custom User-Agent (anti-bot workaround)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from optimiser.clients._retry import DEFAULT_USER_AGENT
from optimiser.clients.amber import AmberClient
from optimiser.clients.bom import BOMClient
from optimiser.clients.solcast import SolcastClient
from optimiser.config import AmberConfig, SolcastConfig, WeatherConfig

# ── Helpers ──────────────────────────────────────────────────────


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    """Build a real HTTPStatusError with the given status code."""
    request = httpx.Request("GET", "http://test")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


def _ok_response(json_body: dict | list) -> MagicMock:
    """A mock response that returns successfully and yields json_body."""
    resp = MagicMock(spec=httpx.Response)
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_body)
    return resp


def _err_response(status: int) -> MagicMock:
    """A mock response that raises HTTPStatusError on raise_for_status."""
    resp = MagicMock(spec=httpx.Response)
    resp.raise_for_status = MagicMock(side_effect=_http_status_error(status))
    resp.status_code = status
    return resp


@pytest.fixture(autouse=True)
def _no_sleep():
    """Replace asyncio.sleep with an instant no-op so retry waits don't
    slow tests down. Tenacity uses asyncio.sleep internally for AsyncRetrying.
    """

    async def _sleep(*_a, **_kw):
        return None

    with patch("asyncio.sleep", new=_sleep):
        yield


# ── Amber retry behaviour ────────────────────────────────────────


class TestAmberRetry:
    @pytest.mark.asyncio
    async def test_succeeds_after_two_5xx(self) -> None:
        client = AmberClient(AmberConfig(api_key="k", site_id="s"))
        # Two 503s, then OK
        client._client.get = AsyncMock(
            side_effect=[
                _err_response(503),
                _err_response(503),
                _ok_response([]),
            ]
        )
        result = await client._fetch(next_count=1, previous=0, resolution=30)
        prices, log_rows = result
        assert prices == []
        assert log_rows == []
        assert client._client.get.await_count == 3
        await client.close()

    @pytest.mark.asyncio
    async def test_does_not_retry_429(self) -> None:
        """Amber's 50/5min limit means retrying 429 just re-triggers it."""
        client = AmberClient(AmberConfig(api_key="k", site_id="s"))
        client._client.get = AsyncMock(return_value=_err_response(429))
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await client._fetch(next_count=1, previous=0, resolution=30)
        assert excinfo.value.response.status_code == 429
        # Should be a single attempt — no retries
        assert client._client.get.await_count == 1
        await client.close()

    @pytest.mark.asyncio
    async def test_does_not_retry_401(self) -> None:
        """Auth failures are permanent, not transient."""
        client = AmberClient(AmberConfig(api_key="k", site_id="s"))
        client._client.get = AsyncMock(return_value=_err_response(401))
        with pytest.raises(httpx.HTTPStatusError):
            await client._fetch(next_count=1, previous=0, resolution=30)
        assert client._client.get.await_count == 1
        await client.close()

    @pytest.mark.asyncio
    async def test_retries_on_network_error(self) -> None:
        client = AmberClient(AmberConfig(api_key="k", site_id="s"))
        client._client.get = AsyncMock(
            side_effect=[
                httpx.ConnectError("conn refused"),
                _ok_response([]),
            ]
        )
        result = await client._fetch(next_count=1, previous=0, resolution=30)
        prices, log_rows = result
        assert prices == []
        assert log_rows == []
        assert client._client.get.await_count == 2
        await client.close()

    @pytest.mark.asyncio
    async def test_gives_up_after_max_attempts(self) -> None:
        client = AmberClient(AmberConfig(api_key="k", site_id="s"))
        client._client.get = AsyncMock(return_value=_err_response(502))
        with pytest.raises(httpx.HTTPStatusError):
            await client._fetch(next_count=1, previous=0, resolution=30)
        # Default policy: 3 attempts
        assert client._client.get.await_count == 3
        await client.close()


# ── Solcast retry behaviour ──────────────────────────────────────


class TestSolcastRetry:
    @pytest.mark.asyncio
    async def test_does_not_retry_429(self) -> None:
        """Solcast 429 must NOT be retried — could burn the daily quota."""
        client = SolcastClient(
            SolcastConfig(
                api_key="k",
                resource_id="r",
                base_url="http://test",
            )
        )
        client._client.get = AsyncMock(return_value=_err_response(429))
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            await client.get_forecast()
        assert excinfo.value.response.status_code == 429
        # Single attempt — no retries
        assert client._client.get.await_count == 1
        await client.close()

    @pytest.mark.asyncio
    async def test_retries_5xx(self) -> None:
        client = SolcastClient(
            SolcastConfig(
                api_key="k",
                resource_id="r",
                base_url="http://test",
            )
        )
        client._client.get = AsyncMock(
            side_effect=[
                _err_response(503),
                _ok_response({"forecasts": []}),
            ]
        )
        await client.get_forecast()
        assert client._client.get.await_count == 2
        await client.close()


class TestSolcastQuota:
    @pytest.mark.asyncio
    async def test_successful_calls_increment_count(self) -> None:
        client = SolcastClient(
            SolcastConfig(
                api_key="k",
                resource_id="r",
                base_url="http://test",
            )
        )
        client._client.get = AsyncMock(return_value=_ok_response({"forecasts": []}))
        assert client.calls_today == 0
        await client.get_forecast()
        assert client.calls_today == 1
        await client.get_forecast()
        assert client.calls_today == 2
        await client.close()

    @pytest.mark.asyncio
    async def test_failed_calls_do_not_count(self) -> None:
        """4xx/5xx don't count against quota."""
        client = SolcastClient(
            SolcastConfig(
                api_key="k",
                resource_id="r",
                base_url="http://test",
            )
        )
        client._client.get = AsyncMock(return_value=_err_response(429))
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_forecast()
        assert client.calls_today == 0
        await client.close()

    @pytest.mark.asyncio
    async def test_preflight_blocks_when_quota_spent(self, capsys) -> None:
        """When quota is exhausted, returns cached forecast without calling."""
        client = SolcastClient(
            SolcastConfig(
                api_key="k",
                resource_id="r",
                base_url="http://test",
                max_calls_per_day=3,
                safety_buffer=1,  # effective: 2 calls allowed
            )
        )
        client._client.get = AsyncMock(return_value=_ok_response({"forecasts": []}))

        # First 2 calls succeed
        await client.get_forecast()
        await client.get_forecast()
        assert client._client.get.await_count == 2

        # Third call: blocked by pre-flight, returns cached (empty list)
        result = await client.get_forecast()
        assert result == []
        # No further HTTP call made
        assert client._client.get.await_count == 2

        captured = capsys.readouterr()
        assert "quota exhausted" in captured.out.lower()
        await client.close()

    @pytest.mark.asyncio
    async def test_quota_resets_at_utc_midnight(self) -> None:
        from freezegun import freeze_time

        client = SolcastClient(
            SolcastConfig(
                api_key="k",
                resource_id="r",
                base_url="http://test",
            )
        )
        client._client.get = AsyncMock(return_value=_ok_response({"forecasts": []}))

        with freeze_time("2026-01-01 23:00:00"):
            for _ in range(5):
                await client.get_forecast()
            assert client.calls_today == 5

        with freeze_time("2026-01-02 00:00:01"):
            assert client.calls_today == 0  # reset on day rollover
            await client.get_forecast()
            assert client.calls_today == 1

        await client.close()


# ── BOM retry + UA ───────────────────────────────────────────────


class TestBOMRetry:
    def test_user_agent_header_set(self) -> None:
        client = BOMClient(WeatherConfig())
        ua = client._client.headers.get("User-Agent")
        assert ua == DEFAULT_USER_AGENT
        assert "energy-optimiser" in ua

    @pytest.mark.asyncio
    async def test_does_not_retry_403(self) -> None:
        """BOM 403 = anti-bot block. Retrying makes it worse."""
        client = BOMClient(WeatherConfig())
        client._client.get = AsyncMock(return_value=_err_response(403))
        # get_outdoor_temp swallows exceptions — falls back to last_temp
        result = await client.get_outdoor_temp()
        assert result is None  # no previous reading, so None
        assert client._client.get.await_count == 1
        await client.close()

    @pytest.mark.asyncio
    async def test_retries_on_5xx_then_succeeds(self) -> None:
        client = BOMClient(WeatherConfig())
        client._client.get = AsyncMock(
            side_effect=[
                _err_response(502),
                _ok_response({"observations": {"data": [{"air_temp": 18.5}]}}),
            ]
        )
        temp = await client.get_outdoor_temp()
        assert temp == 18.5
        assert client._client.get.await_count == 2
        await client.close()
