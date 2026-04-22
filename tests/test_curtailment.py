"""Tests for the flat-top PV curtailment detector.

The detector is a pure (state, inputs) → (event, body) function — no
I/O, no clock. Drive it with hand-built tick sequences and assert
on the emitted kind.
"""

from __future__ import annotations

import pytest

from optimiser.curtailment import (
    STREAK_THRESHOLD_TICKS,
    CurtailmentState,
    evaluate,
)


_CEILING = 95.0  # Matches BatteryConfig default soc_ceiling_pct


def _at_ceiling(**overrides):
    """Default inputs representing a clear curtailment state.
    Override fields per test to cover edge cases."""
    base = dict(
        soc_pct=95.0,          # Exactly at ceiling
        battery_power_kw=0.0,  # Battery not absorbing
        pv_kw=5.5,             # Equals house + export_limit
        house_load_kw=0.5,
        grid_export_limit_kw=5.0,
        soc_ceiling_pct=_CEILING,
    )
    base.update(overrides)
    return base


class TestEvaluate:
    def test_first_tick_under_threshold_emits_nothing(self) -> None:
        state = CurtailmentState()
        kind, body = evaluate(state=state, **_at_ceiling())
        assert kind is None and body is None
        assert state.streak == 1
        assert not state.fired

    def test_streak_fires_on_threshold(self) -> None:
        state = CurtailmentState()
        for _ in range(STREAK_THRESHOLD_TICKS - 1):
            kind, _ = evaluate(state=state, **_at_ceiling())
            assert kind is None
        kind, body = evaluate(state=state, **_at_ceiling())
        assert kind == "suspected"
        assert body is not None
        assert body["streak_ticks"] == STREAK_THRESHOLD_TICKS
        assert state.fired

    def test_fired_streak_suppresses_further_suspected_events(self) -> None:
        state = CurtailmentState()
        for _ in range(STREAK_THRESHOLD_TICKS + 5):
            evaluate(state=state, **_at_ceiling())
        # Only one "suspected" fires per episode — further matching
        # ticks extend the streak but don't re-emit.
        kind, _ = evaluate(state=state, **_at_ceiling())
        assert kind is None
        assert state.fired

    def test_non_match_clears_fired_streak(self) -> None:
        state = CurtailmentState()
        # Get into fired state
        for _ in range(STREAK_THRESHOLD_TICKS):
            evaluate(state=state, **_at_ceiling())
        assert state.fired
        # A single non-matching tick (cloud passes, PV drops well below
        # ceiling) clears the latch.
        kind, body = evaluate(state=state, **_at_ceiling(pv_kw=2.0))
        assert kind == "cleared"
        assert body is not None
        assert body["prior_streak_ticks"] >= STREAK_THRESHOLD_TICKS
        assert not state.fired
        assert state.streak == 0

    def test_non_match_without_fired_emits_nothing(self) -> None:
        """Transient one-off non-match during streak build-up doesn't
        emit a 'cleared' (we never fired a 'suspected' in the first place)."""
        state = CurtailmentState()
        evaluate(state=state, **_at_ceiling())   # streak 1
        evaluate(state=state, **_at_ceiling())   # streak 2
        kind, _ = evaluate(state=state, **_at_ceiling(pv_kw=1.0))
        assert kind is None
        assert state.streak == 0

    def test_battery_actively_charging_is_not_curtailment(self) -> None:
        """Battery absorbing PV → MPPT is not at its ceiling. Not curtailment."""
        state = CurtailmentState()
        for _ in range(STREAK_THRESHOLD_TICKS + 2):
            # Battery at 5 kW charge, pv at sink = house + export + battery
            kind, _ = evaluate(
                state=state, **_at_ceiling(battery_power_kw=5.0, pv_kw=5.5)
            )
            assert kind is None
        assert state.streak == 0

    def test_soc_below_ceiling_is_not_curtailment(self) -> None:
        """If SOC has headroom, PV could still flow to battery — we're
        not actually pinned."""
        state = CurtailmentState()
        for _ in range(STREAK_THRESHOLD_TICKS + 2):
            kind, _ = evaluate(state=state, **_at_ceiling(soc_pct=80.0))
            assert kind is None

    def test_pv_below_ceiling_is_not_curtailment(self) -> None:
        """Natural cloud cover — PV below what the sinks could absorb."""
        state = CurtailmentState()
        for _ in range(STREAK_THRESHOLD_TICKS + 2):
            kind, _ = evaluate(state=state, **_at_ceiling(pv_kw=3.0))
            assert kind is None

    def test_missing_house_load_does_not_emit(self) -> None:
        """Grid sensor offline → house_load None. Detector decays
        any prior streak and emits nothing."""
        state = CurtailmentState()
        kind, _ = evaluate(state=state, **_at_ceiling(house_load_kw=None))
        assert kind is None
        assert state.streak == 0

    def test_missing_export_limit_does_not_emit(self) -> None:
        """Before the first LP tick writes the cap, we don't know what
        the inverter is capped at. Can't make a call."""
        state = CurtailmentState()
        kind, _ = evaluate(state=state, **_at_ceiling(grid_export_limit_kw=None))
        assert kind is None

    def test_recovery_then_new_curtailment_fires_again(self) -> None:
        """After clearing, a second curtailment episode fires its own
        'suspected' event — this ensures daily summaries aggregate per
        episode, not per day."""
        state = CurtailmentState()
        for _ in range(STREAK_THRESHOLD_TICKS):
            evaluate(state=state, **_at_ceiling())
        assert state.fired
        # Cloud passes
        evaluate(state=state, **_at_ceiling(pv_kw=2.0))
        assert not state.fired
        # Sun returns, battery still full
        for i in range(STREAK_THRESHOLD_TICKS - 1):
            kind, _ = evaluate(state=state, **_at_ceiling())
            assert kind is None
        kind, _ = evaluate(state=state, **_at_ceiling())
        assert kind == "suspected"

    @pytest.mark.parametrize(
        "pv_kw, expect_match",
        [
            (5.2, True),   # Within 0.3 of ceiling
            (5.5, True),   # Exactly at
            (5.8, True),   # 0.3 over — still within tolerance
            (6.0, False),  # Well over — not a match (shouldn't happen physically)
            (4.9, False),  # Under — natural variation
        ],
    )
    def test_ceiling_tolerance_band(self, pv_kw: float, expect_match: bool) -> None:
        state = CurtailmentState()
        kind, _ = evaluate(state=state, **_at_ceiling(pv_kw=pv_kw))
        if expect_match:
            assert state.streak == 1
        else:
            assert state.streak == 0
