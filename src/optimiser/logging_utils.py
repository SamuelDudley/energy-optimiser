"""Structured JSON event logging.

All events are JSON, written to stdout (Docker logs).
Tick snapshots are written to NDJSON files.
"""

from __future__ import annotations

import gzip
import json
import logging
import sys
import uuid
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from .time_utils import now_utc
from .types import Event, EventType, TickSnapshot

logger = logging.getLogger("optimiser")

_VERSION = "0.1.0"


def new_tick_id() -> str:
    return uuid.uuid4().hex[:12]


def _serialise(obj: Any) -> Any:
    """JSON serialiser for dataclass fields."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    return str(obj)


class EventLogWriter:
    """Append-only daily NDJSON event log for ops observability.

    One file per UTC date (``events-YYYY-MM-DD.ndjson``). Plain text
    rather than gzipped because the event rate is high enough that
    per-emit gzip open/close would dominate; daily rotation by event
    date lets the /ops/* endpoints DuckDB-read_json over a glob.
    Operators can compress old days via logrotate without breaking
    queries (DuckDB handles ``.ndjson`` and ``.ndjson.gz`` together).

    Volume estimate: ~30-60 events/min steady-state * 1440 min/day
    * ~250 bytes/event ≈ 10-20 MB/day uncompressed. After a week
    that's ~100 MB before compression — manageable.
    """

    def __init__(self, event_dir: str | Path) -> None:
        self._dir = Path(event_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, d: date) -> Path:
        return self._dir / f"events-{d.isoformat()}.ndjson"

    def write_line(self, ts: datetime, json_line: str) -> None:
        """Append a pre-serialised NDJSON record to today's file.

        ``ts`` selects the daily file; ``json_line`` is the already-
        encoded record (no trailing newline expected). ``emit_event``
        is the only caller — it builds the JSON once for both stdout
        and disk so we don't re-serialise.
        """
        path = self._path_for(ts.date())
        with path.open("ab") as f:
            f.write(json_line.encode("utf-8"))
            f.write(b"\n")

    def close(self) -> None:
        return


# Optional sink for emitted events. When set (typically by Service at
# startup) every emit_event also appends one NDJSON line to the daily
# event log file under StorageConfig.event_log_dir. Stdout still gets
# the same record so existing log-driver-based pipelines are unchanged.
_event_log_writer: EventLogWriter | None = None


def set_event_log_writer(writer: EventLogWriter | None) -> None:
    """Install the event-log sink. None disables persistence."""
    global _event_log_writer
    _event_log_writer = writer


def emit_event(event: Event) -> None:
    """Write a structured event to stdout as JSON; mirror to the event
    log file if a writer has been installed via ``set_event_log_writer``.
    """
    record = {
        "ts": event.timestamp.isoformat(),
        "event": event.event_type.value,
        "data": event.data,
    }
    if event.tick_id:
        record["tick_id"] = event.tick_id
    try:
        line = json.dumps(record, default=_serialise)
        print(line, flush=True)
        if _event_log_writer is not None:
            _event_log_writer.write_line(event.timestamp, line)
    except Exception:
        logger.exception("Failed to emit event")


def emit(
    event_type: EventType,
    data: dict[str, Any] | None = None,
    tick_id: str | None = None,
) -> None:
    """Convenience wrapper for emit_event."""
    emit_event(
        Event(
            timestamp=now_utc(),
            event_type=event_type,
            data=data or {},
            tick_id=tick_id,
        )
    )


class _ApiCallScope:
    """Mutable handle yielded by ``api_call`` so callers can record the
    response (or override the ok flag) before the context manager exits."""

    __slots__ = ("http_status", "ok", "extra")

    def __init__(self) -> None:
        self.http_status: int | None = None
        self.ok: bool = False
        self.extra: dict[str, Any] = {}

    def set_response(self, resp: Any) -> None:
        """Capture status + 2xx-ness from an httpx.Response-like object."""
        try:
            self.http_status = int(resp.status_code)
            self.ok = bool(getattr(resp, "is_success", 200 <= self.http_status < 300))
        except (AttributeError, TypeError, ValueError):
            pass


@contextmanager
def api_call(client: str, op: str) -> Iterator[_ApiCallScope]:
    """Time an external API call and emit ``API_CALL`` on exit.

    Always emits — success, non-2xx, and exception paths all produce one
    event with ``ms`` measured from entry. Callers record the response
    via ``scope.set_response(resp)``; exceptions raised inside the block
    propagate but ``ok=False`` is recorded first.

    Schema: ``{client, op, http_status, ms, ok, extra?}`` — see
    EventType.API_CALL docstring in types.py.
    """
    scope = _ApiCallScope()
    t0 = perf_counter()
    exc_class: str | None = None
    try:
        yield scope
    except BaseException as exc:
        exc_class = type(exc).__name__
        scope.ok = False
        raise
    finally:
        ms = (perf_counter() - t0) * 1000.0
        data: dict[str, Any] = {
            "client": client,
            "op": op,
            "http_status": scope.http_status,
            "ms": round(ms, 2),
            "ok": scope.ok,
        }
        if exc_class is not None:
            scope.extra["exception"] = exc_class
        if scope.extra:
            data["extra"] = scope.extra
        emit(EventType.API_CALL, data)


def _decode_one_gzip_member(blob: bytes, start: int) -> tuple[list[bytes], int] | None:
    """Decompress one gzip member starting at ``blob[start]``.

    Returns ``(lines, end_offset)`` on success — ``end_offset`` is the
    byte index where the next member begins (or ``len(blob)``). Returns
    ``None`` on any decode failure.

    Uses ``zlib.decompressobj`` in single-member gzip mode
    (``wbits = MAX_WBITS | 16``) rather than byte-scanning for the next
    ``0x1f 0x8b``. Magic-byte scanning gives wrong answers because those
    two bytes occur with non-trivial frequency inside compressed
    payloads — splitting one valid member across a false-positive
    interior magic produces many spurious "corrupt member" spans, which
    is exactly the failure mode that ate this file on 2026-05-08.
    """
    d = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    try:
        data = d.decompress(blob[start:])
        if not d.eof:
            return None
        data += d.flush()
    except zlib.error:
        return None
    end = len(blob) - len(d.unused_data)
    lines = [ln for ln in data.splitlines() if ln.strip()]
    return lines, end


def recover_torn_gzip(path: Path) -> int | None:
    """Heal a torn multi-member gzip in place; return recovered line count.

    A hard crash (kernel panic, power loss) can leave today's snapshot
    file with one corrupt gzip member partway through. Python's
    ``gzip.open`` then stops at the bad member silently, and DuckDB's
    ``read_ndjson_objects`` errors hard ("Input is not a GZIP stream") —
    the per-file try/except in ``api/handlers/ops.py`` swallows that and
    today's data goes invisible on the dashboard.

    Strategy: probe the file with ``zlib.decompress(... wbits=MAX_WBITS|32)``
    (auto-multi-member) — if it decodes end-to-end the file is healthy
    and we leave it alone. Otherwise walk members structurally with
    ``zlib.decompressobj`` (single-member mode), using ``unused_data`` to
    find each member's true end. On a member-decode failure scan forward
    for the next ``0x1f 0x8b`` magic from the failure point and try
    again. Successful members contribute their lines; failed spans are
    counted and dropped. Rewrite as a fresh single-member gzip via
    tempfile + atomic rename. Returns the number of recovered lines, or
    ``None`` if no recovery was needed.

    Idempotent: a healthy file passes the probe and is left untouched.
    """
    if not path.exists():
        return None
    blob = path.read_bytes()
    if not blob:
        return None
    # Probe: ``gzip.decompress`` walks every member in a multi-member
    # stream and raises on any corruption — that's the property we want.
    # ``zlib.decompress(..., wbits=MAX_WBITS|32)`` only handles single-
    # member auto-detect and silently stops after the first member, so a
    # corrupt tail wouldn't trip it.
    try:
        gzip.decompress(blob)
        return None
    except (OSError, EOFError, zlib.error):
        pass

    recovered: list[bytes] = []
    n_corrupt = 0
    pos = 0
    while pos < len(blob) - 1:
        magic = blob.find(b"\x1f\x8b", pos)
        if magic < 0:
            if pos < len(blob):
                n_corrupt += 1
            break
        if magic > pos:
            n_corrupt += 1
        result = _decode_one_gzip_member(blob, magic)
        if result is None:
            n_corrupt += 1
            pos = magic + 2
            continue
        lines, end = result
        recovered.extend(lines)
        pos = end

    if n_corrupt == 0:
        return None
    tmp = path.with_suffix(path.suffix + ".recovered")
    with gzip.open(tmp, "wb") as f:
        for line in recovered:
            f.write(line)
            f.write(b"\n")
    tmp.replace(path)
    logger.warning(
        "recovered torn gzip %s: kept %d lines, dropped %d corrupt spans",
        path,
        len(recovered),
        n_corrupt,
    )
    return len(recovered)


class SnapshotWriter:
    """Writes tick snapshots to daily NDJSON files, gzipped.

    Each write is a self-contained gzip member (concatenated multi-member
    gzip — a standard format readable by gzip, zcat, and Python's
    ``gzip.open`` without any special handling). This means the file is
    always in a fully-terminated state on disk: SIGKILL, OOM, or power loss
    can only lose the in-flight write, never truncate previously-written
    snapshots into an unreadable (no-trailer) gzip stream.

    On startup, today's file is probed: if any gzip member fails to
    decode (hard crash mid-write), the file is repaired in place via
    ``recover_torn_gzip`` before the first new append. Without this the
    /ops/* endpoints silently drop the entire day until the next UTC
    rotation.
    """

    def __init__(self, snapshot_dir: str | Path) -> None:
        self._dir = Path(snapshot_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            recover_torn_gzip(self._path_for(now_utc().date()))
        except Exception:
            logger.exception("snapshot startup recovery failed (non-fatal)")

    def _path_for(self, d: date) -> Path:
        return self._dir / f"{d.isoformat()}.ndjson.gz"

    def write(self, snapshot: TickSnapshot) -> None:
        """Append a snapshot to today's NDJSON file as its own gzip member."""
        path = self._path_for(snapshot.timestamp.date())
        line = json.dumps(asdict(snapshot), default=_serialise) + "\n"
        with gzip.open(path, "ab") as f:
            f.write(line.encode("utf-8"))

    def close(self) -> None:
        # Each write is self-contained; nothing to flush on shutdown.
        return


def setup_logging() -> None:
    """Configure logging for the service."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    # Suppress noisy libraries
    logging.getLogger("pymodbus").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
