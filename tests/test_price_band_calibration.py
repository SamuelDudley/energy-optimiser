"""Tests for the Amber price-band calibration script.

Synthetic in-memory DuckDB seeded with rows of known hit-rate; assert
the script computes hit-rate, MAE, asymmetric breach split, and cross-
channel correlation correctly. Also a CLI smoke test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from optimiser.analysis.price_band_calibration import (
    compute_calibration,
    main,
    render_report,
)
from optimiser.store import PRICE_FORECAST_LOG_DDL, PRICE_FORECAST_LOG_MIGRATIONS

# ── Schema bootstrap ─────────────────────────────────────────────


@pytest.fixture
def conn() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with the production price_forecast_log schema.
    Re-uses the live DDL + migrations so any column drift between the
    test fixture and the production schema fails fast."""
    c = duckdb.connect(":memory:")
    c.execute(PRICE_FORECAST_LOG_DDL)
    for stmt in PRICE_FORECAST_LOG_MIGRATIONS:
        c.execute(stmt)
    yield c
    c.close()


def _insert_forecast(
    conn: duckdb.DuckDBPyConnection,
    *,
    fetched_at: datetime,
    interval_start: datetime,
    f_low: float | None = 22.0,
    f_pred: float | None = 24.0,
    f_high: float | None = 28.0,
    e_low: float | None = 5.0,
    e_pred: float | None = 6.5,
    e_high: float | None = 8.5,
    per_kwh: float = 0.0,
    export_per_kwh: float = 0.0,
) -> None:
    conn.execute(
        """INSERT INTO price_forecast_log VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )""",
        [
            fetched_at, 5,
            interval_start, interval_start + timedelta(minutes=5),
            "ForecastInterval",
            per_kwh, export_per_kwh, 9.0,
            f_pred, f_low, f_high,
            "none", "neutral", None, 40.0,
            e_pred, e_low, e_high,
        ],
    )


def _insert_actual(
    conn: duckdb.DuckDBPyConnection,
    *,
    interval_start: datetime,
    per_kwh: float,
    export_per_kwh: float,
) -> None:
    conn.execute(
        """INSERT INTO price_forecast_log VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )""",
        [
            interval_start + timedelta(minutes=10), 5,
            interval_start, interval_start + timedelta(minutes=5),
            "ActualInterval",
            per_kwh, export_per_kwh, 9.0,
            None, None, None,
            "none", "neutral", None, 40.0,
            None, None, None,
        ],
    )


# ── Hit-rate ─────────────────────────────────────────────────────


class TestHitRate:
    def test_hit_rate_70_percent_within_band(
        self, conn: duckdb.DuckDBPyConnection,
    ) -> None:
        """Seed 100 import-side intervals, 70 of which have realised in
        [low, high] = [22, 28], 20 above high (>28), 10 below low
        (<22). Expect within_band ≈ 70%, above_high ≈ 20%, below_low
        ≈ 10%, MAE = mean |realised − 24|.
        """
        # Lookahead bucket 0-6h: forecast issued 30 min before interval.
        anchor = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
        for i in range(70):
            interval = anchor + timedelta(hours=i)
            fetched = interval - timedelta(minutes=30)
            _insert_forecast(
                conn, fetched_at=fetched, interval_start=interval
            )
            _insert_actual(
                conn, interval_start=interval,
                per_kwh=23.0,  # in band
                export_per_kwh=6.0,  # in band
            )
        for i in range(70, 90):  # 20 above high
            interval = anchor + timedelta(hours=i)
            fetched = interval - timedelta(minutes=30)
            _insert_forecast(
                conn, fetched_at=fetched, interval_start=interval
            )
            _insert_actual(
                conn, interval_start=interval,
                per_kwh=30.0,  # > 28 high
                export_per_kwh=10.0,  # > 8.5 high
            )
        for i in range(90, 100):  # 10 below low
            interval = anchor + timedelta(hours=i)
            fetched = interval - timedelta(minutes=30)
            _insert_forecast(
                conn, fetched_at=fetched, interval_start=interval
            )
            _insert_actual(
                conn, interval_start=interval,
                per_kwh=20.0,  # < 22 low
                export_per_kwh=4.0,  # < 5 low
            )

        stats, _ = compute_calibration(conn, since=anchor - timedelta(days=1))
        # 0-6h bucket: import side
        b0 = next(
            s for s in stats if s.channel == "import" and s.bucket == "0-6h"
        )
        assert b0.n == 100
        assert b0.within_band_pct == pytest.approx(70.0, abs=0.5)
        assert b0.above_high_pct == pytest.approx(20.0, abs=0.5)
        assert b0.below_low_pct == pytest.approx(10.0, abs=0.5)
        # MAE: 70 × |23−24| + 20 × |30−24| + 10 × |20−24| = 70 + 120 + 40 = 230 / 100 = 2.30
        assert b0.mae_predicted == pytest.approx(2.30, abs=0.01)


# ── Lookahead stratification ─────────────────────────────────────


class TestLookaheadBuckets:
    def test_lookahead_bucket_assignment(
        self, conn: duckdb.DuckDBPyConnection,
    ) -> None:
        """Same realised price, three forecasts at different lookaheads.
        Each should fall into a distinct bucket."""
        anchor = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        # Forecast 30min before → 0-6h bucket
        _insert_forecast(
            conn, fetched_at=anchor - timedelta(minutes=30),
            interval_start=anchor,
        )
        # Forecast 8h before → 6-12h bucket
        _insert_forecast(
            conn, fetched_at=anchor - timedelta(hours=8),
            interval_start=anchor,
        )
        # Forecast 18h before → 12-24h bucket
        _insert_forecast(
            conn, fetched_at=anchor - timedelta(hours=18),
            interval_start=anchor,
        )
        _insert_actual(
            conn, interval_start=anchor,
            per_kwh=24.0, export_per_kwh=6.5,
        )

        stats, _ = compute_calibration(conn)
        ns = {(s.channel, s.bucket): s.n for s in stats}
        # Each bucket got exactly one forecast row from this single
        # interval. (Two channels: import + export; same n per bucket.)
        assert ns["import", "0-6h"] == 1
        assert ns["import", "6-12h"] == 1
        assert ns["import", "12-24h"] == 1
        assert ns["import", "24-36h"] == 0


# ── Empty / partial data handling ────────────────────────────────


class TestEmptyData:
    def test_no_data_returns_zero_n_with_none_metrics(
        self, conn: duckdb.DuckDBPyConnection,
    ) -> None:
        stats, corr = compute_calibration(conn)
        assert all(s.n == 0 for s in stats)
        assert all(s.mae_predicted is None for s in stats)
        assert corr.n == 0
        assert corr.correlation is None

    def test_only_forecasts_no_actuals(
        self, conn: duckdb.DuckDBPyConnection,
    ) -> None:
        anchor = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
        _insert_forecast(
            conn, fetched_at=anchor - timedelta(hours=1),
            interval_start=anchor,
        )
        # No matching ActualInterval row.
        stats, _ = compute_calibration(conn)
        assert all(s.n == 0 for s in stats)


# ── Cross-channel correlation ────────────────────────────────────


class TestCrossChannelCorrelation:
    def test_perfect_positive_correlation(
        self, conn: duckdb.DuckDBPyConnection,
    ) -> None:
        """If import and export residuals move identically (perfect
        NEM coupling), correlation should be ~+1.0."""
        anchor = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
        # Vary the realised price linearly; both channels move together.
        for i in range(30):
            interval = anchor + timedelta(hours=i)
            _insert_forecast(
                conn, fetched_at=interval - timedelta(minutes=30),
                interval_start=interval,
                f_pred=24.0, e_pred=6.0,
            )
            # realised − pred = +i for both → perfect correlation
            _insert_actual(
                conn, interval_start=interval,
                per_kwh=24.0 + i,
                export_per_kwh=6.0 + i,
            )

        _, corr = compute_calibration(conn)
        assert corr.n == 30
        assert corr.correlation == pytest.approx(1.0, abs=0.001)

    def test_independent_residuals(
        self, conn: duckdb.DuckDBPyConnection,
    ) -> None:
        """Anti-correlated residuals: when one is +1 the other is −1.
        Expected correlation ≈ −1.0."""
        anchor = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
        # 20 anti-correlated pairs.
        for i in range(20):
            interval = anchor + timedelta(hours=i)
            _insert_forecast(
                conn, fetched_at=interval - timedelta(minutes=30),
                interval_start=interval,
                f_pred=24.0, e_pred=6.0,
            )
            sign = 1 if i % 2 == 0 else -1
            _insert_actual(
                conn, interval_start=interval,
                per_kwh=24.0 + sign,
                export_per_kwh=6.0 - sign,  # opposite sign
            )

        _, corr = compute_calibration(conn)
        assert corr.n == 20
        assert corr.correlation == pytest.approx(-1.0, abs=0.001)

    def test_zero_correlation_when_one_channel_constant(
        self, conn: duckdb.DuckDBPyConnection,
    ) -> None:
        """If one channel has zero residual variance, correlation is
        undefined (degenerate); we return None rather than NaN."""
        anchor = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
        for i in range(10):
            interval = anchor + timedelta(hours=i)
            _insert_forecast(
                conn, fetched_at=interval - timedelta(minutes=30),
                interval_start=interval,
                f_pred=24.0, e_pred=6.0,
            )
            _insert_actual(
                conn, interval_start=interval,
                per_kwh=24.0 + i,
                export_per_kwh=6.0,  # zero residual variance
            )

        _, corr = compute_calibration(conn)
        assert corr.n == 10
        assert corr.correlation is None


# ── Report rendering ─────────────────────────────────────────────


class TestRenderReport:
    def test_report_well_formed(
        self, conn: duckdb.DuckDBPyConnection,
    ) -> None:
        """Smoke test on the markdown output: every bucket appears,
        the table header is present, and the correlation block doesn't
        crash on empty data."""
        stats, corr = compute_calibration(conn)
        text = render_report(stats, corr)
        assert "# Amber price-band calibration report" in text
        assert "| chan   | bucket | n" in text
        # All four lookahead buckets present in both channels.
        for bucket in ("0-6h", "6-12h", "12-24h", "24-36h"):
            for channel in ("import", "export"):
                assert f"| {channel:6s} | {bucket:6s} |" in text


# ── CLI smoke ────────────────────────────────────────────────────


class TestCLI:
    def test_missing_db_returns_error_code(self, tmp_path) -> None:
        # Database doesn't exist — main() should return non-zero rather
        # than crash.
        rc = main(["--db", str(tmp_path / "nonexistent.duckdb")])
        assert rc == 2

    def test_runs_against_seeded_db(
        self, tmp_path, capsys
    ) -> None:
        """End-to-end CLI: build a tiny on-disk DuckDB, run main(),
        confirm a non-empty markdown report lands on stdout."""
        db_path = tmp_path / "telemetry.duckdb"
        c = duckdb.connect(str(db_path))
        c.execute(PRICE_FORECAST_LOG_DDL)
        for stmt in PRICE_FORECAST_LOG_MIGRATIONS:
            c.execute(stmt)
        anchor = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
        # One forecast/actual pair so the report has at least one row.
        c.execute(
            """INSERT INTO price_forecast_log VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )""",
            [
                anchor - timedelta(minutes=30), 5,
                anchor, anchor + timedelta(minutes=5),
                "ForecastInterval",
                0.0, 0.0, 9.0,
                24.0, 22.0, 28.0, "none", "neutral", None, 40.0,
                6.5, 5.0, 8.5,
            ],
        )
        c.execute(
            """INSERT INTO price_forecast_log VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )""",
            [
                anchor + timedelta(minutes=10), 5,
                anchor, anchor + timedelta(minutes=5),
                "ActualInterval",
                23.5, 6.2, 9.0,
                None, None, None, "none", "neutral", None, 40.0,
                None, None, None,
            ],
        )
        c.close()

        rc = main(["--db", str(db_path)])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Amber price-band calibration report" in captured.out
        # The seeded interval falls into the 0-6h bucket on import side
        # (forecast 30 min before interval). MAE = |23.5-24.0| = 0.5.
        assert "0-6h" in captured.out
