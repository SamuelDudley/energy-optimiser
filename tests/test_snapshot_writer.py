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


def test_recover_torn_gzip_heals_corrupted_member(tmp_path: Path) -> None:
    """Hard-crash repro: a single garbage gzip member between good ones
    breaks DuckDB's ``read_ndjson_objects``. ``recover_torn_gzip`` must
    walk member starts, drop the bad member, rewrite the file, and leave
    DuckDB able to decode it end-to-end."""
    import duckdb

    from optimiser.logging_utils import recover_torn_gzip

    w = SnapshotWriter(tmp_path)
    ts = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
    for i in range(3):
        w.write(_snap(ts.replace(minute=i)))
    path = tmp_path / "2026-04-24.ndjson.gz"
    blob = path.read_bytes()

    # Find the boundary between members (next 0x1f8b after the first one)
    # and splice 16 bytes of garbage starting with the gzip magic right
    # there. The walker will see the synthetic "member start", but the
    # 0x00 compression-method byte is invalid, so ``gzip.decompress``
    # fails on that span — same shape as a hard-crash torn member.
    boundary = blob.find(b"\x1f\x8b", 2)
    assert boundary > 0, "test fixture needs at least 2 members"
    bad = b"\x1f\x8b" + b"\x00" * 14
    path.write_bytes(blob[:boundary] + bad + blob[boundary:])

    # DuckDB cannot read the corrupted file.
    con = duckdb.connect(":memory:")
    try:
        con.execute("SELECT COUNT(*) FROM read_ndjson_objects(?)", [str(path)]).fetchone()
        broken = False
    except Exception:
        broken = True
    assert broken, "test setup failed: corrupted file should not decode"

    n = recover_torn_gzip(path)
    assert n is not None and n >= 3, f"expected ≥3 recovered lines, got {n}"

    # Post-recovery: DuckDB reads it cleanly and recovers all 3 originals.
    rows = con.execute(
        "SELECT json_extract_string(j, '$.timestamp') "
        "FROM read_ndjson_objects(?) AS t(j) "
        "ORDER BY 1",
        [str(path)],
    ).fetchall()
    timestamps = [r[0] for r in rows]
    assert any(t.startswith("2026-04-24T12:00") for t in timestamps)
    assert any(t.startswith("2026-04-24T12:02") for t in timestamps)


def test_recover_torn_gzip_noop_on_healthy_file(tmp_path: Path) -> None:
    """Idempotency: a healthy multi-member gzip must be left untouched."""
    from optimiser.logging_utils import recover_torn_gzip

    w = SnapshotWriter(tmp_path)
    ts = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
    for i in range(3):
        w.write(_snap(ts.replace(minute=i)))
    path = tmp_path / "2026-04-24.ndjson.gz"
    before = path.read_bytes()

    assert recover_torn_gzip(path) is None
    assert path.read_bytes() == before


def test_recover_torn_gzip_preserves_big_member_with_internal_magic_bytes(
    tmp_path: Path,
) -> None:
    """Regression: an earlier byte-scanning algorithm split a single
    healthy big gzip member into many false-positive sub-spans whenever
    the compressed payload happened to contain ``0x1f 0x8b`` internally,
    erasing the entire member on a startup self-heal. This is the bug
    that ate ~245 lines of post-recovery data on 2026-05-08.

    Construct a single big gzip member whose compressed bytes contain
    multiple ``0x1f 0x8b`` sequences (achieved cheaply by writing many
    randomly-flavoured lines), append a deliberately corrupt member
    after it, and verify ``recover_torn_gzip`` keeps every line from the
    big member.
    """
    import gzip as _gzip
    import os

    path = tmp_path / "2026-04-24.ndjson.gz"
    # 200 lines of varied bytes — a few internal 0x1f8b sequences are
    # near-certain at this size; not relying on luck, just on volume.
    lines = [json.dumps({"i": i, "blob": os.urandom(64).hex()}).encode("utf-8") for i in range(200)]
    with _gzip.open(path, "wb") as f:
        for ln in lines:
            f.write(ln)
            f.write(b"\n")

    raw = path.read_bytes()
    n_internal_magic = sum(
        1 for i in range(2, len(raw) - 1) if raw[i] == 0x1F and raw[i + 1] == 0x8B
    )
    assert n_internal_magic >= 1, "test fixture failed to produce internal magic bytes"

    # Append a deliberately corrupt "second member" so the file fails
    # the multi-member probe and triggers recovery.
    bad = b"\x1f\x8b" + b"\x00" * 14
    path.write_bytes(raw + bad)

    from optimiser.logging_utils import recover_torn_gzip

    n = recover_torn_gzip(path)
    assert n is not None and n == 200, (
        f"big member fragmentation regression: expected 200 lines, got {n}"
    )


def test_recover_torn_gzip_handles_missing_and_empty(tmp_path: Path) -> None:
    """Missing file or zero-byte file should return None without raising."""
    from optimiser.logging_utils import recover_torn_gzip

    missing = tmp_path / "no-such-file.ndjson.gz"
    assert recover_torn_gzip(missing) is None

    empty = tmp_path / "empty.ndjson.gz"
    empty.write_bytes(b"")
    assert recover_torn_gzip(empty) is None


def test_snapshot_writer_init_heals_today_file(tmp_path: Path, monkeypatch) -> None:
    """SnapshotWriter.__init__ probes today's file and recovers it before
    the first append. Reproduces the hard-crash scenario observed
    2026-05-08 where the dashboard /ops/solve series cleanly stopped at
    the UTC rotation boundary because today's file was unreadable by
    DuckDB."""
    import duckdb

    from optimiser import logging_utils

    today = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
    # Pre-populate today's file with a torn gzip *before* spinning up
    # a fresh SnapshotWriter — this is the "service restarts after a
    # hard crash" path we're regression-testing.
    pre = SnapshotWriter(tmp_path)
    for i in range(2):
        pre.write(_snap(today.replace(minute=i)))
    path = tmp_path / "2026-04-24.ndjson.gz"
    blob = path.read_bytes()
    path.write_bytes(blob + b"\x1f\x8b" + b"\x00" * 14)

    monkeypatch.setattr(logging_utils, "now_utc", lambda: today)
    SnapshotWriter(tmp_path)  # __init__ should run recover_torn_gzip

    con = duckdb.connect(":memory:")
    n = con.execute("SELECT COUNT(*) FROM read_ndjson_objects(?)", [str(path)]).fetchone()[0]
    assert n >= 2


def test_post_dispatch_state_round_trips(tmp_path: Path) -> None:
    """The new system_state_post_dispatch field — populated by service.py
    after dispatch is applied, observability use only — must serialise
    when present and stay None when not."""
    from optimiser.types import SystemState

    w = SnapshotWriter(tmp_path)
    ts = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
    snap = _snap(ts)

    # Replace with a snapshot carrying a distinct post-dispatch reading.
    post = SystemState(
        timestamp=ts,
        soc_pct=49.7,
        battery_power_kw=-5.2,
        pv_power_kw=0.0,
        grid_power_kw=-4.7,
        house_load_kw=0.5,
        ems_mode=6,
        outdoor_temp_c=None,
        occupied=None,
    )
    snap_with_post = snap.__class__(
        **{**{k: getattr(snap, k) for k in snap.__slots__}, "system_state_post_dispatch": post},
    )
    w.write(snap_with_post)
    w.write(snap)  # default: post-dispatch is None

    path = tmp_path / "2026-04-24.ndjson.gz"
    with gzip.open(path, "rt") as f:
        lines = [json.loads(line) for line in f if line.strip()]

    assert lines[0]["system_state_post_dispatch"]["battery_power_kw"] == -5.2
    assert lines[0]["system_state_post_dispatch"]["ems_mode"] == 6
    # Pre-dispatch state is unchanged
    assert lines[0]["system_state"]["battery_power_kw"] == 0.0
    # Second snapshot has no post-dispatch reading
    assert lines[1]["system_state_post_dispatch"] is None
