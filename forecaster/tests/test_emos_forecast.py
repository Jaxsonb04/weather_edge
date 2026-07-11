"""Network-free test for the EMOS forecast artifact writer (Phase 2)."""

from __future__ import annotations

import sqlite3
import io
import json
from contextlib import redirect_stdout
from datetime import date, timedelta

from emos_forecast import (
    build_emos_archive,
    fetch_live_model_forecasts,
    main,
    serve_live_emos,
)
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


def test_build_emos_archive_roundtrip():
    conn = sqlite3.connect(":memory:")
    _seed(conn)

    written = build_emos_archive(conn, lead_days=1)
    assert written > 60  # most days past warm-up get an out-of-sample EMOS forecast

    loaded = conn.execute(
        "SELECT predicted_high_f, sigma_f FROM forecast_emos_daily_high "
        "WHERE lead_days = 1 AND station_id = 'KSFO'"
    ).fetchall()
    assert len(loaded) == written
    for mu, sigma in loaded:
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


def test_serve_live_emos_with_injected_forecasts(monkeypatch):
    import emos_forecast as ef

    conn = sqlite3.connect(":memory:")
    _seed(conn)  # 140 settled days, all strictly before the target
    target = date(2024, 6, 1)
    monkeypatch.setattr(ef, "_settlement_today", lambda city=ef.DEFAULT_CITY: target)
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


def test_serve_live_emos_allows_current_settlement_day_truth_row(monkeypatch):
    import emos_forecast as ef

    conn = sqlite3.connect(":memory:")
    _seed(conn)
    target = date(2024, 5, 19)
    monkeypatch.setattr(ef, "_settlement_today", lambda city=ef.DEFAULT_CITY: target)
    live = {"gfs_seamless": 71.0, "ecmwf_ifs025": 70.0, "ncep_nbm_conus": 72.0}

    result = serve_live_emos(
        conn,
        target,
        lead_days=1,
        store_lead_days=0,
        live_models=live,
        recalibrate=False,
    )

    assert result is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM forecast_emos_daily_high "
        "WHERE target_date=? AND lead_days=0 AND source='live'",
        (target.isoformat(),),
    ).fetchone()[0] == 1


def test_serve_live_emos_drops_models_unseen_in_training(monkeypatch):
    import emos_forecast as ef

    conn = sqlite3.connect(":memory:")
    _seed(conn)
    target = date(2024, 6, 1)
    monkeypatch.setattr(ef, "_settlement_today", lambda city=ef.DEFAULT_CITY: target)
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
    # fixed lead 1. The same-day target (lead 0) has no NWP archive of its own,
    # so it is FIT at lead 1 (the closest learned coefficients) but STORED at
    # its true lead 0; the next-day and 2-day-out markets fit and store at
    # leads 1 and 2 so each EMOS fit's per-model biases match its horizon.
    import emos_forecast as ef

    calls: list[tuple] = []

    def fake_serve(conn, target, *, lead_days=1, store_lead_days=None, **kwargs):
        calls.append((target, lead_days, store_lead_days))
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
        (today, 1, 0),
        (today + timedelta(days=1), 1, 1),
        (today + timedelta(days=2), 2, 2),
    ]
    assert "served=3 targets=3 cities=1 leads=0..2" in out.getvalue()


def test_serve_rolling_fetches_open_meteo_once_per_city(tmp_path, monkeypatch):
    import emos_forecast as ef
    import nwp_archive

    today = date(2026, 7, 10)
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            daily = {"time": [(today + timedelta(days=i)).isoformat() for i in range(3)]}
            for model in ef.NWP_MODELS:
                daily[f"temperature_2m_max_{model}"] = [70.0, 71.0, 72.0]
            return json.dumps({"daily": daily}).encode("utf-8")

    def fake_urlopen(url, **_kwargs):
        calls.append(url)
        return Response()

    served_models = []

    def fake_serve(_conn, _target, *, live_models=None, **_kwargs):
        served_models.append(live_models)
        return (70.0, 2.5)

    monkeypatch.setattr(nwp_archive, "urlopen", fake_urlopen)
    monkeypatch.setattr(ef, "serve_live_emos", fake_serve)
    monkeypatch.setattr(ef, "_settlement_today", lambda city=ef.DEFAULT_CITY: today)
    db_path = tmp_path / "weather.db"

    status = ef.main(["--db", str(db_path), "--serve-rolling", "--cities", "all"])

    assert status == 0
    assert len(calls) == len(ef.CITIES) == 15
    assert len(served_models) == 45
    for city_index in range(len(ef.CITIES)):
        city_targets = served_models[city_index * 3 : city_index * 3 + 3]
        assert [set(models.values()) for models in city_targets] == [
            {70.0},
            {71.0},
            {72.0},
        ]


def test_multi_forecast_fetch_missing_target_degrades_to_empty_helper(monkeypatch):
    import emos_forecast as ef

    target = date(2026, 7, 10)
    monkeypatch.setattr(
        ef,
        "_http_get_json",
        lambda _url: {
            "daily": {
                "time": [target.isoformat()],
                **{
                    f"temperature_2m_max_{model}": [70.0]
                    for model in ef.NWP_MODELS
                },
            }
        },
    )

    multi = ef.fetch_live_model_forecasts_multi()

    assert multi[target]
    assert fetch_live_model_forecasts(target + timedelta(days=1)) == {}


def test_multi_forecast_fetch_skips_malformed_day_and_keeps_partial_target(monkeypatch):
    import emos_forecast as ef

    target = date(2026, 7, 10)
    next_target = target + timedelta(days=1)
    first_model, second_model, *remaining_models = ef.NWP_MODELS
    monkeypatch.setattr(
        ef,
        "_http_get_json",
        lambda _url: {
            "daily": {
                "time": [target.isoformat(), "not-a-date", next_target.isoformat()],
                f"temperature_2m_max_{first_model}": [70.0, 999.0, 71.0],
                f"temperature_2m_max_{second_model}": [72.0, 999.0, None],
                **{
                    f"temperature_2m_max_{model}": [None, 999.0]
                    for model in remaining_models
                },
            }
        },
    )

    multi = ef.fetch_live_model_forecasts_multi()

    assert multi == {
        target: {first_model: 70.0, second_model: 72.0},
        next_target: {first_model: 71.0},
    }


def test_serve_live_emos_stores_lead0_row_with_lead1_fit(monkeypatch):
    import emos_forecast as ef

    conn = sqlite3.connect(":memory:")
    _seed(conn)  # lead-1 history: 140 settled days before the target
    target = date(2024, 6, 1)
    monkeypatch.setattr(ef, "_settlement_today", lambda city=ef.DEFAULT_CITY: target)
    live = {"gfs_seamless": 71.0, "ecmwf_ifs025": 70.0, "ncep_nbm_conus": 72.0}
    result = serve_live_emos(conn, target, lead_days=1, store_lead_days=0, live_models=live)
    assert result is not None
    row = conn.execute(
        "SELECT lead_days, predicted_high_f, sigma_f FROM forecast_emos_daily_high "
        "WHERE target_date = ? AND source = 'live'",
        (target.isoformat(),),
    ).fetchone()
    assert row is not None
    assert row[0] == 0  # stored at its true (same-day) lead
    assert abs(row[1] - result[0]) < 1e-9
    # Identical inputs at lead 1: same fit, same (mu, sigma), different key.
    lead1 = serve_live_emos(conn, target, lead_days=1, live_models=live)
    assert lead1 is not None
    assert abs(lead1[0] - result[0]) < 1e-9 and abs(lead1[1] - result[1]) < 1e-9


def test_serve_live_emos_applies_trailing_bias_recalibration(monkeypatch):
    import emos_forecast as ef

    conn = sqlite3.connect(":memory:")
    _seed(conn)  # settles 2024-01-01 .. 2024-05-19
    target = date(2024, 5, 20)  # serve date = 2024-05-19 at lead 1
    monkeypatch.setattr(ef, "_settlement_today", lambda city=ef.DEFAULT_CITY: target)
    # Rolling-origin record: a constant +2F warm error over the trailing
    # window (45 scored days ending 2024-05-18). cli_settlements was created
    # by the legacy-table migration inside _seed's first truth load.
    import city_truth
    from emos_forecast import ensure_schema as ensure_emos_schema

    city_truth.ensure_schema(conn)
    ensure_emos_schema(conn)
    start = date(2024, 4, 4)
    for i in range(45):
        day = (start + timedelta(days=i)).isoformat()
        truth = conn.execute(
            "SELECT max_temperature_f FROM cli_settlements WHERE station_id='KSFO' AND local_date=?",
            (day,),
        ).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO forecast_emos_daily_high "
            "(station_id, target_date, lead_days, predicted_high_f, sigma_f, n_models, "
            " model_spread_f, fetched_at, method, source, actual_high_f) "
            "VALUES ('KSFO', ?, 1, ?, 2.0, 3, 1.0, 'x', 'emos_wmean', 'rolling_origin', NULL)",
            (day, truth + 2.0),
        )
    conn.commit()

    live = {"gfs_seamless": 71.0, "ecmwf_ifs025": 70.0, "ncep_nbm_conus": 72.0}
    raw = serve_live_emos(conn, target, live_models=live, recalibrate=False)
    recal = serve_live_emos(conn, target, live_models=live, recalibrate=True)
    assert raw is not None and recal is not None
    # Constant error -> zero spread -> deadband subtracts nothing; the shrunk
    # correction is exactly 2.0 * 45/55 and sigma is untouched (bias-only).
    assert abs((raw[0] - recal[0]) - 2.0 * 45 / 55) < 1e-9
    assert abs(raw[1] - recal[1]) < 1e-9
