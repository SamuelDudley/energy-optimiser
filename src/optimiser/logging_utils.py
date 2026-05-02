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


class SnapshotWriter:
    """Writes tick snapshots to daily NDJSON files, gzipped.

    Each write is a self-contained gzip member (concatenated multi-member
    gzip — a standard format readable by gzip, zcat, and Python's
    ``gzip.open`` without any special handling). This means the file is
    always in a fully-terminated state on disk: SIGKILL, OOM, or power loss
    can only lose the in-flight write, never truncate previously-written
    snapshots into an unreadable (no-trailer) gzip stream.
    """

    def __init__(self, snapshot_dir: str | Path) -> None:
        self._dir = Path(snapshot_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

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
