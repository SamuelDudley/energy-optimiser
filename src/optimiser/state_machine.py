"""Operational state machine for service lifecycle.

Manages transitions between INITIALISE, ACTIVE, ACTIVE_NO_PRICE,
DEGRADED, and FALLBACK states based on connectivity and data freshness.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from .logging_utils import emit
from .time_utils import now_utc
from .types import EventType, ServiceState

logger = logging.getLogger(__name__)

# Thresholds
DEGRADED_TIMEOUT = timedelta(minutes=5)
PRICE_STALE_THRESHOLD = timedelta(hours=1)
AMBER_RETRY_THRESHOLD = 3  # consecutive failures


class StateMachine:
    """Operational state machine for the energy optimiser."""

    def __init__(self) -> None:
        self._state = ServiceState.INITIALISE
        self._modbus_lost_at: float | None = None
        self._amber_failures: int = 0
        self._price_stale_since: float | None = None

    @property
    def state(self) -> ServiceState:
        return self._state

    def _transition(self, new_state: ServiceState, reason: str) -> None:
        if new_state != self._state:
            old = self._state
            self._state = new_state
            logger.info("State: %s → %s (%s)", old.value, new_state.value, reason)
            emit(
                EventType.STATE_TRANSITION,
                {
                    "from": old.value,
                    "to": new_state.value,
                    "reason": reason,
                },
            )

    def on_startup_complete(self, modbus_ok: bool, amber_ok: bool) -> None:
        """Called after initial connection attempts."""
        if modbus_ok and amber_ok:
            self._transition(ServiceState.ACTIVE, "startup complete")
        elif modbus_ok and not amber_ok:
            self._transition(ServiceState.ACTIVE_NO_PRICE, "startup: amber unavailable")
        elif not modbus_ok:
            self._transition(ServiceState.FALLBACK, "startup: modbus unavailable")

    def on_modbus_success(self) -> None:
        """Called after a successful Modbus read/write."""
        self._modbus_lost_at = None
        if self._state == ServiceState.DEGRADED:
            if self._amber_failures < AMBER_RETRY_THRESHOLD:
                self._transition(ServiceState.ACTIVE, "modbus reconnected")
            else:
                self._transition(
                    ServiceState.ACTIVE_NO_PRICE, "modbus reconnected, amber still down"
                )
        elif self._state == ServiceState.FALLBACK:
            # Recover from FALLBACK only if amber is also healthy.
            # Symmetric with on_amber_success() which checks modbus.
            if self._amber_failures < AMBER_RETRY_THRESHOLD:
                self._transition(ServiceState.ACTIVE, "modbus reconnected, amber ok")
            else:
                self._transition(
                    ServiceState.ACTIVE_NO_PRICE, "modbus reconnected, amber still down"
                )

    def on_modbus_failure(self) -> None:
        """Called when Modbus communication fails."""
        now = now_utc().timestamp()

        if self._modbus_lost_at is None:
            self._modbus_lost_at = now

        if self._state == ServiceState.ACTIVE or self._state == ServiceState.ACTIVE_NO_PRICE:
            self._transition(ServiceState.DEGRADED, "modbus lost")

        if self._state == ServiceState.DEGRADED:
            elapsed = timedelta(seconds=now - self._modbus_lost_at)
            if elapsed >= DEGRADED_TIMEOUT:
                self._transition(ServiceState.FALLBACK, f"modbus unreachable for {elapsed}")

    def on_amber_success(self) -> None:
        """Called after a successful Amber API response."""
        self._amber_failures = 0
        self._price_stale_since = None

        if self._state == ServiceState.ACTIVE_NO_PRICE:
            self._transition(ServiceState.ACTIVE, "amber recovered")
        elif self._state == ServiceState.FALLBACK:
            # Only recover from fallback if modbus is also OK
            if self._modbus_lost_at is None:
                self._transition(ServiceState.ACTIVE, "amber recovered, modbus ok")

    def on_amber_failure(self) -> None:
        """Called when Amber API request fails."""
        self._amber_failures += 1

        if self._amber_failures >= AMBER_RETRY_THRESHOLD:
            if self._state == ServiceState.ACTIVE:
                self._transition(
                    ServiceState.ACTIVE_NO_PRICE, f"amber failed {self._amber_failures}x"
                )
                emit(EventType.PRICE_STALE, {"consecutive_failures": self._amber_failures})

    def on_price_age_check(self, price_age: timedelta | None) -> None:
        """Check if the price forecast is too stale."""
        if price_age and price_age > PRICE_STALE_THRESHOLD:
            if self._state == ServiceState.ACTIVE_NO_PRICE:
                self._transition(ServiceState.FALLBACK, f"price data stale ({price_age})")

    @property
    def should_run_planner(self) -> bool:
        return self._state in (ServiceState.ACTIVE, ServiceState.ACTIVE_NO_PRICE)

    @property
    def should_apply_commands(self) -> bool:
        return self._state in (ServiceState.ACTIVE, ServiceState.ACTIVE_NO_PRICE)

    @property
    def should_fallback(self) -> bool:
        return self._state == ServiceState.FALLBACK
