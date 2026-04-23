"""Solver wrapper: builds → solves → extracts decisions.

Two entry points:
- `solve(...)` — deterministic single-scenario LP (sanity tests only).
- `solve_stochastic(...)` — production entry point, weighted P10/P50/P90.

Both return an `LPSolution` for any outcome; failures (timeout,
infeasibility, build errors) are encoded in `LPSolution.status` rather
than raised.
"""

from __future__ import annotations

import logging
import time

import pulp

from ..config import BatteryConfig
from ..types import (
    LoadCommand,
    LoadProfile,
    ManagedLoadStatus,
    PriceInterval,
    PVForecast,
    SystemState,
)
from .constants import NUMERIC_EPS, SOC_BOUND_PENALTY, SOLVER_TIMEOUT_S
from .formulation import LPVars, build_lp, build_stochastic_lp
from .loads import LPLoad
from .result import LPSolution, SlotDecision, SolveStatus

logger = logging.getLogger(__name__)


def solve(
    state: SystemState,
    prices_planning: list[PriceInterval],
    pv_forecast: list[PVForecast] | None,
    load_profile: LoadProfile,
    managed_loads: list[ManagedLoadStatus],
    lp_loads: list[LPLoad],
    battery_config: BatteryConfig,
    timeout_s: float = SOLVER_TIMEOUT_S,
) -> LPSolution:
    """Build and solve the deterministic LP. Returns an `LPSolution`."""
    t0 = time.monotonic()
    try:
        prob, vars = build_lp(
            state=state,
            prices_planning=prices_planning,
            pv_forecast=pv_forecast,
            load_profile=load_profile,
            managed_loads=managed_loads,
            lp_loads=lp_loads,
            battery_config=battery_config,
        )
    except Exception as exc:
        logger.exception("LP build failed")
        return _failure(SolveStatus.ERROR, t0, f"build failed: {exc}")

    return _run_and_extract(
        prob=prob,
        vars=vars,
        managed_loads=managed_loads,
        lp_loads=lp_loads,
        battery_config=battery_config,
        timeout_s=timeout_s,
        t0=t0,
    )


def solve_stochastic(
    state: SystemState,
    prices_planning: list[PriceInterval],
    pv_forecast: list[PVForecast] | None,
    load_profile: LoadProfile,
    managed_loads: list[ManagedLoadStatus],
    lp_loads: list[LPLoad],
    battery_config: BatteryConfig,
    scenario_weights: dict[str, float] | None = None,
    timeout_s: float = SOLVER_TIMEOUT_S,
) -> LPSolution:
    """Build and solve the stochastic LP across PV percentile scenarios.

    Slot-0 decisions are tied across scenarios by non-anticipativity, so
    the returned `LPSolution.slot_0` is the unique here-and-now action
    optimal against the weighted expected cost. The forward trajectory
    is taken from the base scenario (one of the equally-valid plans).
    """
    t0 = time.monotonic()
    try:
        prob, svars = build_stochastic_lp(
            state=state,
            prices_planning=prices_planning,
            pv_forecast=pv_forecast,
            load_profile=load_profile,
            managed_loads=managed_loads,
            lp_loads=lp_loads,
            battery_config=battery_config,
            scenario_weights=scenario_weights,
        )
    except Exception as exc:
        logger.exception("Stochastic LP build failed")
        return _failure(SolveStatus.ERROR, t0, f"build failed: {exc}")

    return _run_and_extract(
        prob=prob,
        vars=svars.base,
        managed_loads=managed_loads,
        lp_loads=lp_loads,
        battery_config=battery_config,
        timeout_s=timeout_s,
        t0=t0,
        scenarios=svars.scenarios,
        stochastic_meta={
            "scenarios": list(svars.scenarios.keys()),
            "base_scenario": svars.base_scenario,
        },
    )


# ── Internals ────────────────────────────────────────────────────


def _solver(timeout_s: float):
    """Return a PuLP solver configured for our LP.

    Uses `pulp.HiGHS` (the in-process highspy binding) exclusively.
    The alternative `pulp.HiGHS_CMD` spawns a subprocess for each solve,
    which has two downsides: (a) subprocess start-up overhead, and
    (b) if HiGHS ever hangs past its internal `timeLimit`, an
    `asyncio.to_thread` cancellation from the outer wall-clock timeout
    can't kill it — we'd leak one subprocess per pathological tick.

    In-process highspy can't be killed mid-solve either, but it shares
    the Python process, so nothing is left behind; the next tick gets a
    fresh solver object. Our Docker image installs `highspy` via pip
    and does NOT install the standalone `highs` binary, so HiGHS_CMD
    was never actually reachable in production.
    """
    try:
        return pulp.HiGHS(msg=False, timeLimit=timeout_s)
    except Exception:
        logger.exception("HiGHS solver unavailable — is highspy installed?")
        return None


def _run_and_extract(
    prob: pulp.LpProblem,
    vars: LPVars,
    managed_loads: list[ManagedLoadStatus],
    lp_loads: list[LPLoad],
    battery_config: BatteryConfig,
    timeout_s: float,
    t0: float,
    scenarios: dict[str, LPVars] | None = None,
    stochastic_meta: dict | None = None,
) -> LPSolution:
    solver = _solver(timeout_s)
    if solver is None:
        return _failure(SolveStatus.ERROR, t0, "no solver available")

    try:
        prob.solve(solver)
    except Exception as exc:
        logger.exception("LP solve raised")
        return _failure(SolveStatus.ERROR, t0, f"solve raised: {exc}")

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    pulp_status = pulp.LpStatus[prob.status]
    status = _map_status(pulp_status)

    if status not in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE):
        return LPSolution(
            status=status,
            slot_0=None,
            forward_trajectory=[],
            load_commands=[],
            grid_export_limit_kw=None,
            expected_total_cost_cents=float("inf"),
            solve_time_ms=elapsed_ms,
            reason=f"solver returned {pulp_status}",
        )

    return _extract_solution(
        prob=prob,
        vars=vars,
        managed_loads=managed_loads,
        lp_loads=lp_loads,
        battery_config=battery_config,
        elapsed_ms=elapsed_ms,
        status=status,
        scenarios=scenarios,
        stochastic_meta=stochastic_meta,
    )


def _map_status(pulp_status: str) -> SolveStatus:
    return {
        "Optimal": SolveStatus.OPTIMAL,
        "Not Solved": SolveStatus.TIMEOUT,
        "Infeasible": SolveStatus.INFEASIBLE,
        "Unbounded": SolveStatus.UNBOUNDED,
        "Undefined": SolveStatus.ERROR,
    }.get(pulp_status, SolveStatus.ERROR)


def _failure(status: SolveStatus, t0: float, reason: str) -> LPSolution:
    return LPSolution(
        status=status,
        slot_0=None,
        forward_trajectory=[],
        load_commands=[],
        grid_export_limit_kw=None,
        expected_total_cost_cents=float("inf"),
        solve_time_ms=(time.monotonic() - t0) * 1000.0,
        reason=reason,
    )


def _v(var) -> float:
    val = var.value() if hasattr(var, "value") else None
    return float(val) if val is not None else 0.0


def _extract_solution(
    prob: pulp.LpProblem,
    vars: LPVars,
    managed_loads: list[ManagedLoadStatus],
    lp_loads: list[LPLoad],
    battery_config: BatteryConfig,
    elapsed_ms: float,
    status: SolveStatus,
    scenarios: dict[str, LPVars] | None = None,
    stochastic_meta: dict | None = None,
) -> LPSolution:
    n = len(vars.slots)
    status_by_id = {s.load_id: s for s in managed_loads}

    trajectory: list[SlotDecision] = []
    for t in range(n):
        bat_kw = _v(vars.bat_charge_grid[t]) + _v(vars.bat_charge_pv[t]) - _v(vars.bat_discharge[t])
        load_kw_t: dict[str, float] = {}
        for load in lp_loads:
            lv = vars.loads.get(load.load_id)
            if lv is None:
                continue
            load_kw_t[load.load_id] = _v(lv.power_kw[t])
        trajectory.append(
            SlotDecision(
                slot_start=vars.slots[t],
                battery_kw=bat_kw,
                grid_import_kw=_v(vars.grid_import[t]),
                grid_export_kw=_v(vars.grid_export[t]),
                pv_to_house_kw=_v(vars.pv_to_house[t]),
                pv_to_battery_kw=_v(vars.pv_to_battery[t]),
                pv_to_export_kw=_v(vars.pv_to_export[t]),
                soc_pct_end=_v(vars.soc_pct[t]),
                load_kw=load_kw_t,
            )
        )

    slot_0 = trajectory[0]

    load_commands: list[LoadCommand] = []
    for load in lp_loads:
        lv = vars.loads.get(load.load_id)
        status_obj = status_by_id.get(load.load_id)
        if lv is None or status_obj is None:
            continue
        load_commands.append(load.extract_command(lv, status_obj, vars.slots[0]))

    # Grid export cap semantics — what we write to register 40038.
    #
    # The LP's `grid_export[0]` is a *plan* bounded at 0..export_limit_kw
    # (the DNSP cap, e.g. 5 kW). The register is a *ceiling* enforced by
    # the inverter's MPPT: it caps real PV flow at house_load + battery_in
    # + export, so a stale low value silently curtails solar whenever the
    # battery fills up. (With battery full + house 0.5 kW + reg 40038 at
    # 1 kW, the inverter throttles to 1.5 kW regardless of PV capability.)
    #
    # In the stochastic case each scenario's slot-0 `grid_export` is free
    # to differ (non-anticipativity is deliberately not applied here — see
    # formulation._add_non_anticipativity). The cap we commit to must
    # therefore be derived from across all scenarios, not just the base.
    #
    # Two cases:
    #   - All scenarios want zero export (negative export price, or
    #     nowhere to send surplus): write 0 and accept any curtailment
    #     as the cost of avoiding the penalty.
    #   - Any scenario plans positive export: pin to the DNSP cap
    #     (battery_config.export_limit_kw). Each scenario's plan is
    #     already bounded by this, so it's strictly safe; it also
    #     captures any unplanned solar windfall when actual PV beats the
    #     p90 forecast. `slot_0.grid_export_kw` (base-scenario flow)
    #     becomes advisory — useful for snapshot/logging, not the
    #     commit.
    if scenarios is not None:
        any_plans_export = any(
            _v(s.grid_export[0]) >= NUMERIC_EPS for s in scenarios.values()
        )
    else:
        any_plans_export = slot_0.grid_export_kw >= NUMERIC_EPS
    export_limit: float | None = (
        battery_config.export_limit_kw if any_plans_export else 0.0
    )

    raw_cost = pulp.value(prob.objective) or 0.0

    # Subtract the SOC out-of-band penalty portion so the reported cost
    # is the *economic* expected cost. The penalty keeps the LP numerically
    # well-behaved in recovery scenarios (initial SOC outside the band)
    # but should not show up in dashboards as a huge cost — it's an
    # internal regulariser, not a real grid bill. Penalty per-scenario:
    #   weight * SOC_BOUND_PENALTY * (Σ soc_over + Σ soc_under + terminal)
    # Deterministic: single LPVars with weight=1. Stochastic: sum over
    # scenario LPVars, each carrying its own weight.
    def _penalty_for(v: LPVars) -> float:
        parts = [_v(x) for x in v.soc_over_ceiling]
        parts += [_v(x) for x in v.soc_under_floor]
        if v.soc_terminal_slack is not None:
            parts.append(_v(v.soc_terminal_slack))
        return v.weight * SOC_BOUND_PENALTY * sum(parts)

    if scenarios is not None:
        penalty_cost = sum(_penalty_for(s) for s in scenarios.values())
    else:
        penalty_cost = _penalty_for(vars)
    cost = raw_cost - penalty_cost

    extra = ""
    if stochastic_meta:
        extra = f" [{len(stochastic_meta['scenarios'])} scenarios]"
    # `cost` is the economic expected cost; `raw_cost` retains the
    # slack-inflated value for debugging. Only log raw_cost when the
    # penalty is non-negligible so steady-state logs stay tidy.
    penalty_note = (
        f" (penalty={penalty_cost:.0f}c)"
        if penalty_cost > 1.0
        else ""
    )
    reason = (
        f"LP {status.value}{extra}: bat={slot_0.battery_kw:+.2f}kW, "
        f"export={slot_0.grid_export_kw:.2f}kW, "
        f"cost={cost:.0f}c{penalty_note}, solve={elapsed_ms:.0f}ms"
    )

    return LPSolution(
        status=status,
        slot_0=slot_0,
        forward_trajectory=trajectory,
        load_commands=load_commands,
        grid_export_limit_kw=export_limit,
        expected_total_cost_cents=float(cost),
        solve_time_ms=elapsed_ms,
        reason=reason,
    )
