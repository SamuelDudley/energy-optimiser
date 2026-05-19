"""User-strategy modes — runtime state, persistence, LP-side overrides.

Two modes are supported, both time-bounded LP-side constraints:

- ``buy``     — window + import-price ceiling
- ``conserve`` — window + export-price floor

See ``docs/superpowers/specs/2026-05-19-user-strategy-modes-design.md`` for
the design rationale and ``docs/superpowers/plans/2026-05-19-user-strategy-modes.md``
for the implementation plan.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .logging_utils import emit
from .types import EventType

logger = logging.getLogger(__name__)

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


class ModeManager:
    """In-memory + JSON-persisted set of active user-strategy modes.

    At most one mode of each kind is active at a time. Re-activating a
    kind replaces the existing entry. Expired entries are dropped lazily
    on the next call to ``active()`` — that's also when MODE_EXPIRED is
    emitted, so callers see a deterministic event stream.

    The JSON file is rewritten on every state change. Corrupt files are
    treated like a missing file (empty state + warning); we never crash
    the service trying to parse junk.
    """

    def __init__(self, state_path: Path) -> None:
        self._state_path = Path(state_path)
        self._modes: dict[str, ActiveMode] = {}
        self._load()

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            logger.warning(
                "active_modes file corrupt or unreadable; starting empty (%s)",
                self._state_path,
            )
            return
        now = datetime.now(UTC)
        dropped_any = False
        for kind, payload in (raw or {}).items():
            if payload is None:
                continue
            try:
                m = ActiveMode.from_dict(payload)
            except (KeyError, ValueError):
                logger.warning("dropping malformed mode entry %r", kind)
                continue
            if m.end_at <= now:
                # Already expired at load — emit with the restart reason
                # so audit can tell post-restart drops apart from normal
                # window-end expiries. Don't add to live state.
                emit(
                    EventType.MODE_EXPIRED,
                    {"kind": kind, "reason": "service_started_after_end_at"},
                )
                dropped_any = True
                continue
            self._modes[kind] = m
        if dropped_any:
            # Persist the cleaned state so a subsequent restart doesn't
            # re-replay the same expired entries.
            self._persist()

    def _persist(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {kind: m.to_dict() for kind, m in self._modes.items()}
        # Atomic-ish write: tmp file + rename so a crash mid-write
        # doesn't leave a truncated JSON the next start can't parse.
        tmp = self._state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self._state_path)

    def activate(self, mode: ActiveMode) -> ActiveMode:
        """Insert (or replace) an active mode. Persists to disk and emits MODE_ACTIVATED."""
        self._modes[mode.kind] = mode
        self._persist()
        emit(
            EventType.MODE_ACTIVATED,
            {
                "kind": mode.kind,
                "params": dict(mode.params),
                "source": mode.source,
                "end_at": mode.end_at.isoformat(),
                "activated_at": mode.activated_at.isoformat(),
            },
        )
        return mode

    def cancel(self, kind: ModeKind) -> bool:
        """Remove an active mode early. Returns True if a mode was removed."""
        if kind not in self._modes:
            return False
        del self._modes[kind]
        self._persist()
        emit(EventType.MODE_EXPIRED, {"kind": kind, "reason": "user_cancelled"})
        return True

    def active(self, now: datetime) -> list[ActiveMode]:
        """Return currently-active modes, pruning runtime expiries.

        Load-time expiries were already handled in ``_load()`` with the
        ``service_started_after_end_at`` reason. Anything that expires
        between construction and now is a runtime expiry tagged
        ``window_ended``.
        """
        expired = [kind for kind, m in self._modes.items() if m.end_at <= now]
        if not expired:
            return list(self._modes.values())
        for kind in expired:
            del self._modes[kind]
            emit(EventType.MODE_EXPIRED, {"kind": kind, "reason": "window_ended"})
        self._persist()
        return list(self._modes.values())

    def to_overrides(self, now: datetime, slots: list[datetime]) -> ModeOverrides:
        """Compute the per-slot mask consumed by the LP formulation.

        ``slots`` are the slot *start* times. A slot is considered
        in-window if ``slot_start < end_at`` (strict inequality —
        a slot starting exactly at ``end_at`` belongs to the
        post-window epoch).
        """
        active = {m.kind: m for m in self.active(now)}
        buy = active.get("buy")
        conserve = active.get("conserve")
        buy_mask = tuple((buy is not None and slot < buy.end_at) for slot in slots)
        conserve_mask = tuple((conserve is not None and slot < conserve.end_at) for slot in slots)
        return ModeOverrides(
            buy_active_at=buy_mask,
            buy_ceiling_c_per_kwh=(buy.params["ceiling_c_per_kwh"] if buy else None),
            conserve_active_at=conserve_mask,
            conserve_floor_c_per_kwh=(conserve.params["floor_c_per_kwh"] if conserve else None),
        )
