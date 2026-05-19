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
    AmberUsageRow,
    EventType,
    LoadTelemetryRow,
    PriceForecastLogRow,
    PVForecast,
    PVForecastLogRow,
    TelemetryRow,
    WeatherForecastLogRow,
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
    ts                     TIMESTAMPTZ NOT NULL,
    soc_pct                REAL,
    battery_kw             REAL,
    pv_kw                  REAL,
    grid_kw                REAL,
    grid_kw_shelly         REAL,
    house_load_kw          REAL,
    import_price           REAL,
    export_price           REAL,
    spot_price             REAL,
    renewables_pct         REAL,
    spike_status           VARCHAR,
    pv_forecast_kw         REAL,
    outdoor_temp_c         REAL,
    occupied               BOOLEAN,
    ems_mode               INTEGER,
    planner_action         VARCHAR,
    planner_reason         VARCHAR,
    schema_version         INTEGER,
    -- Extended inverter telemetry: purely observational, LP does not read
    -- these. Captured now so future backtests / models can use the history.
    soh_pct                REAL,
    cell_temp_avg_c        REAL,
    cell_temp_max_c        REAL,
    cell_temp_min_c        REAL,
    cell_volt_avg_v        REAL,
    pcs_temp_c             REAL,
    available_charge_kw    REAL,
    available_discharge_kw REAL,
    running_state          INTEGER,
    alarm1                 INTEGER,
    alarm2                 INTEGER,
    alarm3                 INTEGER,
    alarm4                 INTEGER,
    alarm5                 INTEGER,
    -- Lifetime energy counters: DOUBLE (float64) because REAL loses
    -- precision around 10^7 kWh.
    lifetime_pv_kwh        DOUBLE,
    lifetime_load_kwh      DOUBLE,
    lifetime_charge_kwh    DOUBLE,
    lifetime_discharge_kwh DOUBLE,
    lifetime_import_kwh    DOUBLE,
    lifetime_export_kwh    DOUBLE,
    mppt1_voltage_v        REAL,
    mppt1_current_a        REAL,
    mppt2_voltage_v        REAL,
    mppt2_current_a        REAL,
    mppt3_voltage_v        REAL,
    mppt3_current_a        REAL,
    mppt4_voltage_v        REAL,
    mppt4_current_a        REAL,
    grid_freq_hz           REAL,
    phase_a_voltage_v      REAL,
    phase_b_voltage_v      REAL,
    phase_c_voltage_v      REAL,
    remote_ems_mode        INTEGER
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
    # Extended inverter telemetry (2026-04). ADD COLUMN IF NOT EXISTS
    # leaves legacy rows with NULL, which is the correct representation
    # of "this field wasn't captured when that row was written."
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS soh_pct                REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS cell_temp_avg_c        REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS cell_temp_max_c        REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS cell_temp_min_c        REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS cell_volt_avg_v        REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS pcs_temp_c             REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS available_charge_kw    REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS available_discharge_kw REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS running_state          INTEGER",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS alarm1                 INTEGER",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS alarm2                 INTEGER",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS alarm3                 INTEGER",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS alarm4                 INTEGER",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS alarm5                 INTEGER",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS lifetime_pv_kwh        DOUBLE",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS lifetime_load_kwh      DOUBLE",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS lifetime_charge_kwh    DOUBLE",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS lifetime_discharge_kwh DOUBLE",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS lifetime_import_kwh    DOUBLE",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS lifetime_export_kwh    DOUBLE",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS mppt1_voltage_v        REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS mppt1_current_a        REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS mppt2_voltage_v        REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS mppt2_current_a        REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS mppt3_voltage_v        REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS mppt3_current_a        REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS mppt4_voltage_v        REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS mppt4_current_a        REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS grid_freq_hz           REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS phase_a_voltage_v      REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS phase_b_voltage_v      REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS phase_c_voltage_v      REAL",
    "ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS remote_ems_mode        INTEGER",
    # 2026-05-03: cell volt min/max registers (30622/30623) returned isError
    # on every read — firmware doesn't expose them. Two persistent failures
    # per tick polluted the err_count signal floor. Drop the columns; the
    # avg cell voltage is still captured.
    "ALTER TABLE telemetry DROP COLUMN IF EXISTS cell_volt_max_v",
    "ALTER TABLE telemetry DROP COLUMN IF EXISTS cell_volt_min_v",
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
    fetched_at                 TIMESTAMPTZ NOT NULL,
    resolution                 INTEGER NOT NULL,        -- 5 or 30
    interval_start             TIMESTAMPTZ NOT NULL,
    interval_end               TIMESTAMPTZ NOT NULL,
    interval_type              VARCHAR,                 -- Actual/Current/Forecast Interval
    per_kwh                    REAL,                    -- AEMO point estimate (general channel)
    export_per_kwh             REAL,                    -- feedIn revenue, sign-flipped
    spot_per_kwh               REAL,
    forecast_predicted         REAL,                    -- general.advancedPrice.predicted
    forecast_low               REAL,                    -- general.advancedPrice.low
    forecast_high              REAL,                    -- general.advancedPrice.high
    spike_status               VARCHAR,
    descriptor                 VARCHAR,
    is_locked                  BOOLEAN,                 -- CurrentInterval.estimate inverted
    renewables_pct             REAL,
    -- feedIn channel advancedPrice, sign-flipped at the parser boundary
    -- to the customer convention (positive = revenue from export). Same
    -- population pattern as the import-side fields: populated on
    -- ForecastInterval, NULL on Current/Actual. predicted is consumed
    -- by the LP cost objective; low/high are captured for future
    -- stochastic price scenarios (see KNOWN-ISSUES #24, currently
    -- unread).
    export_forecast_predicted  REAL,
    export_forecast_low        REAL,
    export_forecast_high       REAL
);
"""

# Migration for installs that pre-date the export advancedPrice columns
# (added 2026-04-28). DuckDB's `ADD COLUMN IF NOT EXISTS` makes these
# idempotent — safe to re-run on every startup.
PRICE_FORECAST_LOG_MIGRATIONS = [
    "ALTER TABLE price_forecast_log ADD COLUMN IF NOT EXISTS export_forecast_predicted REAL",
    "ALTER TABLE price_forecast_log ADD COLUMN IF NOT EXISTS export_forecast_low       REAL",
    "ALTER TABLE price_forecast_log ADD COLUMN IF NOT EXISTS export_forecast_high      REAL",
]

# Settled per-5-min usage from Amber's /usage endpoint. Each row is one
# billed interval on one channel; SUM(cost_cents) GROUP BY nem_date is
# the net bill for that day. Fetched once a day for the previous NEM day
# (and on startup for any missing days). PK = (ts, channel) so re-runs
# UPSERT cleanly — Amber occasionally re-publishes a day with refined
# `quality` flags.
AMBER_USAGE_DDL = """
CREATE TABLE IF NOT EXISTS amber_usage (
    ts                  TIMESTAMPTZ NOT NULL,
    nem_date            VARCHAR     NOT NULL,
    channel             VARCHAR     NOT NULL,
    kwh                 REAL,
    cost_cents          REAL,
    per_kwh_cents       REAL,
    spot_per_kwh_cents  REAL,
    renewables_pct      REAL,
    descriptor          VARCHAR,
    spike_status        VARCHAR,
    quality             VARCHAR,
    PRIMARY KEY (ts, channel)
);
"""

# Mirrors pv_forecast_log: every fetched interval logged with its
# fetched_at, so forecast evolution and calibration can be analysed
# downstream. Not consumed by the LP — strictly observational.
WEATHER_FORECAST_LOG_DDL = """
CREATE TABLE IF NOT EXISTS weather_forecast_log (
    fetched_at      TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    temp_c          REAL,
    apparent_temp_c REAL,
    humidity_pct    REAL,
    rain_chance_pct REAL,
    rain_mm         REAL,
    wind_kmh        REAL
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
        self._db.execute(WEATHER_FORECAST_LOG_DDL)
        self._db.execute(AMBER_USAGE_DDL)
        # Apply migrations for installs that predate any added columns.
        for stmt in (*TELEMETRY_MIGRATIONS, *PRICE_FORECAST_LOG_MIGRATIONS):
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
        # Named-column insert so adding columns to the DDL can't silently
        # misalign values. Order below must match the column list.
        self._db.execute(
            """INSERT INTO telemetry (
                ts, soc_pct, battery_kw, pv_kw, grid_kw, grid_kw_shelly,
                house_load_kw, import_price, export_price, spot_price,
                renewables_pct, spike_status, pv_forecast_kw, outdoor_temp_c,
                occupied, ems_mode, planner_action, planner_reason,
                schema_version,
                soh_pct, cell_temp_avg_c, cell_temp_max_c, cell_temp_min_c,
                cell_volt_avg_v, pcs_temp_c,
                available_charge_kw, available_discharge_kw,
                running_state, alarm1, alarm2, alarm3, alarm4, alarm5,
                lifetime_pv_kwh, lifetime_load_kwh,
                lifetime_charge_kwh, lifetime_discharge_kwh,
                lifetime_import_kwh, lifetime_export_kwh,
                mppt1_voltage_v, mppt1_current_a,
                mppt2_voltage_v, mppt2_current_a,
                mppt3_voltage_v, mppt3_current_a,
                mppt4_voltage_v, mppt4_current_a,
                grid_freq_hz,
                phase_a_voltage_v, phase_b_voltage_v, phase_c_voltage_v,
                remote_ems_mode
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?
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
                row.soh_pct,
                row.cell_temp_avg_c,
                row.cell_temp_max_c,
                row.cell_temp_min_c,
                row.cell_volt_avg_v,
                row.pcs_temp_c,
                row.available_charge_kw,
                row.available_discharge_kw,
                row.running_state,
                row.alarm1,
                row.alarm2,
                row.alarm3,
                row.alarm4,
                row.alarm5,
                row.lifetime_pv_kwh,
                row.lifetime_load_kwh,
                row.lifetime_charge_kwh,
                row.lifetime_discharge_kwh,
                row.lifetime_import_kwh,
                row.lifetime_export_kwh,
                row.mppt1_voltage_v,
                row.mppt1_current_a,
                row.mppt2_voltage_v,
                row.mppt2_current_a,
                row.mppt3_voltage_v,
                row.mppt3_current_a,
                row.mppt4_voltage_v,
                row.mppt4_current_a,
                row.grid_freq_hz,
                row.phase_a_voltage_v,
                row.phase_b_voltage_v,
                row.phase_c_voltage_v,
                row.remote_ems_mode,
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
                "pv_forecast_log write failed (%d rows dropped)",
                len(rows),
            )

    def write_weather_forecast_log(self, rows: list[WeatherForecastLogRow]) -> None:
        """Append BOM hourly forecast rows. Best-effort."""
        if not rows:
            return
        try:
            self._db.executemany(
                """INSERT INTO weather_forecast_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    [
                        r.fetched_at,
                        r.period_end,
                        r.temp_c,
                        r.apparent_temp_c,
                        r.humidity_pct,
                        r.rain_chance_pct,
                        r.rain_mm,
                        r.wind_kmh,
                    ]
                    for r in rows
                ],
            )
        except Exception:
            logger.exception(
                "weather_forecast_log write failed (%d rows dropped)",
                len(rows),
            )

    def update_pv_actuals(self, actuals: dict[datetime, float]) -> int:
        """Populate `pv_forecast_log.actual_kw` for every forecast row
        whose `period_end` matches a Solcast estimated-actual.

        Updates *all* rows for the given `period_end` (across every
        `fetched_at`), not just the latest. This lets analysts look at
        either "latest forecast vs actual" (for replay) or "how did the
        forecast evolve vs the truth" (for calibration) with a single
        table.

        Returns the number of rows touched. Best-effort: failures are
        logged and swallowed — this is observability, not critical path.
        """
        if not actuals:
            return 0
        try:
            pairs = [(kw, pe) for pe, kw in actuals.items()]
            # DuckDB's executemany returns None, so we count via a
            # follow-up query. The update uses period_end as the key;
            # actual_kw overwrites any prior value, which is intentional
            # — the freshest actuals estimate wins.
            self._db.executemany(
                """UPDATE pv_forecast_log
                   SET actual_kw = ?
                   WHERE period_end = ?""",
                pairs,
            )
            touched = self._db.sql(
                "SELECT COUNT(*) FROM pv_forecast_log WHERE actual_kw IS NOT NULL"
            ).fetchone()
            return int(touched[0]) if touched else 0
        except Exception:
            logger.exception(
                "update_pv_actuals failed (%d entries dropped)",
                len(actuals),
            )
            return 0

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
            latest_fetch = self._db.sql("SELECT MAX(fetched_at) FROM pv_forecast_log").fetchone()
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

    def write_amber_usage(self, rows: list[AmberUsageRow]) -> None:
        """UPSERT settled per-5-min Amber usage rows.

        Idempotent on (ts, channel) — the daily wake loop refetches the
        same day on startup if the recent backfill window overlaps, and
        Amber occasionally re-publishes a day once `quality` settles.
        Best-effort: failures are logged and swallowed (not critical
        path — billing data drives observability, not control).
        """
        if not rows:
            return
        try:
            self._db.executemany(
                """INSERT INTO amber_usage VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                ) ON CONFLICT (ts, channel) DO UPDATE SET
                    nem_date           = EXCLUDED.nem_date,
                    kwh                = EXCLUDED.kwh,
                    cost_cents         = EXCLUDED.cost_cents,
                    per_kwh_cents      = EXCLUDED.per_kwh_cents,
                    spot_per_kwh_cents = EXCLUDED.spot_per_kwh_cents,
                    renewables_pct     = EXCLUDED.renewables_pct,
                    descriptor         = EXCLUDED.descriptor,
                    spike_status       = EXCLUDED.spike_status,
                    quality            = EXCLUDED.quality""",
                [
                    [
                        r.ts,
                        r.nem_date,
                        r.channel,
                        r.kwh,
                        r.cost_cents,
                        r.per_kwh_cents,
                        r.spot_per_kwh_cents,
                        r.renewables_pct,
                        r.descriptor,
                        r.spike_status,
                        r.quality,
                    ]
                    for r in rows
                ],
            )
        except Exception:
            logger.exception(
                "amber_usage write failed (%d rows dropped)",
                len(rows),
            )

    def latest_amber_usage_date(self) -> str | None:
        """Return the most recent NEM date present in amber_usage, or None.

        Used by the daily wake loop's startup backfill: pull from
        latest+1 up to yesterday in one ≤7-day batch (Amber's max
        window). Returns None on empty table → caller backfills the
        configured number of recent days.
        """
        try:
            row = self._db.sql("SELECT MAX(nem_date) FROM amber_usage").fetchone()
        except Exception:
            logger.exception("latest_amber_usage_date failed")
            return None
        return row[0] if row and row[0] is not None else None

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
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                        r.export_forecast_predicted,
                        r.export_forecast_low,
                        r.export_forecast_high,
                    ]
                    for r in rows
                ],
            )
        except Exception:
            logger.exception(
                "price_forecast_log write failed (%d rows dropped)",
                len(rows),
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
        statistic: str = "mean",
    ) -> list[float] | None:
        """Query a load profile with optional context filters.

        Returns 48 slots of *non-managed* baseload — `house_load_kw`
        minus the sum of measured managed-load power at the same
        timestamp, floored at zero. The LP later adds its own forward
        managed-load plan back into the energy balance, so leaving
        managed loads in the profile would double-count them.

        `statistic` selects the per-slot aggregation:
          - "mean": AVG (default; matches historical behaviour).
          - "median": robust to single outlier days. Use to keep one
            heavy-load day from lifting the LP's expected baseline.

        Returns None if insufficient samples (< min_samples).
        Excludes pre-fix (schema_version < CURRENT) rows.
        """
        agg_fn = {"mean": "AVG", "median": "MEDIAN"}.get(statistic)
        if agg_fn is None:
            raise ValueError(f"unknown statistic {statistic!r}; expected 'mean' or 'median'")
        ref = as_of or datetime.now(UTC)
        conditions = [
            "t.house_load_kw IS NOT NULL",
            "t.schema_version >= ?",
            "t.ts > ?::TIMESTAMPTZ - INTERVAL '90 days'",
        ]
        params: list = [CURRENT_SCHEMA_VERSION, ref]

        if weekday is not None:
            if weekday:
                conditions.append("EXTRACT(DOW FROM t.ts) BETWEEN 1 AND 5")
            else:
                conditions.append("EXTRACT(DOW FROM t.ts) IN (0, 6)")

        if occupied is not None:
            conditions.append("t.occupied = ?")
            params.append(occupied)

        if temp_bucket is not None:
            bucket_map = {
                "cold": "t.outdoor_temp_c < 10",
                "mild": "t.outdoor_temp_c >= 10 AND t.outdoor_temp_c < 20",
                "warm": "t.outdoor_temp_c >= 20 AND t.outdoor_temp_c < 30",
                "hot": "t.outdoor_temp_c >= 30",
            }
            if temp_bucket in bucket_map:
                conditions.append(f"({bucket_map[temp_bucket]})")

        where = " AND ".join(conditions)

        # Check sample count
        count_result = self._db.execute(
            f"SELECT COUNT(*) FROM telemetry t WHERE {where}", params
        ).fetchone()
        if not count_result or count_result[0] < min_samples:
            return None

        # Per-ts sum of measured managed-load power, then JOIN onto
        # telemetry rows in the filtered window. LEFT JOIN + COALESCE
        # makes pre-managed-load history (empty load_telemetry) a no-op:
        # managed_kw resolves to 0 and the average collapses to the
        # historical house_load_kw — the previous behaviour.
        # GREATEST(...,0) guards against rare cases where the heat-pump
        # CT briefly reports more than the inverter's house derivation
        # (sign jitter, transient mis-reads).
        result = self._db.execute(
            f"""WITH managed_per_ts AS (
                SELECT ts, SUM(COALESCE(power_kw, 0.0)) AS managed_kw
                FROM load_telemetry
                GROUP BY ts
            )
            SELECT
                (EXTRACT(HOUR FROM t.ts) * 2 + EXTRACT(MINUTE FROM t.ts) / 30)::INT AS slot,
                {agg_fn}(GREATEST(t.house_load_kw - COALESCE(m.managed_kw, 0.0), 0.0)) AS stat_kw
            FROM telemetry t
            LEFT JOIN managed_per_ts m ON m.ts = t.ts
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
