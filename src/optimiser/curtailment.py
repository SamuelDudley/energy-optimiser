"""PV curtailment detector — flat-top heuristic.

When the battery is full and the inverter's MPPT is pinned at the total
available sink (house_load + export_cap + whatever the battery is taking,
which is zero when full), `pv_kw` holds *exactly* at the ceiling instead
of tracking irradiance. That flat plateau is the tell-tale of curtailment;
natural-cloud days produce a wobbly PV curve, not a clean ceiling.

We don't try to quantify how much PV was wasted here — that needs
Solcast's `estimated_actuals` (separate daily backfill, not wired yet).
This module just tracks whether the MPPT *looks* throttled right now
and emits a rising-edge event when it is. Downstream: the tick row can
count "minutes curtailed today" for cheap, and replay can distinguish
"LP planned poorly" from "physics ceiling".
"""

from __future__ import annotations

from dataclasses import dataclass

# Tolerances — tuned for a 60 s tick on a 10 kW inverter with 0.1 kW
# measurement granularity. Too tight and cloud wobble on sunny days
# gives false clears; too loose and mild curtailment hides.
SOC_FULL_MARGIN_PCT: float = 2.0  # How close to ceiling counts as "full"
BATTERY_IDLE_KW: float = 0.2  # Battery considered not absorbing below this
CEILING_MATCH_KW: float = 0.3  # pv ≈ sink_ceiling if within this delta
STREAK_THRESHOLD_TICKS: int = 5  # Consecutive matching ticks before firing


@dataclass
class CurtailmentState:
    """Streak state for the flat-top detector. Stored on Service as a
    mutable singleton — the detector is a pure function of (state, event),
    no hidden globals.

    `fired` is latched True on the first tick past the threshold and
    stays True until a non-matching tick clears it. This keeps the event
    stream one-shot-per-episode rather than one-per-tick.
    """

    streak: int = 0
    fired: bool = False


def evaluate(
    *,
    soc_pct: float,
    battery_power_kw: float,
    pv_kw: float,
    house_load_kw: float | None,
    grid_export_limit_kw: float | None,
    soc_ceiling_pct: float,
    state: CurtailmentState,
) -> tuple[str | None, dict | None]:
    """Advance the detector by one tick.

    Returns `(event_kind, event_body)`:
      - `("suspected", {...})` on rising edge of a ≥ THRESHOLD streak
      - `("cleared", {...})` on the tick that breaks an already-fired streak
      - `(None, None)` on every other tick

    `event_body` carries enough fields for the log consumer to understand
    what triggered or cleared the signal. No side effects — caller emits.
    """

    # Inputs we need for a real decision. If any are missing (grid sensor
    # offline → house_load None; first tick → ceiling None), we can't
    # make a call this tick. Treat as "no match" so any prior streak
    # decays naturally.
    if house_load_kw is None or grid_export_limit_kw is None:
        return _advance_non_match(state)

    # Is the battery effectively full?
    if soc_pct < soc_ceiling_pct - SOC_FULL_MARGIN_PCT:
        return _advance_non_match(state)

    # Is the battery idle (not absorbing PV)?
    if abs(battery_power_kw) > BATTERY_IDLE_KW:
        return _advance_non_match(state)

    # Does pv_kw match the sink ceiling (house_load + export_cap)?
    # Battery contribution is already filtered by the idle check above.
    sink_ceiling = house_load_kw + grid_export_limit_kw
    delta = pv_kw - sink_ceiling
    # One-sided: pv can be *below* the ceiling without curtailment
    # (natural cloud), but pv *at* the ceiling with battery idle and
    # full is the throttle signature. Allow small positive slack for
    # measurement rounding.
    if delta < -CEILING_MATCH_KW or delta > CEILING_MATCH_KW:
        return _advance_non_match(state)

    # Matching tick — extend the streak.
    state.streak += 1
    if state.streak >= STREAK_THRESHOLD_TICKS and not state.fired:
        state.fired = True
        return (
            "suspected",
            {
                "streak_ticks": state.streak,
                "soc_pct": soc_pct,
                "pv_kw": pv_kw,
                "house_load_kw": house_load_kw,
                "export_limit_kw": grid_export_limit_kw,
                "sink_ceiling_kw": sink_ceiling,
            },
        )
    return (None, None)


def _advance_non_match(state: CurtailmentState) -> tuple[str | None, dict | None]:
    """Non-matching tick. Reset the streak; if we had already fired,
    emit a `cleared` event so downstream knows the episode is over."""
    had_fired = state.fired
    prior_streak = state.streak
    state.streak = 0
    state.fired = False
    if had_fired:
        return ("cleared", {"prior_streak_ticks": prior_streak})
    return (None, None)
