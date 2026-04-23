"""Configuration loading from TOML."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .types import LoadCategory


@dataclass(frozen=True, slots=True)
class AmberConfig:
    api_key: str
    site_id: str
    poll_5min_interval_s: int = 60
    poll_30min_interval_s: int = 300
    forecast_intervals_5min: int = 12
    previous_intervals_5min: int = 2
    forecast_intervals_30min: int = 72  # Amber returns up to ~72 (36h); more = longer LP horizon


@dataclass(frozen=True, slots=True)
class SolcastConfig:
    enabled: bool = True
    api_key: str = ""
    resource_id: str = ""
    base_url: str = "https://api.solcast.com.au"
    poll_interval_s: int = 8640  # 86400/10 — exact hobbyist quota
    forecast_hours: int = 48
    # Hobbyist tier: hard 10 successful calls/day, resets at midnight UTC.
    # Tracked client-side; pre-flight refuses to call once the budget is
    # spent (with `safety_buffer` reserved for emergency manual fetches).
    max_calls_per_day: int = 10
    safety_buffer: int = 1


@dataclass(frozen=True, slots=True)
class SigenergyConfig:
    host: str
    port: int = 502
    slave_id: int = 1


@dataclass(frozen=True, slots=True)
class BatteryConfig:
    capacity_kwh: float = 40.0
    soc_floor_pct: float = 15.0
    soc_ceiling_pct: float = 95.0
    # Backup SOC reserved for grid-outage. Hardware (reg 40046) refuses to
    # discharge below this even when grid is present. Effective LP floor is
    # max(soc_floor_pct, backup_soc_pct).
    backup_soc_pct: float = 15.0
    max_ac_charge_kw: float = 10.0  # Grid import limit (AC-coupled)
    max_dc_charge_kw: float = 13.0  # Solar charge limit (DC-coupled, ~PV array size)
    max_discharge_kw: float = 10.0
    round_trip_efficiency: float = 0.90
    export_limit_kw: float = 5.0
    pv_array_kw: float = 13.0  # Nameplate PV capacity


@dataclass(frozen=True, slots=True)
class ManagedLoadConfig:
    load_id: str
    category: LoadCategory
    shelly_host: str
    shelly_channel: int = 0
    has_relay: bool = False

    # ── SHIFTABLE (legacy one-shot cycle model) ──────────────────
    daily_energy_kwh: float | None = None
    cycle_duration_min: int | None = None

    # ── SIGNAL_DRIVEN (rolling daily scheduler — HP in PV mode, EV) ──
    daily_target_kwh: float | None = None
    deadline_hour_local: int = 22  # Local hour by which target must be met.
    hysteresis_buffer: int = 2  # Extra slots beyond strict need before asserting.
    hysteresis_extra: int = 4  # Extra slots beyond k_assert to keep an asserted relay closed.
    pv_surplus_threshold_kw: float = 0.5  # Buffer above draw_kw before scoring as PV surplus.
    element_warning_threshold_kw: float = (
        2.5  # Above this, suspect resistive element (LC misconfigured).
    )

    # ── Common ───────────────────────────────────────────────────
    # draw_kw default 0.9: Haier HP330M1-U1 inverter compressor average. Guess
    # until measured — tune from Shelly CT data once running.
    draw_kw: float | None = 0.9
    # power_zero_threshold_kw 0.3: HP standby/comms typically draws 0.1–0.2 kW.
    # Below 0.3 kW means the compressor is genuinely off.
    power_zero_threshold_kw: float = 0.3
    precondition_strategy: str | None = None


@dataclass(frozen=True, slots=True)
class WeatherConfig:
    bom_url: str = "http://www.bom.gov.au/fwo/IDN60801/IDN60801.94926.json"
    poll_interval_s: int = 1800


@dataclass(frozen=True, slots=True)
class OccupancyConfig:
    unifi_host: str = ""
    unifi_port: int = 443
    unifi_username: str = ""
    unifi_password: str = ""
    unifi_site: str = "default"
    poll_interval_s: int = 300
    tracked_macs: list[str] = field(default_factory=list)
    away_threshold_min: int = 30


@dataclass(frozen=True, slots=True)
class StorageConfig:
    db_path: str = "/var/lib/energy-optimiser/telemetry.duckdb"
    snapshot_dir: str = "/var/lib/energy-optimiser/snapshots"


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    tick_interval_s: int = 60
    telemetry_write_interval_s: int = 300
    # LP configuration
    lp_wall_clock_timeout_s: float = 12.0  # Max wall-clock for LP solve thread
    lp_scenario_weight_p10: float = 0.20  # Stochastic PV scenario weights
    lp_scenario_weight_p50: float = 0.60
    lp_scenario_weight_p90: float = 0.20

    @property
    def lp_scenario_weights(self) -> dict[str, float]:
        return {
            "p10": self.lp_scenario_weight_p10,
            "p50": self.lp_scenario_weight_p50,
            "p90": self.lp_scenario_weight_p90,
        }


@dataclass(frozen=True, slots=True)
class Config:
    amber: AmberConfig
    solcast: SolcastConfig
    sigenergy: SigenergyConfig
    battery: BatteryConfig
    managed_loads: list[ManagedLoadConfig]
    weather: WeatherConfig
    occupancy: OccupancyConfig
    storage: StorageConfig
    planner: PlannerConfig


def _parse_load(raw: dict) -> ManagedLoadConfig:
    return ManagedLoadConfig(
        load_id=raw["load_id"],
        category=LoadCategory(raw["category"]),
        shelly_host=raw["shelly_host"],
        shelly_channel=raw.get("shelly_channel", 0),
        has_relay=raw.get("has_relay", False),
        # Legacy SHIFTABLE
        daily_energy_kwh=raw.get("daily_energy_kwh"),
        cycle_duration_min=raw.get("cycle_duration_min"),
        # SIGNAL_DRIVEN
        daily_target_kwh=raw.get("daily_target_kwh"),
        deadline_hour_local=raw.get("deadline_hour_local", 22),
        hysteresis_buffer=raw.get("hysteresis_buffer", 2),
        hysteresis_extra=raw.get("hysteresis_extra", 4),
        pv_surplus_threshold_kw=raw.get("pv_surplus_threshold_kw", 0.5),
        element_warning_threshold_kw=raw.get("element_warning_threshold_kw", 2.5),
        # Common
        draw_kw=raw.get("draw_kw", 0.9),
        power_zero_threshold_kw=raw.get("power_zero_threshold_kw", 0.3),
        precondition_strategy=raw.get("precondition_strategy"),
    )


def load_config(path: str | Path) -> Config:
    """Load configuration from a TOML file."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    loads = [_parse_load(entry) for entry in raw.get("managed_load", [])]

    return Config(
        amber=AmberConfig(**raw.get("amber", {})),
        solcast=SolcastConfig(**raw.get("solcast", {})),
        sigenergy=SigenergyConfig(**raw.get("sigenergy", {})),
        battery=BatteryConfig(**raw.get("battery", {})),
        managed_loads=loads,
        weather=WeatherConfig(**raw.get("weather", {})),
        occupancy=OccupancyConfig(**raw.get("occupancy", {})),
        storage=StorageConfig(**raw.get("storage", {})),
        planner=PlannerConfig(**raw.get("planner", {})),
    )
