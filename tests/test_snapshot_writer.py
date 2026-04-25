"""Tests for SnapshotWriter durability guarantees."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

from optimiser.logging_utils import SnapshotWriter


def _snap(ts: datetime):
    from optimiser.types import (
        LoadProfile,
        PlannerOutput,
        SystemState,
        TickSnapshot,
    )

    return TickSnapshot(
        tick_id="t",
        timestamp=ts,
        version="0.0.0",
        system_state=SystemState(
            timestamp=ts,
            soc_pct=50.0,
            battery_power_kw=0.0,
            pv_power_kw=0.0,
            grid_power_kw=0.0,
            house_load_kw=0.5,
            ems_mode=2,
            outdoor_temp_c=None,
            occupied=None,
        ),
        price_forecast=[],
        pv_forecast=None,
        load_profile=LoadProfile(slots=[0.0] * 48, maturity_level=0, context="t"),
        managed_loads=[],
        maturity_level=0,
        output=PlannerOutput(
            battery_action="self_consume",  # type: ignore[arg-type]
            charge_limit_kw=0.0,
            discharge_limit_kw=0.0,
            target_soc=None,
            load_commands=[],
            grid_export_limit_kw=None,
            reason="t",
        ),
    )


def test_write_produces_valid_gzip(tmp_path: Path) -> None:
    w = SnapshotWriter(tmp_path)
    ts = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
    for i in range(5):
        w.write(_snap(ts.replace(minute=i)))
    # No close() — file must already be readable as a sealed multi-member gzip.
    path = tmp_path / "2026-04-24.ndjson.gz"
    with gzip.open(path, "rt") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 5
    assert [d["system_state"]["house_load_kw"] for d in lines] == [0.5] * 5


def test_truncated_tail_does_not_corrupt_prior_members(tmp_path: Path) -> None:
    """Simulate SIGKILL mid-write: chop bytes off the end of the file and
    verify prior snapshots remain readable. This is the property the old
    single-stream writer could not provide."""
    w = SnapshotWriter(tmp_path)
    ts = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
    for i in range(3):
        w.write(_snap(ts.replace(minute=i)))
    path = tmp_path / "2026-04-24.ndjson.gz"
    good_len = path.stat().st_size
    # Start a 4th write then chop its tail to simulate crash mid-write.
    w.write(_snap(ts.replace(minute=3)))
    partial = path.read_bytes()
    # Keep all 3 complete members + half the 4th member's bytes.
    truncated_len = good_len + (len(partial) - good_len) // 2
    path.write_bytes(partial[:truncated_len])
    # Python's gzip raises EOFError on the truncated 4th member, but the first
    # 3 members must decode cleanly before that.
    lines: list[dict] = []
    try:
        with gzip.open(path, "rt") as f:
            for line in f:
                if line.strip():
                    lines.append(json.loads(line))
    except (EOFError, OSError):
        pass  # expected on the truncated trailing member
    assert len(lines) >= 3
    assert lines[0]["timestamp"].startswith("2026-04-24T12:00")
    assert lines[2]["timestamp"].startswith("2026-04-24T12:02")


def test_close_is_safe_without_writes(tmp_path: Path) -> None:
    w = SnapshotWriter(tmp_path)
    w.close()
    w.close()
