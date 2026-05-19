"""Replay must reconstruct mode_overrides from snapshots by default."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from optimiser.modes import ModeOverrides
from optimiser.replay import build_overrides_from_snapshot
from optimiser.types import ActiveModeRecord


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
