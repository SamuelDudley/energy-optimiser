"""All numeric constants for the LP optimiser, in one place.

Keep these here rather than scattered through formulation code so the
rationale and units are visible in one read. Anything genuinely
configurable per-deployment lives in `config.py` instead.
"""

from __future__ import annotations

# ── Time ─────────────────────────────────────────────────────────

# Slot resolution. 5 minutes captures negative-import / negative-export
# spikes that average out at 30-min granularity. 5-min × 48h horizon
# = 576 slots per scenario.
SLOT_MINUTES: int = 5

# Planning horizon. 48h gives the optimiser visibility into tomorrow's
# PV (overnight EV charging benefit, "save grid for tonight, refill on
# tomorrow's solar" trade-offs).
HORIZON_HOURS: int = 48


# ── Scenario weights (P10/P50/P90 stochastic) ────────────────────

# Default scenario weights — pessimistic, central, optimistic. Sum to 1.
# Weights bias the LP towards the central case while keeping P10
# meaningful as a safety hedge. Configurable in `LPConfig`.
DEFAULT_SCENARIO_WEIGHTS: dict[str, float] = {
    "p10": 0.20,
    "p50": 0.60,
    "p90": 0.20,
}


# ── Price-axis stochasticity ─────────────────────────────────────

# How the LP composes Amber's `advancedPrice.{low,predicted,high}`
# bands into stochastic scenarios. See lp/scenarios.py for the
# semantics of each mode.
#
# - POINT  (default): one scenario, weight 1.0, predicted-or-spot.
#   Reproduces the deterministic LP that shipped before scenarios
#   were introduced. Identical objective and dispatch.
# - SHARED: 3 scenarios pairing import_low/export_low,
#   import_predicted/export_predicted, import_high/export_high.
#   Encodes the NEM-coupled assumption that wholesale surprises
#   move both prices the same direction.
# - CROSS:  9 scenarios on a 3×3 grid. Treats the import and export
#   bands as independent. Composed with the 3 PV scenarios in
#   build_stochastic_lp this becomes 27 compound scenarios.
#
# Default is POINT pending sweep evidence (see KNOWN-ISSUES #24,
# steps d–e). Operators can override per-deployment via the
# `[planner].lp_price_scenario_mode` config knob; the constant
# below is the fallback when the config field is unset.
#
# Imported lazily to avoid a circular import (scenarios.py imports
# from types.py only — but constants.py is imported by formulation.py
# which is in the same package, so an enum import here is fine).
from .scenarios import PriceScenarioMode  # noqa: E402

PRICE_SCENARIO_MODE: PriceScenarioMode = PriceScenarioMode.POINT


# ── Terminal SOC ─────────────────────────────────────────────────

# Floor on SOC at the last slot of the (possibly truncated) LP horizon.
# Guards against "arrive empty at end of planning horizon and then grid-
# import through an unpriced tail". Applied as max(soc_floor_pct, this).
#
# Currently the legacy scalar — used as the constant arm in
# `terminal_soc_floor_pct()` if hour-aware lookup is unavailable. Kept
# separate from the table so the constant baseline (20%) is still
# inspectable. Will be retired once the PV-aware V function trained
# from `terminal_value_data.py` lands.
TERMINAL_SOC_FLOOR_PCT: float = 20.0


# Hand-calibrated piecewise table: NEM hour at the terminal slot →
# terminal-floor SOC %. Captures the time-of-day-dependent value of
# end-of-horizon energy ahead of the proper V function being fitted.
#
# Shape rationale (NEM time, UTC+10, DST-stable):
#   00–05  high — overnight, 5–7h of pure house draw before PV
#   05–07  highest — morning peak window active or imminent, no PV
#   07–10  moderate — peak winding down, PV starting to ramp
#   10–14  lowest — PV peak; battery refills within ~1h regardless
#   14–17  moderate — PV declining, build for evening peak
#   17–20  high — evening peak active
#   20–24  moderate — post-peak overnight buffer
#
# Intentionally piecewise-constant rather than smooth: easy to inspect,
# trivially LP-embeddable (single max() against a per-tick scalar),
# and clearly sub-optimal-on-purpose so the V fit has obvious upside.
_TERMINAL_FLOOR_BY_NEM_HOUR: tuple[tuple[range, float], ...] = (
    (range(0, 5), 28.0),
    (range(5, 7), 30.0),
    (range(7, 10), 22.0),
    (range(10, 14), 15.0),
    (range(14, 17), 20.0),
    (range(17, 20), 28.0),
    (range(20, 24), 22.0),
)


def terminal_soc_floor_pct(terminal_time_nem) -> float:  # type: ignore[no-untyped-def]
    """Return the terminal-floor SOC % for an LP horizon ending at
    `terminal_time_nem` (datetime; expected in NEM time, UTC+10).
    Lookup is piecewise-constant by NEM hour. Falls back to
    `TERMINAL_SOC_FLOOR_PCT` if the hour somehow doesn't match a
    bucket — defensive only, the table covers 0–23 with no gaps.
    """
    h = terminal_time_nem.hour
    for hr_range, floor in _TERMINAL_FLOOR_BY_NEM_HOUR:
        if h in hr_range:
            return floor
    return TERMINAL_SOC_FLOOR_PCT


# ── Signal-driven load (HW heat pump, future EV) ─────────────────

# When today's deadline passes with the daily-energy target unmet, the
# shortfall rolls forward into tomorrow's target. This multiplier caps
# the combined rolled+own target so a stale service (multi-day outage)
# can't demand a physically impractical day's worth of heating in one
# 24h window. Excess beyond the cap is silently forgiven — a tank cold
# for 2+ consecutive days needs operator attention, not a panicked LP
# catch-up. See `BinarySignalDrivenLoad.add_to` for the implementation.
ROLL_FORWARD_CAP_MULTIPLIER: float = 2.0


# ── Battery wear cost ────────────────────────────────────────────

# Per-kWh cost charged to the LP for each direction of battery flow.
# Round-trip cost ≈ 2 × WEAR_COST_PER_KWH (charged once, discharged once).
#
# Sizing rationale:
#   LFP replacement cost (~$500/kWh installed) / usable throughput
#   (~6000 cycles × 0.8 DoD) implies a "true" wear cost floor of roughly
#   10 c/kWh one-way = 20 c/kWh round-trip. That's economically correct
#   but suppresses arbitrage too aggressively on typical-spread days.
#
#   2.5 c/kWh one-way (= 5 c/kWh round-trip) is the pragmatic value: it
#   matches the marginal degradation at the operating point (LFP cycled
#   gently within a 15–100% band), and the LP behaviour falls out
#   correctly as a result — discharge through evening peaks ≥ 8c,
#   grid-arb on cloudy days when day↔peak spread > ~5c, refill from
#   morning PV.
#
#   Brief excursion to 5 c/kWh (2026-04-26 → 2026-04-26) was a
#   redundant safety hedge layered on top of the new hard SOC floor.
#   Closed-loop simulator showed it suppressed legitimate cycling: max
#   SOC capped at ~52% on normal days (battery half-used), $0.78/day of
#   evening-peak revenue forfeited, "might as well leave it in manual
#   mode 2". The hard floor at battery_config.soc_floor_pct is now the
#   safety mechanism — wear cost can return to its true economic value.
#
#   Break-even on a single 1kWh arbitrage round-trip at 90% efficiency:
#     required_spread_cents = (2 × wear + import × (1 − 0.9)) / 0.9
#     → W=2.5, 20c import → break-even spread ≈ 7.8c
#
# Tunable via the `wear_cost_per_kwh` parameter on solve_stochastic /
# build_stochastic_lp / simulate(...) for replay/sweep work; the
# constant is the production default.
WEAR_COST_PER_KWH: float = 2.5


# ── PV curtail penalty ───────────────────────────────────────────
#
# Per-kWh cost charged when the LP allocates surplus PV to `pv_curtailed`
# instead of `bat_charge_pv`. Without this term, curtail is free in the
# objective while charging carries `WEAR_COST_PER_KWH` — so on a flat-
# pricing day the LP prefers to throw surplus PV away rather than store
# it. That's a real failure mode on cheap-wholesale midday slots.
#
# Sizing:
#   - 0 c/kWh: today's behaviour, prefers curtail over charge in flat
#     pricing.
#   - 1 c/kWh: between zero and wear (2.5). LP chooses charge over
#     curtail unless future-use value of the stored kWh is *negative*.
#   - >= 2.5 c/kWh: would override legitimate curtail (battery hard-
#     full, scenario PV beats battery+export+house). Pathological.
#
# 1 c/kWh is roughly the implicit value of stored PV under "no current
# use, future imports cheap" — a hedge against forecast error. Tune via
# replay if the wear cost moves or pricing patterns shift.
PV_CURTAIL_PENALTY_PER_KWH: float = 1.0


# ── Export tie-break penalty ─────────────────────────────────────
#
# At non-positive export prices (ep ≤ 0), exporting earns no revenue
# (or costs money) and the LP is left to choose between export-and-
# break-even-or-lose and store-for-later-use purely on future-use
# value. In tied or near-tied cases, HiGHS picks arbitrarily and the
# slot-0 plan can flip between exporting at 0c and storing across
# successive ticks — noisy with no economic upside.
#
# This tiny penalty (0.05 c/kWh, just above zero) breaks the tie
# deterministically toward storing. Doesn't touch positive export
# prices at all (the penalty only applies when ep ≤ 0), so peak-
# period revenue is unaffected.
EXPORT_TIE_BREAK_PENALTY_PER_KWH: float = 0.05


# ── Import tie-break reward ──────────────────────────────────────
#
# Mirror of EXPORT_TIE_BREAK_PENALTY_PER_KWH on the import side. At
# non-positive import prices (ip ≤ 0; Amber occasionally pays
# customers to take wholesale electricity), importing is free or
# revenue-positive — but at exactly the wear-cost-equivalent
# threshold the LP becomes indifferent between import-and-charge
# and discharge-into-the-free-import. Without a tie-break, HiGHS
# arbitrarily resolves the indifference and slot-0 charge magnitude
# flips across successive ticks at the same conditions.
#
# Subtracted from the cost objective (i.e. an extra 0.05 c/kWh of
# nominal revenue) when ip ≤ 0, biasing the LP toward soaking up
# free electricity. Sized symmetrically with the export penalty;
# well below WEAR_COST_PER_KWH so it cannot drive a charge that
# wear-cost otherwise rejects.
IMPORT_TIE_BREAK_REWARD_PER_KWH: float = 0.05


# ── SOC out-of-band penalty ──────────────────────────────────────
#
# Applied per %-slot of slack when SOC is outside [effective_floor,
# soc_ceiling_pct]. Cost-term units are cents: a price × kWh product.
# Normal slot cost is ~0.1–1.0 cent (sub-kWh at sub-dollar prices).
# Setting the penalty to 1e4 per unit-of-slack-per-slot means: even a
# single-slot 0.1%-of-SOC overshoot costs ~10 cents, more than any
# arbitrage gain — yet stays well below big-M numerical limits.
# The LP will always prefer returning to band, without infeasibility.
SOC_BOUND_PENALTY: float = 1e4


# ── Solver ───────────────────────────────────────────────────────

# Hard wall-clock limit for the LP solve. Past this, the optimiser
# falls back to SELF_CONSUME mode + all relays off. The 20s budget
# absorbs the cost of `_force_binary_relay = True` on
# SIGNAL_DRIVEN_CONTINUOUS loads (every slot's relay is binary, ~390
# binaries per scenario at 5-min × 32h coverage); with the HP HW
# wired in 2026-05-02 typical solve climbed to ~10s. Bumped 10→20
# to give headroom for additional binary loads (future EV) and the
# heavier price-scenario modes:
#   - POINT  (default):  3 compound scenarios. Typical <10s with HW.
#   - SHARED:            9 compound scenarios. Typical <15s with HW.
#   - CROSS:            27 compound scenarios. Risk of timeout — measure first.
# The wall-clock timeout (lp_wall_clock_timeout_s in PlannerConfig)
# defaults to 22s — slightly larger than this so HiGHS' own timeLimit
# fires first and produces a graceful "best feasible" return rather
# than the hard fallback path.
SOLVER_TIMEOUT_S: int = 20

# Mixed-integer treatment: only slot 0 binaries are integer-constrained
# (the decision we commit to this tick). Future-slot binaries are
# LP-relaxed since we re-solve every tick anyway. This keeps the problem
# fast even with multiple binary loads.
RELAX_FUTURE_BINARIES: bool = True


# ── Numerical hygiene ────────────────────────────────────────────

# Treated as zero in floating-point comparisons. Used when reading back
# solver outputs (binary variables sometimes solve to e.g. 0.999998).
NUMERIC_EPS: float = 1e-4
