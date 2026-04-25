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


# ── Terminal SOC ─────────────────────────────────────────────────

# Floor on SOC at the last slot of the (possibly truncated) LP horizon.
# Guards against "arrive empty at end of planning horizon and then grid-
# import through an unpriced tail". Applied as max(soc_floor_pct, this).
# Crude but safe v1 — replace with a PV-aware terminal value function once
# we have replay data to calibrate.
TERMINAL_SOC_FLOOR_PCT: float = 20.0


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
#   but leaves a lot of achievable arbitrage on the table because it
#   suppresses cycling for spreads under ~20c — which is most of the
#   Amber-tariff day.
#
#   2.5 c/kWh one-way (= 5 c/kWh round-trip) is a pragmatic middle
#   ground: preserves the break-even threshold above typical flat-day
#   spreads (which aren't worth the cycle) while still firing on
#   genuine peak-trough events and spikes.
#
#   Break-even on a single 1kWh arbitrage round-trip at 90% efficiency:
#     required_spread_cents = (2 × wear + import × (1 − 0.9)) / 0.9
#     → W=2.5, 20c import → break-even spread ≈ 7.8c
#
# Tune via replay once snapshots are flowing; suspect we'll land
# somewhere in the 2–4 c/kWh range.
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

# Hard wall-clock limit for the LP solve. Past this, the optimiser falls
# back to SELF_CONSUME mode + all relays off. The 10s budget is generous
# for our problem size (~few thousand variables); production solves
# typically finish in <500ms with HiGHS.
SOLVER_TIMEOUT_S: int = 10

# Mixed-integer treatment: only slot 0 binaries are integer-constrained
# (the decision we commit to this tick). Future-slot binaries are
# LP-relaxed since we re-solve every tick anyway. This keeps the problem
# fast even with multiple binary loads.
RELAX_FUTURE_BINARIES: bool = True


# ── Numerical hygiene ────────────────────────────────────────────

# Treated as zero in floating-point comparisons. Used when reading back
# solver outputs (binary variables sometimes solve to e.g. 0.999998).
NUMERIC_EPS: float = 1e-4
