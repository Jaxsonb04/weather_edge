"""Network-free test for the EMOS forecast artifact writer (Phase 2)."""

from __future__ import annotations

import sqlite3
import io
from contextlib import redirect_stdout
from datetime import date, timedelta

from emos_forecast import build_emos_archive, load_emos_archive, main, serve_live_emos
from nwp_archive import ensure_schema as ensure_nwp_schema
from nwp_archive import upsert_forecasts


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE clisfo_settlements "
        "(local_date TEXT PRIMARY KEY, max_temperature_f INTEGER, fetched_at TEXT, source TEXT)"
    )
    ensure_nwp_schema(conn)
    base = date(2024, 1, 1)
    rows = []
    for i in range(140):  # > EMOS warm-up (min_train=60)
        day = (base + timedelta(days=i)).isoformat()
        truth = 65 + (i % 9)
        conn.execute("INSERT INTO clisfo_settlements VALUES (?, ?, ?, ?)", (day, truth, "x", "t"))
        for model, offset in (("gfs_seamless", 1.0), ("ecmwf_ifs025", -1.0), ("ncep_nbm_conus", 0.0)):
            rows.append(("KSFO", day, model, 1, truth + offset, "x", "test"))
    upsert_forecasts(conn, rows)
    conn.commit()


def test_build_and_load_emos_archive_roundtrip():
    conn = sqlite3.connect(":memory:")
    _seed(conn)

    written = build_emos_archive(conn, lead_days=1)
    assert written > 60  # most days past warm-up get an out-of-sample EMOS forecast

    loaded = load_emos_archive(conn, lead_days=1)
    assert len(loaded) == written
    for mu, sigma in loaded.values():
        assert 60.0 < mu < 80.0
        assert sigma >= 1.5  # sigma respects the floor

    # Re-running is idempotent (same PK -> in-place), not duplicative.
    again = build_emos_archive(conn, lead_days=1)
    assert again == written
    assert conn.execute("SELECT COUNT(*) FROM forecast_emos_daily_high").fetchone()[0] == written

    # CLISFO truth is joined where available, for downstream scoring.
    scored = conn.execute(
        "SELECT COUNT(*) FROM forecast_emos_daily_high WHERE actual_high_f IS NOT NULL"
    ).fetchone()[0]
    assert scored == written


def test_load_emos_archive_missing_table_returns_empty():
    conn = sqlite3.connect(":memory:")
    assert load_emos_archive(conn, lead_days=1) == {}


def test_serve_live_emos_with_injected_forecasts():
    conn = sqlite3.connect(":memory:")
    _seed(conn)  # 140 settled days, all strictly before the target
    target = date(2024, 6, 1)
    live = {"gfs_seamless": 71.0, "ecmwf_ifs025": 70.0, "ncep_nbm_conus": 72.0}
    result = serve_live_emos(conn, target, live_models=live)
    assert result is not None
    mu, sigma = result
    assert 60.0 < mu < 85.0 and sigma >= 1.5
    # persisted under source='live' so it never collides with the rolling archive
    row = conn.execute(
        "SELECT predicted_high_f, source FROM forecast_emos_daily_high WHERE target_date = ? AND source = 'live'",
        (target.isoformat(),),
    ).fetchone()
    assert row is not None and abs(row[0] - mu) < 1e-9


def test_serve_live_emos_refuses_settled_target():
    conn = sqlite3.connect(":memory:")
    _seed(conn)  # settles 2024-01-01 .. 2024-05-19
    live = {"gfs_seamless": 70.0, "ecmwf_ifs025": 70.0, "ncep_nbm_conus": 70.0}
    assert serve_live_emos(conn, date(2024, 3, 1), live_models=live) is None  # already settled
    # no 'live' row written for a settled day -> rolling-origin archive uncontaminated
    assert conn.execute("SELECT COUNT(*) FROM forecast_emos_daily_high WHERE source='live'").fetchone()[0] == 0


def test_serve_live_emos_drops_models_unseen_in_training():
    conn = sqlite3.connect(":memory:")
    _seed(conn)
    target = date(2024, 6, 1)
    seen = {"gfs_seamless": 71.0, "ecmwf_ifs025": 70.0, "ncep_nbm_conus": 72.0}
    with_bogus = {**seen, "bogus_unseen_model": 200.0}
    a = serve_live_emos(conn, target, live_models=with_bogus)
    b = serve_live_emos(conn, target, live_models=seen)
    assert a is not None and b is not None
    assert abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9  # bogus dropped -> no skew


def test_serve_live_emos_returns_none_without_enough_history():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE clisfo_settlements "
        "(local_date TEXT PRIMARY KEY, max_temperature_f INTEGER, fetched_at TEXT, source TEXT)"
    )
    ensure_nwp_schema(conn)
    result = serve_live_emos(
        conn, date(2024, 1, 1),
        live_models={"gfs_seamless": 70.0, "ecmwf_ifs025": 70.0, "ncep_nbm_conus": 70.0},
    )
    assert result is None  # below EMOS warm-up


def test_serve_rolling_logs_zero_served_summary(tmp_path):
    db_path = tmp_path / "weather.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE clisfo_settlements "
            "(local_date TEXT PRIMARY KEY, max_temperature_f INTEGER, fetched_at TEXT, source TEXT)"
        )
    out = io.StringIO()

    with redirect_stdout(out):
        status = main(["--db", str(db_path), "--serve-rolling", "--cities", "sfo"])

    assert status == 0
    assert "live EMOS rolling summary: served=0 targets=3 cities=1 leads=0..2" in out.getvalue()


def test_serve_rolling_serves_each_target_at_its_true_lead(tmp_path, monkeypatch):
    # Regression: serve-rolling must serve today+offset at lead=offset, not a
    # fixed lead 1. The same-day target (lead 0) has no NWP archive and is never
    # sent to serve_live_emos; the next-day and 2-day-out markets are served at
    # leads 1 and 2 so each EMOS fit's per-model biases match its horizon.
    import emos_forecast as ef

    calls: list[tuple] = []

    def fake_serve(conn, target, *, lead_days=1, **kwargs):
        calls.append((target, lead_days))
        return (70.0, 3.0)

    monkeypatch.setattr(ef, "serve_live_emos", fake_serve)
    db_path = tmp_path / "weather.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE clisfo_settlements "
            "(local_date TEXT PRIMARY KEY, max_temperature_f INTEGER, fetched_at TEXT, source TEXT)"
        )

    out = io.StringIO()
    with redirect_stdout(out):
        status = ef.main(["--db", str(db_path), "--serve-rolling", "--cities", "sfo"])

    assert status == 0
    today = ef._settlement_today()
    assert calls == [
        (today + timedelta(days=1), 1),
        (today + timedelta(days=2), 2),
    ]
    assert "served=2 targets=3 cities=1 leads=0..2" in out.getvalue()
