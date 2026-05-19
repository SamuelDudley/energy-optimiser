"""Tests for the user-strategy-modes data types and manager."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from optimiser.modes import ActiveMode, ModeManager, ModeOverrides

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


class TestModeManagerActivateCancel:
    def test_activate_emits_event(self, tmp_path, monkeypatch) -> None:
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            "optimiser.modes.emit",
            lambda et, payload: events.append((et.name, payload)),
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
            lambda et, payload: events.append((et.name, payload)),
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
        assert any(et == "MODE_EXPIRED" and p["reason"] == "user_cancelled" for et, p in events)

    def test_cancel_returns_false_when_not_active(self, tmp_path) -> None:
        mgr = ModeManager(tmp_path / "active_modes.json")
        assert mgr.cancel("buy") is False


class TestModeManagerExpiry:
    def test_expired_modes_dropped_lazily(self, tmp_path, monkeypatch) -> None:
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            "optimiser.modes.emit",
            lambda et, payload: events.append((et.name, payload)),
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
            lambda et, payload: events.append((et.name, payload)),
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


class TestPruneSocReached:
    def test_no_buy_active_is_noop(self, tmp_path) -> None:
        mgr = ModeManager(tmp_path / "active_modes.json")
        mgr.prune_soc_reached(80.0)
        assert mgr.active(NOW) == []

    def test_buy_without_cutoff_is_noop(self, tmp_path, monkeypatch) -> None:
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            "optimiser.modes.emit",
            lambda et, payload: events.append((et.name, payload)),
        )
        mgr = ModeManager(tmp_path / "active_modes.json")
        mgr.activate(
            ActiveMode(
                kind="buy",
                end_at=NOW + timedelta(hours=2),
                params={"ceiling_c_per_kwh": 12.0},
                activated_at=NOW,
                source="dashboard",
            )
        )
        events.clear()
        mgr.prune_soc_reached(95.0)
        assert len(mgr.active(NOW)) == 1
        assert not any(et == "MODE_EXPIRED" for et, _ in events)

    def test_buy_with_cutoff_reached_is_pruned(self, tmp_path, monkeypatch) -> None:
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            "optimiser.modes.emit",
            lambda et, payload: events.append((et.name, payload)),
        )
        mgr = ModeManager(tmp_path / "active_modes.json")
        mgr.activate(
            ActiveMode(
                kind="buy",
                end_at=NOW + timedelta(hours=2),
                params={"ceiling_c_per_kwh": 12.0, "soc_cutoff_pct": 80.0},
                activated_at=NOW,
                source="dashboard",
            )
        )
        events.clear()
        mgr.prune_soc_reached(80.0)
        assert mgr.active(NOW) == []
        expired = [p for et, p in events if et == "MODE_EXPIRED"]
        assert len(expired) == 1
        assert expired[0] == {"kind": "buy", "reason": "soc_reached"}

    def test_buy_with_cutoff_above_current_is_kept(self, tmp_path) -> None:
        mgr = ModeManager(tmp_path / "active_modes.json")
        mgr.activate(
            ActiveMode(
                kind="buy",
                end_at=NOW + timedelta(hours=2),
                params={"ceiling_c_per_kwh": 12.0, "soc_cutoff_pct": 80.0},
                activated_at=NOW,
                source="dashboard",
            )
        )
        mgr.prune_soc_reached(75.0)
        assert len(mgr.active(NOW)) == 1

    def test_prune_persists_removal(self, tmp_path) -> None:
        path = tmp_path / "active_modes.json"
        mgr = ModeManager(path)
        mgr.activate(
            ActiveMode(
                kind="buy",
                end_at=NOW + timedelta(hours=2),
                params={"ceiling_c_per_kwh": 12.0, "soc_cutoff_pct": 80.0},
                activated_at=NOW,
                source="dashboard",
            )
        )
        mgr.prune_soc_reached(85.0)
        mgr2 = ModeManager(path)
        assert mgr2.active(NOW) == []


class TestSocCutoffOverridesField:
    def test_to_overrides_carries_soc_cutoff(self, tmp_path) -> None:
        mgr = ModeManager(tmp_path / "active_modes.json")
        mgr.activate(
            ActiveMode(
                kind="buy",
                end_at=NOW + timedelta(minutes=30),
                params={"ceiling_c_per_kwh": 12.0, "soc_cutoff_pct": 80.0},
                activated_at=NOW,
                source="dashboard",
            )
        )
        slots = [NOW + timedelta(minutes=5 * i) for i in range(6)]
        o = mgr.to_overrides(NOW, slots)
        assert o.buy_soc_cutoff_pct == 80.0

    def test_to_overrides_no_cutoff_is_none(self, tmp_path) -> None:
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
        slots = [NOW + timedelta(minutes=5 * i) for i in range(6)]
        o = mgr.to_overrides(NOW, slots)
        assert o.buy_soc_cutoff_pct is None
