"""REST API for user-strategy modes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from optimiser.api.handlers.modes import register_modes_routes
from optimiser.api.probe import SERVICE_PROBE_KEY
from optimiser.modes import ActiveMode, ModeManager

NOW = datetime(2026, 5, 19, 4, 0, 0, tzinfo=UTC)


class _Probe:
    def __init__(self, mode_manager: ModeManager) -> None:
        self._mm = mode_manager

    @property
    def mode_manager(self) -> ModeManager:
        return self._mm


@pytest.fixture
async def client(tmp_path) -> TestClient:
    app = web.Application()
    mgr = ModeManager(tmp_path / "active_modes.json")
    app[SERVICE_PROBE_KEY] = _Probe(mgr)
    register_modes_routes(app)
    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)
    yield client
    await client.close()


async def test_post_buy_valid(client) -> None:
    resp = await client.post(
        "/modes/buy",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "ceiling_c_per_kwh": 12.0,
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["kind"] == "buy"
    assert body["params"]["ceiling_c_per_kwh"] == 12.0


async def test_post_buy_rejects_past_end_at(client) -> None:
    resp = await client.post(
        "/modes/buy",
        json={
            "end_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            "ceiling_c_per_kwh": 12.0,
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert "end_at" in body["error"]


async def test_post_buy_rejects_window_over_48h(client) -> None:
    resp = await client.post(
        "/modes/buy",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=49)).isoformat(),
            "ceiling_c_per_kwh": 12.0,
        },
    )
    assert resp.status == 400


async def test_post_buy_rejects_ceiling_zero(client) -> None:
    resp = await client.post(
        "/modes/buy",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "ceiling_c_per_kwh": 0.0,
        },
    )
    assert resp.status == 400


async def test_post_buy_rejects_ceiling_above_100(client) -> None:
    resp = await client.post(
        "/modes/buy",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "ceiling_c_per_kwh": 101.0,
        },
    )
    assert resp.status == 400


async def test_get_modes_returns_active_set(client) -> None:
    await client.post(
        "/modes/buy",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "ceiling_c_per_kwh": 10.0,
        },
    )
    await client.post(
        "/modes/conserve",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
            "floor_c_per_kwh": 22.0,
        },
    )
    resp = await client.get("/modes")
    body = await resp.json()
    kinds = {m["kind"] for m in body["modes"]}
    assert kinds == {"buy", "conserve"}


async def test_delete_buy_when_active(client) -> None:
    await client.post(
        "/modes/buy",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "ceiling_c_per_kwh": 10.0,
        },
    )
    resp = await client.delete("/modes/buy")
    assert resp.status == 204
    resp = await client.get("/modes")
    body = await resp.json()
    assert body["modes"] == []


async def test_delete_buy_when_inactive_returns_404(client) -> None:
    resp = await client.delete("/modes/buy")
    assert resp.status == 404


async def test_post_conserve_valid(client) -> None:
    resp = await client.post(
        "/modes/conserve",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "floor_c_per_kwh": 22.0,
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["kind"] == "conserve"
    assert body["params"]["floor_c_per_kwh"] == 22.0


async def test_suggest_buy_ceiling(client) -> None:
    """Suggest 75th percentile of in-window import prices."""
    from optimiser.types import PriceInterval

    base = datetime.now(UTC)
    strip = [
        PriceInterval(
            start=base + timedelta(minutes=5 * i),
            end=base + timedelta(minutes=5 * (i + 1)),
            import_per_kwh=float(p),
            export_per_kwh=2.0,
            spot_per_kwh=float(p) * 0.3,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
        )
        for i, p in enumerate([5, 6, 7, 8, 9, 10, 11, 12])
    ]
    client.app[SERVICE_PROBE_KEY].amber_price_window = lambda end_at: strip

    resp = await client.get("/modes/suggest?kind=buy&duration_minutes=40")
    assert resp.status == 200
    body = await resp.json()
    # 75th percentile of [5..12] via linear interpolation:
    # idx = 0.75 * 7 = 5.25 → 10 * 0.75 + 11 * 0.25 = 10.25
    assert body["suggested_ceiling_c_per_kwh"] == pytest.approx(10.25, abs=0.01)


async def test_suggest_conserve_floor(client) -> None:
    """Suggest 75th percentile of in-window export prices."""
    from optimiser.types import PriceInterval

    base = datetime.now(UTC)
    strip = [
        PriceInterval(
            start=base + timedelta(minutes=5 * i),
            end=base + timedelta(minutes=5 * (i + 1)),
            import_per_kwh=10.0,
            export_per_kwh=float(p),
            spot_per_kwh=3.0,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
        )
        for i, p in enumerate([5, 6, 7, 8, 9, 10, 15, 20, 25, 30])
    ]
    client.app[SERVICE_PROBE_KEY].amber_price_window = lambda end_at: strip

    resp = await client.get("/modes/suggest?kind=conserve&duration_minutes=50")
    assert resp.status == 200
    body = await resp.json()
    # 75th percentile of [5, 6, 7, 8, 9, 10, 15, 20, 25, 30]:
    # idx = 0.75 * 9 = 6.75 → 15 * 0.25 + 20 * 0.75 = 18.75
    assert body["suggested_floor_c_per_kwh"] == pytest.approx(18.75, abs=0.01)
