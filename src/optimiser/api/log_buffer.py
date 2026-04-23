"""In-memory log ring buffer + helpers for /logs.

The ring buffer is a bounded deque of dicts (structured records, not
formatted strings) so the /logs handler can filter by level and
timestamp without reparsing. Authoritative log history lives on disk
(RotatingFileHandler) and in Docker's log driver; the ring buffer is
optimised for "give me the most recent N — fast, over HTTP".
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any


class RingBufferHandler(logging.Handler):
    """Holds the last N formatted log records in memory.

    Records are captured as dicts with the fields the /logs handler
    needs (timestamp, level, logger, message). A threading.Lock guards
    the deque because Python logging handlers may be called from any
    thread (e.g. solver in asyncio.to_thread).
    """

    def __init__(self, capacity: int) -> None:
        super().__init__()
        self._buf: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            # Include exception info if present — shows up as a single
            # multi-line string, same as a terminal.
            if record.exc_info:
                entry["exc_info"] = self.format(
                    logging.LogRecord(
                        record.name,
                        record.levelno,
                        record.pathname,
                        record.lineno,
                        "",
                        (),
                        record.exc_info,
                    )
                )
            with self._lock:
                self._buf.append(entry)
        except Exception:
            self.handleError(record)

    def snapshot(
        self,
        *,
        since: datetime | None,
        until: datetime | None,
        min_level: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return filtered records, newest-first.

        Filters are applied in a single pass over a snapshot of the
        deque so a concurrent write can't mutate the view mid-query.
        """
        with self._lock:
            items = list(self._buf)

        out: list[dict[str, Any]] = []
        for entry in reversed(items):
            ts = datetime.fromisoformat(entry["ts"])
            if since is not None and ts < since:
                continue
            if until is not None and ts >= until:
                continue
            if logging.getLevelName(entry["level"]) < min_level:
                continue
            out.append(entry)
            if len(out) >= limit:
                break
        return out
