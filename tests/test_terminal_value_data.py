"""Unit tests for the terminal-value data generator.

End-to-end behaviour (running the simulator against a snapshot archive)
is exercised interactively via the CLI; these tests cover the
forward-looking feature extraction logic, which is the only fresh
code that doesn't already have coverage via simulate.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from optimiser.terminal_value_data import (
    _extract_features,
    _load_profile_kwh,
    _price_kwh_window,
)
from optimiser.types import (
    LoadProfile,
    PriceInterval,
    PVForecast,
    SystemState,
    TickSnapshot,
)


UTC = timezone.utc
ANCHOR = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)  # 22:00 AEST


def _system_state(soc: float = 50.0) -> SystemState:
    return SystemState(
        timestamp=ANCHOR,
        soc_pct=soc,
        battery_power_kw=0.0,
        pv_power_kw=0.0,
        grid_power_kw=1.0,
        house_load_kw=1.0,
        ems_mode=2,
        outdoor_temp_c=20.0,
        occupied=True,
    )


def _pv(start: datetime, end: datetime, p10: float, p50: float, p90: float) -> PVForecast:
    return PVForecast(
        start=start, end=end,
        pv_estimate_kw=p50, pv_estimate10_kw=p10, pv_estimate90_kw=p90,
    )


def _price(start: datetime, end: datetime, imp: float, exp: float = 5.0) -> PriceInterval:
    return PriceInterval(
        start=start, end=end,
        import_per_kwh=imp, export_per_kwh=exp,
        spot_per_kwh=imp * 0.3,
        renewables_pct=40.0,
        spike_status="none",
        descriptor="neutral",
    )


# ── Price window summaries ───────────────────────────────────────


class TestPriceKwhWindow:
    def test_min_max_mean_over_clean_window(self) -> None:
        # 4 × 30-min slots, all overlap [anchor, anchor+2h)
        slots = [
            _price(ANCHOR, ANCHOR + timedelta(minutes=30), 10.0, 5.0),
            _price(ANCHOR + timedelta(minutes=30), ANCHOR + timedelta(hours=1), 20.0, 5.0),
            _price(ANCHOR + timedelta(hours=1), ANCHOR + timedelta(minutes=90), 30.0, 5.0),
            _price(ANCHOR + timedelta(minutes=90), ANCHOR + timedelta(hours=2), 40.0, 8.0),
        ]
        mn, mx, mean, mxe, mne = _price_kwh_window(slots, ANCHOR, ANCHOR + timedelta(hours=2))
        assert mn == 10.0
        assert mx == 40.0
        assert mean == pytest.approx(25.0)  # equal-duration slots
        assert mxe == 8.0
        assert mne == pytest.approx((5 + 5 + 5 + 8) / 4)

    def test_partial_overlap_clipped(self) -> None:
        # First slot only half inside the window; mean should weight by duration
        slots = [
            # Slot fully before anchor → ignored
            _price(ANCHOR - timedelta(hours=1), ANCHOR - timedelta(minutes=30), 99.0),
            # Slot starting before anchor but ending after → 30 min counts
            _price(ANCHOR - timedelta(minutes=30), ANCHOR + timedelta(minutes=30), 10.0),
            # Slot fully inside
            _price(ANCHOR + timedelta(minutes=30), ANCHOR + timedelta(hours=1), 30.0),
            # Slot fully after horizon → ignored
            _price(ANCHOR + timedelta(hours=2), ANCHOR + timedelta(hours=3), 99.0),
        ]
        mn, mx, mean, _, _ = _price_kwh_window(slots, ANCHOR, ANCHOR + timedelta(hours=1))
        assert mn == 10.0
        assert mx == 30.0
        # Equal 30-min weights inside the window → simple average
        assert mean == pytest.approx(20.0)

    def test_empty_window_returns_nan(self) -> None:
        slots: list[PriceInterval] = []
        mn, mx, mean, mxe, mne = _price_kwh_window(slots, ANCHOR, ANCHOR + timedelta(hours=1))
        assert mn != mn  # NaN
        assert mx != mx
        assert mean != mean


# ── Load profile sums ────────────────────────────────────────────


class TestLoadProfileKwh:
    def test_constant_profile_integrates_to_kw_times_hours(self) -> None:
        # 48 × 1.0 kW slots = 24h × 1 kW = 24 kWh
        profile = LoadProfile(slots=[1.0] * 48, maturity_level=0, context="test")
        kwh = _load_profile_kwh(profile, ANCHOR, ANCHOR + timedelta(hours=24))
        assert kwh == pytest.approx(24.0)

    def test_short_window(self) -> None:
        # 1h window of 2 kW = 2 kWh
        profile = LoadProfile(slots=[2.0] * 48, maturity_level=0, context="test")
        kwh = _load_profile_kwh(profile, ANCHOR, ANCHOR + timedelta(hours=1))
        assert kwh == pytest.approx(2.0)

    def test_anchor_off_half_hour_boundary_clips_first_slot(self) -> None:
        # Anchor at :15 → first slot only contributes :15→:30 = 15 min
        anchor_off = ANCHOR.replace(minute=15)
        profile = LoadProfile(slots=[2.0] * 48, maturity_level=0, context="test")
        kwh = _load_profile_kwh(profile, anchor_off, anchor_off + timedelta(hours=1))
        # 15 min @ 2 kW (slot 0) + 30 min @ 2 kW (slot 1) + 15 min @ 2 kW (slot 2) = 2.0 kWh
        assert kwh == pytest.approx(2.0)

    def test_missing_profile_falls_back_to_one_kw(self) -> None:
        # Empty/None → 1 kW × horizon
        kwh = _load_profile_kwh(None, ANCHOR, ANCHOR + timedelta(hours=12))  # type: ignore[arg-type]
        assert kwh == pytest.approx(12.0)
        empty = LoadProfile(slots=[], maturity_level=0, context="test")
        kwh = _load_profile_kwh(empty, ANCHOR, ANCHOR + timedelta(hours=12))
        assert kwh == pytest.approx(12.0)


# ── Full feature extraction ──────────────────────────────────────


class TestExtractFeatures:
    def _snap(
        self,
        pv_slots: list[PVForecast] | None = None,
        price_slots: list[PriceInterval] | None = None,
        load_profile: LoadProfile | None = None,
    ) -> TickSnapshot:
        # Default: 24h of 30-min slots starting at ANCHOR
        if pv_slots is None:
            pv_slots = []
            for i in range(48):
                s = ANCHOR + timedelta(minutes=30 * i)
                e = s + timedelta(minutes=30)
                # Daytime PV: middle 12 slots (slots 12-24 ≈ 18-24h after
                # anchor at 12:00 UTC = 04-10 AEST next morning).
                # Just keep it deterministic: 5 kW for slots 18..30.
                kw = 5.0 if 18 <= i < 30 else 0.0
                pv_slots.append(_pv(s, e, kw * 0.7, kw, kw * 1.3))
        if price_slots is None:
            price_slots = [
                _price(
                    ANCHOR + timedelta(minutes=30 * i),
                    ANCHOR + timedelta(minutes=30 * (i + 1)),
                    imp=15.0,
                    exp=5.0,
                )
                for i in range(48)
            ]
        if load_profile is None:
            load_profile = LoadProfile(slots=[1.0] * 48, maturity_level=0, context="test")
        return TickSnapshot(
            tick_id="t",
            timestamp=ANCHOR,
            version="test",
            system_state=_system_state(),
            price_forecast=price_slots,
            pv_forecast=pv_slots,
            load_profile=load_profile,
            managed_loads=[],
            maturity_level=0,
            output=None,
            actual_cost_cents=None,
            counterfactual_cost_cents=None,
            lp_solution=None,
            lp_dispatch=None,
            system_state_post_dispatch=None,
        )

    def test_extracts_pv_and_load_kwh(self) -> None:
        snap = self._snap()
        f = _extract_features(snap, ANCHOR, horizon_hours=24.0)
        # PV: 12 slots × 30 min × 5 kW = 30 kWh @ P50
        assert f["horizon_pv_p50_kwh"] == pytest.approx(30.0)
        assert f["horizon_pv_p10_kwh"] == pytest.approx(30.0 * 0.7)
        assert f["horizon_pv_p90_kwh"] == pytest.approx(30.0 * 1.3)
        # House: 24h × 1 kW = 24 kWh
        assert f["horizon_house_load_kwh"] == pytest.approx(24.0)

    def test_price_summaries(self) -> None:
        snap = self._snap()
        f = _extract_features(snap, ANCHOR, horizon_hours=24.0)
        assert f["horizon_min_import_c"] == 15.0
        assert f["horizon_max_import_c"] == 15.0
        assert f["horizon_mean_import_c"] == pytest.approx(15.0)
        assert f["horizon_max_export_c"] == 5.0

    def test_nem_time_features(self) -> None:
        # Anchor 12:00 UTC = 22:00 NEM (UTC+10)
        snap = self._snap()
        f = _extract_features(snap, ANCHOR, horizon_hours=24.0)
        assert f["hour_of_day_nem"] == pytest.approx(22.0)
        # 2026-04-26 is Sunday → day_of_week=6
        # NEM 2026-04-26 22:00 = same UTC date Sunday
        assert f["day_of_week_nem"] == 6
        assert f["month"] == 4

    def test_short_horizon_clips_pv(self) -> None:
        snap = self._snap()
        # 1h horizon: anchor + 1h falls in pre-PV window → 0 kWh
        f = _extract_features(snap, ANCHOR, horizon_hours=1.0)
        assert f["horizon_pv_p50_kwh"] == 0.0
        # House: 1h × 1 kW = 1 kWh
        assert f["horizon_house_load_kwh"] == pytest.approx(1.0)

    def test_horizon_ending_mid_pv_window(self) -> None:
        snap = self._snap()
        # 12h horizon ending at slot 24 → PV slots 18-24 = 6 slots × 30 min × 5 kW = 15 kWh
        f = _extract_features(snap, ANCHOR, horizon_hours=12.0)
        assert f["horizon_pv_p50_kwh"] == pytest.approx(15.0)
