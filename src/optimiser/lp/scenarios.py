"""Price-scenario constructors for the stochastic LP.

The LP today treats prices deterministically: `forecast_predicted` when
present, else `import_per_kwh` / `export_per_kwh`. Amber publishes a
confidence band — `advancedPrice.{low, predicted, high}` — on both
the `general` (import) and `feedIn` (export) channels for every
ForecastInterval row, parsed since 2026-04-28 (see KNOWN-ISSUES #9).
This module turns that band into a list of weighted price scenarios
the solver can iterate over alongside the existing PV scenarios.

Three modes:

- `POINT` (default, status-quo behaviour): one scenario, weight 1.0.
  The resolver returns `forecast_predicted` when populated, falling
  back to the spot point estimate. Identical to the deterministic
  resolver that lived inline in `formulation.py` before scenarios
  were introduced.

- `SHARED`: three scenarios where import and export move together
  through the band — (low, low), (predicted, predicted), (high, high).
  Three scenarios, weights `(0.2, 0.6, 0.2)` matching the PV-side
  convention. Encodes the NEM-coupled assumption: a wholesale
  surprise moves the customer's import and export prices the same
  direction at once. Cheap to run (3× the price-axis cost — same
  total scenarios as POINT × 3 PV).

- `CROSS`: full 3×3 cross-product, treating the import and export
  bands as independent dimensions. Nine scenarios, weights = product
  of marginals (so all weights sum to 1). More honest about Amber's
  per-channel uncertainty but multiplies solve time. Composed with
  the 3 PV scenarios in `build_stochastic_lp`, total compound
  scenario count = 9 × 3 = 27.

The flag `lp.constants.PRICE_SCENARIO_MODE` selects the mode at solve
time; the default is `POINT`. Sweep evidence from `/sim-sweep` against
post-2026-04-28 snapshots is needed before flipping the default —
see KNOWN-ISSUES #24.

Resolver fallback chain
-----------------------

For each scenario, `resolve_ip` / `resolve_ep` apply this rule per
slot:

1. If the requested band leg is populated (e.g. `forecast_high` for a
   "high" scenario), use it.
2. Otherwise fall back to `forecast_predicted` (or
   `export_forecast_predicted`) if populated.
3. Otherwise fall back to the spot point estimate
   (`import_per_kwh` / `export_per_kwh`).

Step (1) only fires on ForecastInterval rows where Amber publishes
the full advancedPrice block. Step (2) catches partial / malformed
data (defence in depth — Amber publishes the whole block atomically
in practice). Step (3) fires on settled intervals (CurrentInterval /
ActualInterval) where there is no advancedPrice — at that point the
spot field holds the locked actual price, so all band scenarios
collapse to the same value at that slot. This collapse means that
non-anticipativity at slot 0 is never disturbed by the band itself
on a locked or near-locked slot — the price-axis variation only
exists on forward forecast slots.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from ..types import PriceInterval

# Per-channel band leg identifiers. Kept as string literals so the
# scenario name (which embeds them) is readable in diagnostics.
BandLeg = Literal["low", "predicted", "high"]

# Default scenario weights — same triplet as the PV side. Sum to 1.
# Tuned to bias the central case while keeping P10/P90 meaningful as
# safety hedges. Configurable per-call via `build_price_scenarios`.
_DEFAULT_BAND_WEIGHTS: dict[BandLeg, float] = {
    "low": 0.20,
    "predicted": 0.60,
    "high": 0.20,
}


class PriceScenarioMode(StrEnum):
    """How the price-axis stochasticity is composed into the LP.

    StrEnum so the value is JSON-serialisable in snapshots and config
    without bespoke handling.
    """

    POINT = "point"
    SHARED = "shared"
    CROSS = "cross"


@dataclass(frozen=True, slots=True)
class PriceScenario:
    """One leg of the price-scenario distribution.

    A scenario doesn't carry per-slot prices — instead it carries
    a recipe for resolving (import, export) prices from a
    `PriceInterval`, which the cost-term loop then applies per slot.
    This avoids materialising a per-slot price array per scenario
    and keeps the scenario count distinct from the slot count.
    """

    name: str
    weight: float
    import_band: BandLeg
    export_band: BandLeg

    def resolve_ip(self, price: PriceInterval) -> float:
        """Resolved import cost for this scenario at one slot."""
        return _resolve(
            band=self.import_band,
            band_low=price.forecast_low,
            band_predicted=price.forecast_predicted,
            band_high=price.forecast_high,
            point_fallback=price.import_per_kwh,
        )

    def resolve_ep(self, price: PriceInterval) -> float:
        """Resolved export revenue for this scenario at one slot."""
        return _resolve(
            band=self.export_band,
            band_low=price.export_forecast_low,
            band_predicted=price.export_forecast_predicted,
            band_high=price.export_forecast_high,
            point_fallback=price.export_per_kwh,
        )


def _resolve(
    *,
    band: BandLeg,
    band_low: float | None,
    band_predicted: float | None,
    band_high: float | None,
    point_fallback: float,
) -> float:
    """Apply the fallback chain: requested → predicted → spot."""
    requested = (
        band_low if band == "low"
        else band_high if band == "high"
        else band_predicted
    )
    if requested is not None:
        return requested
    if band_predicted is not None:
        return band_predicted
    return point_fallback


def _point_scenario() -> PriceScenario:
    """The deterministic single-scenario case. Identical resolver
    behaviour to the pre-scenarios `predicted-or-spot` rule.
    """
    return PriceScenario(
        name="point",
        weight=1.0,
        import_band="predicted",
        export_band="predicted",
    )


def build_price_scenarios(
    mode: PriceScenarioMode,
    band_weights: dict[BandLeg, float] | None = None,
) -> list[PriceScenario]:
    """Construct the list of price scenarios for the requested mode.

    `band_weights` lets callers override the per-leg marginal weights;
    defaults to the (0.2, 0.6, 0.2) triplet. The total of every
    returned list always sums to 1.0 (modulo float tolerance), which
    `build_stochastic_lp`'s caller then composes with PV weights.

    POINT: 1 scenario, weight 1.0.
    SHARED: 3 scenarios, weights = the band marginals (sum to 1).
    CROSS: 9 scenarios, weights = product of marginals (sum to 1).
    """
    weights = band_weights or _DEFAULT_BAND_WEIGHTS
    _validate_weights(weights)

    if mode is PriceScenarioMode.POINT:
        return [_point_scenario()]

    if mode is PriceScenarioMode.SHARED:
        return [
            PriceScenario(
                name=f"shared_{leg}",
                weight=weights[leg],
                import_band=leg,
                export_band=leg,
            )
            for leg in ("low", "predicted", "high")
        ]

    if mode is PriceScenarioMode.CROSS:
        return [
            PriceScenario(
                name=f"i_{i}_e_{e}",
                weight=weights[i] * weights[e],
                import_band=i,
                export_band=e,
            )
            for i in ("low", "predicted", "high")
            for e in ("low", "predicted", "high")
        ]

    raise ValueError(f"unknown PriceScenarioMode: {mode!r}")


def _validate_weights(weights: dict[BandLeg, float]) -> None:
    if set(weights) != {"low", "predicted", "high"}:
        raise ValueError(
            f"band_weights must have keys low/predicted/high, got {sorted(weights)}"
        )
    total = sum(weights.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(
            f"band_weights must sum to 1.0, got {total:.6f}"
        )
    for leg, w in weights.items():
        if w < 0.0:
            raise ValueError(f"band_weights[{leg}] must be non-negative, got {w}")
