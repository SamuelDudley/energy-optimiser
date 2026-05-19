"""REST API for user-strategy modes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from optimiser.api.handlers.modes import register_modes_routes

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
    app["service_probe"] = _Probe(mgr)
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
