"""User-strategy modes — runtime state, persistence, LP-side overrides.

Two modes are supported, both time-bounded LP-side constraints:

- ``buy``     — window + import-price ceiling
- ``conserve`` — window + export-price floor

See ``docs/superpowers/specs/2026-05-19-user-strategy-modes-design.md`` for
the design rationale and ``docs/superpowers/plans/2026-05-19-user-strategy-modes.md``
for the implementation plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

ModeKind = Literal["buy", "conserve"]
_KINDS: tuple[ModeKind, ...] = ("buy", "conserve")


@dataclass(frozen=True, slots=True)
class ActiveMode:
    """One currently-active user-strategy mode.

    ``end_at`` is when the mode auto-expires. ``params`` carries the
    per-mode threshold (``ceiling_c_per_kwh`` for buy,
    ``floor_c_per_kwh`` for conserve). All datetimes are UTC; naive
    datetimes are rejected at construction to avoid timezone bugs at
    LP slot boundaries.
    """

    kind: ModeKind
    end_at: datetime
    params: dict[str, Any]
    activated_at: datetime
    source: str

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"kind must be one of {_KINDS}, got {self.kind!r}")
        for fname, dt in (("end_at", self.end_at), ("activated_at", self.activated_at)):
            if dt.tzinfo is None:
                raise ValueError(f"{fname} must be UTC (tz-aware), got naive {dt!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "end_at": self.end_at.isoformat(),
            "params": dict(self.params),
            "activated_at": self.activated_at.isoformat(),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ActiveMode:
        return cls(
            kind=d["kind"],
            end_at=datetime.fromisoformat(d["end_at"]),
            params=dict(d["params"]),
            activated_at=datetime.fromisoformat(d["activated_at"]),
            source=d["source"],
        )


@dataclass(frozen=True, slots=True)
class ModeOverrides:
    """Per-slot LP-side view of currently-active modes.

    Pre-computed once at LP build time so the per-scenario constraint
    loop does cheap tuple lookups rather than time-window arithmetic
    on every slot.
    """

    buy_active_at: tuple[bool, ...]
    buy_ceiling_c_per_kwh: float | None
    conserve_active_at: tuple[bool, ...]
    conserve_floor_c_per_kwh: float | None

    @classmethod
    def empty(cls, n_slots: int) -> ModeOverrides:
        falses = tuple([False] * n_slots)
        return cls(
            buy_active_at=falses,
            buy_ceiling_c_per_kwh=None,
            conserve_active_at=falses,
            conserve_floor_c_per_kwh=None,
        )

    def any_buy_active(self) -> bool:
        return any(self.buy_active_at)

    def any_conserve_active(self) -> bool:
        return any(self.conserve_active_at)
