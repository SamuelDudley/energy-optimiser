"""Grid-charge dispatch: keep PV generating (mode 4) + buy 'take it all'.

See docs/superpowers/specs/2026-06-26-buy-pv-passthrough-design.md.

Grid-dominant charging used to emit mode 3 (COMMAND_CHARGING_GRID_FIRST),
which curtails PV on the hardware. These tests pin the new behaviour:
grid-dominant charge always uses mode 4 (COMMAND_CHARGING_PV_FIRST) so PV
keeps generating while grid tops the charge up; and a buy-mode dispatch
caps at full AC+DC so it pulls max grid + all PV.
"""

from __future__ import annotations

from datetime import UTC, datetime

from optimiser.config import BatteryConfig
from optimiser.lp.dispatch import DispatchKind, dispatch_from_slot
from optimiser.lp.result import SlotDecision
from optimiser.types import RemoteEMSControlMode

_NOW = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)
_BAT = BatteryConfig()  # max_ac_charge_kw=10, max_dc_charge_kw=13


def _charge_slot(
    *,
    battery_kw: float,
    grid_to_battery_kw: float,
    pv_to_battery_kw: float,
    soc_pct_end: float = 55.0,
) -> SlotDecision:
    return SlotDecision(
        slot_start=_NOW,
        battery_kw=battery_kw,
        grid_import_kw=grid_to_battery_kw,
        grid_export_kw=0.0,
        pv_to_house_kw=0.0,
        pv_to_battery_kw=pv_to_battery_kw,
        pv_to_export_kw=0.0,
        soc_pct_end=soc_pct_end,
        grid_to_battery_kw=grid_to_battery_kw,
    )


def test_grid_dominant_charge_uses_mode4() -> None:
    """Grid-dominant charge emits mode 4 (PV-first) so PV stays on, with
    the cap at the LP's planned total."""
    slot = _charge_slot(battery_kw=10.0, grid_to_battery_kw=10.0, pv_to_battery_kw=0.0)
    d = dispatch_from_slot(slot, _BAT, current_soc_pct=50.0)
    assert d.mode == RemoteEMSControlMode.COMMAND_CHARGING_PV_FIRST
    assert d.kind == DispatchKind.CHARGE
    assert d.cap_kw == 10.0


def test_grid_plus_pv_charge_uses_mode4_total_cap() -> None:
    """Mixed grid+PV charge (grid-dominant) → mode 4, cap = total kW."""
    slot = _charge_slot(battery_kw=12.0, grid_to_battery_kw=8.0, pv_to_battery_kw=4.0)
    d = dispatch_from_slot(slot, _BAT, current_soc_pct=50.0)
    assert d.mode == RemoteEMSControlMode.COMMAND_CHARGING_PV_FIRST
    assert d.cap_kw == 12.0


def test_buy_active_charge_caps_at_full_ac_plus_dc() -> None:
    """In buy mode, the cap is full AC+DC so the inverter pulls max grid
    (10kW) plus all available PV ('take it all')."""
    slot = _charge_slot(battery_kw=10.0, grid_to_battery_kw=10.0, pv_to_battery_kw=0.0)
    d = dispatch_from_slot(slot, _BAT, current_soc_pct=50.0, buy_active=True)
    assert d.mode == RemoteEMSControlMode.COMMAND_CHARGING_PV_FIRST
    assert d.cap_kw == _BAT.max_ac_charge_kw + _BAT.max_dc_charge_kw


def test_pv_dominant_charge_stays_mode2() -> None:
    """Regression guard: PV-dominant charge is unchanged (mode 2 trim)."""
    slot = _charge_slot(battery_kw=8.0, grid_to_battery_kw=2.0, pv_to_battery_kw=6.0)
    d = dispatch_from_slot(slot, _BAT, current_soc_pct=50.0)
    assert d.mode == RemoteEMSControlMode.MAXIMUM_SELF_CONSUMPTION
    assert d.cap_kw == 8.0
