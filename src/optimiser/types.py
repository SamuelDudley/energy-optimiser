"""Core value types for the energy optimiser."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum, auto
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .lp.dispatch import LPDispatch
    from .lp.result import LPSolution


# ── Battery ──────────────────────────────────────────────────────


class BatteryAction(StrEnum):
    CHARGE_GRID = auto()
    CHARGE_PV = auto()
    DISCHARGE_PV = auto()
    DISCHARGE_ESS = auto()
    SELF_CONSUME = auto()
    STANDBY = auto()


class RemoteEMSControlMode(IntEnum):
    PCS_REMOTE_CONTROL = 0
    STANDBY = 1
    MAXIMUM_SELF_CONSUMPTION = 2
    COMMAND_CHARGING_GRID_FIRST = 3
    COMMAND_CHARGING_PV_FIRST = 4
    COMMAND_DISCHARGING_PV_FIRST = 5
    COMMAND_DISCHARGING_ESS_FIRST = 6


BATTERY_ACTION_TO_EMS_MODE: dict[BatteryAction, RemoteEMSControlMode] = {
    BatteryAction.CHARGE_GRID: RemoteEMSControlMode.COMMAND_CHARGING_GRID_FIRST,
    BatteryAction.CHARGE_PV: RemoteEMSControlMode.COMMAND_CHARGING_PV_FIRST,
    BatteryAction.DISCHARGE_PV: RemoteEMSControlMode.COMMAND_DISCHARGING_PV_FIRST,
    BatteryAction.DISCHARGE_ESS: RemoteEMSControlMode.COMMAND_DISCHARGING_ESS_FIRST,
    BatteryAction.SELF_CONSUME: RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION,
    BatteryAction.STANDBY: RemoteEMSControlMode.STANDBY,
}


# ── Managed Loads ────────────────────────────────────────────────


class LoadCategory(StrEnum):
    SHIFTABLE = auto()  # Run a complete cycle once (legacy HW model).
    SIGNAL_DRIVEN = auto()  # Continuous assert/de-assert; appliance manages own cycles
    # (future EV charging where rapid relay flapping is acceptable).
    SIGNAL_DRIVEN_CONTINUOUS = auto()  # Same as SIGNAL_DRIVEN, plus min-on/min-off
    # constraints so the LP commits to contiguous run-blocks. Required for
    # appliances that don't tolerate stop-start (HW heat pump compressors).
    PRECONDITIONABLE = auto()
    OBSERVABLE = auto()
    DEADLINE_BIDIR = auto()


class LoadCycleState(StrEnum):
    IDLE = auto()
    RUNNING = auto()
    COMPLETE_TODAY = auto()


# ── Operational State Machine ────────────────────────────────────


class ServiceState(StrEnum):
    INITIALISE = auto()
    ACTIVE = auto()
    ACTIVE_NO_PRICE = auto()
    DEGRADED = auto()
    FALLBACK = auto()


# ── Structured Events ────────────────────────────────────────────


class EventType(StrEnum):
    TICK_COMPLETE = auto()
    TICK_OVERRUN = auto()
    STATE_TRANSITION = auto()
    MODBUS_WRITE = auto()
    MODBUS_ERROR = auto()
    MODBUS_RECONNECTED = auto()  # Rising-edge: client transitioned from disconnected to connected
    PRICE_UPDATE = auto()
    PRICE_STALE = auto()
    EXPORT_BLOCKED_STALE_PRICE = auto()  # Export clamped to 0 because 5-min prices aged out
    HW_CYCLE_START = auto()
    HW_CYCLE_COMPLETE = auto()
    HW_CYCLE_FAULT = auto()
    VALIDATION_WARNING = auto()
    VALIDATION_REJECT = auto()
    OCCUPANCY_CHANGE = auto()
    PROFILE_REBUILD = auto()
    PLANNER_FALLBACK = auto()
    LOAD_CYCLE_START = auto()
    LOAD_CYCLE_COMPLETE = auto()
    LOAD_CYCLE_FAULT = auto()
    FALLBACK_TRIGGERED = auto()  # LP failed or watcher detected deviation
    LP_SOLVE_COMPLETE = auto()  # Each LP solve emits (status, cost, ms)
    VERIFY_DEVIATION = auto()  # Single-poll deviation (not yet escalated)
    BREAKER_LATCHED = auto()  # Circuit breaker entered latched state
    BREAKER_PROBE = auto()  # Probe LP run after cooldown
    BREAKER_CLEARED = auto()  # Returned to normal LP control
    PV_CURTAILMENT_SUSPECTED = auto()  # MPPT appears throttled at sink ceiling
    PV_CURTAILMENT_CLEARED = auto()  # Throttle signal cleared (back below ceiling)
    MODE2_TRIM = auto()  # Phase-A PV reading + Phase-B trim value (per dispatch)
    MODE2_TRIM_BLIND = auto()  # Phase-A telemetry unavailable — uncapped fallthrough
    AMBER_HORIZON_SHORT = auto()  # 30-min interval count fell below alert threshold
    AMBER_HORIZON_RECOVERED = auto()  # 30-min count climbed back above threshold
    # Ops-dashboard observability events. Emitted at every external call
    # site so the /ops endpoints can compute per-client error rates,
    # latency percentiles, and Modbus read/write health from NDJSON
    # alone (no additional in-memory metrics surface). Payload schemas:
    #   API_CALL: {client, op, http_status, ms, ok, extra?}
    #     - client: "amber"|"solcast"|"bom"|"shelly"|"unifi"
    #     - op: short verb describing the request ("prices_5min", "status", ...)
    #     - http_status: int or None (None = transport-level failure)
    #     - ms: float, wall-clock duration of the call
    #     - ok: bool — True iff the call returned usable data
    #     - extra: optional dict for client-specific fields (rl_remaining, device_id)
    #   MODBUS_READ_BATCH: {ms, reg_count, err_count, reconnected, grid_sensor_ok}
    #     - One per read_state(). Per-register events would be ~1200/min;
    #       the batch keeps NDJSON volume sane while preserving the
    #       tick-budget signal that matters for ops.
    API_CALL = auto()
    MODBUS_READ_BATCH = auto()
    # User strategy modes
    #   MODE_ACTIVATED: {kind, params, source, end_at, activated_at}
    #   MODE_EXPIRED:   {kind, reason}  reason ∈ {"window_ended", "user_cancelled", "service_started_after_end_at"}
    MODE_ACTIVATED = auto()
    MODE_EXPIRED = auto()


# ── Value Objects ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PriceInterval:
    start: datetime
    end: datetime
    import_per_kwh: float  # `perKwh` from Amber; AEMO-derived point estimate
    export_per_kwh: float
    spot_per_kwh: float
    renewables_pct: float
    spike_status: str
    descriptor: str  # Amber enum; see _FORECAST_DESCRIPTORS in amber.py
    # advancedPrice on the `general` (import) channel. Populated only on
    # ForecastIntervals; None on Current/Actual.
    forecast_low: float | None = None
    forecast_high: float | None = None
    forecast_predicted: float | None = None  # Amber's own model ("Advanced Price Forecast")
    # advancedPrice on the `feedIn` (export) channel. Same population
    # pattern as the import side. Sign-flipped to the customer
    # convention at the parser boundary so positive = revenue from
    # export — matches `export_per_kwh`. Used by the LP cost objective
    # via `lp/formulation.py` (predicted preferred, fallback to
    # `export_per_kwh`). low/high are captured for future scenario
    # calibration (see KNOWN-ISSUES #24) and currently unread.
    export_forecast_low: float | None = None
    export_forecast_high: float | None = None
    export_forecast_predicted: float | None = None
    is_locked: bool | None = (
        None  # CurrentInterval.estimate inverted: False=estimate, True=locked, None=Actual/Forecast
    )


@dataclass(frozen=True, slots=True)
class PriceForecastLogRow:
    """One row per price interval, at each fetch. Logged to
    `price_forecast_log` in DuckDB for later calibration analysis.
    Not consumed by the LP — this is strictly an observability artefact.
    """

    fetched_at: datetime
    resolution: int  # 5 or 30 — which Amber endpoint cadence
    interval_start: datetime
    interval_end: datetime
    interval_type: str | None  # ActualInterval / CurrentInterval / ForecastInterval
    per_kwh: float  # AEMO point on the general channel
    export_per_kwh: float  # feedIn channel perKwh, sign-flipped to customer
    spot_per_kwh: float
    forecast_predicted: float | None  # general.advancedPrice.predicted
    forecast_low: float | None  # general.advancedPrice.low
    forecast_high: float | None  # general.advancedPrice.high
    spike_status: str
    descriptor: str
    is_locked: bool | None
    renewables_pct: float
    # feedIn.advancedPrice.{predicted,low,high}, sign-flipped at parser
    # boundary so positive = revenue from export. Defaults to None so
    # existing call sites and forward-compat row reconstruction stay
    # safe; populated by `clients/amber.py::_fetch` when present.
    export_forecast_predicted: float | None = None
    export_forecast_low: float | None = None
    export_forecast_high: float | None = None


@dataclass(frozen=True, slots=True)
class PVForecast:
    start: datetime
    end: datetime
    pv_estimate_kw: float
    pv_estimate10_kw: float
    pv_estimate90_kw: float


@dataclass(frozen=True, slots=True)
class WeatherForecastInterval:
    """One hour of BOM hourly forecast. All fields optional — BOM's
    JSON sometimes omits keys for early/late intervals.
    """

    period_end: datetime
    temp_c: float | None
    apparent_temp_c: float | None
    humidity_pct: float | None
    rain_chance_pct: float | None
    rain_mm: float | None  # expected amount; BOM reports a min/max, we store the mid
    wind_kmh: float | None


@dataclass(frozen=True, slots=True)
class WeatherForecastLogRow:
    """One row per forecast interval, at each BOM fetch. Mirrors the
    pv_forecast_log pattern: redundant logging so a single table traces
    forecast evolution.
    """

    fetched_at: datetime
    period_end: datetime
    temp_c: float | None
    apparent_temp_c: float | None
    humidity_pct: float | None
    rain_chance_pct: float | None
    rain_mm: float | None
    wind_kmh: float | None


@dataclass(frozen=True, slots=True)
class PVForecastLogRow:
    """One row per forecast interval, at each Solcast fetch. Logged to
    `pv_forecast_log` in DuckDB for later calibration analysis (p10/p50/p90
    calibration, forecast drift, replay engine inputs). Not consumed by the
    LP — strictly an observability artefact. `actual_kw` is left None at
    insert time; a backfill job (not yet implemented) would later populate
    it from the telemetry table's measured `pv_kw`.
    """

    fetched_at: datetime
    period_end: datetime  # forecast intervals are anchored to period_end in Solcast
    pv_estimate_kw: float
    pv_estimate10_kw: float
    pv_estimate90_kw: float
    actual_kw: float | None = None


@dataclass(frozen=True, slots=True)
class SystemState:
    timestamp: datetime
    soc_pct: float
    battery_power_kw: float
    pv_power_kw: float
    grid_power_kw: float | None  # None when grid sensor is offline
    house_load_kw: float | None  # None when unavailable or derivation is suspect
    ems_mode: int
    outdoor_temp_c: float | None
    occupied: bool

    # ── Extended inverter telemetry (added 2026-04 for backtest coverage) ──
    # All optional: older call sites and tests may omit these. When the
    # Sigenergy read fails or the register is unsupported on a given
    # firmware, the field stays None (null-over-wrong).

    # Battery health & thermal
    soh_pct: float | None = None
    cell_temp_avg_c: float | None = None
    cell_temp_max_c: float | None = None
    cell_temp_min_c: float | None = None
    cell_volt_avg_v: float | None = None
    pcs_temp_c: float | None = None

    # BMS-reported real-time power limits. Drop below nameplate when the
    # battery is cold, near SOC floor/ceiling, or thermally derated.
    available_charge_kw: float | None = None
    available_discharge_kw: float | None = None

    # Plant state + alarms (bitfields; decode against Appendices 1-5, 11)
    running_state: int | None = None
    alarm1: int | None = None
    alarm2: int | None = None
    alarm3: int | None = None
    alarm4: int | None = None
    alarm5: int | None = None

    # Monotonic lifetime energy counters (kWh). Stored as DOUBLE because
    # REAL (float32) loses precision at ~10^7 kWh.
    lifetime_pv_kwh: float | None = None
    lifetime_load_kwh: float | None = None
    lifetime_charge_kwh: float | None = None
    lifetime_discharge_kwh: float | None = None
    lifetime_import_kwh: float | None = None
    lifetime_export_kwh: float | None = None

    # Per-MPPT string voltage/current. Null if the inverter has fewer
    # than 4 strings wired or the register read fails.
    mppt1_voltage_v: float | None = None
    mppt1_current_a: float | None = None
    mppt2_voltage_v: float | None = None
    mppt2_current_a: float | None = None
    mppt3_voltage_v: float | None = None
    mppt3_current_a: float | None = None
    mppt4_voltage_v: float | None = None
    mppt4_current_a: float | None = None

    # Grid AC quality
    grid_freq_hz: float | None = None
    phase_a_voltage_v: float | None = None
    phase_b_voltage_v: float | None = None
    phase_c_voltage_v: float | None = None

    # Readback of holding reg 40031 (what the inverter currently has as
    # its commanded remote EMS mode). Closes the loop against our writes.
    remote_ems_mode: int | None = None


@dataclass(frozen=True, slots=True)
class ManagedLoadStatus:
    load_id: str
    category: LoadCategory
    power_kw: float
    energy_today_kwh: float
    relay_on: bool | None = None
    cycle_state: LoadCycleState | None = None
    # UTC timestamp when the relay's current state began. Updated by the
    # controller on observed transitions; None until the first observation.
    # Read by BinarySignalDrivenContinuousLoad to enforce min-on / min-off
    # carry-over across LP rebuilds — without this field, the in-horizon
    # min-on/min-off constraints don't bind slot 0 (no slot -1 to subtract
    # from), so a fresh tick could turn off mid-block.
    relay_state_since: datetime | None = None
    # Cumulative relay-on minutes since local midnight. Parallel accumulator
    # to energy_today_kwh — feeds the time-mode daily-run constraint
    # (BinarySignalDrivenLoad with daily_run_minutes set). None when the load
    # has no relay or we haven't accumulated yet.
    relay_on_minutes_today: float | None = None


@dataclass(frozen=True, slots=True)
class LoadCommand:
    """A command for a managed load.

    `start_cycle` is the legacy one-shot trigger for SHIFTABLE loads.
    `desired_relay_on` is the continuous relay state for SIGNAL_DRIVEN loads
    (None = no change, True = close, False = open).
    """

    load_id: str
    start_cycle: bool
    reason: str
    desired_relay_on: bool | None = None


@dataclass(frozen=True, slots=True)
class PlannerOutput:
    battery_action: BatteryAction
    charge_limit_kw: float
    discharge_limit_kw: float
    target_soc: float
    load_commands: list[LoadCommand]
    grid_export_limit_kw: float | None  # None = don't change, float = set limit
    reason: str


@dataclass(frozen=True, slots=True)
class LoadProfile:
    slots: list[float]  # 48 values, 30-min intervals
    maturity_level: int
    context: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    warnings: list[str]
    rejected_fields: list[str]


@dataclass(frozen=True, slots=True)
class Event:
    timestamp: datetime
    event_type: EventType
    data: dict[str, Any]
    tick_id: str | None = None


@dataclass(frozen=True, slots=True)
class PVProbeResult:
    """Output of a Phase-A "uncap and measure" PV probe.

    Used to produce a true-MPP slot-0 PV reading for the LP, displacing
    the (possibly curtailed) `system_state.pv_power_kw` and the
    conservative P10 forecast. The probe writes 40032=max + mode 2,
    waits for the cascade to settle, then reads.

    `saturated` flags the case where measured_pv is only a *lower bound*
    on true MPP — i.e. the cascade had no slack so MPPT may have
    throttled. Saturation requires BOTH battery acceptance AND export to
    be at their respective caps; if either had slack, true PV ≤
    measured_pv_kw + telemetry noise.

    `pv_kw` is None when the probe couldn't read state after settling
    (Modbus blip during the 5-second window). Caller falls back to
    Solcast in that case.
    """

    pv_kw: float | None
    saturated: bool
    bat_kw: float | None
    bat_avail_kw: float | None
    grid_export_kw: float | None
    export_cap_kw: float | None
    house_kw: float | None
    soc_pct: float | None


@dataclass(frozen=True, slots=True)
class TickSnapshot:
    tick_id: str
    timestamp: datetime
    version: str
    system_state: SystemState
    price_forecast: list[PriceInterval]
    pv_forecast: list[PVForecast] | None
    load_profile: LoadProfile
    managed_loads: list[ManagedLoadStatus]
    maturity_level: int
    output: PlannerOutput  # Legacy shape — kept for back-compat
    actual_cost_cents: float | None = None
    counterfactual_cost_cents: float | None = None
    lp_solution: LPSolution | None = None  # v0.2.0+: native LP output
    lp_dispatch: LPDispatch | None = None  # v0.2.0+: mode + cap sent to inverter
    # Inverter state captured AFTER the tick's dispatch + load commands
    # were applied (and after the mode-2 adaptive trim's Phase-A/B
    # sequence). Lets observability / replay distinguish "what the LP
    # planned with" (`system_state`, sampled at tick start) from "what
    # the inverter is doing in response" (this field). Closes the
    # ~30–60s observability lag at slot transitions where the snapshot
    # would otherwise show pre-dispatch state. None if the post-dispatch
    # read failed or the tick took the no-apply branch.
    system_state_post_dispatch: SystemState | None = None
    # Phase-A "uncap and measure" probe result. Populated by the
    # service when a probe ran this tick (gated on PV > threshold,
    # planner enabled, etc.). None when the probe was skipped (night,
    # degraded, recently fresh) or failed. See `service._tick_body`
    # and `clients/sigenergy.measure_uncapped_pv`.
    pv_probe: PVProbeResult | None = None
    # The slot-0 PV value the LP actually consumed. Resolves the gating
    # logic in one place: when the probe is fresh and unsaturated, this
    # is `pv_probe.pv_kw`; when saturated or absent, this is None
    # (LP fell back to Solcast). Replay reads this to reproduce the
    # exact LP input the live tick saw.
    pv_avail_slot_0_used_kw: float | None = None


@dataclass(frozen=True, slots=True)
class TelemetryRow:
    ts: datetime
    soc_pct: float | None
    battery_kw: float | None
    pv_kw: float | None
    grid_kw: float | None
    grid_kw_shelly: float | None
    house_load_kw: float | None
    import_price: float | None
    export_price: float | None
    spot_price: float | None
    renewables_pct: float | None
    spike_status: str | None
    pv_forecast_kw: float | None
    outdoor_temp_c: float | None
    occupied: bool | None
    ems_mode: int | None
    planner_action: str | None
    planner_reason: str | None

    # ── Extended inverter telemetry (added 2026-04 for backtest coverage) ──
    # Mirrors the SystemState additions. All optional; older writers and
    # tests may omit these.
    soh_pct: float | None = None
    cell_temp_avg_c: float | None = None
    cell_temp_max_c: float | None = None
    cell_temp_min_c: float | None = None
    cell_volt_avg_v: float | None = None
    pcs_temp_c: float | None = None
    available_charge_kw: float | None = None
    available_discharge_kw: float | None = None
    running_state: int | None = None
    alarm1: int | None = None
    alarm2: int | None = None
    alarm3: int | None = None
    alarm4: int | None = None
    alarm5: int | None = None
    lifetime_pv_kwh: float | None = None
    lifetime_load_kwh: float | None = None
    lifetime_charge_kwh: float | None = None
    lifetime_discharge_kwh: float | None = None
    lifetime_import_kwh: float | None = None
    lifetime_export_kwh: float | None = None
    mppt1_voltage_v: float | None = None
    mppt1_current_a: float | None = None
    mppt2_voltage_v: float | None = None
    mppt2_current_a: float | None = None
    mppt3_voltage_v: float | None = None
    mppt3_current_a: float | None = None
    mppt4_voltage_v: float | None = None
    mppt4_current_a: float | None = None
    grid_freq_hz: float | None = None
    phase_a_voltage_v: float | None = None
    phase_b_voltage_v: float | None = None
    phase_c_voltage_v: float | None = None
    remote_ems_mode: int | None = None


@dataclass(frozen=True, slots=True)
class LoadTelemetryRow:
    ts: datetime
    load_id: str
    category: str
    power_kw: float | None
    energy_today_kwh: float | None
    cycle_state: str | None
    relay_on: bool | None


@dataclass(frozen=True, slots=True)
class AmberUsageRow:
    """One settled 5-min interval from Amber's /usage endpoint.

    These are the actuals that land on the bill — fetched once a day after
    the NEM day rolls over. `cost_cents` carries Amber's signed convention
    (positive on the general/import channel, negative on feedIn/export),
    so SUM(cost_cents) over a day is the net bill in cents.

    `nem_date` is Amber's `date` field — the bill's date column. NEM is
    UTC+10 year-round, which differs from local Canberra time during AEDT.
    Using Amber's value avoids a DST-aware conversion at write time.
    """

    ts: datetime  # interval start, UTC
    nem_date: str  # YYYY-MM-DD, NEM-day = bill date
    channel: str  # "general" (import) | "feedIn" (export) | "controlledLoad"
    kwh: float  # always >=0; direction is implied by channel
    cost_cents: float  # signed: + on general, − on feedIn (per Amber)
    per_kwh_cents: float  # c/kWh, sign matches cost
    spot_per_kwh_cents: float | None
    renewables_pct: float | None
    descriptor: str | None  # veryLow | low | neutral | high | spike
    spike_status: str | None  # none | potential | spike
    quality: str | None  # billable | estimated | awaiting
