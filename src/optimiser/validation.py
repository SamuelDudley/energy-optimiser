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
