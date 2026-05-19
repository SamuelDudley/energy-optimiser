"""Tests for the user-strategy-modes data types and manager."""

from __future__ import annotations

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
