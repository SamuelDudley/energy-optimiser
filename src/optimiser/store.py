"""DuckDB telemetry and load telemetry store."""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from .config import StorageConfig
from .logging_utils import emit
from .types import (
    EventType,
    LoadTelemetryRow,
    PriceForecastLogRow,
    PVForecast,
    PVForecastLogRow,
    TelemetryRow,
)

logger = logging.getLogger(__name__)

# Bump when the meaning of stored fields changes in a way that invalidates
# historical data for downstream consumers (e.g. load profiler). Rows
# written by older code carry a lower (or NULL) schema_version and are
# excluded from analytical queries.
#
# History:
#   1: initial schema. PV register was a placeholder (always 0), so
#      house_load_kw is wrong (off by the actual PV power).
#   2: PV register fixed (30035). house_load_kw is now correct.
CURRENT_SCHEMA_VERSION = 2

TELEMETRY_DDL = """
CREATE TABLE IF NOT EXISTS telemetry (
    ts              TIMESTAMPTZ NOT NULL,
    soc_pct         REAL,
    battery_kw      REAL,
    pv_kw           REAL,
    grid_kw         REAL,
    grid_kw_shelly  REAL,
    house_load_kw   REAL,
    import_price    REAL,
    export_price    REAL,
    spot_price      REAL,
    renewables_pct  REAL,
    spike_status    VARCHAR,
    pv_forecast_kw  REAL,
    outdoor_temp_c  REAL,
    occupied        BOOLEAN,
    ems_mode        INTEGER,
    planner_action  VARCHAR,
    planner_reason  VARCHAR,
    schema_version  INTEGER
);
"""

LOAD_TELEMETRY_DDL = """
CREATE TABLE IF NOT EXISTS load_telemetry (
    ts              TIMESTAMPTZ NOT NULL,
    load_id         VARCHAR NOT NULL,
    category        VARCHAR NOT NULL,
    power_kw        REAL,
    energy_today_kwh REAL,
    cycle_state     VARCHAR,
    relay_on        BOOLEAN,
    schema_version  INTEGER
);
"""

# Migration: ALTER TABLE for installs that pre-date the schema_version
# column. ADD COLUMN IF NOT EXISTS leaves existing rows with NULL, which
# the profiler queries treat as "legacy / excluded".
TELEMETRY_MIGRATIONS = [
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS schema_version INTEGER",
    "ALTER TABLE load_telemetry ADD COLUMN IF NOT EXISTS schema_version INTEGER",
]

PV_FORECAST_LOG_DDL = """
CREATE TABLE IF NOT EXISTS pv_forecast_log (
    fetched_at      TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    pv_estimate_kw  REAL,
    pv_estimate10_kw REAL,
    pv_estimate90_kw REAL,
    actual_kw       REAL
);
"""

# Captures every price interval we see, at every fetch. The redundancy
# is the point: the same interval logged at successive fetches traces
# how a forecast evolves, and the last row for a given interval_start
# (where interval_type = 'ActualInterval') is the realised truth. Join
# the two to answer "was the forecast calibrated?".
#
# Two cadences are logged: the 5-min acute fetches (~every tick) and the
# 30-min planning fetches (~every 5 min). Both go to the same table with
# a `resolution` column to distinguish. No dedup: same (fetched_at,
# resolution, interval_start) is unique because we only fetch each
# cadence once per polling cycle.
PRICE_FORECAST_LOG_DDL = """
CREATE TABLE IF NOT EXISTS price_forecast_log (
    fetched_at          TIMESTAMPTZ NOT NULL,
    resolution          INTEGER NOT NULL,        -- 5 or 30
    interval_start      TIMESTAMPTZ NOT NULL,
    interval_end        TIMESTAMPTZ NOT NULL,
    interval_type       VARCHAR,                 -- ActualInterval / CurrentInterval / ForecastInterval
    per_kwh             REAL,                    -- AEMO point estimate (general channel)
    export_per_kwh      REAL,                    -- feedIn channel perKwh
    spot_per_kwh        REAL,
    forecast_predicted  REAL,                    -- Amber advancedPrice.predicted
    forecast_low        REAL,                    -- Amber advancedPrice.low
    forecast_high       REAL,                    -- Amber advancedPrice.high
    spike_status        VARCHAR,
    descriptor          VARCHAR,
    is_locked           BOOLEAN,                 -- CurrentInterval.estimate inverted
    renewables_pct      REAL
);
"""


class TelemetryStore:
    """DuckDB-backed telemetry persistence with a write-ahead buffer.

    Writes are buffered: if a write fails (disk full, lock contention,
    transient corruption), the row stays in memory and is retried on the
    next write call. Buffer overflow drops the oldest rows and emits a
    warning event (data is gone but the service stays up).
    """

    def __init__(
        self,
        config: StorageConfig,
        max_buffer: int = 500,
    ) -> None:
        db_path = Path(config.db_path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = duckdb.connect(str(db_path))
        self._max_buffer = max_buffer
        # Pending rows that haven't been successfully written yet.
        self._pending_telemetry: deque[TelemetryRow] = deque()
        self._pending_load: deque[LoadTelemetryRow] = deque()
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.execute(TELEMETRY_DDL)
        self._db.execute(LOAD_TELEMETRY_DDL)
        self._db.execute(PV_FORECAST_LOG_DDL)
        self._db.execute(PRICE_FORECAST_LOG_DDL)
        # Apply migrations for installs that predate any added columns.
        for stmt in TELEMETRY_MIGRATIONS:
            try:
                self._db.execute(stmt)
            except Exception:
                logger.exception("Migration failed: %s", stmt)
        logger.info("DuckDB tables initialised (schema v%d)", CURRENT_SCHEMA_VERSION)

    def close(self) -> None:
        # Final flush attempt on close — best-effort.
        try:
            self._flush_telemetry()
            self._flush_load()
        except Exception:
            logger.exception("Final flush during close failed")
        self._db.close()

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        return self._db

    @property
    def pending_count(self) -> tuple[int, int]:
        """Diagnostics: (telemetry_pending, load_telemetry_pending)."""
        return len(self._pending_telemetry), len(self._pending_load)

    # ── Writes (buffered) ────────────────────────────────────────

    def write_telemetry(self, row: TelemetryRow) -> None:
        """Buffer a telemetry row and try to flush the buffer."""
        self._pending_telemetry.append(row)
        self._enforce_buffer_cap(self._pending_telemetry, "telemetry")
        self._flush_telemetry()

    def write_load_telemetry(self, row: LoadTelemetryRow) -> None:
        """Buffer a load telemetry row and try to flush the buffer."""
        self._pending_load.append(row)
        self._enforce_buffer_cap(self._pending_load, "load_telemetry")
        self._flush_load()

    def _enforce_buffer_cap(self, buf: deque, name: str) -> None:
        if len(buf) > self._max_buffer:
            dropped = len(buf) - self._max_buffer
            for _ in range(dropped):
                buf.popleft()
            emit(
                EventType.VALIDATION_REJECT,
                {
                    "message": (
                        f"{name} write buffer overflow ({self._max_buffer} max), "
                        f"dropped {dropped} oldest rows"
                    ),
                    "table": name,
                    "dropped": dropped,
                },
            )

    def _flush_telemetry(self) -> None:
        while self._pending_telemetry:
            row = self._pending_telemetry[0]
            try:
                self._do_write_telemetry(row)
            except Exception:
                logger.warning(
                    "Telemetry write failed, %d rows pending",
                    len(self._pending_telemetry),
                )
                return  # stop trying — retry on next call
            self._pending_telemetry.popleft()

    def _flush_load(self) -> None:
        while self._pending_load:
            row = self._pending_load[0]
            try:
                self._do_write_load(row)
            except Exception:
                logger.warning(
                    "Load telemetry write failed, %d rows pending",
                    len(self._pending_load),
                )
                return
            self._pending_load.popleft()

    def _do_write_telemetry(self, row: TelemetryRow) -> None:
        self._db.execute(
            """INSERT INTO telemetry VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )""",
            [
                row.ts,
                row.soc_pct,
                row.battery_kw,
                row.pv_kw,
                row.grid_kw,
                row.grid_kw_shelly,
                row.house_load_kw,
                row.import_price,
                row.export_price,
                row.spot_price,
                row.renewables_pct,
                row.spike_status,
                row.pv_forecast_kw,
                row.outdoor_temp_c,
                row.occupied,
                row.ems_mode,
                row.planner_action,
                row.planner_reason,
                CURRENT_SCHEMA_VERSION,
            ],
        )

    def _do_write_load(self, row: LoadTelemetryRow) -> None:
        self._db.execute(
            "INSERT INTO load_telemetry VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                row.ts,
                row.load_id,
                row.category,
                row.power_kw,
                row.energy_today_kwh,
                row.cycle_state,
                row.relay_on,
                CURRENT_SCHEMA_VERSION,
            ],
        )

    def write_pv_forecast_log(self, rows: list[PVForecastLogRow]) -> None:
        """Append PV forecast rows. Best-effort: failures are logged and
        swallowed — this is observability, not critical path."""
        if not rows:
            return
        try:
            self._db.executemany(
                """INSERT INTO pv_forecast_log VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    [
                        r.fetched_at,
                        r.period_end,
                        r.pv_estimate_kw,
                        r.pv_estimate10_kw,
                        r.pv_estimate90_kw,
                        r.actual_kw,
                    ]
                    for r in rows
                ],
            )
        except Exception:
            logger.exception(
                "pv_forecast_log write failed (%d rows dropped)", len(rows),
            )

    def read_latest_pv_forecast(
        self, max_age_minutes: int = 60
    ) -> tuple[list[PVForecast], datetime] | None:
        """Load the most recent Solcast forecast from the log, if fresh.

        Returns (forecasts, fetched_at) for the most recent `fetched_at`
        batch if that batch is within `max_age_minutes` of now, else None.
        Used by the service on startup to seed the Solcast cache and
        skip the initial API call (Solcast has a hard 10/day quota).
        """
        try:
            latest_fetch = self._db.sql(
                "SELECT MAX(fetched_at) FROM pv_forecast_log"
            ).fetchone()
        except Exception:
            logger.exception("read_latest_pv_forecast failed")
            return None
        if not latest_fetch or latest_fetch[0] is None:
            return None
        fetched_at: datetime = latest_fetch[0]
        age_minutes = (datetime.now(fetched_at.tzinfo) - fetched_at).total_seconds() / 60
        if age_minutes > max_age_minutes:
            return None

        # Only keep intervals that haven't expired yet (period_end in the future).
        # A 60-min-old forecast still has 47+ hours of unexpired intervals.
        rows = self._db.sql(
            """SELECT period_end, pv_estimate_kw, pv_estimate10_kw, pv_estimate90_kw
               FROM pv_forecast_log
               WHERE fetched_at = ? AND period_end > ?
               ORDER BY period_end""",
            params=[fetched_at, datetime.now(fetched_at.tzinfo)],
        ).fetchall()
        if not rows:
            return None

        forecasts = [
            PVForecast(
                start=period_end - timedelta(minutes=30),
                end=period_end,
                pv_estimate_kw=p50,
                pv_estimate10_kw=p10,
                pv_estimate90_kw=p90,
            )
            for (period_end, p50, p10, p90) in rows
        ]
        return forecasts, fetched_at

    def write_price_forecast_log(self, rows: list[PriceForecastLogRow]) -> None:
        """Append price forecast rows. Best-effort: failures are logged
        and swallowed — this is observability, not critical path. Uses a
        single executemany for efficiency; if one row is malformed the
        whole batch rolls back but the service keeps running.
        """
        if not rows:
            return
        try:
            self._db.executemany(
                """INSERT INTO price_forecast_log VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                [
                    [
                        r.fetched_at,
                        r.resolution,
                        r.interval_start,
                        r.interval_end,
                        r.interval_type,
                        r.per_kwh,
                        r.export_per_kwh,
                        r.spot_per_kwh,
                        r.forecast_predicted,
                        r.forecast_low,
                        r.forecast_high,
                        r.spike_status,
                        r.descriptor,
                        r.is_locked,
                        r.renewables_pct,
                    ]
                    for r in rows
                ],
            )
        except Exception:
            logger.exception(
                "price_forecast_log write failed (%d rows dropped)", len(rows),
            )

    # ── Reads (analytical) ───────────────────────────────────────

    def get_rolling_p95(self, days: int = 7, as_of: datetime | None = None) -> float | None:
        """Get the P95 house load over the last N days.

        Excludes pre-fix data via schema_version filter — house_load_kw
        in v1 rows is wrong (PV register was a placeholder).
        """
        ref = as_of or datetime.now(UTC)
        days_int = int(days)  # belt-and-braces — DuckDB INTERVAL needs literal
        result = self._db.execute(
            f"""SELECT PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY house_load_kw)
            FROM telemetry
            WHERE house_load_kw IS NOT NULL
              AND schema_version >= ?
              AND ts > ?::TIMESTAMPTZ - INTERVAL {days_int} DAY""",
            [CURRENT_SCHEMA_VERSION, ref],
        ).fetchone()
        return result[0] if result and result[0] is not None else None

    def get_data_span_days(self) -> int:
        """Get the number of days of valid (post-fix) telemetry data."""
        result = self._db.execute(
            "SELECT DATEDIFF('day', MIN(ts), MAX(ts)) FROM telemetry WHERE schema_version >= ?",
            [CURRENT_SCHEMA_VERSION],
        ).fetchone()
        return result[0] if result and result[0] is not None else 0

    def get_valid_load_rows(self) -> int:
        """Count rows with valid house_load_kw (post-fix only)."""
        result = self._db.execute(
            "SELECT COUNT(*) FROM telemetry "
            "WHERE house_load_kw IS NOT NULL AND schema_version >= ?",
            [CURRENT_SCHEMA_VERSION],
        ).fetchone()
        return result[0] if result else 0

    def get_temp_buckets_seen(self) -> int:
        """Count distinct temperature buckets in valid (post-fix) data."""
        result = self._db.execute(
            """SELECT COUNT(DISTINCT CASE
                WHEN outdoor_temp_c < 10 THEN 'cold'
                WHEN outdoor_temp_c < 20 THEN 'mild'
                WHEN outdoor_temp_c < 30 THEN 'warm'
                ELSE 'hot'
            END)
            FROM telemetry
            WHERE outdoor_temp_c IS NOT NULL
              AND schema_version >= ?""",
            [CURRENT_SCHEMA_VERSION],
        ).fetchone()
        return result[0] if result else 0

    def get_load_profile_slots(
        self,
        temp_bucket: str | None = None,
        occupied: bool | None = None,
        weekday: bool | None = None,
        min_samples: int = 50,
        as_of: datetime | None = None,
    ) -> list[float] | None:
        """Query a load profile with optional context filters.

        Returns 48 slots (30-min averages) or None if insufficient data.
        Excludes pre-fix (schema_version < CURRENT) rows.
        """
        ref = as_of or datetime.now(UTC)
        conditions = [
            "house_load_kw IS NOT NULL",
            "schema_version >= ?",
            "ts > ?::TIMESTAMPTZ - INTERVAL '90 days'",
        ]
        params: list = [CURRENT_SCHEMA_VERSION, ref]

        if weekday is not None:
            if weekday:
                conditions.append("EXTRACT(DOW FROM ts) BETWEEN 1 AND 5")
            else:
                conditions.append("EXTRACT(DOW FROM ts) IN (0, 6)")

        if occupied is not None:
            conditions.append("occupied = ?")
            params.append(occupied)

        if temp_bucket is not None:
            bucket_map = {
                "cold": "outdoor_temp_c < 10",
                "mild": "outdoor_temp_c >= 10 AND outdoor_temp_c < 20",
                "warm": "outdoor_temp_c >= 20 AND outdoor_temp_c < 30",
                "hot": "outdoor_temp_c >= 30",
            }
            if temp_bucket in bucket_map:
                conditions.append(f"({bucket_map[temp_bucket]})")

        where = " AND ".join(conditions)

        # Check sample count
        count_result = self._db.execute(
            f"SELECT COUNT(*) FROM telemetry WHERE {where}", params
        ).fetchone()
        if not count_result or count_result[0] < min_samples:
            return None

        # Query 48-slot profile
        result = self._db.execute(
            f"""SELECT
                (EXTRACT(HOUR FROM ts) * 2 + EXTRACT(MINUTE FROM ts) / 30)::INT AS slot,
                AVG(house_load_kw) AS mean_kw
            FROM telemetry
            WHERE {where}
            GROUP BY slot
            ORDER BY slot""",
            params,
        ).fetchall()

        if len(result) < 24:  # Need at least half the slots
            return None

        # Fill missing slots with overall average
        slot_map = {int(r[0]): float(r[1]) for r in result}
        avg = sum(slot_map.values()) / len(slot_map) if slot_map else 2.0
        return [slot_map.get(i, avg) for i in range(48)]
