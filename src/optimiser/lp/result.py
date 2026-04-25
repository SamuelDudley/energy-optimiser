"""Result types from the LP solver."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum, auto

from ..types import LoadCommand


class SolveStatus(StrEnum):
    OPTIMAL = auto()  # Solver found the optimal solution
    FEASIBLE = auto()  # Found a solution but couldn't prove optimality
    # (timed out partway). Usually still good.
    INFEASIBLE = auto()  # Constraints contradict each other; no solution exists
    UNBOUNDED = auto()  # Objective unbounded (model bug)
    TIMEOUT = auto()  # Hit wall-clock limit before any feasible solution
    ERROR = auto()  # Solver crashed or threw


@dataclass(frozen=True, slots=True)
class SlotDecision:
    """The LP's planned action for a single slot (slot 0 = now)."""

    slot_start: datetime
    battery_kw: float  # signed: + charge, - discharge
    grid_import_kw: float
    grid_export_kw: float
    pv_to_house_kw: float
    pv_to_battery_kw: float
    pv_to_export_kw: float
    soc_pct_end: float  # SOC at end of this slot
    # Grid contribution to battery charging in this slot. Read directly
    # off the LP variable rather than inferred via subtraction — needed
    # by `dispatch_from_slot` to choose mode 3 (grid-dominant) vs mode 2
    # (PV-dominant adaptive trim). Default 0.0 keeps positional
    # construction in tests working.
    grid_to_battery_kw: float = 0.0
    load_kw: dict[str, float] = field(default_factory=dict)  # per load_id


@dataclass(frozen=True, slots=True)
class LPSolution:
    """The solved LP — committed slot-0 decisions and forward trajectory.

    Slot 0 is what the service applies this tick. Slots 1..N are the LP's
    plan for the future, exposed for snapshotting / replay analysis but
    not directly executed (the next tick's solve supersedes them).
    """

    status: SolveStatus
    slot_0: SlotDecision | None  # None if status != OPTIMAL/FEASIBLE
    forward_trajectory: list[SlotDecision]  # All slots including 0
    load_commands: list[LoadCommand]  # Slot-0 commands (for service.apply)
    grid_export_limit_kw: float | None  # Slot 0 — None means inverter default
    expected_total_cost_cents: float  # Objective value (lower = better)
    solve_time_ms: float
    reason: str  # Human-readable: why this decision
