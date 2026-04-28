"""Replay engine for backtesting LP configuration changes.

Reads historical tick snapshots and runs a candidate LP solve against
the same inputs the original tick saw. Produces per-tick cost deltas
that show how a different battery config, scenario weighting, or
managed-load setup would have performed in hindsight.

This used to compare a candidate `Planner` (greedy decision tree) against
the original. Since the move to LP-based optimisation, "candidate" means
a candidate `BatteryConfig` + scenario weights + load configs. The LP
itself is the algorithm; what changes is the parameterisation.

Cost model design choice: we keep the legacy `estimate_interval_cost(action,
state, price)` and apply it to both original and candidate. The original
ran greedy and recorded a `BatteryAction`; the candidate's action is
derived from `lp_solution_to_planner_output(...).battery_action`. Apples
to apples — both costs computed by the same function over the same state.
The LP's own `expected_total_cost_cents` is more accurate but uses a
different model (full grid flow accounting), so isn't comparable to a
historical greedy decision priced by `estimate_interval_cost`.

If the candidate LP fails (infeasible/timeout/error), the candidate
action defaults to `SELF_CONSUME` — that's what the real system would
do via `trigger_fallback`.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import BatteryConfig, ManagedLoadConfig
from .lp.dispatch import dispatch_from_slot
from .lp.loads import build_lp_loads
from .lp.result import SolveStatus
from .lp.snapshot_adapter import lp_solution_to_planner_output
from .lp.solver import solve_stochastic
from .time_utils import parse_iso
from .types import (
    BatteryAction,
    LoadCategory,
    LoadCycleState,
    LoadProfile,
    ManagedLoadStatus,
    PlannerOutput,
    PriceInterval,
    PVForecast,
    SystemState,
    TickSnapshot,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Per-tick outcome of replaying one candidate LP solve.

    `delta_cents` is `candidate − original`: negative means the candidate
    saved money relative to the historical decision. Costs and delta are
    `None` for ticks where the historical `system_state.house_load_kw` was
    itself unavailable (grid sensor offline at that tick) — the cost model
    can't run without ground-truth load.
    """

    tick_id: str
    timestamp: datetime
    original_action: BatteryAction
    candidate_action: BatteryAction
    original_cost_cents: float | None
    candidate_cost_cents: float | None
    delta_cents: float | None
    original_reason: str
    candidate_reason: str
    candidate_solve_status: str  # 'optimal' / 'infeasible' / etc.
    candidate_solve_ms: int  # Useful for performance regression detection


# ── Snapshot reconstruction (unchanged from greedy version) ──────


def _reconstruct_price_interval(d: dict) -> PriceInterval:
    # `.get()` for every advancedPrice field — old NDJSON snapshots
    # written before the export-side fields existed will be missing
    # them; the LP falls back to `export_per_kwh` in that case, which
    # exactly reproduces the deployed-at-the-time decision. New
    # snapshots carry both channels.
    return PriceInterval(
        start=parse_iso(d["start"]),
        end=parse_iso(d["end"]),
        import_per_kwh=d["import_per_kwh"],
        export_per_kwh=d["export_per_kwh"],
        spot_per_kwh=d["spot_per_kwh"],
        renewables_pct=d["renewables_pct"],
        spike_status=d["spike_status"],
        descriptor=d["descriptor"],
        forecast_low=d.get("forecast_low"),
        forecast_high=d.get("forecast_high"),
        forecast_predicted=d.get("forecast_predicted"),
        export_forecast_low=d.get("export_forecast_low"),
        export_forecast_high=d.get("export_forecast_high"),
        export_forecast_predicted=d.get("export_forecast_predicted"),
    )


def _reconstruct_pv_forecast(d: dict) -> PVForecast:
    return PVForecast(
        start=parse_iso(d["start"]),
        end=parse_iso(d["end"]),
        pv_estimate_kw=d["pv_estimate_kw"],
        pv_estimate10_kw=d["pv_estimate10_kw"],
        pv_estimate90_kw=d["pv_estimate90_kw"],
    )


def _reconstruct_system_state(d: dict) -> SystemState:
    return SystemState(
        timestamp=parse_iso(d["timestamp"]),
        soc_pct=d["soc_pct"],
        battery_power_kw=d["battery_power_kw"],
        pv_power_kw=d["pv_power_kw"],
        grid_power_kw=d["grid_power_kw"],
        house_load_kw=d["house_load_kw"],
        ems_mode=d["ems_mode"],
        outdoor_temp_c=d.get("outdoor_temp_c"),
        occupied=d.get("occupied", True),
    )


def _reconstruct_load_profile(d: dict) -> LoadProfile:
    return LoadProfile(
        slots=d["slots"],
        maturity_level=d["maturity_level"],
        context=d["context"],
    )


def _reconstruct_managed_load(d: dict) -> ManagedLoadStatus:
    return ManagedLoadStatus(
        load_id=d["load_id"],
        category=LoadCategory(d["category"]),
        power_kw=d["power_kw"],
        energy_today_kwh=d["energy_today_kwh"],
        relay_on=d.get("relay_on"),
        cycle_state=LoadCycleState(d["cycle_state"]) if d.get("cycle_state") else None,
    )


def _reconstruct_snapshot(raw: dict) -> TickSnapshot:
    """Reconstruct a TickSnapshot from a parsed JSON dict."""
    state = _reconstruct_system_state(raw["system_state"])
    prices = [_reconstruct_price_interval(p) for p in raw["price_forecast"]]
    pv = (
        [_reconstruct_pv_forecast(p) for p in raw["pv_forecast"]]
        if raw.get("pv_forecast")
        else None
    )
    profile = _reconstruct_load_profile(raw["load_profile"])
    loads = [_reconstruct_managed_load(entry) for entry in raw.get("managed_loads", [])]

    output_raw = raw["output"]
    output = PlannerOutput(
        battery_action=BatteryAction(output_raw["battery_action"]),
        charge_limit_kw=output_raw["charge_limit_kw"],
        discharge_limit_kw=output_raw["discharge_limit_kw"],
        target_soc=output_raw["target_soc"],
        load_commands=[],  # Don't replay load commands — only used for re-deciding battery
        grid_export_limit_kw=output_raw.get("grid_export_limit_kw"),
        reason=output_raw["reason"],
    )

    return TickSnapshot(
        tick_id=raw["tick_id"],
        timestamp=parse_iso(raw["timestamp"]),
        version=raw.get("version", "unknown"),
        system_state=state,
        price_forecast=prices,
        pv_forecast=pv,
        load_profile=profile,
        managed_loads=loads,
        maturity_level=raw.get("maturity_level", 0),
        output=output,
        actual_cost_cents=raw.get("actual_cost_cents"),
        counterfactual_cost_cents=raw.get("counterfactual_cost_cents"),
        # LP fields are not reconstructed — replay re-solves from scratch.
        # The raw JSON preserves them for offline analysis (e.g. DuckDB
        # queries against the NDJSON files directly).
        lp_solution=None,
        lp_dispatch=None,
    )


def load_snapshots(glob_pattern: str) -> Iterator[TickSnapshot]:
    """Load tick snapshots from NDJSON.gz files matching a glob pattern.

    Usage:
        snapshots = load_snapshots("snapshots/2026-03-*.ndjson.gz")
    """
    base = Path(glob_pattern).parent
    pattern = Path(glob_pattern).name

    for path in sorted(base.glob(pattern)):
        try:
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                        yield _reconstruct_snapshot(raw)
                    except Exception:
                        logger.warning("Failed to parse snapshot line in %s", path)
        except Exception:
            logger.exception("Failed to read snapshot file %s", path)


# ── Cost model ───────────────────────────────────────────────────


def estimate_interval_cost(
    action: BatteryAction,
    state: SystemState,
    price: PriceInterval,
) -> float | None:
    """Estimate the cost (cents) of a battery action for one 30-min interval.

    Positive = cost (paid for grid import), negative = revenue (export earnings).

    Returns None when `state.house_load_kw` is unavailable — we can't model
    grid flows without a ground-truth load reading, so the caller should
    skip the interval rather than compare against a fabricated value.

    Approximation: battery is treated as having unlimited capacity at rated
    power for the interval. Real system has SOC limits; this model ignores
    them. Good enough for replay comparisons since both original and candidate
    use the same model — biases cancel.
    """
    interval_hours = 0.5
    house_load = state.house_load_kw
    if house_load is None:
        return None

    if action == BatteryAction.SELF_CONSUME:
        grid_import = max(0, house_load - state.pv_power_kw)
        grid_export = max(0, state.pv_power_kw - house_load)
        return (
            grid_import * price.import_per_kwh - grid_export * price.export_per_kwh
        ) * interval_hours

    if action in (BatteryAction.DISCHARGE_ESS, BatteryAction.DISCHARGE_PV):
        discharge_kw = min(house_load, 10.0)
        grid_import = max(0, house_load - discharge_kw - state.pv_power_kw)
        grid_export = max(0, state.pv_power_kw + discharge_kw - house_load)
        grid_export = min(grid_export, 5.0)  # 5kW export limit
        return (
            grid_import * price.import_per_kwh - grid_export * price.export_per_kwh
        ) * interval_hours

    if action in (BatteryAction.CHARGE_GRID, BatteryAction.CHARGE_PV):
        charge_kw = 10.0 if action == BatteryAction.CHARGE_GRID else 0.0
        total_import = max(0, house_load + charge_kw - state.pv_power_kw)
        return total_import * price.import_per_kwh * interval_hours

    # STANDBY
    grid_import = max(0, house_load - state.pv_power_kw)
    return grid_import * price.import_per_kwh * interval_hours


# ── Replay engine ────────────────────────────────────────────────


def replay(
    snapshots: Iterator[TickSnapshot],
    candidate_battery_config: BatteryConfig,
    candidate_managed_loads: list[ManagedLoadConfig] | None = None,
    candidate_scenario_weights: dict[str, float] | None = None,
) -> Iterator[ReplayResult]:
    """Replay a candidate LP configuration against historical snapshots.

    For each snapshot, runs `solve_stochastic` with the candidate parameters
    against the historical state/prices/PV forecast. Yields a `ReplayResult`
    comparing original (historical greedy or LP) vs candidate decision and
    cost.

    Args:
        snapshots: iterator of historical `TickSnapshot`s (typically from
            `load_snapshots()`).
        candidate_battery_config: the BatteryConfig to use for the candidate
            solve. Vary this to test different battery sizing or efficiency
            assumptions.
        candidate_managed_loads: optional managed-load configs for the
            candidate LP. Defaults to empty (no LPLoad coordination), which
            means the candidate plans only the battery. Useful when testing
            a battery-config change without dragging HW heat pump variability
            into the comparison.
        candidate_scenario_weights: stochastic LP scenario weights. None
            uses the LP's defaults (P10:0.20, P50:0.60, P90:0.20).
    """
    candidate_lp_loads = build_lp_loads(candidate_managed_loads or [])

    for snap in snapshots:
        # Run the candidate LP. Failures default to SELF_CONSUME (matches
        # what the real system does via `trigger_fallback`).
        candidate_action, candidate_reason, candidate_status, candidate_ms = _solve_candidate(
            snap=snap,
            battery_config=candidate_battery_config,
            lp_loads=candidate_lp_loads,
            scenario_weights=candidate_scenario_weights,
        )

        # Cost both via the same model. Either side may return None when
        # the historical house_load is itself unavailable — propagate None
        # through to the result rather than silently substituting zero.
        if snap.price_forecast:
            current_price = snap.price_forecast[0]
            original_cost = (
                snap.actual_cost_cents
                if snap.actual_cost_cents is not None
                else estimate_interval_cost(
                    snap.output.battery_action,
                    snap.system_state,
                    current_price,
                )
            )
            candidate_cost = estimate_interval_cost(
                candidate_action,
                snap.system_state,
                current_price,
            )
        else:
            original_cost = None
            candidate_cost = None

        delta = (
            candidate_cost - original_cost
            if candidate_cost is not None and original_cost is not None
            else None
        )

        yield ReplayResult(
            tick_id=snap.tick_id,
            timestamp=snap.timestamp,
            original_action=snap.output.battery_action,
            candidate_action=candidate_action,
            original_cost_cents=original_cost,
            candidate_cost_cents=candidate_cost,
            delta_cents=delta,
            original_reason=snap.output.reason,
            candidate_reason=candidate_reason,
            candidate_solve_status=candidate_status,
            candidate_solve_ms=candidate_ms,
        )


def _solve_candidate(
    *,
    snap: TickSnapshot,
    battery_config: BatteryConfig,
    lp_loads,
    scenario_weights: dict[str, float] | None,
) -> tuple[BatteryAction, str, str, int]:
    """Run one candidate solve, returning `(action, reason, status, solve_ms)`.

    On any non-OPTIMAL/FEASIBLE outcome, returns SELF_CONSUME — that's the
    runtime fallback behaviour, so the replay reflects what would have
    actually happened.
    """
    try:
        solution = solve_stochastic(
            state=snap.system_state,
            prices_planning=snap.price_forecast,
            pv_forecast=snap.pv_forecast,
            load_profile=snap.load_profile,
            managed_loads=snap.managed_loads,
            lp_loads=lp_loads,
            battery_config=battery_config,
            scenario_weights=scenario_weights,
        )
    except Exception as exc:
        logger.warning("Candidate solve raised for %s: %s", snap.tick_id, exc)
        return BatteryAction.SELF_CONSUME, f"replay error: {exc}", "exception", 0

    if (
        solution.status not in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        or solution.slot_0 is None
    ):
        reason = f"replay fallback: lp {solution.status.value}" + (
            f" ({solution.reason})" if solution.reason else ""
        )
        return (
            BatteryAction.SELF_CONSUME,
            reason,
            solution.status.value,
            solution.solve_time_ms,
        )

    dispatch = dispatch_from_slot(
        solution.slot_0,
        battery_config,
        current_soc_pct=snap.system_state.soc_pct,
    )
    output = lp_solution_to_planner_output(solution, dispatch)
    return (
        output.battery_action,
        output.reason,
        solution.status.value,
        solution.solve_time_ms,
    )


# ── Summary ──────────────────────────────────────────────────────


def summarise_replay(results: list[ReplayResult]) -> dict:
    """Summarise replay results. Ticks with `delta_cents is None` (grid
    sensor was offline at that historical tick, so the cost model can't
    run) are excluded from the cost totals but still counted in
    `total_ticks` and `uncostable_ticks`."""
    if not results:
        return {"total_ticks": 0}

    costable = [r for r in results if r.delta_cents is not None]
    uncostable = len(results) - len(costable)
    total_delta = sum(r.delta_cents for r in costable)
    changed = sum(1 for r in results if r.candidate_action != r.original_action)
    failed = sum(1 for r in results if r.candidate_solve_status not in ("optimal", "feasible"))
    avg_solve_ms = sum(r.candidate_solve_ms for r in results) / len(results)

    return {
        "total_ticks": len(results),
        "uncostable_ticks": uncostable,
        "total_delta_aud": total_delta / 100,
        "changed_decisions": changed,
        "changed_pct": changed / len(results) * 100,
        "avg_delta_per_tick_cents": (total_delta / len(costable) if costable else None),
        "candidate_solve_failures": failed,
        "candidate_solve_failure_pct": failed / len(results) * 100,
        "avg_candidate_solve_ms": avg_solve_ms,
        "first_tick": results[0].timestamp.isoformat(),
        "last_tick": results[-1].timestamp.isoformat(),
    }
