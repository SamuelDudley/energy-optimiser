"""Prometheus metrics registry.

Holds the gauges / counters / histograms the service updates inline as
events occur (no scraping loop). A single `Metrics` instance lives on
the Service and is handed to the API server via the probe so the
`/metrics` handler can render the registry on scrape.

Each Metrics has its own `CollectorRegistry` — deliberately NOT the
default global. That keeps tests isolated (no bleed between fixtures)
and keeps the service from accidentally picking up process-wide
collectors we didn't intend to expose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

if TYPE_CHECKING:
    from ..lp.dispatch import LPDispatch
    from ..types import SystemState


# State-machine states we care to emit. A multi-series gauge
# (one label value per state, 1/0) is the Prometheus canonical
# representation of a categorical.
_STATE_LABELS = (
    "INITIALISE",
    "ACTIVE",
    "ACTIVE_NO_PRICE",
    "DEGRADED",
    "FALLBACK",
)


class Metrics:
    """Container for all Prometheus metric objects."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        # ── Live gauges (updated at tick end) ─────────────────────
        self.soc_pct = Gauge(
            "eo_battery_soc_pct", "Battery state of charge %", registry=self.registry
        )
        self.battery_kw = Gauge(
            "eo_battery_power_kw",
            "Battery power kW (+charge, -discharge)",
            registry=self.registry,
        )
        self.pv_kw = Gauge(
            "eo_pv_power_kw", "PV production kW", registry=self.registry
        )
        self.house_load_kw = Gauge(
            "eo_house_load_kw", "House load kW", registry=self.registry
        )
        self.grid_kw = Gauge(
            "eo_grid_power_kw",
            "Grid power kW (+import, -export)",
            registry=self.registry,
        )
        self.import_price = Gauge(
            "eo_amber_import_price_c_per_kwh",
            "Current Amber import price c/kWh",
            registry=self.registry,
        )
        self.export_price = Gauge(
            "eo_amber_export_price_c_per_kwh",
            "Current Amber export price c/kWh",
            registry=self.registry,
        )
        self.commanded_mode = Gauge(
            "eo_commanded_mode",
            "EMS control mode last written to reg 40031 (2/3/5/6)",
            registry=self.registry,
        )
        self.commanded_cap_kw = Gauge(
            "eo_commanded_cap_kw",
            "Commanded cap kW (reg 40032 charge / 40034 discharge)",
            registry=self.registry,
        )
        self.commanded_target_soc_pct = Gauge(
            "eo_commanded_target_soc_pct",
            "Commanded target SOC % (reg 40047) — NaN when not set",
            registry=self.registry,
        )
        self.state_machine_state = Gauge(
            "eo_state_machine_state",
            "State machine current state (1 for active, 0 otherwise)",
            ["state"],
            registry=self.registry,
        )
        self.circuit_breaker_open = Gauge(
            "eo_circuit_breaker_open",
            "LP circuit breaker latched (1) or closed (0)",
            registry=self.registry,
        )
        self.sigenergy_connected = Gauge(
            "eo_sigenergy_connected",
            "Sigenergy Modbus connection status (1/0)",
            registry=self.registry,
        )
        self.heartbeat_age_s = Gauge(
            "eo_heartbeat_age_seconds",
            "Age of the watchdog heartbeat file (seconds since last touch)",
            registry=self.registry,
        )

        # ── Counters (monotonic, reset on restart) ─────────────────
        self.lp_solves = Counter(
            "eo_lp_solves_total",
            "LP solve count by final status",
            ["status"],
            registry=self.registry,
        )
        self.dispatch_writes = Counter(
            "eo_dispatch_writes_total",
            "apply_lp_dispatch attempts by result",
            ["result"],
            registry=self.registry,
        )
        self.circuit_breaker_trips = Counter(
            "eo_circuit_breaker_trips_total",
            "Circuit-breaker latch events (fallback triggered)",
            ["reason"],
            registry=self.registry,
        )
        self.ticks = Counter(
            "eo_ticks_total", "Tick loop iterations", registry=self.registry
        )
        self.tick_errors = Counter(
            "eo_tick_errors_total",
            "Tick iterations that raised an unhandled exception",
            registry=self.registry,
        )

        # ── Histograms ─────────────────────────────────────────────
        self.lp_solve_duration_ms = Histogram(
            "eo_lp_solve_duration_ms",
            "LP solve wall-clock duration (ms)",
            buckets=(100, 250, 500, 1000, 2500, 5000, 10000),
            registry=self.registry,
        )
        self.tick_duration_ms = Histogram(
            "eo_tick_duration_ms",
            "Tick loop iteration wall-clock (ms)",
            buckets=(50, 100, 250, 500, 1000, 2500),
            registry=self.registry,
        )

    # ── Update helpers ─────────────────────────────────────────────

    def record_live_state(
        self,
        state: SystemState,
        current_import_price: float | None,
        current_export_price: float | None,
        sigenergy_connected: bool,
        service_state: str,
        circuit_breaker_open: bool,
        heartbeat_age_s: float | None,
    ) -> None:
        """Write all per-tick gauges. Called at the end of _tick after the
        dispatch is applied, so gauges reflect the *just-commanded*
        state, not state from mid-tick."""
        if state.soc_pct is not None:
            self.soc_pct.set(state.soc_pct)
        if state.battery_power_kw is not None:
            self.battery_kw.set(state.battery_power_kw)
        if state.pv_power_kw is not None:
            self.pv_kw.set(state.pv_power_kw)
        if state.house_load_kw is not None:
            self.house_load_kw.set(state.house_load_kw)
        if state.grid_power_kw is not None:
            self.grid_kw.set(state.grid_power_kw)
        if current_import_price is not None:
            self.import_price.set(current_import_price)
        if current_export_price is not None:
            self.export_price.set(current_export_price)

        self.sigenergy_connected.set(1 if sigenergy_connected else 0)
        self.circuit_breaker_open.set(1 if circuit_breaker_open else 0)
        if heartbeat_age_s is not None:
            self.heartbeat_age_s.set(heartbeat_age_s)

        # Categorical state machine — one series per state value.
        for label in _STATE_LABELS:
            self.state_machine_state.labels(state=label).set(
                1 if label == service_state else 0
            )

    def record_dispatch(self, dispatch: LPDispatch | None) -> None:
        """Update the commanded-* gauges from the just-written dispatch.
        `None` clears them to NaN so stale values don't linger across a
        fallback."""
        if dispatch is None:
            # prometheus_client doesn't have a "clear" — setting to NaN is
            # the closest equivalent (Prometheus renders NaN as a gap).
            self.commanded_mode.set(float("nan"))
            self.commanded_cap_kw.set(float("nan"))
            self.commanded_target_soc_pct.set(float("nan"))
            return
        self.commanded_mode.set(dispatch.mode.value)
        self.commanded_cap_kw.set(dispatch.cap_kw)
        target_soc = getattr(dispatch, "target_soc_pct", None)
        self.commanded_target_soc_pct.set(
            target_soc if target_soc is not None else float("nan")
        )

    def record_lp_solve(self, status: str, duration_ms: float) -> None:
        self.lp_solves.labels(status=status).inc()
        self.lp_solve_duration_ms.observe(duration_ms)

    def record_dispatch_write(self, ok: bool) -> None:
        self.dispatch_writes.labels(result="success" if ok else "failure").inc()

    def record_circuit_breaker_trip(self, reason: str) -> None:
        self.circuit_breaker_trips.labels(reason=reason).inc()

    def record_tick_end(self, duration_ms: float, errored: bool) -> None:
        self.ticks.inc()
        if errored:
            self.tick_errors.inc()
        self.tick_duration_ms.observe(duration_ms)
