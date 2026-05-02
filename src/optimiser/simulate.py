"""Closed-loop multi-tick simulator for evaluating LP strategy changes.

The single-tick replay engine in `replay.py` re-solves each historical
tick against a candidate LP config in isolation — useful for sanity but
blind to compounding effects (early ticks shifting later state). This
module rolls the simulation forward: feed each tick's prices/PV
forecast into a candidate LP, take slot-0, advance SOC under physics,
re-solve at the next slot. The candidate's *cumulative* decisions
diverge from the historical record, and we can measure the realised
cost of the divergent trajectory against actual prices.

Why this matters for follow-up issue #6 (LP treats PV refill as free):
the failure mode is "drain Sat night assuming Sun PV refills for free,
get caught by overcast Sun and grid-charge at peak." A single-tick
replay can't catch it because each tick looks locally rational. A
closed-loop sim CAN — the same overconfident LP keeps making the same
locally-rational discharge choice, the SOC bottoms out, and on the
overcast day the candidate ends up grid-charging at retail.

Usage:
    from optimiser.simulate import simulate, ScenarioModifier
    from optimiser.config import BatteryConfig

    # Real history, current production config
    base = simulate(
        snapshots="snapshots/2026-04-25.ndjson.gz",
        battery_config=BatteryConfig(soc_floor_pct=15.0),
    )

    # Same history, more conservative scenario weights
    candidate = simulate(
        snapshots="snapshots/2026-04-25.ndjson.gz",
        battery_config=BatteryConfig(soc_floor_pct=15.0),
        scenario_weights={"p10": 0.40, "p50": 0.40, "p90": 0.20},
    )

    print(f"baseline cost:  ${base.total_cost_aud:.2f}")
    print(f"candidate cost: ${candidate.total_cost_aud:.2f}")

Adverse-scenario mode: pass a `ScenarioModifier` to perturb PV forecasts
(over-optimistic) or actual PV (under-delivers) before the LP sees them.
That's how we stress-test the LP under conditions worse than the
historical record."""

from __future__ import annotations

import dataclasses
import gzip
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .config import BatteryConfig
from .lp.constants import SLOT_MINUTES
from .lp.dispatch import dispatch_from_slot
from .lp.loads import build_lp_loads
from .lp.result import SolveStatus
from .lp.solver import solve_stochastic
from .replay import _reconstruct_snapshot
from .types import (
    TickSnapshot,
)

logger = logging.getLogger(__name__)


# ── Scenario perturbations ───────────────────────────────────────


@dataclass(frozen=True)
class ScenarioModifier:
    """Perturbs the snapshot stream before the LP sees it.

    `pv_forecast_multiplier`: applied to all `PVForecast.pv_estimate*_kw`
        fields. >1.0 makes the LP MORE optimistic about PV than reality
        (forces the candidate to overcommit discharges), <1.0 makes it
        more pessimistic. Set to e.g. 1.5 to test "Solcast over-forecast
        by 50%" (the classic LP-bets-on-PV failure mode).

    `actual_pv_multiplier`: applied to the historical
        `system_state.pv_power_kw` used as ground truth for physics +
        cost accounting. <1.0 means "PV under-delivered relative to
        history". Combined with `pv_forecast_multiplier > 1.0` simulates
        a forecast bust.

    `import_price_multiplier`, `export_price_multiplier`: scale the
        respective price field in every `PriceInterval`. Useful for
        sweeping price-sensitivity.

    Identity ScenarioModifier (all multipliers = 1.0) leaves the
    historical stream unchanged."""

    pv_forecast_multiplier: float = 1.0
    actual_pv_multiplier: float = 1.0
    import_price_multiplier: float = 1.0
    export_price_multiplier: float = 1.0
    name: str = "history"

    def apply_to_snapshot(self, snap: TickSnapshot) -> TickSnapshot:
        prices = [
            dataclasses.replace(
                p,
                import_per_kwh=p.import_per_kwh * self.import_price_multiplier,
                export_per_kwh=p.export_per_kwh * self.export_price_multiplier,
                forecast_predicted=(
                    p.forecast_predicted * self.import_price_multiplier
                    if p.forecast_predicted is not None
                    else None
                ),
                # Mirror the import-side scaling on the export advancedPrice.
                # low/high are not yet read by the LP (KNOWN-ISSUES #24) but
                # scale them anyway so any future scenario work that consumes
                # them sees a consistent perturbation across all four export
                # price fields.
                export_forecast_predicted=(
                    p.export_forecast_predicted * self.export_price_multiplier
                    if p.export_forecast_predicted is not None
                    else None
                ),
                export_forecast_low=(
                    p.export_forecast_low * self.export_price_multiplier
                    if p.export_forecast_low is not None
                    else None
                ),
                export_forecast_high=(
                    p.export_forecast_high * self.export_price_multiplier
                    if p.export_forecast_high is not None
                    else None
                ),
            )
            for p in snap.price_forecast
        ]
        pv = (
            [
                dataclasses.replace(
                    p,
                    pv_estimate_kw=p.pv_estimate_kw * self.pv_forecast_multiplier,
                    pv_estimate10_kw=p.pv_estimate10_kw * self.pv_forecast_multiplier,
                    pv_estimate90_kw=p.pv_estimate90_kw * self.pv_forecast_multiplier,
                )
                for p in snap.pv_forecast
            ]
            if snap.pv_forecast
            else None
        )
        # actual_pv_multiplier applies to system_state.pv_power_kw —
        # i.e. the realised PV that physics evaluates against. The LP
        # never sees this directly; only the cost-accounting physics
        # at simulate-step does.
        state = dataclasses.replace(
            snap.system_state,
            pv_power_kw=snap.system_state.pv_power_kw * self.actual_pv_multiplier,
        )
        return dataclasses.replace(
            snap,
            system_state=state,
            price_forecast=prices,
            pv_forecast=pv,
        )


# ── Result types ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SimulationStep:
    """One tick of the closed-loop simulation."""

    ts: datetime
    soc_pct_start: float
    soc_pct_end: float
    bat_kw: float  # signed slot-0 plan
    grid_import_kw: float  # realised after physics
    grid_export_kw: float  # realised after physics
    pv_actual_kw: float  # ground-truth PV at this tick
    house_load_kw: float
    import_price: float
    export_price: float
    cost_cents: float  # realised this slot (positive = paid grid)
    dispatch_kind: str  # for the audit trail
    solve_status: str
    solve_ms: float
    lp_planned_soc_pct_end: float


@dataclass
class SimulationResult:
    """Full closed-loop run — every step plus aggregates."""

    steps: list[SimulationStep] = field(default_factory=list)

    @property
    def total_cost_cents(self) -> float:
        return sum(s.cost_cents for s in self.steps)

    @property
    def total_cost_aud(self) -> float:
        return self.total_cost_cents / 100.0

    @property
    def total_kwh_imported(self) -> float:
        h = SLOT_MINUTES / 60.0
        return sum(max(0.0, s.grid_import_kw) * h for s in self.steps)

    @property
    def total_kwh_exported(self) -> float:
        h = SLOT_MINUTES / 60.0
        return sum(max(0.0, s.grid_export_kw) * h for s in self.steps)

    @property
    def total_kwh_charged_to_battery(self) -> float:
        h = SLOT_MINUTES / 60.0
        return sum(max(0.0, s.bat_kw) * h for s in self.steps)

    @property
    def total_kwh_discharged_from_battery(self) -> float:
        h = SLOT_MINUTES / 60.0
        return sum(-min(0.0, s.bat_kw) * h for s in self.steps)

    @property
    def min_soc_pct(self) -> float:
        return min((s.soc_pct_end for s in self.steps), default=float("nan"))

    @property
    def max_soc_pct(self) -> float:
        return max((s.soc_pct_end for s in self.steps), default=float("nan"))

    @property
    def n_solve_failures(self) -> int:
        return sum(1 for s in self.steps if s.solve_status not in ("optimal", "feasible"))

    def summary(self) -> dict:
        return {
            "n_steps": len(self.steps),
            "total_cost_aud": round(self.total_cost_aud, 2),
            "kwh_imported": round(self.total_kwh_imported, 2),
            "kwh_exported": round(self.total_kwh_exported, 2),
            "kwh_charged_battery": round(self.total_kwh_charged_to_battery, 2),
            "kwh_discharged_battery": round(self.total_kwh_discharged_from_battery, 2),
            "min_soc_pct": round(self.min_soc_pct, 2),
            "max_soc_pct": round(self.max_soc_pct, 2),
            "solve_failures": self.n_solve_failures,
            "first_ts": self.steps[0].ts.isoformat() if self.steps else None,
            "last_ts": self.steps[-1].ts.isoformat() if self.steps else None,
        }


# ── Snapshot loading + lookup ────────────────────────────────────


def _load_indexed_snapshots(
    paths: list[Path],
) -> dict[datetime, TickSnapshot]:
    """Load all snapshots into a {timestamp → snapshot} index. We do
    this eagerly because the simulator needs random-access lookup by
    time to step at slot resolution (5 min) regardless of the
    snapshots' original 60s cadence."""
    index: dict[datetime, TickSnapshot] = {}
    for path in paths:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    snap = _reconstruct_snapshot(raw)
                    index[snap.timestamp] = snap
                except Exception:
                    logger.warning("Failed to parse snapshot in %s", path)
    return index


def _nearest_snapshot(
    index: dict[datetime, TickSnapshot],
    ts: datetime,
    tolerance: timedelta = timedelta(minutes=2),
) -> TickSnapshot | None:
    """Find the snapshot closest to `ts` within `tolerance`. Snapshots
    fire every ~60s; the simulator steps every SLOT_MINUTES (5). So at
    each sim step, picking the nearest historical tick gives us its
    forecasts + actual PV/house at the right moment."""
    closest: TickSnapshot | None = None
    best_delta = tolerance
    for snap_ts, snap in index.items():
        d = abs(snap_ts - ts)
        if d <= best_delta:
            best_delta = d
            closest = snap
    return closest


# ── Physics step ─────────────────────────────────────────────────


def _physics_step(
    *,
    soc_pct: float,
    battery_kw: float,
    pv_actual_kw: float,
    house_load_kw: float,
    battery_config: BatteryConfig,
    export_limit_kw: float,
    slot_hours: float,
) -> tuple[float, float, float]:
    """Apply one slot of physics under the LP's slot-0 plan.

    Returns `(soc_pct_end, grid_import_kw, grid_export_kw)`.

    Model: the LP's signed `battery_kw` is treated as a commanded rate
    that the inverter executes for the slot. SOC delta uses the LP's
    sign convention (charge × eta, discharge ÷ 1). Grid flows are
    computed by balancing house + battery_charge − pv − battery_discharge
    and capping export at the DNSP limit.

    This is approximate — real mode-6 dispatch is load-following so
    the realised battery rate is min(commanded, available_to_serve).
    For a closed-loop simulator the simple model is consistent across
    candidates → biases cancel for relative comparison."""
    eta = battery_config.round_trip_efficiency
    if battery_kw >= 0:
        delta_kwh = battery_kw * slot_hours * eta
    else:
        delta_kwh = battery_kw * slot_hours
    soc_pct_end = soc_pct + (delta_kwh / battery_config.capacity_kwh) * 100.0
    soc_pct_end = max(0.0, min(100.0, soc_pct_end))

    # House first, PV next, battery either side, grid balances.
    # Battery charging consumes; battery discharging supplies.
    bat_charge_kw = max(0.0, battery_kw)
    bat_discharge_kw = -min(0.0, battery_kw)
    net_demand = house_load_kw + bat_charge_kw - pv_actual_kw - bat_discharge_kw
    if net_demand > 0:
        grid_import_kw = net_demand
        grid_export_kw = 0.0
    else:
        grid_import_kw = 0.0
        grid_export_kw = min(-net_demand, export_limit_kw)
    return soc_pct_end, grid_import_kw, grid_export_kw


# ── Main entry ───────────────────────────────────────────────────


def simulate(
    *,
    snapshots: str | list[Path] | None = None,
    snapshot_index: dict[datetime, TickSnapshot] | None = None,
    battery_config: BatteryConfig,
    scenario_weights: dict[str, float] | None = None,
    wear_cost_per_kwh: float | None = None,
    terminal_floor_override_pct: float | None = None,
    modifier: ScenarioModifier | None = None,
    initial_soc_pct: float | None = None,
    start_ts: datetime | None = None,
    end_ts: datetime | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> SimulationResult:
    """Run a closed-loop simulation over a snapshot range.

    Args:
        snapshots: glob string or explicit list of NDJSON(.gz) paths.
            Mutually exclusive with `snapshot_index`.
        snapshot_index: pre-loaded `{timestamp → TickSnapshot}` index.
            Pass this when running many sims over the same archive
            (e.g. terminal-value data generation) to skip the
            ~5–10s re-parse cost per call. Mutually exclusive with
            `snapshots`.
        battery_config: candidate LP `BatteryConfig`.
        scenario_weights: candidate stochastic weights (None → defaults).
        modifier: optional `ScenarioModifier` for adverse-scenario
            stress tests. None → run against the historical record
            unchanged.
        initial_soc_pct: SOC at `start_ts`. Defaults to the SOC of the
            first snapshot in range.
        start_ts, end_ts: bound the simulation window. Defaults to the
            first/last snapshot in the index.
        progress: optional callback `(steps_done, total_steps)` for
            CLI progress bars.
    """
    if snapshot_index is not None:
        if snapshots is not None:
            raise ValueError("pass exactly one of snapshots or snapshot_index")
        index = snapshot_index
    else:
        if snapshots is None:
            raise ValueError("must pass snapshots or snapshot_index")
        if isinstance(snapshots, str):
            base = Path(snapshots).parent
            pattern = Path(snapshots).name
            paths = sorted(base.glob(pattern))
        else:
            paths = list(snapshots)
        index = _load_indexed_snapshots(paths)
    if not index:
        raise ValueError(f"No snapshots loaded from {snapshots!r}")

    sorted_ts = sorted(index.keys())
    sim_start = start_ts or sorted_ts[0]
    sim_end = end_ts or sorted_ts[-1]

    first_snap = index[sorted_ts[0]]
    soc = initial_soc_pct if initial_soc_pct is not None else first_snap.system_state.soc_pct

    slot_hours = SLOT_MINUTES / 60.0
    step_delta = timedelta(minutes=SLOT_MINUTES)
    mod = modifier or ScenarioModifier()

    lp_loads = build_lp_loads([])  # battery-only sim — managed loads add
    # noise that obscures battery strategy; keep the comparison clean

    result = SimulationResult()
    total_steps = int((sim_end - sim_start) / step_delta) + 1

    ts = sim_start
    step_i = 0
    while ts <= sim_end:
        snap = _nearest_snapshot(index, ts)
        if snap is None:
            ts += step_delta
            step_i += 1
            continue
        snap = mod.apply_to_snapshot(snap)

        # Override system_state.soc_pct with our evolved SOC. The LP
        # is solved as if the inverter is at our simulated SOC.
        state = dataclasses.replace(snap.system_state, soc_pct=soc)

        sol = solve_stochastic(
            state=state,
            prices_planning=snap.price_forecast,
            pv_forecast=snap.pv_forecast,
            load_profile=snap.load_profile,
            managed_loads=[],  # battery-only sim
            lp_loads=lp_loads,
            battery_config=battery_config,
            scenario_weights=scenario_weights,
            wear_cost_per_kwh=wear_cost_per_kwh,
            terminal_floor_override_pct=terminal_floor_override_pct,
        )

        if sol.status not in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE) or sol.slot_0 is None:
            # Fallback: SELF_CONSUME. Battery doesn't charge/discharge;
            # PV serves house, surplus exports.
            bat_kw = 0.0
            dispatch_kind = "fallback"
            planned_soc_end = soc
        else:
            bat_kw = sol.slot_0.battery_kw
            disp = dispatch_from_slot(
                sol.slot_0,
                battery_config,
                current_soc_pct=soc,
            )
            dispatch_kind = disp.kind.value
            planned_soc_end = sol.slot_0.soc_pct_end

        # Realised physics uses ACTUAL pv (post-modifier) + actual house
        pv_actual = snap.system_state.pv_power_kw or 0.0
        house = snap.system_state.house_load_kw or 0.0
        soc_end, grid_in, grid_out = _physics_step(
            soc_pct=soc,
            battery_kw=bat_kw,
            pv_actual_kw=pv_actual,
            house_load_kw=house,
            battery_config=battery_config,
            export_limit_kw=battery_config.export_limit_kw,
            slot_hours=slot_hours,
        )

        # Cost: import × ip − export × ep, in cents
        # Use live (slot-0) price from price_forecast
        price = snap.price_forecast[0] if snap.price_forecast else None
        if price:
            cost = (grid_in * price.import_per_kwh - grid_out * price.export_per_kwh) * slot_hours
        else:
            cost = 0.0

        result.steps.append(
            SimulationStep(
                ts=ts,
                soc_pct_start=soc,
                soc_pct_end=soc_end,
                bat_kw=bat_kw,
                grid_import_kw=grid_in,
                grid_export_kw=grid_out,
                pv_actual_kw=pv_actual,
                house_load_kw=house,
                import_price=price.import_per_kwh if price else 0.0,
                export_price=price.export_per_kwh if price else 0.0,
                cost_cents=cost,
                dispatch_kind=dispatch_kind,
                solve_status=sol.status.value,
                solve_ms=sol.solve_time_ms,
                lp_planned_soc_pct_end=planned_soc_end,
            )
        )

        soc = soc_end
        ts += step_delta
        step_i += 1
        if progress and step_i % 50 == 0:
            progress(step_i, total_steps)

    return result
