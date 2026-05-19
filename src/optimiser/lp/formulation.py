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
from typing import TYPE_CHECKING

import pulp

from ..config import BatteryConfig
from ..types import LoadProfile, ManagedLoadStatus, PriceInterval, PVForecast, SystemState
from .constants import (
    DEFAULT_SCENARIO_WEIGHTS,
    EXPORT_TIE_BREAK_PENALTY_PER_KWH,
    HORIZON_HOURS,
    IMPORT_TIE_BREAK_REWARD_PER_KWH,
    PRICE_SCENARIO_MODE,
    PV_CURTAIL_PENALTY_PER_KWH,
    SLOT_MINUTES,
    SOC_BOUND_PENALTY,
    WEAR_COST_PER_KWH,
    terminal_soc_floor_pct,
)
from .loads import LoadVars, LPLoad
from .scenarios import (
    PriceScenario,
    PriceScenarioMode,
    build_price_scenarios,
)

if TYPE_CHECKING:
    from ..modes import ModeOverrides


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
    #
    # Only an upper-band slack exists. The lower-band slack was retired
    # 2026-04-25 (the 1e4 penalty was forcing the LP to grid-charge to
    # recover `effective_floor` regardless of price); the
    # terminal-floor constraint at the end of the horizon now provides
    # the only long-horizon SOC-recovery pressure.
    soc_over_ceiling: list[pulp.LpVariable] = field(default_factory=list)
    soc_terminal_slack: pulp.LpVariable | None = None
    weight: float = 1.0


@dataclass
class StochasticLPVars:
    """Multi-scenario LP vars. Slot-0 decisions across all scenarios are
    tied by non-anticipativity constraints — they take the same value
    in any solved scenario, so reading from any one is equivalent.

    `base` is the canonical scenario for slot-0 extraction.

    Compound scenario keys are `f"{pv_name}__{price_name}"` (see
    `build_stochastic_lp`). In POINT mode that's `p10__point`,
    `p50__point`, `p90__point`. Use `pv_scenario(name)` to find a
    scenario by PV percentile alone — it returns the heaviest-weighted
    compound for that PV bucket, so callers can keep their old
    `scenarios["p10"]` semantics across all modes.
    """

    slots: list[datetime]
    slot_hours: float
    scenarios: dict[str, LPVars]
    base_scenario: str

    @property
    def base(self) -> LPVars:
        return self.scenarios[self.base_scenario]

    def pv_scenario(self, pv_name: str) -> LPVars:
        """Return the heaviest-weighted compound scenario for a PV
        percentile. Stable across POINT/SHARED/CROSS price modes.

        Used by code that historically indexed `scenarios["p10"]` and
        wants to preserve that semantic without caring about the price
        axis (e.g. dispatch_from_slot looking at the central forecast,
        or diagnostic tooling that compares P10 vs P90 trajectories).
        """
        prefix = f"{pv_name}__"
        candidates = [
            (name, vars) for name, vars in self.scenarios.items() if name.startswith(prefix)
        ]
        if not candidates:
            raise KeyError(
                f"no compound scenario for pv_name={pv_name!r}; available={list(self.scenarios)}"
            )
        return max(candidates, key=lambda nv: nv[1].weight)[1]


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
    terminal_floor_override_pct: float | None = None,
    mode_overrides: ModeOverrides | None = None,
) -> tuple[pulp.LpProblem, LPVars]:
    """Build a single-scenario deterministic LP.

    Always uses POINT-mode price resolution (predicted-or-spot). The
    price-scenario stochastic axis only exists in `build_stochastic_lp`
    — this function is the deterministic baseline used in tests and
    diagnostics.
    """
    if not prices_planning:
        raise ValueError("build_lp requires non-empty prices_planning")

    slots = _slot_grid(state.timestamp, horizon_hours, slot_minutes)
    slots = _truncate_to_priced(slots, prices_planning)
    if not slots:
        raise ValueError("No LP slots covered by prices_planning — forecast starts after slot 0?")
    slot_hours = slot_minutes / 60.0

    prob = pulp.LpProblem("energy_optimiser", pulp.LpMinimize)

    point_scenario = build_price_scenarios(PriceScenarioMode.POINT)[0]
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
        price_scenario=point_scenario,
        terminal_floor_override_pct=terminal_floor_override_pct,
        mode_overrides=mode_overrides,
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
    price_scenario_mode: PriceScenarioMode | None = None,
    slot_0_pv_override_kw: float | None = None,
    terminal_floor_override_pct: float | None = None,
    mode_overrides: ModeOverrides | None = None,
) -> tuple[pulp.LpProblem, StochasticLPVars]:
    """Build a two-stage stochastic LP across compound (PV × price) scenarios.

    Each compound scenario gets its own copy of slots-1..N variables (so
    it can plan a scenario-appropriate trajectory), but slot-0 variables
    are tied across scenarios via non-anticipativity constraints. The
    result is the optimal here-and-now decision against the weighted
    expected cost across the compound distribution.

    Compound scenarios: each PV percentile (default P10/P50/P90) is
    paired with each price scenario from `price_scenario_mode`. The
    compound weight is the product `pv_weight × price_weight`. Modes:
      - POINT: 1 price scenario × 3 PV =  3 compound scenarios
      - SHARED: 3 × 3 =  9 compound scenarios
      - CROSS:  9 × 3 = 27 compound scenarios
    See `lp/scenarios.py` for the price-axis taxonomy.

    `scenario_weights`: maps PV scenario name to probability mass.
    Defaults to `DEFAULT_SCENARIO_WEIGHTS` (P10 0.2, P50 0.6, P90 0.2).
    Names must be one of "p10"/"p50"/"p90" (matched to PVForecast
    percentile fields).

    `price_scenario_mode`: defaults to the value of `constants.
    PRICE_SCENARIO_MODE` (which is itself POINT by default — see
    KNOWN-ISSUES #24). Pass an explicit mode to A/B in tests or the
    `/sim-sweep` skill.

    Compound scenario keys are `f"{pv_name}__{price_name}"` (double-
    underscore separator chosen so each component is unambiguously
    parseable). The base scenario is the unique heaviest-weighted
    compound: under default weights that's `p50__point` in POINT mode,
    `p50__shared_predicted` in SHARED, `p50__i_predicted_e_predicted`
    in CROSS.

    ``slot_0_pv_override_kw``: when set, replaces ``pv_avail[0]`` in
    every PV scenario with this value. Slots 1+ keep the per-percentile
    Solcast estimate. Used by the service when a "Phase-A uncap and
    measure" PV probe ran successfully and unsaturated before the
    solve, displacing the conservative P10 forecast at slot-0 with the
    measured ground truth. The non-anticipativity hedge that ties
    ``battery_kw[0]`` across PV scenarios then doesn't gimp slot-0 to
    the worst-case forecast — all scenarios see the same observed PV.
    None preserves the legacy per-scenario forecast behaviour.
    """
    weights = scenario_weights or dict(DEFAULT_SCENARIO_WEIGHTS)
    if not weights:
        raise ValueError("scenario_weights must be non-empty")
    if abs(sum(weights.values()) - 1.0) > 1e-3:
        raise ValueError(f"scenario_weights must sum to 1.0, got {sum(weights.values()):.3f}")
    if not prices_planning:
        raise ValueError("build_stochastic_lp requires non-empty prices_planning")

    mode = price_scenario_mode if price_scenario_mode is not None else PRICE_SCENARIO_MODE
    price_scenarios = build_price_scenarios(mode)

    slots = _slot_grid(state.timestamp, horizon_hours, slot_minutes)
    slots = _truncate_to_priced(slots, prices_planning)
    if not slots:
        raise ValueError("No LP slots covered by prices_planning — forecast starts after slot 0?")
    slot_hours = slot_minutes / 60.0

    prob = pulp.LpProblem("energy_optimiser_stochastic", pulp.LpMinimize)

    scenarios: dict[str, LPVars] = {}
    all_cost_terms: list[pulp.LpAffineExpression] = []

    for pv_name, pv_weight in weights.items():
        for price_scenario in price_scenarios:
            compound_weight = pv_weight * price_scenario.weight
            compound_name = f"{pv_name}__{price_scenario.name}"
            vars, cost_terms = _add_scenario_to_problem(
                prob=prob,
                prefix=f"{compound_name}_",
                weight=compound_weight,
                slots=slots,
                slot_hours=slot_hours,
                state=state,
                prices_planning=prices_planning,
                pv_forecast=pv_forecast,
                pv_percentile=pv_name,
                load_profile=load_profile,
                managed_loads=managed_loads,
                lp_loads=lp_loads,
                battery_config=battery_config,
                wear_cost_per_kwh=wear_cost_per_kwh,
                price_scenario=price_scenario,
                slot_0_pv_override_kw=slot_0_pv_override_kw,
                terminal_floor_override_pct=terminal_floor_override_pct,
                mode_overrides=mode_overrides,
            )
            scenarios[compound_name] = vars
            all_cost_terms.extend(cost_terms)

    # Non-anticipativity: slot-0 decisions must be identical across all
    # compound scenarios (we don't yet know which compound scenario will
    # materialise, so the action we commit to NOW must be independent of
    # both the PV outcome and the price outcome).
    #
    # Base scenario = the unique heaviest-weighted compound. Under
    # default weights that's `(p50, predicted)` ⇒ weight 0.6 × leg
    # marginal. The slot-0 *net* battery kW is tied across scenarios,
    # but the per-source decomposition (grid-vs-PV charge) is NOT — it
    # can legitimately differ across compound scenarios.
    # `dispatch_from_slot` reads the base scenario's decomposition to
    # choose between mode 3 (grid-first) and mode 4 (PV-first). Picking
    # the heaviest-weighted compound makes that decomposition reflect
    # the most likely outcome on both axes.
    base_name = max(scenarios, key=lambda n: scenarios[n].weight)
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
    price_scenario: PriceScenario,
    slot_0_pv_override_kw: float | None = None,
    terminal_floor_override_pct: float | None = None,
    mode_overrides: ModeOverrides | None = None,
) -> tuple[LPVars, list[pulp.LpAffineExpression]]:
    """Add one scenario's variables, constraints, and weighted cost terms
    to the problem. Returns the LPVars and the list of cost terms (already
    multiplied by `weight` for the caller to sum into the objective).

    `price_scenario` resolves the per-slot import / export prices used in
    the cost objective. The deterministic single-scenario case passes
    `PriceScenario(name="point", weight=1.0, "predicted", "predicted")`,
    which collapses to the predicted-or-spot rule. The stochastic case
    passes one PriceScenario per compound scenario; together with the PV
    percentile the scenario carries everything that varies in the cost
    objective.
    """
    n = len(slots)

    # PV availability per slot for this scenario's percentile
    pv_avail = [_pv_estimate_at(pv_forecast, slots[t], pv_percentile) for t in range(n)]
    # Slot-0 override: replace forecast PV with a measured value (e.g.
    # from a pre-LP "uncap and measure" probe). Applied identically to
    # every PV scenario — the whole point is that we *observed* slot-0
    # PV, so non-anticipativity has no forecast uncertainty to hedge
    # against. Slots 1+ keep the per-percentile Solcast estimate
    # because the future remains uncertain.
    if slot_0_pv_override_kw is not None and n > 0:
        pv_avail[0] = max(0.0, slot_0_pv_override_kw)

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
    # ceiling is a soft constraint (slack-penalised) so the LP stays
    # feasible if the inverter's local EMS charged past our ceiling
    # before we regained control. The operating floor is a hard
    # constraint, but clamped to the initial SOC so the LP stays
    # feasible if we start below it (post-fallback / BMS quirk /
    # operator action).
    soc_pct = [
        pulp.LpVariable(
            f"{prefix}soc_{t}",
            lowBound=0.0,
            upBound=100.0,
        )
        for t in range(n)
    ]
    # Slack on the upper side of the operating band, per slot. Penalty
    # is large enough to dominate any arbitrage gain but finite (not
    # big-M) so the LP stays numerically well-conditioned.
    soc_over_ceiling = [pulp.LpVariable(f"{prefix}soc_over_{t}", lowBound=0.0) for t in range(n)]

    # Effective per-slot floor. Same hierarchy as the terminal floor
    # (max of the three configured floor-like bounds) but excluding
    # `TERMINAL_SOC_FLOOR_PCT` — that constant only ever applies at
    # end-of-horizon. Clamped to the initial SOC so the LP stays
    # feasible when we start below the configured floor; in that
    # regime the constraint collapses to "don't discharge further",
    # which is the intended sub-floor behaviour.
    effective_floor = min(
        state.soc_pct,
        max(
            battery_config.soc_floor_pct,
            battery_config.backup_soc_pct,
            battery_config.discharge_cutoff_pct,
        ),
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

        # PV allocation: every kWh of PV goes somewhere (or is curtailed).
        prob += (
            pv_to_house[t] + pv_to_battery[t] + pv_to_export[t] + pv_curtailed[t] == pv_avail[t],
            f"{prefix}pv_alloc_{t}",
        )

        # Bind pv_to_battery to bat_charge_pv (they're the same flow).
        prob += (
            bat_charge_pv[t] == pv_to_battery[t],
            f"{prefix}pv_to_bat_link_{t}",
        )

        # System-wide balance: total sources = total sinks. This is the
        # single energy-conservation constraint.
        #
        # An earlier "house balance" constraint
        #   `pv_to_house + bat_discharge + grid_import
        #      == house_base + load_total + bat_charge_grid`
        # also existed here. Combined with pv_alloc it had the algebraic
        # consequence `grid_export == pv_to_export`, which meant battery
        # energy could serve house load but **never** grid export —
        # silently blocking evening-peak export arbitrage (discovered
        # 2026-04-24 when the plan showed zero battery-to-export during
        # 7-8c evening slots). Dropped.
        prob += (
            pv_avail[t] - pv_curtailed[t] + bat_discharge[t] + grid_import[t]
            == house_base + load_total + bat_charge_grid[t] + bat_charge_pv[t] + grid_export[t],
            f"{prefix}system_balance_{t}",
        )

        # Keep bat_charge_grid bookkeeping honest: grid-sourced battery
        # charging can't exceed grid_import (otherwise LP could label a
        # PV-sourced charge as "grid" arbitrarily, which would mislead
        # the dispatch's mode-3-vs-mode-2 decision).
        prob += (
            bat_charge_grid[t] <= grid_import[t],
            f"{prefix}grid_charge_source_{t}",
        )

        # Keep pv_to_house honest: can't allocate more PV to house than
        # the house is actually consuming. Prevents the LP from padding
        # the pv_to_house reporting variable.
        prob += (
            pv_to_house[t] <= house_base + load_total,
            f"{prefix}pv_to_house_bounded_{t}",
        )

        # Keep pv_to_export ≤ grid_export. The dropped house-balance
        # constraint had the algebraic side-effect of forcing
        # pv_to_export == grid_export; now they're independent unless we
        # bound them. The semantic is "PV's share of total export
        # cannot exceed total export". The implicit complement
        # (grid_export − pv_to_export) is the battery's share of
        # export, which is now allowed to be > 0 (the whole point of
        # this fix).
        prob += (
            pv_to_export[t] <= grid_export[t],
            f"{prefix}pv_to_export_bounded_{t}",
        )

        # Soft upper-band constraint. We penalise excursions above
        # `soc_ceiling_pct` so the LP never plans a charge that pushes
        # SOC over its configured ceiling, but with slack so the LP
        # stays feasible if the initial SOC is already over.
        prob += (
            soc_pct[t] <= battery_config.soc_ceiling_pct + soc_over_ceiling[t],
            f"{prefix}soc_ceiling_soft_{t}",
        )

        # Hard per-slot floor. The LP cannot plan a discharge that
        # drops SOC below `effective_floor`. Two-part design:
        #
        # 1. NO slack on this bound (unlike the previous 1e4 penalty)
        #    so the LP has zero incentive to grid-charge "back up to
        #    floor" — that was the panic-buy regression retired
        #    2026-04-25.
        # 2. The floor is clamped to the initial SOC: if we somehow
        #    start below `soc_floor_pct` (post-fallback re-entry, BMS
        #    quirk, manual operator action), the constraint becomes
        #    `soc_pct[t] >= state.soc_pct` — feasible (the LP just
        #    can't discharge further) and consistent with "fallback-
        #    style" behaviour at sub-floor SOC: no discharge, no panic
        #    buy, PV charge fully welcome (only bounded by PV
        #    availability and `max_dc_charge_kw`), house load served
        #    by grid via the system-balance constraint.
        prob += (
            soc_pct[t] >= effective_floor,
            f"{prefix}soc_floor_hard_{t}",
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
    #
    # Hour-of-day–aware: the terminal floor depends on what NEM hour the
    # last slot lands on (e.g. 30% before morning peak, 15% during PV
    # peak). See `lp/constants.terminal_soc_floor_pct` for the full
    # table + rationale, and `TERMINAL-VALUE-PLAN.md` for the path
    # toward replacing this hand-calibrated curve with a fitted V.
    terminal_time_nem = slots[n - 1] + timedelta(hours=10)
    terminal_floor_from_table = (
        terminal_floor_override_pct
        if terminal_floor_override_pct is not None
        else terminal_soc_floor_pct(terminal_time_nem)
    )
    terminal_floor = max(
        battery_config.soc_floor_pct,
        battery_config.backup_soc_pct,
        battery_config.discharge_cutoff_pct,
        terminal_floor_from_table,
    )
    soc_terminal_slack = pulp.LpVariable(f"{prefix}soc_terminal_slack", lowBound=0.0)
    prob += (
        soc_pct[n - 1] >= terminal_floor - soc_terminal_slack,
        f"{prefix}terminal_soc",
    )

    # ── Mode overrides: buy ──────────────────────────────────────
    # Hard ceiling on grid-charging: at any slot where buy mode is
    # active AND the import price for this scenario exceeds the
    # user-supplied ceiling, force bat_charge_grid to zero. Also,
    # for every in-window slot, forbid battery contribution to
    # grid_export (preserve what was bought). PV export is
    # unaffected — that's controlled by export_cap.
    if mode_overrides is not None and mode_overrides.any_buy_active():
        ceiling = mode_overrides.buy_ceiling_c_per_kwh
        for t in range(n):
            if not mode_overrides.buy_active_at[t]:
                continue
            ip_t = price_scenario.resolve_ip(_price_at(prices_planning, slots[t]))
            if ceiling is not None and ip_t > ceiling:
                prob += (
                    bat_charge_grid[t] == 0,
                    f"{prefix}buy_ceiling_{t}",
                )
            # Battery cannot contribute to grid_export during buy window.
            prob += (
                grid_export[t] <= pv_to_export[t],
                f"{prefix}buy_no_bat_export_{t}",
            )

    # ── Cost terms (already weighted) ────────────────────────────
    # Per-slot price resolution is delegated to `price_scenario`. Its
    # resolver applies the chain: requested band leg → predicted →
    # spot. POINT mode (default; deterministic LP) uses the
    # "predicted" leg, falling back to `import_per_kwh` /
    # `export_per_kwh` on settled intervals. SHARED / CROSS modes
    # provide non-trivial scenarios for the stochastic LP.
    #
    # advancedPrice is published on BOTH channels: the import side
    # from `general.advancedPrice`, the export side from
    # `feedIn.advancedPrice` (verified 2026-04-28 against live API).
    # Export-side fields are sign-flipped at the parser boundary (see
    # clients/amber.py) so positive = revenue from export.
    #
    # See `lp/scenarios.py` for the scenario taxonomy and
    # KNOWN-ISSUES #24 for the calibration/sweep gate before flipping
    # the production default off POINT.
    cost_terms: list[pulp.LpAffineExpression] = []
    for t in range(n):
        price = _price_at(prices_planning, slots[t])
        ip = price_scenario.resolve_ip(price)
        ep = price_scenario.resolve_ep(price)
        cost_terms.append(weight * grid_import[t] * ip * slot_hours)
        cost_terms.append(-weight * grid_export[t] * ep * slot_hours)
        # Export tie-break: at non-positive export prices, add a tiny
        # penalty per kWh exported so the LP deterministically prefers
        # storing over indifferent-export. Doesn't affect positive
        # prices — the term is conditional on ep ≤ 0.
        if ep <= 0:
            cost_terms.append(
                weight * grid_export[t] * EXPORT_TIE_BREAK_PENALTY_PER_KWH * slot_hours
            )
        # Import tie-break: mirror of the export rule. At non-positive
        # import prices (paid-to-take electricity), subtract a tiny
        # reward per kWh imported so the LP deterministically prefers
        # soaking it up over discharging-into-free-import. Symmetric to
        # the export term and conditional on ip ≤ 0; positive prices
        # are unaffected.
        if ip <= 0:
            cost_terms.append(
                -weight * grid_import[t] * IMPORT_TIE_BREAK_REWARD_PER_KWH * slot_hours
            )
        cost_terms.append(
            weight
            * (bat_charge_grid[t] + bat_charge_pv[t] + bat_discharge[t])
            * wear_cost_per_kwh
            * slot_hours
        )
        # PV curtail penalty. Without this, `pv_curtailed` is free in the
        # objective while `bat_charge_pv` carries wear cost — on a flat-
        # priced midday the LP would rather throw PV away than absorb it.
        # Penalty is below wear so genuine forced-curtail cases (battery
        # at hard ceiling, scenario PV exceeds all sinks) still resolve
        # correctly. See constants.PV_CURTAIL_PENALTY_PER_KWH for sizing.
        cost_terms.append(weight * pv_curtailed[t] * PV_CURTAIL_PENALTY_PER_KWH * slot_hours)
        # SOC over-ceiling penalty only (no lower-band slack; see the
        # `soc_over_ceiling` block above). Weighted like any other cost
        # term so all scenarios contribute their share; slack is zero
        # in nominal conditions.
        cost_terms.append(weight * SOC_BOUND_PENALTY * soc_over_ceiling[t])
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
    """Tie cross-scenario decisions: slot-0 battery + every-slot relays.

    Battery: only the slot-0 net kW is tied — that's the single commitment
    we send to register 40031. Per-scenario allocations (PV → house /
    battery / export, grid import) and slot-1+ trajectories diverge so
    each scenario can plan against its own PV trajectory. `grid_export[0]`
    is deliberately NOT tied: the cap we write to register 40038 is a
    ceiling, derived post-solve across scenarios in
    `solver.py::_extract_solution`.

    Relays: tied across scenarios at *every* slot, not just slot 0. The HP
    relay schedule is independent of the PV stochastic axis — HP draw is
    constant, and the only LP coupling to PV is via export prices, which
    are deterministic in POINT mode. Treating the full HP schedule as a
    first-stage decision is therefore equivalent to allowing per-scenario
    schedules under POINT, but lets HiGHS presolve eliminate ~(N-1)/N of
    the binary-relay variables. In practice this dropped the worst-case
    wall-clock solve time from 20.4 s (timeLimit) to ~5 s on the
    evening-peak / hot-water-deadline ticks where the binary relay is the
    binding cost. If price scenarios become non-deterministic (CROSS
    mode, KNOWN-ISSUES #24), this constraint can be relaxed back to
    slot-0-only at a per-scenario relay cost.
    """
    # Net battery kW at slot 0 (signed: + charge, − discharge) — the
    # single commitment we send to register 40031.
    base_net = base.bat_charge_grid[0] + base.bat_charge_pv[0] - base.bat_discharge[0]
    other_net = other.bat_charge_grid[0] + other.bat_charge_pv[0] - other.bat_discharge[0]
    prob += (other_net == base_net, f"nonanti_bat_net_{other_name}")

    # Per-load relay state — tied at every slot (see docstring).
    for load_id, base_lv in base.loads.items():
        other_lv = other.loads.get(load_id)
        if other_lv is None:
            continue
        for extra_name in ("relay",):
            base_extra = base_lv.extras.get(extra_name)
            other_extra = other_lv.extras.get(extra_name)
            if base_extra is not None and other_extra is not None:
                for t in range(len(base_extra)):
                    prob += (
                        other_extra[t] == base_extra[t],
                        f"nonanti_{load_id}_{extra_name}_{other_name}_t{t}",
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
