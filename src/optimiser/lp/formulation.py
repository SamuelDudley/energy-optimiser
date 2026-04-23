"""LP formulation for the energy optimiser.

Builds a PuLP problem from the system state, price/PV forecasts, load
profile, and managed loads. Two public entry points:

- `build_lp(...)` — single-scenario deterministic LP (used in tests and
  as a sanity baseline).
- `build_stochastic_lp(...)` — multi-scenario two-stage stochastic LP
  with non-anticipativity on slot 0 (P10/P50/P90 by default). Used in
  production.

Both are thin wrappers around `_add_scenario_to_problem`, which adds one
scenario's variables, constraints, and weighted cost terms to a shared
`pulp.LpProblem`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pulp

from ..config import BatteryConfig
from ..types import LoadProfile, ManagedLoadStatus, PriceInterval, PVForecast, SystemState
from .constants import (
    DEFAULT_SCENARIO_WEIGHTS,
    HORIZON_HOURS,
    SLOT_MINUTES,
    SOC_BOUND_PENALTY,
    TERMINAL_SOC_FLOOR_PCT,
    WEAR_COST_PER_KWH,
)
from .loads import LoadVars, LPLoad


@dataclass
class LPVars:
    """All decision variables for one scenario."""

    slots: list[datetime]
    slot_hours: float

    bat_charge_grid: list[pulp.LpVariable]
    bat_charge_pv: list[pulp.LpVariable]
    bat_discharge: list[pulp.LpVariable]

    grid_import: list[pulp.LpVariable]
    grid_export: list[pulp.LpVariable]

    pv_to_house: list[pulp.LpVariable]
    pv_to_battery: list[pulp.LpVariable]
    pv_to_export: list[pulp.LpVariable]
    pv_curtailed: list[pulp.LpVariable]

    soc_pct: list[pulp.LpVariable]

    loads: dict[str, LoadVars] = field(default_factory=dict)

    # Slack variables on the operating-band soft constraints + scenario
    # weight — exposed so the solver can subtract the penalty portion of
    # the objective from reported cost (§3.4; pure-economic reporting for
    # dashboards and logs). Defaults make these backwards-compatible with
    # any code constructing LPVars manually.
    soc_over_ceiling: list[pulp.LpVariable] = field(default_factory=list)
    soc_under_floor: list[pulp.LpVariable] = field(default_factory=list)
    soc_terminal_slack: pulp.LpVariable | None = None
    weight: float = 1.0


@dataclass
class StochasticLPVars:
    """Multi-scenario LP vars. Slot-0 decisions across all scenarios are
    tied by non-anticipativity constraints — they take the same value
    in any solved scenario, so reading from any one is equivalent.

    `base` is the canonical scenario for slot-0 extraction.
    """

    slots: list[datetime]
    slot_hours: float
    scenarios: dict[str, LPVars]
    base_scenario: str

    @property
    def base(self) -> LPVars:
        return self.scenarios[self.base_scenario]


# ── Public: deterministic single-scenario ─────────────────────────


def build_lp(
    state: SystemState,
    prices_planning: list[PriceInterval],
    pv_forecast: list[PVForecast] | None,
    load_profile: LoadProfile,
    managed_loads: list[ManagedLoadStatus],
    lp_loads: list[LPLoad],
    battery_config: BatteryConfig,
    wear_cost_per_kwh: float = WEAR_COST_PER_KWH,
    horizon_hours: int = HORIZON_HOURS,
    slot_minutes: int = SLOT_MINUTES,
    pv_percentile: str = "p50",
) -> tuple[pulp.LpProblem, LPVars]:
    """Build a single-scenario deterministic LP."""
    if not prices_planning:
        raise ValueError("build_lp requires non-empty prices_planning")

    slots = _slot_grid(state.timestamp, horizon_hours, slot_minutes)
    slots = _truncate_to_priced(slots, prices_planning)
    if not slots:
        raise ValueError("No LP slots covered by prices_planning — forecast starts after slot 0?")
    slot_hours = slot_minutes / 60.0

    prob = pulp.LpProblem("energy_optimiser", pulp.LpMinimize)

    vars, cost_terms = _add_scenario_to_problem(
        prob=prob,
        prefix="",
        weight=1.0,
        slots=slots,
        slot_hours=slot_hours,
        state=state,
        prices_planning=prices_planning,
        pv_forecast=pv_forecast,
        pv_percentile=pv_percentile,
        load_profile=load_profile,
        managed_loads=managed_loads,
        lp_loads=lp_loads,
        battery_config=battery_config,
        wear_cost_per_kwh=wear_cost_per_kwh,
    )
    prob += pulp.lpSum(cost_terms), "total_cost_cents"
    return prob, vars


# ── Public: stochastic multi-scenario ─────────────────────────────


def build_stochastic_lp(
    state: SystemState,
    prices_planning: list[PriceInterval],
    pv_forecast: list[PVForecast] | None,
    load_profile: LoadProfile,
    managed_loads: list[ManagedLoadStatus],
    lp_loads: list[LPLoad],
    battery_config: BatteryConfig,
    scenario_weights: dict[str, float] | None = None,
    wear_cost_per_kwh: float = WEAR_COST_PER_KWH,
    horizon_hours: int = HORIZON_HOURS,
    slot_minutes: int = SLOT_MINUTES,
) -> tuple[pulp.LpProblem, StochasticLPVars]:
    """Build a two-stage stochastic LP across P10/P50/P90 PV scenarios.

    Each scenario gets its own copy of slots-1..N variables (so it can
    plan a scenario-appropriate trajectory), but slot-0 variables are
    tied across scenarios via non-anticipativity constraints. The result
    is the optimal here-and-now decision against the weighted expected
    cost across all scenarios.

    `scenario_weights`: maps scenario name to probability mass. Defaults
    to `DEFAULT_SCENARIO_WEIGHTS` (P10 0.2, P50 0.6, P90 0.2). Names must
    be one of "p10"/"p50"/"p90" (matched to PVForecast percentile fields).
    """
    weights = scenario_weights or dict(DEFAULT_SCENARIO_WEIGHTS)
    if not weights:
        raise ValueError("scenario_weights must be non-empty")
    if abs(sum(weights.values()) - 1.0) > 1e-3:
        raise ValueError(f"scenario_weights must sum to 1.0, got {sum(weights.values()):.3f}")
    if not prices_planning:
        raise ValueError("build_stochastic_lp requires non-empty prices_planning")

    slots = _slot_grid(state.timestamp, horizon_hours, slot_minutes)
    slots = _truncate_to_priced(slots, prices_planning)
    if not slots:
        raise ValueError("No LP slots covered by prices_planning — forecast starts after slot 0?")
    slot_hours = slot_minutes / 60.0

    prob = pulp.LpProblem("energy_optimiser_stochastic", pulp.LpMinimize)

    scenarios: dict[str, LPVars] = {}
    all_cost_terms: list[pulp.LpAffineExpression] = []

    for scenario_name, weight in weights.items():
        vars, cost_terms = _add_scenario_to_problem(
            prob=prob,
            prefix=f"{scenario_name}_",
            weight=weight,
            slots=slots,
            slot_hours=slot_hours,
            state=state,
            prices_planning=prices_planning,
            pv_forecast=pv_forecast,
            pv_percentile=scenario_name,
            load_profile=load_profile,
            managed_loads=managed_loads,
            lp_loads=lp_loads,
            battery_config=battery_config,
            wear_cost_per_kwh=wear_cost_per_kwh,
        )
        scenarios[scenario_name] = vars
        all_cost_terms.extend(cost_terms)

    # Non-anticipativity: slot-0 decisions must be identical across all
    # scenarios (we don't yet know which scenario will materialise, so
    # the action we commit to NOW must be scenario-independent).
    #
    # Base scenario = the heaviest-weighted one (p50 under defaults). The
    # slot-0 *net* battery kW is tied across scenarios, but the per-source
    # decomposition (grid-vs-PV charge) is NOT — it can legitimately
    # differ depending on the PV scenario. `dispatch_from_slot` reads the
    # base scenario's decomposition to choose between mode 3 (grid-first)
    # and mode 4 (PV-first). Picking the heaviest-weighted scenario makes
    # that decomposition reflect the most likely PV outcome rather than
    # the pessimistic one.
    base_name = max(weights, key=weights.get)
    base = scenarios[base_name]
    for name, vars in scenarios.items():
        if name == base_name:
            continue
        _add_non_anticipativity(prob, base, vars, name, lp_loads)

    prob += pulp.lpSum(all_cost_terms), "weighted_total_cost_cents"

    return prob, StochasticLPVars(
        slots=slots,
        slot_hours=slot_hours,
        scenarios=scenarios,
        base_scenario=base_name,
    )


# ── Internals ────────────────────────────────────────────────────


def _add_scenario_to_problem(
    prob: pulp.LpProblem,
    prefix: str,
    weight: float,
    slots: list[datetime],
    slot_hours: float,
    state: SystemState,
    prices_planning: list[PriceInterval],
    pv_forecast: list[PVForecast] | None,
    pv_percentile: str,
    load_profile: LoadProfile,
    managed_loads: list[ManagedLoadStatus],
    lp_loads: list[LPLoad],
    battery_config: BatteryConfig,
    wear_cost_per_kwh: float,
) -> tuple[LPVars, list[pulp.LpAffineExpression]]:
    """Add one scenario's variables, constraints, and weighted cost terms
    to the problem. Returns the LPVars and the list of cost terms (already
    multiplied by `weight` for the caller to sum into the objective).
    """
    n = len(slots)

    # PV availability per slot for this scenario's percentile
    pv_avail = [_pv_estimate_at(pv_forecast, slots[t], pv_percentile) for t in range(n)]

    # ── Battery flow variables ───────────────────────────────────
    bat_charge_grid = [
        pulp.LpVariable(f"{prefix}bcg_{t}", lowBound=0.0, upBound=battery_config.max_ac_charge_kw)
        for t in range(n)
    ]
    bat_charge_pv = [
        pulp.LpVariable(f"{prefix}bcp_{t}", lowBound=0.0, upBound=battery_config.max_dc_charge_kw)
        for t in range(n)
    ]
    bat_discharge = [
        pulp.LpVariable(f"{prefix}bd_{t}", lowBound=0.0, upBound=battery_config.max_discharge_kw)
        for t in range(n)
    ]

    # ── Grid flow variables ──────────────────────────────────────
    grid_import = [
        pulp.LpVariable(
            f"{prefix}gi_{t}",
            lowBound=0.0,
            upBound=battery_config.max_ac_charge_kw + 20.0,
        )
        for t in range(n)
    ]
    grid_export = [
        pulp.LpVariable(
            f"{prefix}ge_{t}",
            lowBound=0.0,
            upBound=battery_config.export_limit_kw,
        )
        for t in range(n)
    ]

    # ── PV allocation ────────────────────────────────────────────
    pv_to_house = [
        pulp.LpVariable(f"{prefix}pvh_{t}", lowBound=0.0, upBound=pv_avail[t]) for t in range(n)
    ]
    pv_to_battery = [
        pulp.LpVariable(f"{prefix}pvb_{t}", lowBound=0.0, upBound=pv_avail[t]) for t in range(n)
    ]
    pv_to_export = [
        pulp.LpVariable(f"{prefix}pve_{t}", lowBound=0.0, upBound=pv_avail[t]) for t in range(n)
    ]
    pv_curtailed = [
        pulp.LpVariable(f"{prefix}pvc_{t}", lowBound=0.0, upBound=pv_avail[t]) for t in range(n)
    ]

    # ── SOC trajectory ───────────────────────────────────────────
    # Variable bounds are [0, 100] — the physical range. The operating
    # band (`soc_floor_pct`..`soc_ceiling_pct`) is enforced as soft
    # constraints with slack variables penalised in the objective.
    # Reason: if the inverter's local EMS (e.g. mode 2) charges past our
    # ceiling before we regain control, the initial SOC will be outside
    # the band, and a hard bound makes the LP infeasible immediately.
    # Slack variables let the LP plan the fastest legal return to band
    # while staying solvable.
    soc_pct = [
        pulp.LpVariable(
            f"{prefix}soc_{t}",
            lowBound=0.0,
            upBound=100.0,
        )
        for t in range(n)
    ]
    # Slack on each side of the operating band, per slot. Penalty is
    # large enough to dominate any arbitrage gain but finite (not big-M)
    # so the LP stays numerically well-conditioned.
    soc_over_ceiling = [
        pulp.LpVariable(f"{prefix}soc_over_{t}", lowBound=0.0) for t in range(n)
    ]
    soc_under_floor = [
        pulp.LpVariable(f"{prefix}soc_under_{t}", lowBound=0.0) for t in range(n)
    ]
    effective_floor = max(
        battery_config.soc_floor_pct, battery_config.backup_soc_pct
    )

    # ── Add load variables (each load contributes its own vars) ──
    load_vars: dict[str, LoadVars] = {}
    status_by_id = {s.load_id: s for s in managed_loads}
    for load in lp_loads:
        status = status_by_id.get(load.load_id)
        if status is None:
            continue
        lv = load.add_to(
            prob=prob,
            slots=slots,
            slot_hours=slot_hours,
            status=status,
            var_prefix=prefix,
        )
        load_vars[load.load_id] = lv

    # ── Constraints ──────────────────────────────────────────────
    for t in range(n):
        house_base = _house_load_at(load_profile, slots[t])
        load_total = pulp.lpSum(
            load_vars[load.load_id].power_kw[t] for load in lp_loads if load.load_id in load_vars
        )

        # House energy balance: inflows = outflows for the house bus
        prob += (
            pv_to_house[t] + bat_discharge[t] + grid_import[t]
            == house_base + load_total + bat_charge_grid[t],
            f"{prefix}house_balance_{t}",
        )

        # PV allocation: every kWh of PV goes somewhere (or is curtailed)
        prob += (
            pv_to_house[t] + pv_to_battery[t] + pv_to_export[t] + pv_curtailed[t] == pv_avail[t],
            f"{prefix}pv_alloc_{t}",
        )

        # Bind pv_to_battery to bat_charge_pv (they're the same flow)
        prob += (
            bat_charge_pv[t] == pv_to_battery[t],
            f"{prefix}pv_to_bat_link_{t}",
        )

        # System-wide balance: total sources = total sinks. This implicitly
        # links grid_export to whatever battery+PV isn't absorbed by house.
        prob += (
            pv_avail[t] - pv_curtailed[t] + bat_discharge[t] + grid_import[t]
            == house_base + load_total + bat_charge_grid[t] + bat_charge_pv[t] + grid_export[t],
            f"{prefix}system_balance_{t}",
        )

        # Soft operating-band constraints. `soc_pct[t]` is physically in
        # [0, 100] but should stay in [effective_floor, soc_ceiling_pct]
        # under normal operation. Slack activates only when the LP
        # physically can't meet the band (e.g. initial SOC above ceiling).
        prob += (
            soc_pct[t] <= battery_config.soc_ceiling_pct + soc_over_ceiling[t],
            f"{prefix}soc_ceiling_soft_{t}",
        )
        prob += (
            soc_pct[t] >= effective_floor - soc_under_floor[t],
            f"{prefix}soc_floor_soft_{t}",
        )

    # ── SOC dynamics ─────────────────────────────────────────────
    capacity = battery_config.capacity_kwh
    eta = battery_config.round_trip_efficiency
    for t in range(n):
        prev = state.soc_pct if t == 0 else soc_pct[t - 1]
        delta_pct = (
            ((bat_charge_grid[t] + bat_charge_pv[t]) * eta - bat_discharge[t])
            * slot_hours
            / capacity
            * 100.0
        )
        prob += (soc_pct[t] == prev + delta_pct, f"{prefix}soc_dyn_{t}")

    # ── Terminal SOC ─────────────────────────────────────────────
    # Reserve energy past the end of the (possibly truncated) priced
    # horizon. Without this the LP values end-of-horizon energy at 0 and
    # will happily arrive at the floor — fine within the model, disastrous
    # when the next tick's forecast reveals an evening peak we can no
    # longer cover. Softened with slack to stay feasible when the initial
    # SOC is too low to physically recover to terminal_floor within the
    # horizon; the penalty pushes the LP to recover as much as it can.
    terminal_floor = max(
        battery_config.soc_floor_pct,
        battery_config.backup_soc_pct,
        TERMINAL_SOC_FLOOR_PCT,
    )
    soc_terminal_slack = pulp.LpVariable(
        f"{prefix}soc_terminal_slack", lowBound=0.0
    )
    prob += (
        soc_pct[n - 1] >= terminal_floor - soc_terminal_slack,
        f"{prefix}terminal_soc",
    )

    # ── Cost terms (already weighted) ────────────────────────────
    # Import price: prefer Amber's `advancedPrice.predicted` when present
    # (their own ML forecast; explicitly recommended by Amber for
    # forecasting) and fall back to `perKwh` (AEMO point estimate) when
    # not. `predicted` is populated for ~24h of forward intervals;
    # `perKwh` is always populated. Export price stays as `perKwh` —
    # advancedPrice is on the general channel only.
    cost_terms: list[pulp.LpAffineExpression] = []
    for t in range(n):
        price = _price_at(prices_planning, slots[t])
        ip = price.forecast_predicted if price.forecast_predicted is not None else price.import_per_kwh
        ep = price.export_per_kwh
        cost_terms.append(weight * grid_import[t] * ip * slot_hours)
        cost_terms.append(-weight * grid_export[t] * ep * slot_hours)
        cost_terms.append(
            weight
            * (bat_charge_grid[t] + bat_charge_pv[t] + bat_discharge[t])
            * wear_cost_per_kwh
            * slot_hours
        )
        # SOC out-of-band penalty (see constants.SOC_BOUND_PENALTY for
        # sizing). Weighted like any other cost term so all scenarios
        # contribute their share; slack is zero in nominal conditions.
        cost_terms.append(
            weight * SOC_BOUND_PENALTY * (soc_over_ceiling[t] + soc_under_floor[t])
        )
    # Terminal SOC slack (one variable for the whole horizon).
    cost_terms.append(weight * SOC_BOUND_PENALTY * soc_terminal_slack)

    return (
        LPVars(
            slots=slots,
            slot_hours=slot_hours,
            bat_charge_grid=bat_charge_grid,
            bat_charge_pv=bat_charge_pv,
            bat_discharge=bat_discharge,
            grid_import=grid_import,
            grid_export=grid_export,
            pv_to_house=pv_to_house,
            pv_to_battery=pv_to_battery,
            pv_to_export=pv_to_export,
            pv_curtailed=pv_curtailed,
            soc_pct=soc_pct,
            loads=load_vars,
            soc_over_ceiling=soc_over_ceiling,
            soc_under_floor=soc_under_floor,
            soc_terminal_slack=soc_terminal_slack,
            weight=weight,
        ),
        cost_terms,
    )


def _add_non_anticipativity(
    prob: pulp.LpProblem,
    base: LPVars,
    other: LPVars,
    other_name: str,
    lp_loads: list[LPLoad],
) -> None:
    """Tie slot-0 decisions across scenarios.

    Only what we genuinely commit to at slot 0 is tied — physically,
    this is the battery setpoint (signed kW) and load relay states.
    Everything else (per-scenario PV allocation, grid import) is derived
    from the actual PV that materialises in each scenario via the
    energy balance.

    `grid_export[0]` is deliberately NOT tied. The value we actually
    write to register 40038 is a *ceiling*, not a setpoint — each
    scenario's slot-0 export flow may legitimately differ inside that
    ceiling. The cap is derived post-solve across all scenarios in
    `solver.py::_extract_solution`: any scenario planning positive
    export → write the DNSP cap (so a better-than-expected PV
    realisation can flow out); all scenarios agree on zero → pin to 0
    (so transient PV above plan can't leak at a negative export price).

    Tying the individual PV variables would over-constrain the problem:
    different scenarios have different `pv_avail[0]` values, and the
    `pv_alloc` constraint (allocations sum to availability) can't hold
    for three different totals with identical allocations.
    """
    # Net battery kW at slot 0 (signed: + charge, − discharge) — the
    # single commitment we send to register 40031.
    base_net = base.bat_charge_grid[0] + base.bat_charge_pv[0] - base.bat_discharge[0]
    other_net = other.bat_charge_grid[0] + other.bat_charge_pv[0] - other.bat_discharge[0]
    prob += (other_net == base_net, f"nonanti_bat_net_{other_name}")

    # Per-load slot-0 binaries (relay states).
    for load_id, base_lv in base.loads.items():
        other_lv = other.loads.get(load_id)
        if other_lv is None:
            continue
        for extra_name in ("relay",):
            base_extra = base_lv.extras.get(extra_name)
            other_extra = other_lv.extras.get(extra_name)
            if base_extra is not None and other_extra is not None:
                prob += (
                    other_extra[0] == base_extra[0],
                    f"nonanti_{load_id}_{extra_name}_{other_name}",
                )


# ── Helpers ──────────────────────────────────────────────────────


def _slot_grid(start: datetime, horizon_hours: int, slot_minutes: int) -> list[datetime]:
    """Slot start times, snapped backwards to the nearest slot boundary."""
    minute_offset = start.minute % slot_minutes
    snapped = start.replace(second=0, microsecond=0) - timedelta(minutes=minute_offset)
    n = (horizon_hours * 60) // slot_minutes
    step = timedelta(minutes=slot_minutes)
    return [snapped + step * t for t in range(n)]


def _pv_estimate_at(
    pv_forecast: list[PVForecast] | None,
    t: datetime,
    percentile: str = "p50",
) -> float:
    """PV estimate at slot start t for a given percentile.

    `percentile` is one of "p10", "p50", "p90" — maps to the matching
    field on PVForecast. Falls back to 0 if no forecast covers t.
    """
    if not pv_forecast:
        return 0.0
    field_map = {
        "p10": "pv_estimate10_kw",
        "p50": "pv_estimate_kw",
        "p90": "pv_estimate90_kw",
    }
    attr = field_map.get(percentile, "pv_estimate_kw")
    for pv in pv_forecast:
        if pv.start <= t < pv.end:
            return float(getattr(pv, attr))
    return 0.0


def _house_load_at(profile: LoadProfile, t: datetime) -> float:
    """Return the expected house load at slot time `t`.

    `LoadProfile.slots` is contractually 48 × 30-min slots covering a
    local day. `slot_index` maps any datetime to 0..47, so the lookup
    never misses in a correctly-built profile. We assert that here
    rather than silently falling back to a magic default — a mis-sized
    profile is a bug worth surfacing (via fallback to SELF_CONSUME at
    the tick level).
    """
    from ..time_utils import slot_index

    idx = slot_index(t)
    if not (0 <= idx < len(profile.slots)):
        raise ValueError(
            f"LoadProfile has {len(profile.slots)} slots, "
            f"expected 48 (got index {idx} for {t.isoformat()})"
        )
    return float(profile.slots[idx])


def _truncate_to_priced(
    slots: list[datetime],
    prices: list[PriceInterval],
) -> list[datetime]:
    """Return only those slots whose start time falls within the priced
    coverage — i.e. before the latest price interval's end.

    We could also check that the *first* price interval covers slots[0],
    but in practice Amber always returns a "current" interval; if that
    assumption is ever violated, `_price_at` will raise loudly rather than
    silently using a wrong price.
    """
    if not prices:
        return []
    horizon_end = max(p.end for p in prices)
    kept: list[datetime] = []
    for s in slots:
        if s < horizon_end:
            kept.append(s)
        else:
            break
    return kept


def _price_at(prices: list[PriceInterval], t: datetime) -> PriceInterval:
    """Return the price interval covering `t`.

    Raises ValueError if `t` is outside the forecast. Callers must
    truncate the slot grid to priced coverage before building the LP
    (see `_truncate_to_priced`). This was previously a silent `prices[-1]`
    fallback that let the LP plan against made-up prices for up to 24h —
    see KNOWN-ISSUES S1 in the review notes.
    """
    for p in prices:
        if p.start <= t < p.end:
            return p
    raise ValueError(
        f"No price interval covers {t.isoformat()} "
        f"(forecast spans {prices[0].start.isoformat()} → {prices[-1].end.isoformat()})"
    )
