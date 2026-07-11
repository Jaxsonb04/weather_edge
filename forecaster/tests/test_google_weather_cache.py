"""Network-free regression tests for google_weather_cache source-MOS path.

The 2026-07 outage: ``latest_scored_blend_rows`` omitted
``station_adjustment_f`` from its SELECT while ``_weighted_sources_for_row``
read it, so once enough clean scored days accumulated the whole refresh
crashed after the (quota-consuming) Google fetch. These tests pin the fixed
query shape and the fail-open behaviour of ``source_mos_corrections``.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import google_weather_cache as gwc


@contextmanager
def _tmp_weather_db(n_days=40):
    """chdir into a temp dir holding a weather.db with clean scored blend rows."""
    prev_cwd = os.getcwd()
    prev_cache = getattr(gwc.source_mos_corrections, "_cached", None)
    gwc.source_mos_corrections._cached = None
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            with sqlite3.connect("weather.db") as conn:
                gwc.create_blend_archive_table(conn)
                start = date(2026, 5, 1)
                for i in range(n_days):
                    target = start + timedelta(days=i)
                    fetched = datetime(
                        target.year, target.month, target.day, 18, 0, tzinfo=timezone.utc
                    ) - timedelta(days=1)
                    conn.execute(
                        """
                        INSERT INTO forecast_blend_daily_high (
                            fetched_at, target_date, method, predicted_high_f,
                            google_high_f, nws_high_f, open_meteo_high_f,
                            history_high_f, station_adjustment_f, details_json,
                            actual_high_f, abs_error_f
                        ) VALUES (?, ?, 'blend', ?, ?, ?, ?, ?, ?, '{}', ?, ?)
                        """,
                        (
                            fetched.isoformat(),
                            target.isoformat(),
                            65.0 + (i % 5),
                            64.0 + (i % 5),
                            66.0 + (i % 5),
                            65.5 + (i % 5),
                            63.0 + (i % 5),
                            0.5,
                            67.0 + (i % 5),
                            2.0,
                        ),
                    )
                conn.commit()
            yield
        finally:
            os.chdir(prev_cwd)
            gwc.source_mos_corrections._cached = prev_cache


def test_scored_blend_rows_include_station_adjustment():
    with _tmp_weather_db():
        rows = gwc.latest_scored_blend_rows()
        assert rows, "expected clean scored blend rows"
        for row in rows:
            assert "station_adjustment_f" in row.keys()
        # The crash site: must be readable for every eligible row.
        pred = gwc._weighted_sources_for_row(rows[0], corrections=None)
        assert pred is not None


def test_source_mos_corrections_do_not_crash_on_real_query_rows():
    with _tmp_weather_db():
        corrections, metadata = gwc.source_mos_corrections()
        assert isinstance(corrections, dict)
        # Whatever the holdout verdict, an internal failure must not be the reason.
        assert "failed" not in metadata.get("reason", "")


def test_source_mos_corrections_fail_open_on_internal_error():
    prev_cache = getattr(gwc.source_mos_corrections, "_cached", None)
    original = gwc.latest_scored_blend_rows

    def _boom():
        raise RuntimeError("synthetic failure")

    gwc.source_mos_corrections._cached = None
    gwc.latest_scored_blend_rows = _boom
    try:
        corrections, metadata = gwc.source_mos_corrections()
        assert corrections == {}
        assert metadata["mode"] == "disabled"
        assert "failed" in metadata["reason"]
    finally:
        gwc.latest_scored_blend_rows = original
        gwc.source_mos_corrections._cached = prev_cache


def test_missing_station_adjustment_column_degrades_to_null():
    """Older DBs without the column must still be queryable (NULL adjustment)."""
    prev_cwd = os.getcwd()
    prev_cache = getattr(gwc.source_mos_corrections, "_cached", None)
    gwc.source_mos_corrections._cached = None
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            with sqlite3.connect("weather.db") as conn:
                conn.execute(
                    """
                    CREATE TABLE forecast_blend_daily_high (
                        fetched_at TEXT NOT NULL,
                        target_date TEXT NOT NULL,
                        method TEXT NOT NULL,
                        predicted_high_f REAL NOT NULL,
                        google_high_f REAL,
                        nws_high_f REAL,
                        open_meteo_high_f REAL,
                        history_high_f REAL,
                        details_json TEXT,
                        actual_high_f REAL,
                        abs_error_f REAL,
                        PRIMARY KEY (fetched_at, target_date)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO forecast_blend_daily_high VALUES (
                        '2026-05-01T18:00:00+00:00', '2026-05-02', 'blend',
                        65.0, 64.0, 66.0, 65.5, 63.0, '{}', 67.0, 2.0
                    )
                    """
                )
                conn.commit()
            rows = gwc.latest_scored_blend_rows()
            assert rows
            assert rows[0]["station_adjustment_f"] is None
            pred = gwc._weighted_sources_for_row(rows[0], corrections=None)
            assert pred is not None
        finally:
            os.chdir(prev_cwd)
            gwc.source_mos_corrections._cached = prev_cache


def test_sfo_cli_refresh_marks_current_settlement_day_preliminary(monkeypatch):
    conn = sqlite3.connect(":memory:")
    current = date(2026, 7, 10)
    observed = datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(gwc, "fetch_recent_clisfo_settlements", lambda: {current: 68})
    monkeypatch.setattr(gwc.city_truth, "_utcnow", lambda: observed)

    assert gwc.refresh_clisfo_settlements(conn) == 1

    assert conn.execute("SELECT is_final FROM cli_settlements").fetchone()[0] == 0
    assert gwc.clisfo_high_for(conn, current.isoformat()) is None


def test_scoring_does_not_use_fallback_when_preliminary_cli_row_exists():
    conn = sqlite3.connect(":memory:")
    gwc.create_blend_archive_table(conn)
    gwc.city_truth.ensure_schema(conn)
    conn.execute(
        "INSERT INTO forecast_blend_daily_high "
        "(fetched_at, target_date, method, predicted_high_f) "
        "VALUES ('2026-07-09T18:00:00+00:00', '2026-07-10', 'blend', 70)"
    )
    conn.execute(
        "CREATE TABLE nws_daily_high_ground_truth "
        "(station_id TEXT, local_date TEXT, high_f REAL, is_complete INTEGER)"
    )
    conn.execute(
        "INSERT INTO nws_daily_high_ground_truth VALUES ('KSFO', '2026-07-10', 68, 1)"
    )
    gwc.city_truth.upsert_settlement(
        conn, "KSFO", "2026-07-10", 68, is_final=False
    )

    assert gwc.update_scores_for_table(conn, "forecast_blend_daily_high") == 0
    assert conn.execute(
        "SELECT actual_high_f FROM forecast_blend_daily_high"
    ).fetchone()[0] is None

    gwc.city_truth.upsert_settlement(
        conn, "KSFO", "2026-07-10", 71, is_final=True
    )
    assert gwc.update_scores_for_table(conn, "forecast_blend_daily_high") == 1
    assert conn.execute(
        "SELECT actual_high_f, truth_source FROM forecast_blend_daily_high"
    ).fetchone() == (71.0, "clisfo")


def test_adaptive_training_does_not_fallback_to_embedded_actual_for_preliminary_cli():
    prev_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            with sqlite3.connect("weather.db") as conn:
                gwc.create_blend_archive_table(conn)
                gwc.city_truth.ensure_schema(conn)
                conn.execute(
                    "INSERT INTO forecast_blend_daily_high "
                    "(fetched_at, target_date, method, predicted_high_f, details_json, "
                    "actual_high_f, abs_error_f, truth_source) VALUES "
                    "('2026-07-09T18:00:00+00:00', '2026-07-10', 'blend', 70, '{}', "
                    "68, 2, 'nws_daily')"
                )
                gwc.city_truth.upsert_settlement(
                    conn, "KSFO", "2026-07-10", 71, is_final=False
                )

            assert gwc.latest_scored_blend_rows() == []

            with sqlite3.connect("weather.db") as conn:
                gwc.city_truth.upsert_settlement(
                    conn, "KSFO", "2026-07-10", 71, is_final=True
                )
            rows = gwc.latest_scored_blend_rows()
            assert len(rows) == 1
            assert rows[0]["actual_high_f"] == 71.0
        finally:
            os.chdir(prev_cwd)
