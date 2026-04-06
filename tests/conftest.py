"""Shared test fixtures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from optimiser.config import ManagedLoadConfig
from optimiser.types import (
    LoadCategory,
    LoadCycleState,
    LoadProfile,
    ManagedLoadStatus,
    PriceInterval,
    SystemState,
)

UTC = UTC
NOW = datetime(2026, 4, 3, 7, 0, 0, tzinfo=UTC)  # 18:00 Canberra (AEDT, before DST end)

# Signal-driven tests use a morning-Canberra timestamp so the rolling scheduler
# has a long horizon to today's 22:00 deadline (~13h, 156 5-min slots).
SIG_NOW = datetime(2026, 4, 2, 22, 0, 0, tzinfo=UTC)  # 09:00 Apr 3 Canberra (AEDT)


def make_price(
    offset_minutes: int = 0,
    import_ckwh: float = 20.0,
    export_ckwh: float = 5.0,
    spike: str = "none",
    descriptor: str = "neutral",
) -> PriceInterval:
    start = NOW + timedelta(minutes=offset_minutes)
    return PriceInterval(
        start=start,
        end=start + timedelta(minutes=30),
        import_per_kwh=import_ckwh,
        export_per_kwh=export_ckwh,
        spot_per_kwh=import_ckwh * 0.3,
        renewables_pct=40.0,
        spike_status=spike,
        descriptor=descriptor,
    )


def make_prices(
    values: list[float],
    export: float = 5.0,
) -> list[PriceInterval]:
    return [
        make_price(offset_minutes=i * 30, import_ckwh=v, export_ckwh=export)
        for i, v in enumerate(values)
    ]


def make_state(
    soc: float = 50.0,
    grid_kw: float = 1.0,
    pv_kw: float = 0.0,
    battery_kw: float = 0.0,
    temp: float | None = 20.0,
    occupied: bool = True,
) -> SystemState:
    return SystemState(
        timestamp=NOW,
        soc_pct=soc,
        battery_power_kw=battery_kw,
        pv_power_kw=pv_kw,
        grid_power_kw=grid_kw,
        house_load_kw=pv_kw + grid_kw - battery_kw,
        ems_mode=2,
        outdoor_temp_c=temp,
        occupied=occupied,
    )


def make_flat_profile(kw: float = 2.0) -> LoadProfile:
    return LoadProfile(
        slots=[kw] * 48,
        maturity_level=0,
        context="test",
    )


def make_hw_status(
    cycle_state: LoadCycleState = LoadCycleState.IDLE,
    power_kw: float = 0.0,
) -> ManagedLoadStatus:
    return ManagedLoadStatus(
        load_id="hot_water",
        category=LoadCategory.SHIFTABLE,
        power_kw=power_kw,
        energy_today_kwh=0.0,
        relay_on=False,
        cycle_state=cycle_state,
    )


# ── Signal-driven load helpers ───────────────────────────────────


def make_signal_load_config(
    daily_target_kwh: float = 4.0,
    draw_kw: float = 1.0,
    deadline_hour_local: int = 22,
    hysteresis_buffer: int = 2,
    hysteresis_extra: int = 4,
    pv_surplus_threshold_kw: float = 0.5,
    element_warning_threshold_kw: float = 2.5,
    power_zero_threshold_kw: float = 0.3,
) -> ManagedLoadConfig:
    return ManagedLoadConfig(
        load_id="hot_water",
        category=LoadCategory.SIGNAL_DRIVEN,
        shelly_host="test",
        has_relay=True,
        daily_target_kwh=daily_target_kwh,
        draw_kw=draw_kw,
        deadline_hour_local=deadline_hour_local,
        hysteresis_buffer=hysteresis_buffer,
        hysteresis_extra=hysteresis_extra,
        pv_surplus_threshold_kw=pv_surplus_threshold_kw,
        element_warning_threshold_kw=element_warning_threshold_kw,
        power_zero_threshold_kw=power_zero_threshold_kw,
    )


def make_signal_status(
    energy_today_kwh: float = 0.0,
    power_kw: float = 0.0,
    relay_on: bool = False,
) -> ManagedLoadStatus:
    return ManagedLoadStatus(
        load_id="hot_water",
        category=LoadCategory.SIGNAL_DRIVEN,
        power_kw=power_kw,
        energy_today_kwh=energy_today_kwh,
        relay_on=relay_on,
        cycle_state=None,
    )


def sig_state(
    soc: float = 50.0,
    timestamp: datetime | None = None,
) -> SystemState:
    return SystemState(
        timestamp=timestamp or SIG_NOW,
        soc_pct=soc,
        battery_power_kw=0.0,
        pv_power_kw=0.0,
        grid_power_kw=1.0,
        house_load_kw=1.0,
        ems_mode=2,
        outdoor_temp_c=20.0,
        occupied=True,
    )


def sig_prices_30min(
    values: list[float],
    start: datetime | None = None,
    export: float = 5.0,
) -> list[PriceInterval]:
    s = start or SIG_NOW
    return [
        PriceInterval(
            start=s + timedelta(minutes=i * 30),
            end=s + timedelta(minutes=(i + 1) * 30),
            import_per_kwh=v,
            export_per_kwh=export,
            spot_per_kwh=v * 0.3,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
        )
        for i, v in enumerate(values)
    ]


def sig_prices_5min(
    values: list[float],
    start: datetime | None = None,
    export: float = 5.0,
) -> list[PriceInterval]:
    s = start or SIG_NOW
    return [
        PriceInterval(
            start=s + timedelta(minutes=i * 5),
            end=s + timedelta(minutes=(i + 1) * 5),
            import_per_kwh=v,
            export_per_kwh=export,
            spot_per_kwh=v * 0.3,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
        )
        for i, v in enumerate(values)
    ]
