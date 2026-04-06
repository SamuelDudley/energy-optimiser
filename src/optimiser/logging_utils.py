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
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
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


def emit_event(event: Event) -> None:
    """Write a structured event to stdout as JSON."""
    record = {
        "ts": event.timestamp.isoformat(),
        "event": event.event_type.value,
        "data": event.data,
    }
    if event.tick_id:
        record["tick_id"] = event.tick_id
    try:
        print(json.dumps(record, default=_serialise), flush=True)
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


class SnapshotWriter:
    """Writes tick snapshots to daily NDJSON files, gzipped."""

    def __init__(self, snapshot_dir: str | Path) -> None:
        self._dir = Path(snapshot_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._current_date: date | None = None
        self._file: gzip.GzipFile | None = None

    def _path_for(self, d: date) -> Path:
        return self._dir / f"{d.isoformat()}.ndjson.gz"

    def _ensure_file(self, d: date) -> gzip.GzipFile:
        if self._current_date != d:
            self.close()
            self._current_date = d
            self._file = gzip.open(self._path_for(d), "at", encoding="utf-8")
        assert self._file is not None
        return self._file

    def write(self, snapshot: TickSnapshot) -> None:
        """Append a snapshot to today's NDJSON file."""
        d = snapshot.timestamp.date()
        f = self._ensure_file(d)
        line = json.dumps(asdict(snapshot), default=_serialise)
        f.write(line + "\n")
        f.flush()

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None
            self._current_date = None


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
