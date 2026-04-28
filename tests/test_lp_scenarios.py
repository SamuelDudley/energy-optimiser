"""Tests for the price-scenario constructor (lp/scenarios.py).

Covers:
  - Mode shapes (POINT/SHARED/CROSS produce the expected scenario counts)
  - Weight semantics (each mode's weights sum to 1, CROSS marginals
    multiply correctly)
  - Resolver picks the right band leg per scenario when bands populated
  - Resolver falls back to predicted, then to spot, when bands missing
  - POINT-mode resolver matches the deterministic rule it replaces —
    this is the regression gate against drift in the LP cost objective
  - PriceScenario is hashable and picklable (so it can flow through
    `asyncio.to_thread` and snapshot serialisation without surprises)
"""

from __future__ import annotations

import pickle
from datetime import UTC, datetime, timedelta

import pytest

from optimiser.lp.scenarios import (
    PriceScenario,
    PriceScenarioMode,
    build_price_scenarios,
)
from optimiser.types import PriceInterval


def _interval(
    *,
    import_per_kwh: float = 25.0,
    export_per_kwh: float = 6.0,
    forecast_low: float | None = 22.0,
    forecast_predicted: float | None = 24.0,
    forecast_high: float | None = 28.0,
    export_forecast_low: float | None = 5.0,
    export_forecast_predicted: float | None = 6.5,
    export_forecast_high: float | None = 8.0,
) -> PriceInterval:
    start = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    return PriceInterval(
        start=start,
        end=start + timedelta(minutes=5),
        import_per_kwh=import_per_kwh,
        export_per_kwh=export_per_kwh,
        spot_per_kwh=10.0,
        renewables_pct=40.0,
        spike_status="none",
        descriptor="neutral",
        forecast_low=forecast_low,
        forecast_predicted=forecast_predicted,
        forecast_high=forecast_high,
        export_forecast_low=export_forecast_low,
        export_forecast_predicted=export_forecast_predicted,
        export_forecast_high=export_forecast_high,
    )


# ── POINT ────────────────────────────────────────────────────────


class TestPointMode:
    def test_returns_single_scenario(self) -> None:
        scenarios = build_price_scenarios(PriceScenarioMode.POINT)
        assert len(scenarios) == 1
        s = scenarios[0]
        assert s.name == "point"
        assert s.weight == 1.0

    def test_resolvers_match_predicted_or_spot_rule(self) -> None:
        """POINT mode is the regression gate: its resolved values must
        be exactly what the deterministic in-line rule produced before
        scenarios were introduced. That rule was:
            ip = price.forecast_predicted ?? price.import_per_kwh
            ep = price.export_forecast_predicted ?? price.export_per_kwh
        """
        scenarios = build_price_scenarios(PriceScenarioMode.POINT)
        s = scenarios[0]

        forecast = _interval()
        assert s.resolve_ip(forecast) == pytest.approx(forecast.forecast_predicted)
        assert s.resolve_ep(forecast) == pytest.approx(
            forecast.export_forecast_predicted
        )

        # Settled interval — Amber doesn't carry advancedPrice. POINT
        # must fall back to the spot fields, which by then are the
        # locked actual.
        locked = _interval(
            forecast_low=None,
            forecast_predicted=None,
            forecast_high=None,
            export_forecast_low=None,
            export_forecast_predicted=None,
            export_forecast_high=None,
        )
        assert s.resolve_ip(locked) == pytest.approx(locked.import_per_kwh)
        assert s.resolve_ep(locked) == pytest.approx(locked.export_per_kwh)


# ── SHARED ───────────────────────────────────────────────────────


class TestSharedMode:
    def test_returns_three_scenarios(self) -> None:
        scenarios = build_price_scenarios(PriceScenarioMode.SHARED)
        assert len(scenarios) == 3
        names = [s.name for s in scenarios]
        assert names == ["shared_low", "shared_predicted", "shared_high"]

    def test_default_weights_sum_to_one(self) -> None:
        scenarios = build_price_scenarios(PriceScenarioMode.SHARED)
        total = sum(s.weight for s in scenarios)
        assert total == pytest.approx(1.0)

    def test_default_weights_are_p10_p50_p90(self) -> None:
        scenarios = {s.name: s for s in build_price_scenarios(PriceScenarioMode.SHARED)}
        assert scenarios["shared_low"].weight == pytest.approx(0.2)
        assert scenarios["shared_predicted"].weight == pytest.approx(0.6)
        assert scenarios["shared_high"].weight == pytest.approx(0.2)

    def test_resolvers_track_each_other_through_the_band(self) -> None:
        """SHARED mode pairs import_low with export_low, etc. Verify."""
        scenarios = {s.name: s for s in build_price_scenarios(PriceScenarioMode.SHARED)}
        forecast = _interval()
        assert scenarios["shared_low"].resolve_ip(forecast) == pytest.approx(
            forecast.forecast_low
        )
        assert scenarios["shared_low"].resolve_ep(forecast) == pytest.approx(
            forecast.export_forecast_low
        )
        assert scenarios["shared_high"].resolve_ip(forecast) == pytest.approx(
            forecast.forecast_high
        )
        assert scenarios["shared_high"].resolve_ep(forecast) == pytest.approx(
            forecast.export_forecast_high
        )


# ── CROSS ────────────────────────────────────────────────────────


class TestCrossMode:
    def test_returns_nine_scenarios(self) -> None:
        scenarios = build_price_scenarios(PriceScenarioMode.CROSS)
        assert len(scenarios) == 9

    def test_names_form_three_by_three_grid(self) -> None:
        scenarios = build_price_scenarios(PriceScenarioMode.CROSS)
        expected = {
            f"i_{i}_e_{e}"
            for i in ("low", "predicted", "high")
            for e in ("low", "predicted", "high")
        }
        actual = {s.name for s in scenarios}
        assert actual == expected

    def test_weights_are_product_of_marginals(self) -> None:
        scenarios = {s.name: s for s in build_price_scenarios(PriceScenarioMode.CROSS)}
        # Marginals: low=0.2, predicted=0.6, high=0.2
        # Corner: low × low = 0.04. Centre: predicted × predicted = 0.36.
        assert scenarios["i_low_e_low"].weight == pytest.approx(0.04)
        assert scenarios["i_predicted_e_predicted"].weight == pytest.approx(0.36)
        assert scenarios["i_high_e_high"].weight == pytest.approx(0.04)
        # Off-diagonal: low × predicted
        assert scenarios["i_low_e_predicted"].weight == pytest.approx(0.12)
        assert scenarios["i_high_e_low"].weight == pytest.approx(0.04)

    def test_weights_sum_to_one(self) -> None:
        scenarios = build_price_scenarios(PriceScenarioMode.CROSS)
        total = sum(s.weight for s in scenarios)
        assert total == pytest.approx(1.0)

    def test_resolvers_pick_independent_bands(self) -> None:
        scenarios = {s.name: s for s in build_price_scenarios(PriceScenarioMode.CROSS)}
        forecast = _interval()
        # i_low + e_high: import goes low, export goes high
        s = scenarios["i_low_e_high"]
        assert s.resolve_ip(forecast) == pytest.approx(forecast.forecast_low)
        assert s.resolve_ep(forecast) == pytest.approx(forecast.export_forecast_high)


# ── Resolver fallback ────────────────────────────────────────────


class TestResolverFallback:
    """The fallback chain: requested band leg → predicted → spot.

    Locked intervals (no advancedPrice published) collapse all 9 CROSS
    scenarios to the same value — the spot point estimate. This is the
    invariant that keeps non-anticipativity at slot 0 well-behaved.
    """

    def test_locked_interval_collapses_all_cross_scenarios(self) -> None:
        scenarios = build_price_scenarios(PriceScenarioMode.CROSS)
        locked = _interval(
            forecast_low=None,
            forecast_predicted=None,
            forecast_high=None,
            export_forecast_low=None,
            export_forecast_predicted=None,
            export_forecast_high=None,
        )
        ips = {s.resolve_ip(locked) for s in scenarios}
        eps = {s.resolve_ep(locked) for s in scenarios}
        assert ips == {locked.import_per_kwh}
        assert eps == {locked.export_per_kwh}

    def test_partial_data_falls_back_through_predicted(self) -> None:
        """Defence in depth: if the requested band leg is missing but
        predicted is populated (shouldn't happen with Amber's API but
        could with hand-built or migrated data), use predicted rather
        than collapsing to spot. Stops a band-leg None from silently
        knocking the LP onto the rawer spot field for that scenario.
        """
        partial = _interval(
            forecast_low=None,  # missing
            forecast_predicted=24.0,
            forecast_high=None,  # missing
        )
        scenarios = {s.name: s for s in build_price_scenarios(PriceScenarioMode.CROSS)}
        # i_low scenario falls back through predicted, not to spot
        ip_low = scenarios["i_low_e_predicted"].resolve_ip(partial)
        assert ip_low == pytest.approx(24.0)  # predicted, not 25.0 spot

    def test_export_resolver_uses_export_fields_not_import_fields(self) -> None:
        """Easy bug to introduce: the resolver crosses the channels and
        e.g. resolves ep against forecast_predicted (import side). Pin
        the channel separation so the next refactor can't introduce
        that silently.
        """
        forecast = _interval(
            forecast_predicted=999.0,
            export_forecast_predicted=7.5,
        )
        s = build_price_scenarios(PriceScenarioMode.POINT)[0]
        ep = s.resolve_ep(forecast)
        assert ep == pytest.approx(7.5)
        assert ep != pytest.approx(999.0)


# ── Custom band weights ──────────────────────────────────────────


class TestCustomBandWeights:
    def test_custom_weights_override(self) -> None:
        custom = {"low": 0.1, "predicted": 0.8, "high": 0.1}
        scenarios = build_price_scenarios(
            PriceScenarioMode.SHARED, band_weights=custom
        )
        named = {s.name: s for s in scenarios}
        assert named["shared_low"].weight == pytest.approx(0.1)
        assert named["shared_predicted"].weight == pytest.approx(0.8)
        assert named["shared_high"].weight == pytest.approx(0.1)

    def test_invalid_weights_rejected(self) -> None:
        with pytest.raises(ValueError, match="must sum to 1"):
            build_price_scenarios(
                PriceScenarioMode.SHARED,
                band_weights={"low": 0.5, "predicted": 0.5, "high": 0.5},
            )
        with pytest.raises(ValueError, match="must have keys"):
            build_price_scenarios(
                PriceScenarioMode.SHARED,
                band_weights={"low": 0.2, "high": 0.8},  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="must be non-negative"):
            build_price_scenarios(
                PriceScenarioMode.SHARED,
                band_weights={"low": -0.1, "predicted": 0.6, "high": 0.5},
            )


# ── PriceScenario as a value object ──────────────────────────────


class TestScenarioValueSemantics:
    def test_picklable(self) -> None:
        s = PriceScenario(
            name="point", weight=1.0, import_band="predicted",
            export_band="predicted",
        )
        round_trip = pickle.loads(pickle.dumps(s))
        assert round_trip == s

    def test_hashable(self) -> None:
        s = PriceScenario(
            name="i_low_e_high", weight=0.04,
            import_band="low", export_band="high",
        )
        # Frozen dataclass with slots → hashable. Sanity-check that
        # we can use scenarios as dict keys / set members for caching.
        assert {s} == {s}
