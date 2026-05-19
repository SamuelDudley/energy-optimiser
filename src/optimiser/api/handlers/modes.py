"""HTTP handlers for user-strategy mode activation/cancellation/status."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import web

from ...modes import ActiveMode
from ..probe import SERVICE_PROBE_KEY

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

    params: dict[str, float] = {param_name: threshold}

    # Buy mode supports an optional SOC cutoff. When current SOC reaches
    # this value the mode auto-cancels (see ModeManager.prune_soc_reached)
    # and the LP plans to land at this SOC ceiling without overshoot.
    if kind == "buy" and "soc_cutoff_pct" in body and body["soc_cutoff_pct"] is not None:
        raw_cutoff = body["soc_cutoff_pct"]
        if not isinstance(raw_cutoff, (int, float)):
            return _bad("soc_cutoff_pct must be a number")
        cutoff = float(raw_cutoff)
        if not (0.0 < cutoff <= 100.0):
            return _bad("soc_cutoff_pct must be in (0, 100]")
        # Reject if SOC is already at/above the cutoff — the mode would
        # auto-cancel on the first tick. Surface this at activation
        # rather than letting it silently no-op.
        probe = request.app[SERVICE_PROBE_KEY]
        current_soc = _current_soc_pct(probe)
        if current_soc is not None and current_soc >= cutoff:
            return _bad(
                f"soc_cutoff_pct ({cutoff}) must be above current SOC ({current_soc:.1f}); "
                "buy mode would exit immediately"
            )
        params["soc_cutoff_pct"] = cutoff

    mm = request.app[SERVICE_PROBE_KEY].mode_manager
    mode = mm.activate(
        ActiveMode(
            kind=kind,  # type: ignore[arg-type]
            end_at=end_at,
            params=params,
            activated_at=datetime.now(UTC),
            source="dashboard",
        )
    )
    return web.json_response(mode.to_dict())


def _current_soc_pct(probe) -> float | None:
    """Best-effort current SOC from the latest TickSnapshot. None when
    the service hasn't completed its first tick yet (e.g. just-restarted)."""
    snap = probe.last_snapshot
    if snap is None or snap.system_state is None:
        return None
    return snap.system_state.soc_pct


async def activate_buy(request: web.Request) -> web.Response:
    return await _activate_handler(request, "buy", "ceiling_c_per_kwh")


async def activate_conserve(request: web.Request) -> web.Response:
    return await _activate_handler(request, "conserve", "floor_c_per_kwh")


async def cancel_buy(request: web.Request) -> web.Response:
    mm = request.app[SERVICE_PROBE_KEY].mode_manager
    removed = mm.cancel("buy")
    return web.Response(status=204) if removed else web.Response(status=404)


async def cancel_conserve(request: web.Request) -> web.Response:
    mm = request.app[SERVICE_PROBE_KEY].mode_manager
    removed = mm.cancel("conserve")
    return web.Response(status=204) if removed else web.Response(status=404)


async def list_modes(request: web.Request) -> web.Response:
    mm = request.app[SERVICE_PROBE_KEY].mode_manager
    now = datetime.now(UTC)
    modes = [m.to_dict() for m in mm.active(now)]
    return web.json_response({"modes": modes, "now": now.isoformat()})


async def suggest(request: web.Request) -> web.Response:
    kind = request.query.get("kind")
    if kind not in ("buy", "conserve"):
        return _bad("kind must be 'buy' or 'conserve'")
    try:
        duration_minutes = int(request.query.get("duration_minutes", "120"))
    except ValueError:
        return _bad("duration_minutes must be an integer")
    if duration_minutes <= 0 or duration_minutes > 48 * 60:
        return _bad("duration_minutes must be in (0, 2880]")

    probe = request.app[SERVICE_PROBE_KEY]
    end_at = datetime.now(UTC) + timedelta(minutes=duration_minutes)
    strip = probe.amber_price_window(end_at)
    if not strip:
        return _bad("no price data available for window")

    if kind == "buy":
        values = sorted(p.import_per_kwh for p in strip if p.import_per_kwh is not None)
        if not values:
            return _bad("no import prices available")
        return web.json_response(
            {"suggested_ceiling_c_per_kwh": round(_percentile_linear(values, 0.75), 2)}
        )
    else:
        values = sorted(p.export_per_kwh for p in strip if p.export_per_kwh is not None)
        if not values:
            return _bad("no export prices available")
        return web.json_response(
            {"suggested_floor_c_per_kwh": round(_percentile_linear(values, 0.75), 2)}
        )


def _percentile_linear(sorted_values: list[float], q: float) -> float:
    """Linearly-interpolated percentile of a pre-sorted list.

    `q` is a fraction in [0, 1]. Matches numpy's default ``linear`` mode
    (sometimes called `q*(N-1)` interpolation). Used for both
    suggestions so buy ceiling and conserve floor read off the same
    statistic at the 75th percentile of in-window prices.
    """
    if not sorted_values:
        raise ValueError("sorted_values must be non-empty")
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx_f = q * (len(sorted_values) - 1)
    lo = int(idx_f)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx_f - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def register_modes_routes(app: web.Application) -> None:
    app.router.add_get("/modes", list_modes)
    app.router.add_get("/modes/suggest", suggest)
    app.router.add_post("/modes/buy", activate_buy)
    app.router.add_delete("/modes/buy", cancel_buy)
    app.router.add_post("/modes/conserve", activate_conserve)
    app.router.add_delete("/modes/conserve", cancel_conserve)
