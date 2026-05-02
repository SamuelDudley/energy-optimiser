"""Configuration loading from TOML."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .hardware import PV_ARRAY_KW
from .types import LoadCategory


@dataclass(frozen=True, slots=True)
class AmberConfig:
    api_key: str
    site_id: str
    # 5-min poll: one fetch per Amber slot, fired 150s into the slot
    # (`:02:30/:07:30/…` UTC). The earlier per-tick cadence (60s) hit
    # Amber's 50/300s bucket and caused mid-slot LP plan flips when
    # prices wobbled. See CLAUDE.md decision-log entry on slot-aligned
    # Amber poll.
    poll_5min_interval_s: int = 300
    poll_5min_offset_s: int = 150
    poll_30min_interval_s: int = 300
    forecast_intervals_5min: int = 12
    previous_intervals_5min: int = 2
    # Amber publishes up to ~79 30-min intervals (~40h); 96 is a small
    # over-ask that captures all of it. Anything higher is wasted bytes.
    forecast_intervals_30min: int = 96
    # Rising-edge alert threshold for the 30-min planning horizon.
    # Amber's daily AEMO-pre-dispatch refresh briefly drops the visible
    # horizon to ~30 intervals (~15 h) before extending back to ~79 (~40
    # h). Set this BELOW the transient-dip floor (~30) so the daily
    # refresh doesn't generate false alarms, and ABOVE the LP horizon
    # we depend on for multi-day arbitrage. 50 catches sustained
    # shrinkage (an Amber API change, AEMO outage, plan/site change)
    # without firing every day. Set to 0 to disable the alert.
    horizon_alert_threshold_30min: int = 50


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
    # The Sigenergy gateway exposes plant-level aggregates on one slave
    # ID (default 247 per HA integration) and per-inverter registers on
    # another (default 1 for a single-inverter install). Plant registers
    # are preferred for aggregates (SOC, grid power, lifetime counters);
    # per-inverter registers are required for cell temps, SOH, MPPT
    # strings, AC quality — they don't exist at the plant address.
    slave_id: int = 247
    inverter_slave_id: int = 1


@dataclass(frozen=True, slots=True)
class BatteryConfig:
    capacity_kwh: float = 40.0
    # LP planning band.
    #
    # `soc_floor_pct` is a HARD per-slot lower bound on the planned SOC
    # trajectory: the LP cannot plan a discharge that drops SOC below
    # this value. There is no slack penalty on the lower bound, so the
    # LP has no incentive to grid-charge "back up to floor" — that
    # avoids the panic-buy regression seen on 2026-04-25 where a 1e4
    # penalty forced grid-charging regardless of price. If the inverter
    # somehow starts a tick below this floor (post-fallback re-entry,
    # BMS quirk, operator action), the constraint is clamped to the
    # current SOC so the LP stays feasible — sub-floor the LP just
    # can't discharge further; it's still free to be clever about
    # charging based on price signals.
    #
    # `soc_ceiling_pct` is a SOFT per-slot upper bound (slack-penalised)
    # so the LP stays feasible when local EMS has already charged above
    # the ceiling.
    #
    # These fields do NOT directly drive the hardware discharge
    # backstop — see `discharge_cutoff_pct` below for that.
    soc_floor_pct: float = 15.0
    soc_ceiling_pct: float = 100.0
    # Hardware discharge cut-off (reg 40048). This is the SOC at which
    # the inverter physically refuses to discharge further on-grid.
    # Decoupled from `soc_floor_pct` so the LP can plan to a higher
    # floor (e.g. 15%) while the hardware leaves the bottom of the
    # battery available for emergency / out-of-plan use. Default 0%
    # — let the BMS protect the cells, not the EMS register.
    discharge_cutoff_pct: float = 0.0
    # Backup SOC reserved for grid-outage (reg 40046). Per Sigenergy
    # V2.6+ semantics this is the floor the inverter holds in reserve
    # for backup loads when grid is down. Set to 0 if you want all of
    # the battery available on-grid; raise it if you have critical
    # circuits wired to the backup port. The LP's effective floor is
    # max(soc_floor_pct, backup_soc_pct, discharge_cutoff_pct).
    backup_soc_pct: float = 0.0
    max_ac_charge_kw: float = 10.0  # Grid import limit (AC-coupled)
    # Solar charge limit (DC-coupled). Defaults to the PV array
    # nameplate — that's the real bottleneck for PV → battery in a
    # hybrid-DC system. See hardware.PV_ARRAY_KW.
    max_dc_charge_kw: float = PV_ARRAY_KW
    max_discharge_kw: float = 10.0
    round_trip_efficiency: float = 0.90
    export_limit_kw: float = 5.0
    pv_array_kw: float = PV_ARRAY_KW  # Nameplate PV capacity


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

    # ── SIGNAL_DRIVEN_CONTINUOUS (block-running variant) ─────────
    # Once the relay turns on, must remain on for ≥ min_on_slots
    # consecutive slots; once off, must remain off for ≥ min_off_slots
    # before re-asserting. Both required for SIGNAL_DRIVEN_CONTINUOUS;
    # ignored otherwise. Slot length is lp.constants.SLOT_MINUTES.
    min_on_slots: int | None = None
    min_off_slots: int | None = None

    # ── Per-day schedule overrides (off / auto / on) ─────────────
    # Map of local-calendar date (YYYY-MM-DD) → state: "off" or "on".
    # Days not present default to "auto" (LP decides). Semantics:
    #   off  — relay forced OFF for every slot in that local day; the
    #          day's daily-target constraint is dropped (no roll-forward
    #          into or out of a skipped day).
    #   on   — relay forced ON for every slot in that local day; the
    #          appliance's own internal control decides whether to draw
    #          power (HP HW only runs when it needs to). Daily-target
    #          constraint also dropped: we're committed regardless.
    #   auto — existing LP behaviour: daily-target constraint plus any
    #          min-on / min-off carry-over applies.
    # Any in-flight min-on block carry-over is overridden by a non-auto
    # state at slot 0 — user intent wins over safety. Edit config.toml
    # and /deploy to apply.
    schedule_overrides: dict[str, str] = field(default_factory=dict)

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
    # Hourly forecast endpoint. BOM's official JSON forecast API is
    # undocumented but publicly-served (powers the BOM mobile app). The
    # geohash here is the 6-char prefix that the ``/forecasts/hourly``
    # route accepts (the search endpoint returns 7-char IDs — drop the
    # last character). ``r3dp4v`` is Canberra Airport (matches obs
    # station 94926 so forecast and current-obs align geographically);
    # query `api.weather.bom.gov.au/v1/locations?search=<name>` for
    # other locations and use the returned geohash's first 6 chars.
    # Set to empty string to disable — no fetch, no table writes.
    # Note: BOM's ToS assert the API is not for redistribution; personal
    # use is fine but don't build a commercial product on this endpoint.
    bom_forecast_url: str = "https://api.weather.bom.gov.au/v1/locations/r3dp4v/forecasts/hourly"
    forecast_poll_interval_s: int = 3600


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
    # Daily NDJSON event log for ops observability. Written plain
    # (un-gzipped) at emit time because event rate is high enough that
    # per-emit gzip open/close would dominate; daily rotation by event
    # date lets the /ops/* endpoints DuckDB-read_json over a glob.
    # Operators can compress old days via logrotate.
    event_log_dir: str = "/var/lib/energy-optimiser/events"


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    tick_interval_s: int = 60
    telemetry_write_interval_s: int = 300
    # LP configuration
    lp_wall_clock_timeout_s: float = 12.0  # Max wall-clock for LP solve thread
    lp_scenario_weight_p10: float = 0.20  # Stochastic PV scenario weights
    lp_scenario_weight_p50: float = 0.60
    lp_scenario_weight_p90: float = 0.20
    # Price-axis stochasticity. See lp/scenarios.py for semantics.
    # "point" reproduces the deterministic predicted-or-spot LP
    # (default, pending sweep evidence per KNOWN-ISSUES #24).
    # "shared" or "cross" enable Amber's advancedPrice bands as
    # weighted scenarios. String here so config.toml stays plain;
    # parsed via the property below.
    lp_price_scenario_mode: str = "point"
    # One-way battery throughput cost in c/kWh (round-trip ≈ 2×).
    # `None` falls back to lp.constants.WEAR_COST_PER_KWH (the
    # marginal-degradation calibration). Override here when tuning
    # against forecast-band bias (see KNOWN-ISSUES #24).
    lp_wear_cost_per_kwh: float | None = None
    # Terminal-slot SOC floor (%) for the LP horizon. `None` falls
    # back to the hand-calibrated _TERMINAL_FLOOR_BY_NEM_HOUR table
    # in lp.constants (varies by NEM hour the horizon ends at).
    # Setting a constant overrides the table entirely — useful as a
    # tuning knob until the V-function lands. Always wrapped in
    # max(operational floor, backup, cutoff) inside the LP, so it
    # cannot drop below the safety floor.
    lp_terminal_floor_override_pct: float | None = None

    @property
    def lp_scenario_weights(self) -> dict[str, float]:
        return {
            "p10": self.lp_scenario_weight_p10,
            "p50": self.lp_scenario_weight_p50,
            "p90": self.lp_scenario_weight_p90,
        }

    @property
    def parsed_price_scenario_mode(self):  # type: ignore[no-untyped-def]
        """Parse `lp_price_scenario_mode` to the PriceScenarioMode enum.

        Imported here (not at module top) to avoid a circular import
        between config.py (loaded early) and lp.scenarios (which lives
        under lp/ and imports from optimiser.types). Raises ValueError
        if the string is not one of point/shared/cross.
        """
        from .lp.scenarios import PriceScenarioMode

        return PriceScenarioMode(self.lp_price_scenario_mode)


@dataclass(frozen=True, slots=True)
class APIConfig:
    """HTTP API server config.

    The server is read-only: operator monitoring, metric scrapes, log
    tails, telemetry pulls. It never mutates inverter state. The bearer
    token lives in an environment variable, not the config file.
    """

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    bearer_token_env: str = "EO_API_TOKEN"
    log_file_path: str = "/var/lib/energy-optimiser/logs/app.log"
    log_file_max_bytes: int = 10 * 1024 * 1024  # 10 MB
    log_file_backup_count: int = 5
    log_ring_buffer_size: int = 5000
    query_max_limit: int = 10000
    query_timeout_s: float = 5.0


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
    api: APIConfig


_VALID_SCHEDULE_STATES = frozenset({"off", "on"})


def _parse_schedule_overrides(raw: dict, load_id: str) -> dict[str, str]:
    """Validate and normalise per-day schedule overrides.

    Keys must be ISO-format dates (YYYY-MM-DD); values must be 'off'
    or 'on' (lowercase). 'auto' is the default and shouldn't appear as
    an explicit override — flagged with a clear error so the operator
    notices typos rather than silently ignoring them.
    """
    from datetime import date as _date

    result: dict[str, str] = {}
    for k, v in raw.items():
        try:
            _date.fromisoformat(k)
        except ValueError as exc:
            raise ValueError(
                f"managed_load {load_id!r}: schedule_overrides key {k!r} "
                f"is not a YYYY-MM-DD date: {exc}"
            ) from exc
        v_norm = str(v).lower()
        if v_norm not in _VALID_SCHEDULE_STATES:
            raise ValueError(
                f"managed_load {load_id!r}: schedule_overrides[{k!r}] is "
                f"{v!r}, expected one of {sorted(_VALID_SCHEDULE_STATES)} "
                f"(omit the entry for 'auto')"
            )
        result[k] = v_norm
    return result


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
        # SIGNAL_DRIVEN_CONTINUOUS
        min_on_slots=raw.get("min_on_slots"),
        min_off_slots=raw.get("min_off_slots"),
        # Schedule overrides — validated at parse time so bad values
        # surface at startup, not mid-tick.
        schedule_overrides=_parse_schedule_overrides(
            raw.get("schedule_overrides", {}), raw.get("load_id", "?")
        ),
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
        api=APIConfig(**raw.get("api", {})),
    )
