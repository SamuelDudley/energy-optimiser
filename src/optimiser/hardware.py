"""Hardware-level constants shared across the main service and the watchdog.

This module intentionally has **no dependencies** — only literals — so the
watchdog sidecar (which is designed to have the minimum possible import
surface for failure-domain isolation) can pull the same values the main
service uses without dragging in the rest of the package. Keep it this
way; anything more than literals belongs in config.py or types.py.
"""

from __future__ import annotations

# PV array nameplate capacity in kW DC.
#
# Bounds the charge rate we request the inverter to accept (reg 40032,
# ESS_MAX_CHARGING_LIMIT) on mode-2 dispatches and on fallback. The
# inverter's BMS clamps internally at its current acceptance headroom
# (reg 30047 reports ~15.9 kW at 65% SOC / 17.7°C for this install), so
# writing a value slightly above or below that is fine — the binding
# constraint for PV → battery is the array's own DC output cap.
#
# Single source of truth: BatteryConfig's default reads this; the
# watchdog CLI's default reads this × 1000 as the raw register value.
# Change here when the PV array is re-speccd.
PV_ARRAY_KW: float = 15.81
