"""Paranoid fallback: drives the inverter into a known-safe state when
the LP fails or the watcher detects sustained deviation.

With the load-following dispatch path (modes 3/4/6 + caps), there is no
continuous setpoint register to clear — the cap registers (40032/40034)
are only consulted when their corresponding mode is active, and switching
to MAXIMUM_SELF_CONSUMPTION (mode 2) makes both irrelevant. So the fallback
is just:

  1. Switch EMS mode to MAXIMUM_SELF_CONSUMPTION (40031=2). The inverter's
     native safe mode: self-consumes PV against house load with conservative
     defaults, no aggressive grid charging or discharging.
  2. Open all managed-load relays (HW heat pump, etc.).
  3. Emit a structured FALLBACK_TRIGGERED event with the reason and any
     relevant context (commanded vs measured if a verify deviation).

Each step is best-effort and logged regardless of success — the goal is to
push the system toward the safest reachable state, not to fail the entire
fallback if one Modbus or HTTP call hangs. After all steps, the caller
(service tick or watcher) is responsible for latching the runtime breaker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..clients.shelly import ShellyLoadController
from ..clients.sigenergy import SigenergyController
from ..logging_utils import EventType, emit
from .runtime import FallbackReason

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FallbackResult:
    """Outcome of a fallback attempt — for snapshot/replay logging."""

    set_self_consume: bool
    relays_opened: list[str]  # load_ids attempted (failures logged inline)
    reason: FallbackReason


async def trigger_fallback(
    sigenergy: SigenergyController,
    shelly_controllers: list[ShellyLoadController],
    reason: FallbackReason,
    *,
    commanded_kw: float | None = None,
    measured_kw: float | None = None,
    export_price_ckwh: float | None = None,
    block_export: bool = False,
    extra_context: dict | None = None,
) -> FallbackResult:
    """Drive the system into Maximum Self Consumption + all relays off.

    Each step is independent — a failure in one doesn't skip the others.
    Returns a `FallbackResult` describing what was attempted (not necessarily
    what succeeded — Modbus/HTTP failures are logged inline).
    """
    logger.warning(
        "Triggering fallback: reason=%s commanded=%s measured=%s",
        reason.value,
        commanded_kw,
        measured_kw,
    )

    # Step 1: switch to safe EMS mode. The cap registers (40032/40034) are
    # not consulted in mode 2, so we don't need to zero them — they hold
    # whatever the last LP write put there, and the inverter ignores them.
    self_consume_ok = False
    try:
        self_consume_ok = await sigenergy.set_fallback(
            export_price_ckwh=export_price_ckwh,
            block_export=block_export,
        )
    except Exception:
        logger.exception("Fallback: set_fallback raised")

    # Step 2: open all managed-load relays.
    relay_load_ids: list[str] = []
    for ctl in shelly_controllers:
        try:
            await ctl.set_relay(False)
            relay_load_ids.append(ctl.load_id)
        except Exception:
            logger.exception("Fallback: relay-off failed for %s", ctl.load_id)
            # The Shelly controller's _pending_relay_stop mechanism retries
            # on the next status() call, so we don't need to escalate here.

    # Step 3: structured event for the snapshot/event log.
    payload: dict = {
        "reason": reason.value,
        "set_self_consume": self_consume_ok,
        "relays_opened": relay_load_ids,
    }
    if commanded_kw is not None:
        payload["commanded_kw"] = commanded_kw
    if measured_kw is not None:
        payload["measured_kw"] = measured_kw
    if extra_context:
        payload.update(extra_context)
    emit(EventType.FALLBACK_TRIGGERED, payload)

    return FallbackResult(
        set_self_consume=self_consume_ok,
        relays_opened=relay_load_ids,
        reason=reason,
    )
