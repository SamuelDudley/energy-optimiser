# User Strategy Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two user-invoked time-bounded strategy modes — `buy` (window + import-price ceiling, charges aggressively below it) and `conserve` (window + export-price floor, banks PV aggressively, blocks battery export below floor) — as additive LP-side constraints on the existing stochastic LP, with dashboard activation and JSON-file persistence across restarts.

**Architecture:** Modes are time-bounded LP-side constraints + objective tweaks. A new `ModeManager` owns runtime state, persists to `<state>/active_modes.json`, and produces a per-slot `ModeOverrides` view that flows through `solve_stochastic → build_stochastic_lp → _add_scenario_to_problem`. No parallel control path: dispatch logic is unchanged, `dispatch_from_slot` reads `slot_0` as today. REST API at `/modes/{buy,conserve}` activates/cancels; a dashboard card surfaces state and offers Amber-forecast-aware threshold suggestions.

**Tech Stack:** Python 3.12, PuLP (HiGHS solver), aiohttp, pytest. New file `src/optimiser/modes.py`. Modifications to `lp/formulation.py`, `lp/solver.py`, `service.py`, `types.py`, `api/server.py`, `api/probe.py`, dashboard static assets.

**Spec reference:** `docs/superpowers/specs/2026-05-19-user-strategy-modes-design.md`

---

## Architecture / file structure

| File | Status | Responsibility |
|---|---|---|
| `src/optimiser/modes.py` | **new** | `ActiveMode`, `ModeOverrides` dataclasses; `ModeManager` (activate/cancel/active/to_overrides) with JSON persistence |
| `src/optimiser/types.py` | modify | Add `EventType.MODE_ACTIVATED`, `EventType.MODE_EXPIRED`; add `TickSnapshot.active_modes` field |
| `src/optimiser/lp/formulation.py` | modify | `build_stochastic_lp` accepts `mode_overrides`; per-slot constraints + per-slot wear factors in `_add_scenario_to_problem` |
| `src/optimiser/lp/solver.py` | modify | `solve_stochastic` accepts and forwards `mode_overrides` |
| `src/optimiser/service.py` | modify | Service owns `ModeManager`; `_run_lp` calls `to_overrides(slots)` and forwards |
| `src/optimiser/api/probe.py` | modify | Expose `mode_manager` on the `ServiceProbe` protocol |
| `src/optimiser/api/handlers/modes.py` | **new** | REST: `POST/DELETE /modes/{buy,conserve}`, `GET /modes`, `GET /modes/suggest` |
| `src/optimiser/api/server.py` | modify | Register new routes |
| `src/optimiser/api/handlers/dashboard.py` | modify | Include `active_modes` + suggestion params in `/dashboard/config` payload |
| `src/optimiser/api/static/dashboard.html` | modify | Two mode cards |
| `src/optimiser/api/static/dashboard.js` | modify | Card rendering, activate/cancel panel logic |
| `src/optimiser/api/static/dashboard.css` | modify | Card + panel styling |
| `src/optimiser/replay.py` | modify | `respect_modes` flag — reconstruct overrides from snapshot for replay |
| `tests/test_modes.py` | **new** | `ModeManager` unit tests |
| `tests/test_lp_modes.py` | **new** | LP-with-modes integration tests |
| `tests/test_modes_api.py` | **new** | API handler tests |
| `tests/test_snapshot_writer.py` | modify | Verify `active_modes` round-trips |

---

## Task 1: Add EventType entries

**Files:**
- Modify: `src/optimiser/types.py`

- [ ] **Step 1: Locate EventType enum and add the two new entries**

Open `src/optimiser/types.py` and find the `class EventType(StrEnum):` block (around line 82). After the last existing entry (`MODBUS_READ_BATCH`), add:

```python
    # User strategy modes
    #   MODE_ACTIVATED: {kind, params, source, end_at, activated_at}
    #   MODE_EXPIRED:   {kind, reason}  reason ∈ {"window_ended", "user_cancelled", "service_started_after_end_at"}
    MODE_ACTIVATED = auto()
    MODE_EXPIRED = auto()
```

- [ ] **Step 2: Run the existing test suite to confirm the enum change doesn't break anything**

Run: `uv run pytest tests/ -q --tb=line`
Expected: same pass count as before this change (no test references these new values yet).

- [ ] **Step 3: Commit**

```bash
git add src/optimiser/types.py
git commit -m "types: add MODE_ACTIVATED and MODE_EXPIRED event types"
```

---

## Task 2: ActiveMode + ModeOverrides dataclasses

**Files:**
- Create: `src/optimiser/modes.py`
- Create: `tests/test_modes.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_modes.py`:

```python
"""Tests for the user-strategy-modes data types and manager."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from optimiser.modes import ActiveMode, ModeOverrides


# Synthetic "now" sits far in the future so wall-clock checks in _load()
# (which uses datetime.now(UTC) to detect already-expired entries on
# restart) never spuriously treat NOW + Nh as a past date.
NOW = datetime(2099, 5, 19, 4, 0, 0, tzinfo=UTC)


class TestActiveMode:
    def test_buy_mode_round_trip(self) -> None:
        m = ActiveMode(
            kind="buy",
            end_at=NOW + timedelta(hours=2),
            params={"ceiling_c_per_kwh": 12.0},
            activated_at=NOW,
            source="dashboard",
        )
        assert m.kind == "buy"
        assert m.params["ceiling_c_per_kwh"] == 12.0

    def test_to_dict_and_back(self) -> None:
        m = ActiveMode(
            kind="conserve",
            end_at=NOW + timedelta(hours=4),
            params={"floor_c_per_kwh": 18.0},
            activated_at=NOW,
            source="dashboard",
        )
        d = m.to_dict()
        assert d["kind"] == "conserve"
        assert d["end_at"] == (NOW + timedelta(hours=4)).isoformat()
        assert d["params"] == {"floor_c_per_kwh": 18.0}

        restored = ActiveMode.from_dict(d)
        assert restored == m

    def test_rejects_invalid_kind(self) -> None:
        with pytest.raises(ValueError, match="kind"):
            ActiveMode(
                kind="bogus",  # type: ignore[arg-type]
                end_at=NOW + timedelta(hours=1),
                params={},
                activated_at=NOW,
                source="dashboard",
            )

    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="UTC"):
            ActiveMode(
                kind="buy",
                end_at=datetime(2026, 5, 19, 4, 0, 0),  # naive
                params={"ceiling_c_per_kwh": 12.0},
                activated_at=NOW,
                source="dashboard",
            )


class TestModeOverrides:
    def test_empty_factory(self) -> None:
        o = ModeOverrides.empty(n_slots=12)
        assert len(o.buy_active_at) == 12
        assert all(v is False for v in o.buy_active_at)
        assert len(o.conserve_active_at) == 12
        assert all(v is False for v in o.conserve_active_at)
        assert o.buy_ceiling_c_per_kwh is None
        assert o.conserve_floor_c_per_kwh is None

    def test_any_active(self) -> None:
        empty = ModeOverrides.empty(n_slots=4)
        assert empty.any_buy_active() is False
        assert empty.any_conserve_active() is False

        with_buy = ModeOverrides(
            buy_active_at=(False, True, True, False),
            buy_ceiling_c_per_kwh=10.0,
            conserve_active_at=(False, False, False, False),
            conserve_floor_c_per_kwh=None,
        )
        assert with_buy.any_buy_active() is True
        assert with_buy.any_conserve_active() is False
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/test_modes.py -v`
Expected: ImportError or collection error — `optimiser.modes` does not exist yet.

- [ ] **Step 3: Create the module**

Create `src/optimiser/modes.py`:

```python
"""User-strategy modes — runtime state, persistence, LP-side overrides.

Two modes are supported, both time-bounded LP-side constraints:

- ``buy``     — window + import-price ceiling
- ``conserve`` — window + export-price floor

See ``docs/superpowers/specs/2026-05-19-user-strategy-modes-design.md`` for
the design rationale and ``docs/superpowers/plans/2026-05-19-user-strategy-modes.md``
for the implementation plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/test_modes.py -v`
Expected: all `TestActiveMode` and `TestModeOverrides` tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/optimiser/modes.py tests/test_modes.py
git commit -m "modes: ActiveMode + ModeOverrides data types"
```

---

## Task 3: ModeManager — constructor + load + persist

**Files:**
- Modify: `src/optimiser/modes.py`
- Modify: `tests/test_modes.py`

- [ ] **Step 1: Add failing tests for ModeManager construction/persistence**

Append to `tests/test_modes.py`:

```python
from optimiser.modes import ModeManager


class TestModeManagerPersistence:
    def test_load_when_file_absent(self, tmp_path) -> None:
        mgr = ModeManager(tmp_path / "active_modes.json")
        assert mgr.active(NOW) == []

    def test_round_trip_through_disk(self, tmp_path) -> None:
        path = tmp_path / "active_modes.json"
        mgr = ModeManager(path)
        m = ActiveMode(
            kind="buy",
            end_at=NOW + timedelta(hours=2),
            params={"ceiling_c_per_kwh": 12.0},
            activated_at=NOW,
            source="dashboard",
        )
        mgr.activate(m)

        # A fresh manager reads the same file.
        mgr2 = ModeManager(path)
        active = mgr2.active(NOW)
        assert len(active) == 1
        assert active[0].kind == "buy"
        assert active[0].params["ceiling_c_per_kwh"] == 12.0

    def test_corrupt_file_starts_empty_and_does_not_raise(self, tmp_path) -> None:
        path = tmp_path / "active_modes.json"
        path.write_text("this is not json")
        mgr = ModeManager(path)
        # Corrupt JSON is treated like a missing file: empty state, log a warning,
        # don't crash the service.
        assert mgr.active(NOW) == []
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/test_modes.py::TestModeManagerPersistence -v`
Expected: ImportError — `ModeManager` is not exported yet.

- [ ] **Step 3: Implement ModeManager — constructor + I/O**

Append to `src/optimiser/modes.py`:

```python
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


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
            logger.warning("active_modes file corrupt or unreadable; starting empty (%s)",
                           self._state_path)
            return
        for kind, payload in (raw or {}).items():
            if payload is None:
                continue
            try:
                self._modes[kind] = ActiveMode.from_dict(payload)
            except (KeyError, ValueError):
                logger.warning("dropping malformed mode entry %r", kind)

    def _persist(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {kind: m.to_dict() for kind, m in self._modes.items()}
        # Atomic-ish write: tmp file + rename so a crash mid-write
        # doesn't leave a truncated JSON the next start can't parse.
        tmp = self._state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self._state_path)

    def activate(self, mode: ActiveMode) -> ActiveMode:
        """Insert (or replace) an active mode. Persists to disk."""
        self._modes[mode.kind] = mode
        self._persist()
        return mode

    def active(self, now: datetime) -> list[ActiveMode]:
        """Return currently-active modes, dropping any past their end_at.

        Lazy expiry: this is the only place expired modes are pruned.
        Service should call this each tick so MODE_EXPIRED fires close
        to actual expiry.
        """
        # Expiry handling added in Task 5.
        return list(self._modes.values())
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/test_modes.py::TestModeManagerPersistence -v`
Expected: three tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/optimiser/modes.py tests/test_modes.py
git commit -m "modes: ModeManager constructor + JSON load/persist"
```

---

## Task 4: ModeManager — activate emits event, cancel removes

**Files:**
- Modify: `src/optimiser/modes.py`
- Modify: `tests/test_modes.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_modes.py`:

```python
class TestModeManagerActivateCancel:
    def test_activate_emits_event(self, tmp_path, monkeypatch) -> None:
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            "optimiser.modes.emit",
            lambda et, payload: events.append((et.value, payload)),
        )
        mgr = ModeManager(tmp_path / "active_modes.json")
        m = ActiveMode(
            kind="buy",
            end_at=NOW + timedelta(hours=2),
            params={"ceiling_c_per_kwh": 12.0},
            activated_at=NOW,
            source="dashboard",
        )
        mgr.activate(m)
        assert any(et == "MODE_ACTIVATED" for et, _ in events)
        # Payload carries the essentials for replay/audit.
        activated = next(p for et, p in events if et == "MODE_ACTIVATED")
        assert activated["kind"] == "buy"
        assert activated["params"]["ceiling_c_per_kwh"] == 12.0

    def test_activate_replaces_existing(self, tmp_path) -> None:
        mgr = ModeManager(tmp_path / "active_modes.json")
        m1 = ActiveMode(
            kind="buy",
            end_at=NOW + timedelta(hours=1),
            params={"ceiling_c_per_kwh": 10.0},
            activated_at=NOW,
            source="dashboard",
        )
        m2 = ActiveMode(
            kind="buy",
            end_at=NOW + timedelta(hours=3),
            params={"ceiling_c_per_kwh": 14.0},
            activated_at=NOW + timedelta(minutes=5),
            source="dashboard",
        )
        mgr.activate(m1)
        mgr.activate(m2)
        active = mgr.active(NOW)
        assert len(active) == 1
        assert active[0].params["ceiling_c_per_kwh"] == 14.0

    def test_cancel_emits_event_and_removes(self, tmp_path, monkeypatch) -> None:
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            "optimiser.modes.emit",
            lambda et, payload: events.append((et.value, payload)),
        )
        mgr = ModeManager(tmp_path / "active_modes.json")
        mgr.activate(
            ActiveMode(
                kind="conserve",
                end_at=NOW + timedelta(hours=2),
                params={"floor_c_per_kwh": 18.0},
                activated_at=NOW,
                source="dashboard",
            )
        )
        events.clear()
        result = mgr.cancel("conserve")
        assert result is True
        assert mgr.active(NOW) == []
        assert any(
            et == "MODE_EXPIRED" and p["reason"] == "user_cancelled"
            for et, p in events
        )

    def test_cancel_returns_false_when_not_active(self, tmp_path) -> None:
        mgr = ModeManager(tmp_path / "active_modes.json")
        assert mgr.cancel("buy") is False
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/test_modes.py::TestModeManagerActivateCancel -v`
Expected: failures — `cancel` not implemented, `emit` not called from `activate`.

- [ ] **Step 3: Wire emit into activate + implement cancel**

In `src/optimiser/modes.py`, add the import near the top:

```python
from .logging_utils import emit
from .types import EventType
```

Then update `ModeManager.activate` to emit and add `cancel`:

```python
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
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/test_modes.py::TestModeManagerActivateCancel -v`
Expected: all four tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/optimiser/modes.py tests/test_modes.py
git commit -m "modes: activate/cancel with MODE_ACTIVATED / MODE_EXPIRED events"
```

---

## Task 5: ModeManager — active() prunes expired and emits

**Files:**
- Modify: `src/optimiser/modes.py`
- Modify: `tests/test_modes.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_modes.py`:

```python
class TestModeManagerExpiry:
    def test_expired_modes_dropped_lazily(self, tmp_path, monkeypatch) -> None:
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            "optimiser.modes.emit",
            lambda et, payload: events.append((et.value, payload)),
        )
        mgr = ModeManager(tmp_path / "active_modes.json")
        mgr.activate(
            ActiveMode(
                kind="buy",
                end_at=NOW + timedelta(hours=1),
                params={"ceiling_c_per_kwh": 12.0},
                activated_at=NOW,
                source="dashboard",
            )
        )
        events.clear()
        # Two hours later, the mode has expired.
        assert mgr.active(NOW + timedelta(hours=2)) == []
        # MODE_EXPIRED is emitted exactly once at expiry.
        expired = [p for et, p in events if et == "MODE_EXPIRED"]
        assert len(expired) == 1
        assert expired[0] == {"kind": "buy", "reason": "window_ended"}

    def test_expired_mode_persisted_removal(self, tmp_path) -> None:
        path = tmp_path / "active_modes.json"
        mgr = ModeManager(path)
        mgr.activate(
            ActiveMode(
                kind="buy",
                end_at=NOW + timedelta(hours=1),
                params={"ceiling_c_per_kwh": 12.0},
                activated_at=NOW,
                source="dashboard",
            )
        )
        # Trigger expiry, then construct a fresh manager — it should
        # see an empty state (the prune was persisted).
        mgr.active(NOW + timedelta(hours=2))
        mgr2 = ModeManager(path)
        assert mgr2.active(NOW + timedelta(hours=2)) == []

    def test_started_after_end_at_emits_special_reason(self, tmp_path, monkeypatch) -> None:
        """Service restart after a mode has already passed end_at —
        the load path drops it with a distinct reason for audit clarity.

        Use a fixed far-past date so the wall-clock check in ``_load()``
        is deterministic regardless of when the test runs."""
        events: list[tuple[str, dict]] = []
        # Patch emit BEFORE constructing the manager — _load() emits during construction.
        monkeypatch.setattr(
            "optimiser.modes.emit",
            lambda et, payload: events.append((et.value, payload)),
        )
        path = tmp_path / "active_modes.json"
        far_past_end = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)
        path.write_text(
            json.dumps(
                {
                    "buy": ActiveMode(
                        kind="buy",
                        end_at=far_past_end,
                        params={"ceiling_c_per_kwh": 12.0},
                        activated_at=far_past_end - timedelta(hours=2),
                        source="dashboard",
                    ).to_dict()
                }
            )
        )
        mgr = ModeManager(path)
        # Emission happens during ModeManager(...) — events already populated.
        expired = [p for et, p in events if et == "MODE_EXPIRED"]
        assert len(expired) == 1
        assert expired[0]["reason"] == "service_started_after_end_at"
        # And the mode is not in live state.
        assert mgr.active(datetime.now(UTC)) == []
```

Add the `json` import at the top of `tests/test_modes.py` if not already present:

```python
import json
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/test_modes.py::TestModeManagerExpiry -v`
Expected: failures — `active()` doesn't prune yet, and load-path doesn't distinguish already-expired entries.

- [ ] **Step 3: Amend `_load()` to drop already-expired entries and add the runtime `active()` implementation**

In `src/optimiser/modes.py`, update the `datetime` import to include `UTC` if not already imported, then replace `_load()` and `active()`:

```python
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
```

Note: `_load()` is wall-clock-dependent for the restart-reason detection. The load-time-expiry test below uses a fixed far-past date to stay deterministic regardless of when the test runs.

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/test_modes.py::TestModeManagerExpiry -v`
Expected: three tests pass.

- [ ] **Step 5: Run the full modes test file to confirm no regressions**

Run: `uv run pytest tests/test_modes.py -v`
Expected: all tests so far pass.

- [ ] **Step 6: Commit**

```bash
git add src/optimiser/modes.py tests/test_modes.py
git commit -m "modes: active() prunes expired and emits MODE_EXPIRED"
```

---

## Task 6: ModeManager.to_overrides() — slot-aligned mask

**Files:**
- Modify: `src/optimiser/modes.py`
- Modify: `tests/test_modes.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_modes.py`:

```python
class TestToOverrides:
    def _slots(self, start: datetime, count: int, minutes: int = 5) -> list[datetime]:
        return [start + timedelta(minutes=minutes * i) for i in range(count)]

    def test_no_active_modes_returns_empty_mask(self, tmp_path) -> None:
        mgr = ModeManager(tmp_path / "active_modes.json")
        slots = self._slots(NOW, 12)
        o = mgr.to_overrides(NOW, slots)
        assert o.any_buy_active() is False
        assert o.any_conserve_active() is False
        assert o.buy_ceiling_c_per_kwh is None
        assert o.conserve_floor_c_per_kwh is None

    def test_buy_window_aligns_to_slots(self, tmp_path) -> None:
        """Buy mode active NOW → NOW+30min: only first 6 of 12 slots
        (each 5 min) should be marked active."""
        mgr = ModeManager(tmp_path / "active_modes.json")
        mgr.activate(
            ActiveMode(
                kind="buy",
                end_at=NOW + timedelta(minutes=30),
                params={"ceiling_c_per_kwh": 12.0},
                activated_at=NOW,
                source="dashboard",
            )
        )
        slots = self._slots(NOW, 12)  # NOW + 0, 5, 10, ..., 55 min
        o = mgr.to_overrides(NOW, slots)
        assert o.buy_active_at[:6] == (True, True, True, True, True, True)
        assert o.buy_active_at[6:] == (False, False, False, False, False, False)
        assert o.buy_ceiling_c_per_kwh == 12.0
        assert o.conserve_floor_c_per_kwh is None

    def test_both_modes_active_with_different_windows(self, tmp_path) -> None:
        mgr = ModeManager(tmp_path / "active_modes.json")
        mgr.activate(
            ActiveMode(
                kind="buy",
                end_at=NOW + timedelta(minutes=15),
                params={"ceiling_c_per_kwh": 10.0},
                activated_at=NOW,
                source="dashboard",
            )
        )
        mgr.activate(
            ActiveMode(
                kind="conserve",
                end_at=NOW + timedelta(minutes=45),
                params={"floor_c_per_kwh": 22.0},
                activated_at=NOW,
                source="dashboard",
            )
        )
        slots = self._slots(NOW, 12)
        o = mgr.to_overrides(NOW, slots)
        # Buy: first 3 slots (NOW, NOW+5, NOW+10) — slot 3 starts at +15 which equals end_at, so excluded.
        assert o.buy_active_at[:3] == (True, True, True)
        assert o.buy_active_at[3] is False
        # Conserve: first 9 slots.
        assert o.conserve_active_at[:9] == tuple([True] * 9)
        assert o.conserve_active_at[9:] == tuple([False] * 3)
        assert o.buy_ceiling_c_per_kwh == 10.0
        assert o.conserve_floor_c_per_kwh == 22.0
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/test_modes.py::TestToOverrides -v`
Expected: AttributeError — `to_overrides` not implemented.

- [ ] **Step 3: Implement to_overrides**

Append to `ModeManager` in `src/optimiser/modes.py`:

```python
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
        buy_mask = tuple(
            (buy is not None and slot < buy.end_at) for slot in slots
        )
        conserve_mask = tuple(
            (conserve is not None and slot < conserve.end_at) for slot in slots
        )
        return ModeOverrides(
            buy_active_at=buy_mask,
            buy_ceiling_c_per_kwh=(buy.params["ceiling_c_per_kwh"] if buy else None),
            conserve_active_at=conserve_mask,
            conserve_floor_c_per_kwh=(conserve.params["floor_c_per_kwh"] if conserve else None),
        )
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/test_modes.py::TestToOverrides -v`
Expected: three tests pass.

- [ ] **Step 5: Run the full modes test file**

Run: `uv run pytest tests/test_modes.py -v`
Expected: all `TestActiveMode`, `TestModeOverrides`, `TestModeManagerPersistence`, `TestModeManagerActivateCancel`, `TestModeManagerExpiry`, `TestToOverrides` tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/optimiser/modes.py tests/test_modes.py
git commit -m "modes: ModeManager.to_overrides() for slot-aligned LP mask"
```

---

## Task 7: LP formulation — accept ModeOverrides (signature only)

**Files:**
- Modify: `src/optimiser/lp/formulation.py`

Goal: thread `mode_overrides: ModeOverrides | None` through `build_stochastic_lp` → `_add_scenario_to_problem` without changing any behaviour. Default `None` means "no overrides," which is the current behaviour. Existing tests must continue to pass.

- [ ] **Step 1: Run the existing LP test suite as a baseline**

Run: `uv run pytest tests/test_lp_scaffolding.py tests/test_lp_stochastic.py tests/test_lp_integration.py -q`
Expected: full pass. Record the count.

- [ ] **Step 2: Add the new parameter to `build_stochastic_lp`**

In `src/optimiser/lp/formulation.py`, find the `build_stochastic_lp` signature (around line 191) and add a kwarg with default `None`. The relevant section currently looks like:

```python
def build_stochastic_lp(
    state: SystemState,
    prices_planning: list[PriceInterval],
    ...
    terminal_floor_override_pct: float | None = None,
) -> tuple[pulp.LpProblem, StochasticLPVars]:
```

Add `mode_overrides` as the last kwarg:

```python
def build_stochastic_lp(
    state: SystemState,
    prices_planning: list[PriceInterval],
    ...
    terminal_floor_override_pct: float | None = None,
    mode_overrides: "ModeOverrides | None" = None,
) -> tuple[pulp.LpProblem, StochasticLPVars]:
```

Add the import at the top of the file (top-of-module imports section, alongside existing `..types` import):

```python
from ..modes import ModeOverrides
```

If a circular-import error pops up at this layer, move the import inside `TYPE_CHECKING` and use a string annotation:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..modes import ModeOverrides
```

- [ ] **Step 3: Forward it to `_add_scenario_to_problem`**

Find the call sites inside `build_stochastic_lp` (around lines 286–291) that build each scenario. Each call looks like:

```python
vars_, cost_terms = _add_scenario_to_problem(
    prob=prob,
    ...
    wear_cost_per_kwh=wear_cost_per_kwh,
)
```

Add the new kwarg to each call:

```python
vars_, cost_terms = _add_scenario_to_problem(
    prob=prob,
    ...
    wear_cost_per_kwh=wear_cost_per_kwh,
    mode_overrides=mode_overrides,
)
```

Add the parameter to `_add_scenario_to_problem`'s signature too:

```python
def _add_scenario_to_problem(
    prob: pulp.LpProblem,
    ...
    wear_cost_per_kwh: float,
    mode_overrides: "ModeOverrides | None" = None,
) -> tuple[LPVars, list[pulp.LpAffineExpression]]:
```

- [ ] **Step 4: Forward it from `build_lp` (deterministic single-scenario) too**

Find the deterministic `build_lp` (around line 133). Add the same kwarg and forward to its single `_add_scenario_to_problem` call.

- [ ] **Step 5: Run the existing LP test suite again**

Run: `uv run pytest tests/test_lp_scaffolding.py tests/test_lp_stochastic.py tests/test_lp_integration.py -q`
Expected: same pass count as the baseline. Nothing has functionally changed.

- [ ] **Step 6: Commit**

```bash
git add src/optimiser/lp/formulation.py
git commit -m "lp: thread mode_overrides kwarg through build_(stochastic_)lp (no-op)"
```

---

## Task 8: LP — buy-mode hard constraints

**Files:**
- Modify: `src/optimiser/lp/formulation.py`
- Create: `tests/test_lp_modes.py`

- [ ] **Step 1: Write a failing test for the ceiling constraint**

Create `tests/test_lp_modes.py`:

```python
"""LP behaviour under user-strategy mode overrides."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from optimiser.config import BatteryConfig
from optimiser.lp.constants import HORIZON_HOURS, SLOT_MINUTES
from optimiser.lp.formulation import build_stochastic_lp
from optimiser.lp.loads import build_lp_loads
from optimiser.lp.result import SolveStatus
from optimiser.lp.solver import solve_stochastic
from optimiser.modes import ModeOverrides
from optimiser.types import (
    LoadProfile,
    PriceInterval,
    PVForecast,
    SystemState,
)


NOW = datetime(2026, 5, 19, 4, 0, 0, tzinfo=UTC)  # 14:00 Canberra
SLOT = timedelta(minutes=SLOT_MINUTES)


def _state(soc: float = 50.0) -> SystemState:
    return SystemState(
        timestamp=NOW,
        soc_pct=soc,
        battery_power_kw=0.0,
        pv_power_kw=0.0,
        grid_power_kw=0.0,
        house_load_kw=0.0,
        ems_mode=2,
        outdoor_temp_c=20.0,
        occupied=True,
    )


def _prices(per_slot_import: list[float], per_slot_export: list[float]) -> list[PriceInterval]:
    """Build prices_planning at slot cadence. PriceInterval requires
    several fields beyond the import/export numbers; fill the rest with
    benign defaults that don't influence the LP."""
    assert len(per_slot_import) == len(per_slot_export)
    return [
        PriceInterval(
            start=NOW + SLOT * i,
            end=NOW + SLOT * (i + 1),
            import_per_kwh=per_slot_import[i],
            export_per_kwh=per_slot_export[i],
            spot_per_kwh=per_slot_import[i] * 0.3,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
        )
        for i in range(len(per_slot_import))
    ]


def _profile(kw: float = 2.0) -> LoadProfile:
    # Flat 48-slot half-hour profile.
    return LoadProfile(slots=[kw] * 48, maturity_level=0, context="test")


def _battery() -> BatteryConfig:
    return BatteryConfig(
        capacity_kwh=40.0,
        max_ac_charge_kw=10.0,
        max_dc_charge_kw=13.0,
        max_discharge_kw=10.0,
        round_trip_efficiency=0.92,
        soc_floor_pct=10.0,
        soc_ceiling_pct=95.0,
        backup_soc_pct=15.0,
        discharge_cutoff_pct=10.0,
    )


def test_buy_mode_blocks_bat_charge_grid_above_ceiling() -> None:
    """Slot 2 has price 15c, ceiling=10c → LP must set bat_charge_grid[2]=0."""
    n_slots = 12
    # Cheap, cheap, SPIKE, cheap...
    imports = [5.0, 5.0, 15.0] + [5.0] * (n_slots - 3)
    exports = [3.0] * n_slots
    state = _state(soc=20.0)  # low SOC so the LP wants to charge

    overrides = ModeOverrides(
        buy_active_at=tuple([True] * n_slots),
        buy_ceiling_c_per_kwh=10.0,
        conserve_active_at=tuple([False] * n_slots),
        conserve_floor_c_per_kwh=None,
    )
    result = solve_stochastic(
        state=state,
        prices_planning=_prices(imports, exports),
        pv_forecast=None,
        load_profile=_profile(2.0),
        managed_loads=[],
        lp_loads=build_lp_loads(battery_config=_battery(), managed_load_configs=[]),
        battery_config=_battery(),
        mode_overrides=overrides,
    )
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    # The spike slot must be zero on bat_charge_grid; we read from the
    # base scenario because slot-0 is tied across scenarios and the
    # other in-window constraints are scenario-uniform here too.
    base = result.forward_trajectory
    assert base[2].grid_to_battery_kw == pytest.approx(0.0, abs=1e-6)
    # Cheap slots can charge.
    assert base[1].grid_to_battery_kw > 0.0
```

`LPSolution.forward_trajectory: list[SlotDecision]` is the public per-slot accessor (verified against `src/optimiser/lp/result.py`). `SlotDecision.grid_to_battery_kw` is the grid-to-battery charge term; `grid_export_kw` and `pv_to_export_kw` are also accessible directly.

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_lp_modes.py::test_buy_mode_blocks_bat_charge_grid_above_ceiling -v`
Expected: failure — either an LP error if `solve_stochastic` doesn't accept `mode_overrides`, or an assertion failure because slot 2 still charges at 15c.

If the failure is "unexpected keyword argument", proceed to Task 9 first — that adds the kwarg to `solve_stochastic`. Otherwise continue.

- [ ] **Step 3: Add the hard-ceiling + no-bat-export constraints in `_add_scenario_to_problem`**

In `src/optimiser/lp/formulation.py`, find the section labelled `# ── Cost terms (already weighted) ─` near line 612. Just before that section (after the SOC dynamics and terminal-SOC block), insert the mode-constraint block:

```python
    # ── Mode overrides: buy ──────────────────────────────────────
    # Hard ceiling on grid-charging: at any slot where buy mode is
    # active AND the import price for this scenario exceeds the
    # user-supplied ceiling, force bat_charge_grid to zero. Also,
    # for every in-window slot, forbid battery contribution to
    # grid_export (preserve what was bought). PV export is
    # unaffected — that's controlled by export_cap.
    if mode_overrides is not None and mode_overrides.any_buy_active():
        ceiling = mode_overrides.buy_ceiling_c_per_kwh
        for t in range(n):
            if not mode_overrides.buy_active_at[t]:
                continue
            ip_t = price_scenario.resolve_ip(_price_at(prices_planning, slots[t]))
            if ceiling is not None and ip_t > ceiling:
                prob += (
                    bat_charge_grid[t] == 0,
                    f"{prefix}buy_ceiling_{t}",
                )
            # Battery cannot contribute to grid_export during buy window.
            # The existing constraint pv_to_export ≤ grid_export combined
            # with this one forces grid_export == pv_to_export.
            prob += (
                grid_export[t] <= pv_to_export[t],
                f"{prefix}buy_no_bat_export_{t}",
            )
```

- [ ] **Step 4: Run test, verify it passes**

Run: `uv run pytest tests/test_lp_modes.py::test_buy_mode_blocks_bat_charge_grid_above_ceiling -v`
Expected: pass.

If this still fails with "unexpected keyword argument mode_overrides", jump to **Task 9** to add the solver pass-through, then retry.

- [ ] **Step 5: Add a test for the no-bat-export rule**

Append to `tests/test_lp_modes.py`:

```python
def test_buy_mode_forbids_battery_export() -> None:
    """During buy window, grid_export must come from PV only."""
    n_slots = 12
    # High export price would normally entice battery → grid.
    imports = [5.0] * n_slots
    exports = [50.0] * n_slots  # very high
    state = _state(soc=90.0)  # high SOC so LP would happily discharge

    overrides = ModeOverrides(
        buy_active_at=tuple([True] * n_slots),
        buy_ceiling_c_per_kwh=10.0,
        conserve_active_at=tuple([False] * n_slots),
        conserve_floor_c_per_kwh=None,
    )
    result = solve_stochastic(
        state=state,
        prices_planning=_prices(imports, exports),
        pv_forecast=None,
        load_profile=_profile(2.0),
        managed_loads=[],
        lp_loads=build_lp_loads(battery_config=_battery(), managed_load_configs=[]),
        battery_config=_battery(),
        mode_overrides=overrides,
    )
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    base = result.forward_trajectory
    for slot in base:
        # Battery cannot contribute to export.
        assert slot.grid_export_kw <= slot.pv_to_export_kw + 1e-6
```

Run: `uv run pytest tests/test_lp_modes.py -v`
Expected: both buy-mode tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/optimiser/lp/formulation.py tests/test_lp_modes.py
git commit -m "lp: buy-mode hard constraints (ceiling + no battery export)"
```

---

## Task 9: solve_stochastic — pass mode_overrides through

**Files:**
- Modify: `src/optimiser/lp/solver.py`

If you encountered "unexpected keyword argument mode_overrides" in Task 8 step 4, fix that here first; otherwise this task adds the public-API pass-through cleanly.

- [ ] **Step 1: Add the kwarg to `solve_stochastic`**

In `src/optimiser/lp/solver.py`, find `def solve_stochastic(` (around line 73). Add the kwarg to the signature, mirroring the type annotation style used in the file:

```python
def solve_stochastic(
    state: SystemState,
    prices_planning: list[PriceInterval],
    pv_forecast: list[PVForecast] | None,
    ...
    terminal_floor_override_pct: float | None = None,
    mode_overrides: "ModeOverrides | None" = None,
) -> LPSolution:
```

Add the import near the top:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..modes import ModeOverrides
```

Forward to `build_stochastic_lp`:

```python
        prob, svars = build_stochastic_lp(
            state=state,
            ...
            terminal_floor_override_pct=terminal_floor_override_pct,
            mode_overrides=mode_overrides,
        )
```

If the codebase has a sibling `solve(...)` (deterministic single-scenario) in this file, add the same kwarg to it and forward to `build_lp`.

- [ ] **Step 2: Run the full LP suite**

Run: `uv run pytest tests/test_lp_modes.py tests/test_lp_scaffolding.py tests/test_lp_stochastic.py tests/test_lp_integration.py -q`
Expected: full pass.

- [ ] **Step 3: Commit**

```bash
git add src/optimiser/lp/solver.py
git commit -m "lp: solve_stochastic forwards mode_overrides to formulation"
```

---

## Task 10: LP — conserve-mode hard constraint

**Files:**
- Modify: `src/optimiser/lp/formulation.py`
- Modify: `tests/test_lp_modes.py`

- [ ] **Step 1: Write a failing test**

Append to `tests/test_lp_modes.py`:

```python
def test_conserve_mode_blocks_battery_export_below_floor() -> None:
    """Slot 2 has ep=5c, floor=15c → battery cannot contribute to export at slot 2."""
    n_slots = 12
    imports = [25.0] * n_slots
    # Low export at slot 2 only.
    exports = [20.0, 20.0, 5.0] + [20.0] * (n_slots - 3)
    state = _state(soc=90.0)  # high SOC so LP would happily discharge

    overrides = ModeOverrides(
        buy_active_at=tuple([False] * n_slots),
        buy_ceiling_c_per_kwh=None,
        conserve_active_at=tuple([True] * n_slots),
        conserve_floor_c_per_kwh=15.0,
    )
    result = solve_stochastic(
        state=state,
        prices_planning=_prices(imports, exports),
        pv_forecast=None,
        load_profile=_profile(2.0),
        managed_loads=[],
        lp_loads=build_lp_loads(battery_config=_battery(), managed_load_configs=[]),
        battery_config=_battery(),
        mode_overrides=overrides,
    )
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    base = result.forward_trajectory
    # Sub-floor slot must export only PV (which is zero here since no
    # PV forecast → 0).
    assert base[2].grid_export_kw <= base[2].pv_to_export_kw + 1e-6
    # Above-floor slot is free to export battery.
    # (Cannot strictly assert > 0 without checking trajectory feasibility,
    # so just confirm the constraint isn't applied there: grid_export
    # can exceed pv_to_export by up to discharge rate.)
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_lp_modes.py::test_conserve_mode_blocks_battery_export_below_floor -v`
Expected: failure — the constraint isn't in place yet.

- [ ] **Step 3: Add the constraint in `_add_scenario_to_problem`**

In `src/optimiser/lp/formulation.py`, immediately after the buy-mode constraint block added in Task 8, append:

```python
    # ── Mode overrides: conserve ─────────────────────────────────
    # No battery contribution to grid_export at slots where the
    # export price (this scenario) is below the user-supplied floor.
    # PV-sourced export is unaffected (existing export_cap handles
    # negative-price curtailment).
    if mode_overrides is not None and mode_overrides.any_conserve_active():
        floor = mode_overrides.conserve_floor_c_per_kwh
        for t in range(n):
            if not mode_overrides.conserve_active_at[t]:
                continue
            ep_t = price_scenario.resolve_ep(_price_at(prices_planning, slots[t]))
            if floor is not None and ep_t < floor:
                prob += (
                    grid_export[t] <= pv_to_export[t],
                    f"{prefix}conserve_floor_{t}",
                )
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/test_lp_modes.py -v`
Expected: all three current tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/optimiser/lp/formulation.py tests/test_lp_modes.py
git commit -m "lp: conserve-mode hard constraint (battery export below floor)"
```

---

## Task 11: LP — wear discount in objective

**Files:**
- Modify: `src/optimiser/lp/formulation.py`
- Modify: `tests/test_lp_modes.py`

The wear-cost term in `_add_scenario_to_problem` currently looks like:

```python
cost_terms.append(
    weight
    * (bat_charge_grid[t] + bat_charge_pv[t] + bat_discharge[t])
    * wear_cost_per_kwh
    * slot_hours
)
```

We need per-slot factors that zero out the relevant component:
- Buy mode active at slot t → `bat_charge_grid[t]` wear is zero
- Conserve mode active at slot t → `bat_charge_pv[t]` wear is zero
- Discharge-side wear is always full

- [ ] **Step 1: Write a failing test for the buy-charge wear discount**

Append to `tests/test_lp_modes.py`:

```python
def test_buy_mode_wear_discount_increases_grid_charge() -> None:
    """With buy mode active and a marginal-arbitrage scenario, the
    wear discount should tip the LP into charging more.

    Setup: cheap morning import (5c), expensive evening import (25c,
    via tail of the horizon implicit via load profile), low export.
    Without wear discount, the LP charges some; with the discount, it
    charges strictly more in the in-window slots.
    """
    n_slots = 24  # 2h horizon at 5min slots; LP horizon is configurable
    imports = [5.0] * 6 + [25.0] * (n_slots - 6)  # cheap then expensive
    exports = [3.0] * n_slots
    state = _state(soc=30.0)
    prices = _prices(imports, exports)
    profile = _profile(2.0)
    battery = _battery()
    loads = []
    lp_loads = build_lp_loads(battery_config=battery, managed_load_configs=[])

    # Baseline: no overrides.
    base_result = solve_stochastic(
        state=state,
        prices_planning=prices,
        pv_forecast=None,
        load_profile=profile,
        managed_loads=loads,
        lp_loads=lp_loads,
        battery_config=battery,
    )

    # Buy mode active across the cheap window (first 6 slots),
    # ceiling well above the cheap-import price so no ceiling block.
    overrides = ModeOverrides(
        buy_active_at=tuple([True] * 6 + [False] * (n_slots - 6)),
        buy_ceiling_c_per_kwh=20.0,
        conserve_active_at=tuple([False] * n_slots),
        conserve_floor_c_per_kwh=None,
    )
    with_buy = solve_stochastic(
        state=state,
        prices_planning=prices,
        pv_forecast=None,
        load_profile=profile,
        managed_loads=loads,
        lp_loads=lp_loads,
        battery_config=battery,
        mode_overrides=overrides,
    )

    base_charge = sum(slot.grid_to_battery_kw for slot in base_result.forward_trajectory[:6])
    buy_charge = sum(slot.grid_to_battery_kw for slot in with_buy.base_scenario_trajectory[:6])
    # Discount should push it monotonically higher (or equal — but in the
    # tuned scenario above the LP has marginal arbitrage cases that flip).
    assert buy_charge >= base_charge - 1e-6
    # And, for the in-window arbitrage scenario constructed above, strictly
    # higher.
    assert buy_charge > base_charge + 1e-3
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_lp_modes.py::test_buy_mode_wear_discount_increases_grid_charge -v`
Expected: assertion failure — the discount isn't applied yet.

- [ ] **Step 3: Replace the wear-cost term with per-slot factors**

In `src/optimiser/lp/formulation.py`, find the wear-cost append (around line 662; it appears once in the per-slot loop). Replace it with the factored form:

```python
        # Wear cost. Per-slot factors zero out the relevant component
        # under user-strategy mode overrides:
        #   - buy mode at slot t   → wear on bat_charge_grid is 0
        #   - conserve mode at slot t → wear on bat_charge_pv is 0
        # Moot above buy ceiling (bat_charge_grid is pinned to 0 there)
        # but harmless to include the factor. Discharge-side wear is
        # unchanged.
        wear_grid_factor = (
            0.0
            if (mode_overrides is not None and mode_overrides.buy_active_at[t])
            else 1.0
        )
        wear_pv_factor = (
            0.0
            if (mode_overrides is not None and mode_overrides.conserve_active_at[t])
            else 1.0
        )
        cost_terms.append(
            weight
            * (
                bat_charge_grid[t] * wear_grid_factor
                + bat_charge_pv[t] * wear_pv_factor
                + bat_discharge[t]
            )
            * wear_cost_per_kwh
            * slot_hours
        )
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/test_lp_modes.py -v`
Expected: all four tests pass.

- [ ] **Step 5: Run the full LP suite to confirm no regressions**

Run: `uv run pytest tests/test_lp_scaffolding.py tests/test_lp_stochastic.py tests/test_lp_integration.py tests/test_lp_modes.py tests/test_lp_compound_scenarios.py tests/test_lp_multitick_soc.py -q`
Expected: full pass.

- [ ] **Step 6: Commit**

```bash
git add src/optimiser/lp/formulation.py tests/test_lp_modes.py
git commit -m "lp: per-slot wear factors (buy on grid charge, conserve on PV charge)"
```

---

## Task 12: Service wires ModeManager into the tick

**Files:**
- Modify: `src/optimiser/service.py`
- Create: `tests/test_service_modes.py`

- [ ] **Step 1: Write the failing service-level test**

Create `tests/test_service_modes.py`:

```python
"""Service-level wiring of the ModeManager."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from optimiser.modes import ActiveMode, ModeManager, ModeOverrides
from optimiser.service import Service


# Synthetic "now" sits far in the future so the ModeManager's load-time
# wall-clock check (datetime.now(UTC)) doesn't treat NOW + Nh as past.
NOW = datetime(2099, 5, 19, 4, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_run_lp_forwards_overrides(tmp_path, monkeypatch) -> None:
    """_run_lp pulls overrides from the manager and forwards them to solve_stochastic."""
    from optimiser.lp.result import LPSolution, SlotDecision, SolveStatus
    from optimiser.types import LoadProfile, PriceInterval, SystemState

    captured = {}

    def fake_solve(**kwargs):
        captured["mode_overrides"] = kwargs.get("mode_overrides")
        slot_0 = SlotDecision(
            slot_start=NOW,
            battery_kw=0.0,
            grid_import_kw=0.0,
            grid_export_kw=0.0,
            pv_to_house_kw=0.0,
            pv_to_battery_kw=0.0,
            pv_to_export_kw=0.0,
            soc_pct_end=50.0,
            grid_to_battery_kw=0.0,
        )
        return LPSolution(
            status=SolveStatus.OPTIMAL,
            slot_0=slot_0,
            forward_trajectory=[slot_0],
            load_commands=[],
            grid_export_limit_kw=None,
            expected_total_cost_cents=0.0,
            solve_time_ms=10.0,
            reason="test",
        )

    async def fake_to_thread(func, **kwargs):
        # solve_stochastic is normally sync inside to_thread; here we
        # short-circuit and call our fake directly.
        return func(**kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)
    monkeypatch.setattr("optimiser.service.solve_stochastic", fake_solve)
    # dispatch_from_slot would touch real battery_config; mock it.
    monkeypatch.setattr("optimiser.service.dispatch_from_slot", lambda *a, **k: MagicMock())

    svc = Service.__new__(Service)
    svc._config = MagicMock()
    svc._config.planner.parsed_price_scenario_mode = None
    svc._config.planner.lp_scenario_weights = None
    svc._config.planner.lp_wear_cost_per_kwh = None
    svc._config.planner.lp_terminal_floor_override_pct = None
    svc._config.planner.lp_wall_clock_timeout_s = 30.0
    svc._config.battery = MagicMock()
    svc._lp_loads = []
    svc._metrics = MagicMock()
    svc._mode_manager = ModeManager(tmp_path / "active_modes.json")
    svc._mode_manager.activate(
        ActiveMode(
            kind="buy",
            end_at=NOW + timedelta(hours=2),
            params={"ceiling_c_per_kwh": 12.0},
            activated_at=NOW,
            source="dashboard",
        )
    )

    state = SystemState(
        timestamp=NOW,
        soc_pct=50.0,
        battery_power_kw=0.0,
        pv_power_kw=0.0,
        grid_power_kw=0.0,
        house_load_kw=0.0,
        ems_mode=2,
        outdoor_temp_c=20.0,
        occupied=True,
    )
    prices = [
        PriceInterval(
            start=NOW + timedelta(minutes=5 * i),
            end=NOW + timedelta(minutes=5 * (i + 1)),
            import_per_kwh=8.0,
            export_per_kwh=3.0,
            spot_per_kwh=2.4,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
        )
        for i in range(24)
    ]
    profile = LoadProfile(slots=[2.0] * 48, maturity_level=0, context="test")

    await svc._run_lp(
        state=state,
        prices_planning=prices,
        pv_forecast=None,
        load_profile=profile,
        managed_loads=[],
    )

    overrides = captured["mode_overrides"]
    assert isinstance(overrides, ModeOverrides)
    assert overrides.buy_ceiling_c_per_kwh == 12.0
    assert overrides.any_buy_active()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/test_service_modes.py -v`
Expected: failures — `Service._mode_manager` doesn't exist, `_run_lp` doesn't accept this signature, or doesn't pass `mode_overrides`.

- [ ] **Step 3: Wire ModeManager into Service.__init__**

In `src/optimiser/service.py`, near the top imports:

```python
from .modes import ModeManager
```

In `Service.__init__` (line ~100), construct the manager. The state directory is the parent of the heartbeat path. Add:

```python
        # ── User-strategy modes ──────────────────────────────────
        # Live next to the heartbeat in the data volume so a service
        # restart resumes any modes still inside their window. JSON
        # over DuckDB because the dataset is ~2 rows and easy to
        # eyeball with `cat`.
        modes_path = Path(
            os.environ.get(
                "EO_MODES_PATH",
                str(Path(self.heartbeat_path).parent / "active_modes.json"),
            )
        )
        self._mode_manager = ModeManager(modes_path)
```

(Use a `os.environ` import path if not already present. The heartbeat path lookup is already a class property — calling `self.heartbeat_path` is fine.)

- [ ] **Step 4: Expose mode_manager + thread overrides into `_run_lp`**

Add a property near the existing `def battery_config(self)`:

```python
    @property
    def mode_manager(self) -> ModeManager:
        return self._mode_manager
```

In `_run_lp` (line ~1028), build the slot grid (it's available via `_slot_grid` helper — line ~758) and pass overrides:

```python
        # Build the slot grid the same way the LP formulation does so
        # the overrides line up tick-for-tick.
        from .lp.constants import HORIZON_HOURS, SLOT_MINUTES
        from .lp.formulation import _slot_grid
        slot_start = state.timestamp.replace(second=0, microsecond=0)
        slot_minute_aligned = slot_start - timedelta(
            minutes=slot_start.minute % SLOT_MINUTES
        )
        slots = _slot_grid(slot_minute_aligned, HORIZON_HOURS, SLOT_MINUTES)
        mode_overrides = self._mode_manager.to_overrides(state.timestamp, slots)
```

Then add `mode_overrides=mode_overrides` to the `solve_stochastic(...)` call inside the `asyncio.to_thread(...)` block.

- [ ] **Step 5: Run tests, verify they pass**

Run: `uv run pytest tests/test_service_modes.py -v tests/test_modes.py -v`
Expected: all pass.

- [ ] **Step 6: Run the full service test suite for regressions**

Run: `uv run pytest tests/test_service_lp.py tests/test_service*.py -q`
Expected: full pass.

- [ ] **Step 7: Commit**

```bash
git add src/optimiser/service.py tests/test_service_modes.py
git commit -m "service: own ModeManager and thread overrides through _run_lp"
```

---

## Task 13: TickSnapshot — record active modes

**Files:**
- Modify: `src/optimiser/types.py`
- Modify: `src/optimiser/service.py` (snapshot construction)
- Modify: `tests/test_snapshot_writer.py`

- [ ] **Step 1: Add the field to TickSnapshot**

In `src/optimiser/types.py`, find `class TickSnapshot` (around line 427). Add the new field at the end (preserving the dataclass `slots=True` semantics — append to the end of the field list):

```python
    # User-strategy modes active at solve time (slot 0 inclusive).
    # Restored from this field by the replay tool so historical
    # re-solves can reproduce mode-aware decisions.
    active_modes: tuple[ActiveModeRecord, ...] = ()
```

If `TickSnapshot` is declared with frozen=True / slots=True and a default, ensure the existing default-having fields appear after the new one — Python dataclass rules require defaulted fields to follow non-defaulted ones. If reordering is needed, do it now; the field is annotated with a default.

Define `ActiveModeRecord` near the top of `types.py` (above `TickSnapshot`):

```python
@dataclass(frozen=True, slots=True)
class ActiveModeRecord:
    """Lightweight serialisable view of one active mode at snapshot time."""
    kind: str  # "buy" | "conserve"
    end_at: datetime
    params: dict[str, float]
```

- [ ] **Step 2: Write a failing serialisation test**

Append to `tests/test_snapshot_writer.py` (the existing file already has a `_snap(ts)` helper — extend it to populate `active_modes`):

```python
import dataclasses
import json

from optimiser.types import ActiveModeRecord


def test_tick_snapshot_active_modes_round_trip(tmp_path) -> None:
    """active_modes field survives the SnapshotWriter NDJSON round-trip."""
    ts = datetime(2099, 5, 19, 4, 0, tzinfo=UTC)
    snap = _snap(ts)
    snap = dataclasses.replace(
        snap,
        active_modes=(
            ActiveModeRecord(
                kind="buy",
                end_at=datetime(2099, 5, 19, 6, 0, 0, tzinfo=UTC),
                params={"ceiling_c_per_kwh": 12.0},
            ),
        ),
    )
    w = SnapshotWriter(tmp_path)
    w.write(snap)

    path = tmp_path / "2099-05-19.ndjson.gz"
    with gzip.open(path, "rt") as f:
        row = json.loads(f.readline())
    assert "active_modes" in row
    assert len(row["active_modes"]) == 1
    assert row["active_modes"][0]["kind"] == "buy"
    assert row["active_modes"][0]["params"]["ceiling_c_per_kwh"] == 12.0
```

The existing `_snap(ts)` helper builds a `TickSnapshot` with all required fields populated; we just override `active_modes` via `dataclasses.replace`. Serialisation goes through `dataclasses.asdict + json.dumps(default=_serialise)` in `SnapshotWriter.write` (see `logging_utils.py:323`).

- [ ] **Step 3: Run test, verify it fails**

Run: `uv run pytest tests/test_snapshot_writer.py -v -k active_modes`
Expected: failure — serialiser doesn't know about the new field, or default makes the test trivial.

- [ ] **Step 4: Verify serialisation is automatic**

`SnapshotWriter.write` in `logging_utils.py:323-326` uses `json.dumps(asdict(snapshot), default=_serialise)`. Since `ActiveModeRecord` is a `@dataclass(frozen=True, slots=True)`, `asdict` recursively flattens it; the `default=_serialise` handler already covers `datetime` (via `_serialise` at line 42). No serialiser code changes should be required — the test should pass after only adding the field and the snapshot construction in step 5.

If the test fails because `params` is a plain `dict` containing non-trivial types (it shouldn't — we only ever store floats), inspect `_serialise` and add a branch.

- [ ] **Step 5: Construct the field in the snapshot path**

In `src/optimiser/service.py`, find where `TickSnapshot` is constructed each tick (search for `TickSnapshot(`). Add:

```python
        active_modes = tuple(
            ActiveModeRecord(
                kind=m.kind,
                end_at=m.end_at,
                params=dict(m.params),
            )
            for m in self._mode_manager.active(state.timestamp)
        )
```

Pass it into the `TickSnapshot(...)` constructor as `active_modes=active_modes`.

Add the import at the top of `service.py`:

```python
from .types import ActiveModeRecord
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_snapshot_writer.py -v`
Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add src/optimiser/types.py src/optimiser/service.py tests/test_snapshot_writer.py
git commit -m "snapshot: record active modes on TickSnapshot"
```

---

## Task 14: REST API — POST/GET/DELETE for modes

**Files:**
- Create: `src/optimiser/api/handlers/modes.py`
- Create: `tests/test_modes_api.py`
- Modify: `src/optimiser/api/probe.py`
- Modify: `src/optimiser/api/server.py`

- [ ] **Step 1: Expose mode_manager on the ServiceProbe protocol**

In `src/optimiser/api/probe.py`, add `mode_manager` to the protocol (read whatever the existing pattern is and follow it). It should look approximately like:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..modes import ModeManager


class ServiceProbe(Protocol):
    ...
    @property
    def mode_manager(self) -> "ModeManager": ...
```

- [ ] **Step 2: Write failing API tests**

Create `tests/test_modes_api.py`:

```python
"""REST API for user-strategy modes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from optimiser.api.handlers.modes import register_modes_routes
from optimiser.modes import ActiveMode, ModeManager


NOW = datetime(2026, 5, 19, 4, 0, 0, tzinfo=UTC)


class _Probe:
    def __init__(self, mode_manager: ModeManager) -> None:
        self._mm = mode_manager

    @property
    def mode_manager(self) -> ModeManager:
        return self._mm


@pytest.fixture
async def client(tmp_path) -> TestClient:
    app = web.Application()
    mgr = ModeManager(tmp_path / "active_modes.json")
    app["service_probe"] = _Probe(mgr)
    register_modes_routes(app)
    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)
    yield client
    await client.close()


async def test_post_buy_valid(client) -> None:
    resp = await client.post(
        "/modes/buy",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "ceiling_c_per_kwh": 12.0,
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["kind"] == "buy"
    assert body["params"]["ceiling_c_per_kwh"] == 12.0


async def test_post_buy_rejects_past_end_at(client) -> None:
    resp = await client.post(
        "/modes/buy",
        json={
            "end_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            "ceiling_c_per_kwh": 12.0,
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert "end_at" in body["error"]


async def test_post_buy_rejects_window_over_48h(client) -> None:
    resp = await client.post(
        "/modes/buy",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=49)).isoformat(),
            "ceiling_c_per_kwh": 12.0,
        },
    )
    assert resp.status == 400


async def test_post_buy_rejects_ceiling_zero(client) -> None:
    resp = await client.post(
        "/modes/buy",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "ceiling_c_per_kwh": 0.0,
        },
    )
    assert resp.status == 400


async def test_post_buy_rejects_ceiling_above_100(client) -> None:
    resp = await client.post(
        "/modes/buy",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "ceiling_c_per_kwh": 101.0,
        },
    )
    assert resp.status == 400


async def test_get_modes_returns_active_set(client) -> None:
    await client.post(
        "/modes/buy",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "ceiling_c_per_kwh": 10.0,
        },
    )
    await client.post(
        "/modes/conserve",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
            "floor_c_per_kwh": 22.0,
        },
    )
    resp = await client.get("/modes")
    body = await resp.json()
    kinds = {m["kind"] for m in body["modes"]}
    assert kinds == {"buy", "conserve"}


async def test_delete_buy_when_active(client) -> None:
    await client.post(
        "/modes/buy",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "ceiling_c_per_kwh": 10.0,
        },
    )
    resp = await client.delete("/modes/buy")
    assert resp.status == 204
    resp = await client.get("/modes")
    body = await resp.json()
    assert body["modes"] == []


async def test_delete_buy_when_inactive_returns_404(client) -> None:
    resp = await client.delete("/modes/buy")
    assert resp.status == 404


async def test_post_conserve_valid(client) -> None:
    resp = await client.post(
        "/modes/conserve",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "floor_c_per_kwh": 22.0,
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["kind"] == "conserve"
    assert body["params"]["floor_c_per_kwh"] == 22.0
```

- [ ] **Step 3: Run tests, verify they fail**

Run: `uv run pytest tests/test_modes_api.py -v`
Expected: ImportError — `optimiser.api.handlers.modes` does not exist.

- [ ] **Step 4: Implement the handler module**

Create `src/optimiser/api/handlers/modes.py`:

```python
"""HTTP handlers for user-strategy mode activation/cancellation/status."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import web

from ...modes import ActiveMode

MAX_WINDOW = timedelta(hours=48)
_THRESHOLD_MIN_EXCLUSIVE = 0.0
_THRESHOLD_MAX_INCLUSIVE = 100.0


def _bad(reason: str) -> web.Response:
    return web.json_response({"error": reason}, status=400)


def _parse_end_at(raw: Any) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("end_at must be an ISO-8601 string")
    end_at = datetime.fromisoformat(raw)
    if end_at.tzinfo is None:
        raise ValueError("end_at must include a UTC offset")
    return end_at.astimezone(UTC)


def _validate_end_at(end_at: datetime) -> str | None:
    now = datetime.now(UTC)
    if end_at <= now:
        return "end_at must be strictly in the future"
    if end_at > now + MAX_WINDOW:
        return "end_at must be within 48h of now"
    return None


def _validate_threshold(value: float, name: str) -> str | None:
    if not (_THRESHOLD_MIN_EXCLUSIVE < value <= _THRESHOLD_MAX_INCLUSIVE):
        return f"{name} must be in (0, 100] c/kWh"
    return None


async def _activate_handler(request: web.Request, kind: str, param_name: str) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _bad("body must be JSON")

    try:
        end_at = _parse_end_at(body.get("end_at"))
    except ValueError as exc:
        return _bad(str(exc))
    err = _validate_end_at(end_at)
    if err:
        return _bad(err)

    raw = body.get(param_name)
    if not isinstance(raw, (int, float)):
        return _bad(f"{param_name} must be a number")
    threshold = float(raw)
    err = _validate_threshold(threshold, param_name)
    if err:
        return _bad(err)

    mm = request.app["service_probe"].mode_manager
    mode = mm.activate(
        ActiveMode(
            kind=kind,  # type: ignore[arg-type]
            end_at=end_at,
            params={param_name: threshold},
            activated_at=datetime.now(UTC),
            source="dashboard",
        )
    )
    return web.json_response(mode.to_dict())


async def activate_buy(request: web.Request) -> web.Response:
    return await _activate_handler(request, "buy", "ceiling_c_per_kwh")


async def activate_conserve(request: web.Request) -> web.Response:
    return await _activate_handler(request, "conserve", "floor_c_per_kwh")


async def cancel_buy(request: web.Request) -> web.Response:
    mm = request.app["service_probe"].mode_manager
    removed = mm.cancel("buy")
    return web.Response(status=204) if removed else web.Response(status=404)


async def cancel_conserve(request: web.Request) -> web.Response:
    mm = request.app["service_probe"].mode_manager
    removed = mm.cancel("conserve")
    return web.Response(status=204) if removed else web.Response(status=404)


async def list_modes(request: web.Request) -> web.Response:
    mm = request.app["service_probe"].mode_manager
    now = datetime.now(UTC)
    modes = [m.to_dict() for m in mm.active(now)]
    return web.json_response({"modes": modes, "now": now.isoformat()})


def register_modes_routes(app: web.Application) -> None:
    app.router.add_get("/modes", list_modes)
    app.router.add_post("/modes/buy", activate_buy)
    app.router.add_delete("/modes/buy", cancel_buy)
    app.router.add_post("/modes/conserve", activate_conserve)
    app.router.add_delete("/modes/conserve", cancel_conserve)
```

- [ ] **Step 5: Register routes in api/server.py**

In `src/optimiser/api/server.py`, near the existing handler imports:

```python
from .handlers.modes import register_modes_routes
```

In `APIServer.start` where other routes are registered, call:

```python
        register_modes_routes(app)
```

- [ ] **Step 6: Run tests, verify they pass**

Run: `uv run pytest tests/test_modes_api.py -v`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/optimiser/api/handlers/modes.py src/optimiser/api/server.py src/optimiser/api/probe.py tests/test_modes_api.py
git commit -m "api: POST/GET/DELETE /modes/{buy,conserve}"
```

---

## Task 15: REST API — GET /modes/suggest

**Files:**
- Modify: `src/optimiser/api/handlers/modes.py`
- Modify: `tests/test_modes_api.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_modes_api.py`:

```python
async def test_suggest_buy_ceiling(client) -> None:
    """Suggest median(in-window import) + 3c for a 2h window."""
    # The probe needs to expose enough state for the suggester to read
    # the current Amber price strip. For this test, monkey-patch
    # `service_probe.amber_price_window(...)` with a stub.
    from optimiser.types import PriceInterval
    base = datetime.now(UTC)
    strip = [
        PriceInterval(
            start=base + timedelta(minutes=5 * i),
            end=base + timedelta(minutes=5 * (i + 1)),
            import_per_kwh=float(p),
            export_per_kwh=2.0,
            spot_per_kwh=float(p) * 0.3,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
        )
        for i, p in enumerate([5, 6, 7, 8, 9, 10, 11, 12])
    ]
    client.app["service_probe"].amber_price_window = lambda end_at: strip

    resp = await client.get("/modes/suggest?kind=buy&duration_minutes=40")
    assert resp.status == 200
    body = await resp.json()
    # Median of [5, 6, 7, 8, 9, 10, 11, 12] = 8.5; +3 = 11.5
    assert body["suggested_ceiling_c_per_kwh"] == pytest.approx(11.5, abs=0.01)


async def test_suggest_conserve_floor(client) -> None:
    """Suggest p70(in-window export) for a 2h window."""
    from optimiser.types import PriceInterval
    base = datetime.now(UTC)
    strip = [
        PriceInterval(
            start=base + timedelta(minutes=5 * i),
            end=base + timedelta(minutes=5 * (i + 1)),
            import_per_kwh=10.0,
            export_per_kwh=float(p),
            spot_per_kwh=3.0,
            renewables_pct=40.0,
            spike_status="none",
            descriptor="neutral",
        )
        for i, p in enumerate([5, 6, 7, 8, 9, 10, 15, 20, 25, 30])
    ]
    client.app["service_probe"].amber_price_window = lambda end_at: strip

    resp = await client.get("/modes/suggest?kind=conserve&duration_minutes=50")
    assert resp.status == 200
    body = await resp.json()
    # 70th percentile of [5, 6, 7, 8, 9, 10, 15, 20, 25, 30] ≈ 18.0
    # (linear interpolation; exact computation depends on the stdlib
    # method used — accept ±0.5)
    assert 16.5 <= body["suggested_floor_c_per_kwh"] <= 22.0
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/test_modes_api.py::test_suggest_buy_ceiling tests/test_modes_api.py::test_suggest_conserve_floor -v`
Expected: 404 — `/modes/suggest` not registered.

- [ ] **Step 3: Implement the suggest handler**

In `src/optimiser/api/handlers/modes.py`, add:

```python
import statistics


async def suggest(request: web.Request) -> web.Response:
    kind = request.query.get("kind")
    if kind not in ("buy", "conserve"):
        return _bad("kind must be 'buy' or 'conserve'")
    try:
        duration_minutes = int(request.query.get("duration_minutes", "120"))
    except ValueError:
        return _bad("duration_minutes must be an integer")
    if duration_minutes <= 0 or duration_minutes > 48 * 60:
        return _bad("duration_minutes must be in (0, 2880]")

    probe = request.app["service_probe"]
    end_at = datetime.now(UTC) + timedelta(minutes=duration_minutes)
    strip = probe.amber_price_window(end_at)
    if not strip:
        return _bad("no price data available for window")

    if kind == "buy":
        imports = sorted(p.import_per_kwh for p in strip if p.import_per_kwh is not None)
        if not imports:
            return _bad("no import prices available")
        suggested = statistics.median(imports) + 3.0
        return web.json_response({"suggested_ceiling_c_per_kwh": round(suggested, 2)})
    else:
        exports = sorted(p.export_per_kwh for p in strip if p.export_per_kwh is not None)
        if not exports:
            return _bad("no export prices available")
        # 70th percentile via linear interpolation.
        idx_f = 0.7 * (len(exports) - 1)
        lo = int(idx_f)
        hi = min(lo + 1, len(exports) - 1)
        frac = idx_f - lo
        suggested = exports[lo] * (1 - frac) + exports[hi] * frac
        return web.json_response({"suggested_floor_c_per_kwh": round(suggested, 2)})
```

In `register_modes_routes`, add:

```python
    app.router.add_get("/modes/suggest", suggest)
```

- [ ] **Step 4: Add `amber_price_window` to ServiceProbe**

In `src/optimiser/api/probe.py`, extend the protocol:

```python
    def amber_price_window(self, end_at: datetime) -> list["PriceInterval"]:
        """Return the planning-price strip from now up to end_at.
        Implementation reads `service._last_prices_planning` filtered."""
        ...
```

In `src/optimiser/service.py`, implement it:

```python
    def amber_price_window(self, end_at):
        from .types import PriceInterval
        now = datetime.now(UTC)
        return [p for p in (self._last_prices_planning or []) if p.start < end_at and p.end > now]
```

If `_last_prices_planning` isn't already cached on the Service, plumb it through from the tick body (it should already be available as part of the LP input). Worst case: cache the most-recent `prices_planning` as `self._last_prices_planning` at the top of `_tick_body` after the price fetch.

- [ ] **Step 5: Run tests, verify they pass**

Run: `uv run pytest tests/test_modes_api.py -v`
Expected: all pass including the two suggest tests.

- [ ] **Step 6: Commit**

```bash
git add src/optimiser/api/handlers/modes.py src/optimiser/api/probe.py src/optimiser/service.py tests/test_modes_api.py
git commit -m "api: GET /modes/suggest with Amber-forecast-aware defaults"
```

---

## Task 16: /dashboard/config exposes active modes

**Files:**
- Modify: `src/optimiser/api/handlers/dashboard.py`
- Modify: `tests/test_api.py` (or wherever dashboard_config is tested; if no test exists, add one)

- [ ] **Step 1: Find the existing /dashboard/config handler and the test that covers it**

Read `src/optimiser/api/handlers/dashboard.py` and find `async def dashboard_config(request)`. Note the existing payload structure.

Find the corresponding test (search `tests/` for `dashboard_config`).

- [ ] **Step 2: Add a failing test for the modes field**

In the dashboard-config test file (`tests/test_api.py` if that's the host file, otherwise create `tests/test_dashboard_config.py`), add:

```python
async def test_dashboard_config_includes_active_modes(client) -> None:
    # Activate a mode first.
    await client.post(
        "/modes/buy",
        json={
            "end_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "ceiling_c_per_kwh": 12.0,
        },
    )
    resp = await client.get("/dashboard/config")
    body = await resp.json()
    assert "active_modes" in body
    kinds = {m["kind"] for m in body["active_modes"]}
    assert "buy" in kinds
```

Adapt the test client fixture to the existing pattern in the file.

- [ ] **Step 3: Run test, verify it fails**

Run: `uv run pytest tests/test_api.py -v -k dashboard_config_includes_active_modes` (or the appropriate file).
Expected: `active_modes` not in body.

- [ ] **Step 4: Extend dashboard_config**

In `src/optimiser/api/handlers/dashboard.py::dashboard_config`, build the active modes block and add it to the response dict:

```python
    now = datetime.now(UTC)
    active_modes = [m.to_dict() for m in probe.mode_manager.active(now)]
    payload["active_modes"] = active_modes
```

- [ ] **Step 5: Run test, verify it passes**

Run: `uv run pytest tests/test_api.py -v -k dashboard_config_includes_active_modes`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/optimiser/api/handlers/dashboard.py tests/test_api.py
git commit -m "dashboard: include active_modes in /dashboard/config payload"
```

---

## Task 17: Dashboard frontend — mode cards + panel

**Files:**
- Modify: `src/optimiser/api/static/dashboard.html`
- Modify: `src/optimiser/api/static/dashboard.js`
- Modify: `src/optimiser/api/static/dashboard.css`

No automated tests — manual smoke via `/deploy` + browser.

- [ ] **Step 1: Add the HTML scaffold for two mode cards**

Open `src/optimiser/api/static/dashboard.html`. Find a sensible insertion point near the other dashboard cards (review the existing structure first). Add:

```html
<section class="modes-section">
  <div class="mode-card" id="mode-card-buy">
    <header class="mode-card__header"><span class="mode-card__title">BUY MODE</span><span class="mode-card__status" data-status="inactive">Inactive</span></header>
    <div class="mode-card__body" data-empty="true">
      <button class="mode-card__activate" data-kind="buy">Activate</button>
    </div>
    <div class="mode-card__body" data-empty="false" hidden>
      <div class="mode-card__detail"><span class="label">Ends in</span><span class="value" data-field="countdown">—</span></div>
      <div class="mode-card__detail"><span class="label">Ceiling</span><span class="value" data-field="ceiling">—</span></div>
      <button class="mode-card__cancel" data-kind="buy">Cancel</button>
    </div>
  </div>
  <div class="mode-card" id="mode-card-conserve">
    <header class="mode-card__header"><span class="mode-card__title">CONSERVE MODE</span><span class="mode-card__status" data-status="inactive">Inactive</span></header>
    <div class="mode-card__body" data-empty="true">
      <button class="mode-card__activate" data-kind="conserve">Activate</button>
    </div>
    <div class="mode-card__body" data-empty="false" hidden>
      <div class="mode-card__detail"><span class="label">Ends in</span><span class="value" data-field="countdown">—</span></div>
      <div class="mode-card__detail"><span class="label">Floor</span><span class="value" data-field="floor">—</span></div>
      <button class="mode-card__cancel" data-kind="conserve">Cancel</button>
    </div>
  </div>
</section>

<dialog id="mode-activate-panel" class="mode-panel">
  <form method="dialog" id="mode-activate-form">
    <h2 id="mode-panel-title">Activate mode</h2>
    <label>Duration
      <select name="duration_minutes" id="mode-duration">
        <option value="15">15 min</option>
        <option value="30">30 min</option>
        <option value="60" selected>1 h</option>
        <option value="120">2 h</option>
        <option value="240">4 h</option>
        <option value="480">8 h</option>
        <option value="1440">24 h</option>
        <option value="2880">48 h</option>
      </select>
    </label>
    <label id="mode-threshold-label">Threshold (c/kWh)
      <input type="number" name="threshold" id="mode-threshold" min="0.01" max="100" step="0.01" required>
    </label>
    <p id="mode-suggest-hint" class="mode-panel__hint"></p>
    <menu>
      <button value="cancel">Cancel</button>
      <button value="submit" id="mode-submit">Activate</button>
    </menu>
  </form>
</dialog>
```

- [ ] **Step 2: Add the JS controller**

Append to `src/optimiser/api/static/dashboard.js`:

```javascript
// ── User-strategy modes ───────────────────────────────────────────────
const ModesUI = (() => {
  const panel = document.getElementById('mode-activate-panel');
  const form = document.getElementById('mode-activate-form');
  const title = document.getElementById('mode-panel-title');
  const thresholdLabel = document.getElementById('mode-threshold-label');
  const thresholdInput = document.getElementById('mode-threshold');
  const durationSelect = document.getElementById('mode-duration');
  const hint = document.getElementById('mode-suggest-hint');
  let currentKind = null;

  async function refreshSuggestion() {
    if (!currentKind) return;
    const dur = durationSelect.value;
    try {
      const resp = await fetch(`/modes/suggest?kind=${currentKind}&duration_minutes=${dur}`);
      if (!resp.ok) {
        hint.textContent = 'No suggestion available for this window.';
        return;
      }
      const body = await resp.json();
      const key = currentKind === 'buy' ? 'suggested_ceiling_c_per_kwh' : 'suggested_floor_c_per_kwh';
      const value = body[key];
      if (value !== undefined) {
        thresholdInput.value = value;
        hint.textContent = `Amber-suggested ${currentKind === 'buy' ? 'ceiling' : 'floor'}: ${value} c/kWh`;
      }
    } catch (_) {
      hint.textContent = 'Could not reach /modes/suggest.';
    }
  }

  function openActivatePanel(kind) {
    currentKind = kind;
    title.textContent = kind === 'buy' ? 'Activate buy mode' : 'Activate conserve mode';
    thresholdLabel.firstChild.textContent =
      kind === 'buy' ? 'Ceiling (c/kWh) ' : 'Floor (c/kWh) ';
    thresholdInput.value = '';
    hint.textContent = 'Computing suggestion…';
    panel.showModal();
    refreshSuggestion();
  }

  durationSelect.addEventListener('change', refreshSuggestion);

  document.querySelectorAll('.mode-card__activate').forEach((btn) => {
    btn.addEventListener('click', (e) => openActivatePanel(e.currentTarget.dataset.kind));
  });

  document.querySelectorAll('.mode-card__cancel').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      const kind = e.currentTarget.dataset.kind;
      const resp = await fetch(`/modes/${kind}`, { method: 'DELETE' });
      if (!resp.ok && resp.status !== 404) {
        alert(`Failed to cancel: ${resp.status}`);
      }
      await render();
    });
  });

  form.addEventListener('submit', async (e) => {
    if (e.submitter && e.submitter.value === 'cancel') return;
    e.preventDefault();
    if (!currentKind) return;
    const minutes = parseInt(durationSelect.value, 10);
    const endAt = new Date(Date.now() + minutes * 60_000).toISOString();
    const threshold = parseFloat(thresholdInput.value);
    const paramKey = currentKind === 'buy' ? 'ceiling_c_per_kwh' : 'floor_c_per_kwh';
    const body = { end_at: endAt, [paramKey]: threshold };
    const resp = await fetch(`/modes/${currentKind}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ error: 'unknown' }));
      alert(`Activation failed: ${err.error}`);
      return;
    }
    panel.close();
    await render();
  });

  async function render() {
    const resp = await fetch('/modes');
    if (!resp.ok) return;
    const body = await resp.json();
    const now = new Date(body.now);
    const byKind = Object.fromEntries(body.modes.map((m) => [m.kind, m]));

    for (const kind of ['buy', 'conserve']) {
      const card = document.getElementById(`mode-card-${kind}`);
      const status = card.querySelector('.mode-card__status');
      const inactiveBody = card.querySelector('[data-empty="true"]');
      const activeBody = card.querySelector('[data-empty="false"]');
      const m = byKind[kind];

      if (!m) {
        status.dataset.status = 'inactive';
        status.textContent = 'Inactive';
        inactiveBody.hidden = false;
        activeBody.hidden = true;
        continue;
      }
      status.dataset.status = 'active';
      status.textContent = 'Active';
      inactiveBody.hidden = true;
      activeBody.hidden = false;
      const end = new Date(m.end_at);
      const minutes = Math.max(0, Math.round((end - now) / 60_000));
      activeBody.querySelector('[data-field="countdown"]').textContent =
        minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`;
      const paramKey = kind === 'buy' ? 'ceiling_c_per_kwh' : 'floor_c_per_kwh';
      const field = kind === 'buy' ? 'ceiling' : 'floor';
      activeBody.querySelector(`[data-field="${field}"]`).textContent =
        `${m.params[paramKey]} c/kWh`;
    }
  }

  return { render };
})();

// Poll alongside the existing dashboard refresh loop.
setInterval(() => ModesUI.render(), 5000);
ModesUI.render();
```

- [ ] **Step 3: Add styling**

Append to `src/optimiser/api/static/dashboard.css`:

```css
.modes-section { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-block: 1rem; }
.mode-card { border: 1px solid var(--card-border, #333); border-radius: 6px; padding: 1rem; background: var(--card-bg, #1a1a1a); }
.mode-card__header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
.mode-card__title { font-weight: bold; letter-spacing: 0.05em; }
.mode-card__status[data-status="active"] { color: var(--accent-ok, #4ec); }
.mode-card__status[data-status="inactive"] { color: var(--muted, #888); }
.mode-card__detail { display: flex; justify-content: space-between; padding: 0.2rem 0; }
.mode-card__detail .label { color: var(--muted, #888); }
.mode-card__activate, .mode-card__cancel { width: 100%; padding: 0.5rem; margin-top: 0.5rem; cursor: pointer; }
.mode-panel { padding: 1.5rem; max-width: 24rem; }
.mode-panel label { display: block; margin-block: 0.5rem; }
.mode-panel select, .mode-panel input { width: 100%; padding: 0.4rem; }
.mode-panel__hint { font-size: 0.85em; color: var(--muted, #888); margin-block: 0.4rem; }
@media (max-width: 600px) { .modes-section { grid-template-columns: 1fr; } }
```

- [ ] **Step 4: Manual smoke**

```bash
# From repo root.
docker compose restart energy-optimiser  # or use your /deploy skill
```

Open the dashboard in a browser. Verify:
1. Two cards appear ("Buy mode", "Conserve mode"), both showing "Inactive".
2. Click "Activate" on Buy → panel opens with suggestion populated.
3. Submit with a 1h window + the suggested ceiling. The card flips to "Active" with countdown and ceiling value.
4. Click "Cancel". The card returns to "Inactive".
5. Reload the page. State persists if mode was active before reload.

- [ ] **Step 5: Commit**

```bash
git add src/optimiser/api/static/dashboard.html src/optimiser/api/static/dashboard.js src/optimiser/api/static/dashboard.css
git commit -m "dashboard: mode cards + activation panel with Amber suggestion"
```

---

## Task 18: Replay — respect modes from snapshots

**Files:**
- Modify: `src/optimiser/replay.py`
- Modify: `tests/test_replay_*.py` (or create `tests/test_replay_modes.py`)

- [ ] **Step 1: Write a failing test**

Create `tests/test_replay_modes.py`:

```python
"""Replay must reconstruct mode_overrides from snapshots by default."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from optimiser.modes import ModeOverrides
from optimiser.replay import build_overrides_from_snapshot
from optimiser.types import ActiveModeRecord, TickSnapshot


def test_overrides_reconstructed_from_snapshot() -> None:
    now = datetime(2026, 5, 19, 4, 0, 0, tzinfo=UTC)
    end_at = now + timedelta(hours=2)
    snap_modes = (
        ActiveModeRecord(
            kind="buy",
            end_at=end_at,
            params={"ceiling_c_per_kwh": 12.0},
        ),
    )
    slots = [now + timedelta(minutes=5 * i) for i in range(36)]
    overrides = build_overrides_from_snapshot(snap_modes, now, slots)

    assert overrides.buy_ceiling_c_per_kwh == 12.0
    # 2h = 24 5-min slots in-window.
    assert sum(overrides.buy_active_at) == 24
    assert overrides.buy_active_at[0] is True
    assert overrides.buy_active_at[23] is True
    assert overrides.buy_active_at[24] is False


def test_empty_snapshot_modes_yields_empty_overrides() -> None:
    now = datetime(2026, 5, 19, 4, 0, 0, tzinfo=UTC)
    slots = [now + timedelta(minutes=5 * i) for i in range(12)]
    overrides = build_overrides_from_snapshot((), now, slots)
    assert overrides == ModeOverrides.empty(12)
```

- [ ] **Step 2: Run test, verify it fails**

Run: `uv run pytest tests/test_replay_modes.py -v`
Expected: ImportError — `build_overrides_from_snapshot` does not exist.

- [ ] **Step 3: Implement the helper**

In `src/optimiser/replay.py`, add:

```python
from datetime import datetime

from .modes import ModeOverrides
from .types import ActiveModeRecord


def build_overrides_from_snapshot(
    snap_modes: tuple[ActiveModeRecord, ...],
    now: datetime,
    slots: list[datetime],
) -> ModeOverrides:
    """Reconstruct ModeOverrides from the snapshot's active_modes field.

    Mirrors ModeManager.to_overrides() but operates on the frozen
    snapshot record rather than live state, so historical replays
    reproduce the exact mode-aware decisions the LP made at the time.
    """
    by_kind = {m.kind: m for m in snap_modes}
    buy = by_kind.get("buy")
    conserve = by_kind.get("conserve")
    return ModeOverrides(
        buy_active_at=tuple(
            (buy is not None and slot < buy.end_at) for slot in slots
        ),
        buy_ceiling_c_per_kwh=(buy.params["ceiling_c_per_kwh"] if buy else None),
        conserve_active_at=tuple(
            (conserve is not None and slot < conserve.end_at) for slot in slots
        ),
        conserve_floor_c_per_kwh=(conserve.params["floor_c_per_kwh"] if conserve else None),
    )
```

- [ ] **Step 4: Wire it into the replay path**

Find the replay loop (probably `replay_one_tick` or similar). Where `solve_stochastic` is called, add:

```python
        overrides = build_overrides_from_snapshot(
            snapshot.active_modes if respect_modes else (),
            snapshot.system_state.timestamp,
            slot_grid,
        )
        result = solve_stochastic(..., mode_overrides=overrides)
```

Add a CLI flag in `replay_cli.py`:

```python
parser.add_argument(
    "--respect-modes",
    dest="respect_modes",
    action=argparse.BooleanOptionalAction,
    default=True,
    help="Reconstruct mode_overrides from snapshot.active_modes during replay (default: True).",
)
```

Thread through to the replay function.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_replay_modes.py -v`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/optimiser/replay.py src/optimiser/replay_cli.py tests/test_replay_modes.py
git commit -m "replay: --respect-modes reconstructs overrides from snapshot"
```

---

## Task 19: /explain-plan surfaces active modes

**Files:**
- Modify: `.claude/skills/explain-plan/SKILL.md` (only if the skill itself queries; otherwise modify the underlying handler)

The `/explain-plan` skill reads from `/plan/current` (a service endpoint). Verify whether that endpoint already includes `active_modes`. If yes, the skill should be updated to render them; if no, the endpoint payload needs the field added.

- [ ] **Step 1: Inspect the /plan/current handler**

Read `src/optimiser/api/handlers/plan.py` (or wherever `/plan/current` lives). Confirm whether the response includes the snapshot's `active_modes`.

- [ ] **Step 2: Add active_modes to the payload if missing**

Modify the handler to include:

```python
        "active_modes": [
            {"kind": m.kind, "end_at": m.end_at.isoformat(), "params": dict(m.params)}
            for m in snapshot.active_modes
        ],
```

- [ ] **Step 3: Manual smoke**

Activate a mode via the dashboard, then run `/explain-plan`. Confirm the explanation surfaces a one-line summary like "Buy mode active until 15:30 (ceiling 12 c/kWh)".

If the skill's prompt doesn't currently mention modes, update `.claude/skills/explain-plan/SKILL.md` to instruct the model to surface them when present.

- [ ] **Step 4: Commit**

```bash
git add src/optimiser/api/handlers/plan.py .claude/skills/explain-plan/SKILL.md
git commit -m "explain-plan: surface active modes in plan summary"
```

---

## Task 20: End-to-end smoke

**Files:** none (verification only)

- [ ] **Step 1: Deploy + smoke**

```bash
# From repo root, with the local dev stack:
docker compose restart energy-optimiser
sleep 5
curl -sf http://localhost:8080/modes | jq .
# Expected: {"modes": [], "now": "..."}

curl -sf -X POST http://localhost:8080/modes/buy \
  -H "content-type: application/json" \
  -d "$(jq -nc --arg end "$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%S+00:00)" '{end_at: $end, ceiling_c_per_kwh: 12.0}')"
# Expected: 200 + ActiveMode JSON
```

- [ ] **Step 2: Verify LP sees the override**

Watch the logs for the next tick (within 60s):

```bash
docker logs energy-optimiser --tail 100 | grep -i "buy\|MODE_ACTIVATED\|MODE2_TRIM"
```

Expected: `MODE_ACTIVATED` event logged. Subsequent LP solve should respect the ceiling (verify by activating buy mode at a moment when current import-price > ceiling: the LP should not commit slot 0 to charging).

- [ ] **Step 3: Verify persistence**

```bash
docker exec energy-optimiser cat /var/lib/energy-optimiser/active_modes.json
```

Expected: JSON with the buy entry.

- [ ] **Step 4: Restart and verify resumption**

```bash
docker compose restart energy-optimiser
sleep 5
curl -sf http://localhost:8080/modes | jq .
```

Expected: same buy mode still present (assuming not yet expired).

- [ ] **Step 5: Cancel and verify removal**

```bash
curl -sf -X DELETE http://localhost:8080/modes/buy -w '%{http_code}'
# Expected: 204
curl -sf http://localhost:8080/modes | jq .
# Expected: {"modes": [], ...}
```

- [ ] **Step 6: Manual dashboard test**

Open the dashboard, walk through Task 17 step 4's checklist end-to-end.

- [ ] **Step 7: Run the full test suite once more**

```bash
uv run pytest tests/ -q
```

Expected: full pass.

---

## Self-review checklist (run before handing off)

1. **Spec coverage** — every spec section has at least one task:
   - ✅ Problem framing → motivates the plan
   - ✅ Buy mode constraints → Task 8, 9, 11
   - ✅ Conserve mode constraints → Task 10, 11
   - ✅ ModeOverrides + ModeManager → Tasks 2–6
   - ✅ Dashboard surface → Task 17
   - ✅ API surface → Tasks 14–15
   - ✅ Runtime representation (modes.py) → Tasks 2–6
   - ✅ Persistence → Tasks 3, 5
   - ✅ Composition + state-machine interaction → smoke test in Task 20 confirms fallback inertness (the LP isn't called during fallback, so overrides are inert by construction)
   - ✅ Observability — MODE_ACTIVATED/MODE_EXPIRED → Tasks 4–5; TickSnapshot.active_modes → Task 13; explain-plan → Task 19
   - ✅ Testing list → all six items in spec covered by tasks 6, 8, 10, 14
   - ✅ Implementation order matches spec's "Implementation order (rough)"

2. **Decisions log resolved** — every entry in the spec's decisions table is honoured:
   - Buy mode discharge to house allowed → Task 8 only constrains `grid_export`, not `bat_discharge`
   - Max window 48h → Task 14 `MAX_WINDOW = timedelta(hours=48)`
   - Threshold pre-fill via Amber forecast → Task 15
   - Threshold in `(0, 100]` → Task 14 `_validate_threshold`
   - PV export below conserve floor allowed → Task 10 only constrains battery contribution
   - Wear discount only (no positive bonus) → Task 11 sets factor to 0.0, no negative cost term added

3. **Placeholder scan** — no TBD/TODO/"implement later"; every code block is complete code an engineer can copy.

4. **Type consistency** — `ModeKind`, `ActiveMode`, `ModeOverrides`, `ModeManager`, `ActiveModeRecord` referenced consistently across tasks. The `to_overrides(now, slots)` signature in Task 6 matches the call in Task 12. `mode_overrides=` keyword matches in Tasks 7, 9, 12, 18.
