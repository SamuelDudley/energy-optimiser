"""HTTP handlers for user-strategy mode activation/cancellation/status."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import web

from ...modes import ActiveMode

MAX_WINDOW = timedelta(hours=48)
_THRESHOLD_MIN_EXCLUSIVE = 0.0
_THRESHOLD_MAX_INCLUSIVE = 100.0


def _bad(reason: str) -> web.Response:
    return web.json_response({"error": reason}, status=400)


def _parse_end_at(raw: Any) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("end_at must be an ISO-8601 string")
    end_at = datetime.fromisoformat(raw)
    if end_at.tzinfo is None:
        raise ValueError("end_at must include a UTC offset")
    return end_at.astimezone(UTC)


def _validate_end_at(end_at: datetime) -> str | None:
    now = datetime.now(UTC)
    if end_at <= now:
        return "end_at must be strictly in the future"
    if end_at > now + MAX_WINDOW:
        return "end_at must be within 48h of now"
    return None


def _validate_threshold(value: float, name: str) -> str | None:
    if not (_THRESHOLD_MIN_EXCLUSIVE < value <= _THRESHOLD_MAX_INCLUSIVE):
        return f"{name} must be in (0, 100] c/kWh"
    return None


async def _activate_handler(request: web.Request, kind: str, param_name: str) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _bad("body must be JSON")

    try:
        end_at = _parse_end_at(body.get("end_at"))
    except ValueError as exc:
        return _bad(str(exc))
    err = _validate_end_at(end_at)
    if err:
        return _bad(err)

    raw = body.get(param_name)
    if not isinstance(raw, (int, float)):
        return _bad(f"{param_name} must be a number")
    threshold = float(raw)
    err = _validate_threshold(threshold, param_name)
    if err:
        return _bad(err)

    mm = request.app["service_probe"].mode_manager
    mode = mm.activate(
        ActiveMode(
            kind=kind,  # type: ignore[arg-type]
            end_at=end_at,
            params={param_name: threshold},
            activated_at=datetime.now(UTC),
            source="dashboard",
        )
    )
    return web.json_response(mode.to_dict())


async def activate_buy(request: web.Request) -> web.Response:
    return await _activate_handler(request, "buy", "ceiling_c_per_kwh")


async def activate_conserve(request: web.Request) -> web.Response:
    return await _activate_handler(request, "conserve", "floor_c_per_kwh")


async def cancel_buy(request: web.Request) -> web.Response:
    mm = request.app["service_probe"].mode_manager
    removed = mm.cancel("buy")
    return web.Response(status=204) if removed else web.Response(status=404)


async def cancel_conserve(request: web.Request) -> web.Response:
    mm = request.app["service_probe"].mode_manager
    removed = mm.cancel("conserve")
    return web.Response(status=204) if removed else web.Response(status=404)


async def list_modes(request: web.Request) -> web.Response:
    mm = request.app["service_probe"].mode_manager
    now = datetime.now(UTC)
    modes = [m.to_dict() for m in mm.active(now)]
    return web.json_response({"modes": modes, "now": now.isoformat()})


def register_modes_routes(app: web.Application) -> None:
    app.router.add_get("/modes", list_modes)
    app.router.add_post("/modes/buy", activate_buy)
    app.router.add_delete("/modes/buy", cancel_buy)
    app.router.add_post("/modes/conserve", activate_conserve)
    app.router.add_delete("/modes/conserve", cancel_conserve)
