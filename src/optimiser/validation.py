"""Data validation for telemetry rows.

Key principle: reject individual fields, not entire rows. A row with
valid SOC, battery, PV, and price data is still useful even if house_load
is rejected.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from .logging_utils import emit
from .types import EventType, TelemetryRow, ValidationResult

logger = logging.getLogger(__name__)


# Bounds for extended inverter fields. (field, min_inclusive, max_inclusive).
# A reading outside this range is treated as a bad read and nulled. These
# are deliberately generous — the goal is to catch obvious garbage (e.g.
# an S16 sign flip showing -3200°C), not to second-guess real physics.
_EXTENDED_BOUNDS: tuple[tuple[str, float, float], ...] = (
    # Battery thermal
    ("cell_temp_avg_c", -40.0, 100.0),
    ("cell_temp_max_c", -40.0, 100.0),
    ("cell_temp_min_c", -40.0, 100.0),
    ("pcs_temp_c", -40.0, 120.0),
    # Cell voltages (wide — supports LFP ~3.2V, NMC ~3.7V nominals with headroom)
    ("cell_volt_avg_v", 1.5, 5.0),
    ("cell_volt_max_v", 1.5, 5.0),
    ("cell_volt_min_v", 1.5, 5.0),
    # Battery state-of-health
    ("soh_pct", 0.0, 100.0),
    # Dynamic power limits. The BMS reports pack-side maxima, which can
    # legitimately exceed the inverter's AC-side nameplate — a 40 kWh
    # pack's "available discharge" floats around 30-40 kW on a healthy
    # SOC even though the AC side is capped at 10 kW. Widen the upper
    # bound so we catch true garbage (e.g. sentinel leakage) without
    # nulling legitimate high readings.
    ("available_charge_kw", 0.0, 50.0),
    ("available_discharge_kw", 0.0, 50.0),
    # Grid AC quality
    ("grid_freq_hz", 40.0, 70.0),
    ("phase_a_voltage_v", 0.0, 300.0),
    ("phase_b_voltage_v", 0.0, 300.0),
    ("phase_c_voltage_v", 0.0, 300.0),
    # Per-MPPT strings (allow slight negatives from pre-dawn noise)
    ("mppt1_voltage_v", -50.0, 700.0),
    ("mppt2_voltage_v", -50.0, 700.0),
    ("mppt3_voltage_v", -50.0, 700.0),
    ("mppt4_voltage_v", -50.0, 700.0),
    ("mppt1_current_a", -50.0, 50.0),
    ("mppt2_current_a", -50.0, 50.0),
    ("mppt3_current_a", -50.0, 50.0),
    ("mppt4_current_a", -50.0, 50.0),
    # Lifetime counters: non-negative. No upper bound — could legitimately
    # reach many GWh over a long installation life.
    ("lifetime_pv_kwh", 0.0, 1e12),
    ("lifetime_load_kwh", 0.0, 1e12),
    ("lifetime_charge_kwh", 0.0, 1e12),
    ("lifetime_discharge_kwh", 0.0, 1e12),
    ("lifetime_import_kwh", 0.0, 1e12),
    ("lifetime_export_kwh", 0.0, 1e12),
)


def validate_telemetry(
    row: TelemetryRow,
    grid_sensor_online: bool,
    bom_data_age: timedelta | None,
    rolling_p95: float | None,
    grid_kw_shelly: float | None = None,
) -> tuple[TelemetryRow, ValidationResult]:
    """Validate a telemetry row and null out rejected fields.

    Returns a (possibly modified) row and the validation result.
    """
    warnings: list[str] = []
    rejected: list[str] = []
    overrides: dict[str, None] = {}

    # 1. Grid sensor must be online
    if not grid_sensor_online:
        rejected.extend(["grid_kw", "house_load_kw"])
        overrides["grid_kw"] = None
        overrides["house_load_kw"] = None
        warnings.append("Grid sensor offline — grid/load data excluded")

    # 2. House load must be non-negative
    if row.house_load_kw is not None and row.house_load_kw < -0.1:
        rejected.append("house_load_kw")
        overrides["house_load_kw"] = None
        warnings.append(f"Negative house load ({row.house_load_kw:.2f}kW) — derivation error")

    # 3. SOC bounds
    if row.soc_pct is not None and not (0 <= row.soc_pct <= 100):
        rejected.append("soc_pct")
        overrides["soc_pct"] = None
        warnings.append(f"SOC out of range: {row.soc_pct}")

    # 4. Stale BOM data
    if row.outdoor_temp_c is not None and bom_data_age and bom_data_age > timedelta(hours=2):
        rejected.append("outdoor_temp_c")
        overrides["outdoor_temp_c"] = None
        warnings.append("BOM data stale >2h — temp excluded")

    # 5. Outlier detection
    if (
        row.house_load_kw is not None
        and rolling_p95 is not None
        and rolling_p95 > 0
        and row.house_load_kw > 3 * rolling_p95
    ):
        warnings.append(f"House load outlier: {row.house_load_kw:.1f}kW (P95={rolling_p95:.1f})")

    # 6. Mains CT cross-validation
    if (
        row.grid_kw is not None
        and grid_kw_shelly is not None
        and abs(row.grid_kw - grid_kw_shelly) > 0.5
    ):
        warnings.append(
            f"Grid sensor divergence: Modbus={row.grid_kw:.2f}kW Shelly={grid_kw_shelly:.2f}kW"
        )

    # 7. Extended inverter telemetry — null-over-wrong for fields with
    # real-world physical bounds. A wildly out-of-range reading almost
    # always means a transient bad register read, not a genuine signal
    # worth analysing.
    for field, lo, hi in _EXTENDED_BOUNDS:
        val = getattr(row, field)
        if val is not None and not (lo <= val <= hi):
            rejected.append(field)
            overrides[field] = None
            warnings.append(f"{field} out of range: {val} (expected {lo}..{hi})")

    # Apply overrides to create corrected row
    if overrides:
        row_dict = {fname: getattr(row, fname) for fname in TelemetryRow.__dataclass_fields__}
        row_dict.update(overrides)
        row = TelemetryRow(**row_dict)

    result = ValidationResult(
        valid=len(rejected) == 0,
        warnings=warnings,
        rejected_fields=rejected,
    )

    # Emit events for warnings and rejections
    for w in warnings:
        emit(EventType.VALIDATION_WARNING, {"message": w})
    if rejected:
        emit(EventType.VALIDATION_REJECT, {"fields": rejected})

    return row, result
