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
import city_truth
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


def _seed_dispersed(conn: sqlite3.Connection) -> None:
    """Like ``_seed`` but with real irreducible error, so the fitted sigma sits
    well above SIGMA_FLOOR_F.

    ``_seed``'s members are truth +/- a constant, so once debiased they agree
    exactly, the EMOS residual variance is ~0 and every served sigma lands on
    the 1.5 F floor -- where a horizon rescale is invisible. Here the members
    track a signal that truth wanders around by a repeating +/-3 F, giving
    var_c ~ 4-5 and a served sigma ~2.2 F.
    """

    conn.execute(
        "CREATE TABLE clisfo_settlements "
        "(local_date TEXT PRIMARY KEY, max_temperature_f INTEGER, fetched_at TEXT, source TEXT)"
    )
    ensure_nwp_schema(conn)
    base = date(2024, 1, 1)
    wobble = (3, -3, 2, -2, 0)
    rows = []
    for i in range(140):
        day = (base + timedelta(days=i)).isoformat()
        signal = 65 + (i % 9)
        truth = signal + wobble[i % len(wobble)]
        conn.execute("INSERT INTO clisfo_settlements VALUES (?, ?, ?, ?)", (day, truth, "x", "t"))
        for model, offset in (("gfs_seamless", 1.0), ("ecmwf_ifs025", -1.0), ("ncep_nbm_conus", 0.0)):
            rows.append(("KSFO", day, model, 1, signal + offset, "x", "test"))
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
    assert conn.execute(
        "SELECT DISTINCT source FROM forecast_emos_daily_high"
    ).fetchall() == [("rolling_origin_v2",)]


def test_build_emos_archive_replays_truth_at_the_requested_lead(monkeypatch):
    import emos_forecast as ef

    conn = sqlite3.connect(":memory:")
    _seed(conn)
    captured: dict[str, int] = {}

    def fake_predictions(dates, truth, nwp, *, truth_lag_days=0, **kwargs):
        captured["truth_lag_days"] = truth_lag_days
        return {dates[-1]: (72.0, 2.0, 3.5)}

    monkeypatch.setattr(ef, "emos_ngr_predictions_with_spread", fake_predictions)

    assert build_emos_archive(conn, lead_days=1) == 1
    assert captured == {"truth_lag_days": 1}
    # the archived spread is the fit's own debiased range, not a raw re-derivation
    assert conn.execute(
        "SELECT source, predicted_high_f, sigma_f, model_spread_f FROM forecast_emos_daily_high"
    ).fetchall() == [("rolling_origin_v2", 72.0, 2.0, 3.5)]


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


def _mark_onboarded(db_path, stations) -> None:
    """Give each station one scored rolling-origin row, the durable trace that
    build_emos_archive leaves once a station has cleared EMOS_MIN_TRAIN."""

    import emos_forecast as ef

    with sqlite3.connect(db_path) as conn:
        ef.ensure_schema(conn)
        for station in stations:
            conn.execute(
                "INSERT OR REPLACE INTO forecast_emos_daily_high "
                "(station_id, target_date, lead_days, predicted_high_f, sigma_f, n_models, "
                "model_spread_f, fetched_at, method, source, actual_high_f) "
                "VALUES (?, '2026-01-01', 1, 60.0, 3.0, 8, 1.0, '2026-01-01T00:00:00+00:00', "
                "'emos_wmean', ?, 61.0)",
                (station, ef.DEFAULT_SOURCE),
            )


def _seed_scored_history(db_path, station, days, *, lead_days=1, start=date(2025, 1, 1)):
    """``days`` settlement days with CLI truth and a full NWP archive row."""

    import emos_forecast as ef
    import nwp_archive
    import city_truth

    with sqlite3.connect(db_path) as conn:
        nwp_archive.ensure_schema(conn)
        city_truth.ensure_schema(conn)
        for offset in range(days):
            day = (start + timedelta(days=offset)).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO cli_settlements "
                "(station_id, local_date, max_temperature_f, fetched_at, source) "
                "VALUES (?, ?, 70, '2026-01-01T00:00:00+00:00', 'test')",
                (station, day),
            )
            for model in ef.NWP_MODELS:
                conn.execute(
                    "INSERT OR REPLACE INTO nwp_model_forecasts "
                    "(station_id, target_date, model, lead_days, predicted_high_f, "
                    "fetched_at, source) VALUES (?, ?, ?, ?, 70.0, "
                    "'2026-01-01T00:00:00+00:00', 'test')",
                    (station, day, model, lead_days),
                )


def test_serve_rolling_logs_zero_served_summary(tmp_path):
    # A station with no history and no EMOS row of any source is a registry
    # row awaiting its onboarding backfill, not a target -- but a serve that
    # serves NOTHING is still a failure.
    db_path = tmp_path / "weather.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE clisfo_settlements "
            "(local_date TEXT PRIMARY KEY, max_temperature_f INTEGER, fetched_at TEXT, source TEXT)"
        )
    out = io.StringIO()

    with redirect_stdout(out):
        status = main(["--db", str(db_path), "--serve-rolling", "--cities", "sfo"])

    assert status == 1
    assert "live EMOS [sfo] awaiting onboarding backfill: 0/60 scored lead-1 days" in out.getvalue()
    assert (
        "live EMOS rolling summary: served=0 targets=0 cities=1 leads=0..2 awaiting=1"
        in out.getvalue()
    )


def test_scored_history_days_counts_truth_matched_full_archive_days(tmp_path):
    import emos_forecast as ef

    db_path = tmp_path / "weather.db"
    _seed_scored_history(db_path, "KLAS", 7)
    # A lead-2 archive day does not count toward the lead-1 history.
    _seed_scored_history(db_path, "KLAS", 1, lead_days=2, start=date(2025, 3, 1))
    with sqlite3.connect(db_path) as conn:
        # A day with truth but a thin archive (below MIN_MODELS) does not count.
        conn.execute(
            "INSERT INTO cli_settlements (station_id, local_date, max_temperature_f, "
            "fetched_at, source) VALUES ('KLAS', '2025-02-01', 70, 'x', 'test')"
        )
        conn.execute(
            "INSERT INTO nwp_model_forecasts (station_id, target_date, model, lead_days, "
            "predicted_high_f, fetched_at, source) VALUES "
            "('KLAS', '2025-02-01', 'gfs_seamless', 1, 70.0, 'x', 'test')"
        )
        assert ef.scored_history_days(conn, ef.get_city("lv"), lead_days=1) == 7
        assert ef.scored_history_days(conn, ef.get_city("lv"), lead_days=2) == 1
        assert ef.scored_history_days(conn, ef.get_city("sfo"), lead_days=1) == 0


def test_awaiting_onboarding_distinguishes_new_station_from_outage(tmp_path):
    import emos_forecast as ef

    db_path = tmp_path / "weather.db"
    lv = ef.get_city("lv")
    with sqlite3.connect(db_path) as conn:
        # Fresh registry row: no history, no EMOS row -> awaiting (0 days).
        assert ef.awaiting_onboarding(conn, lv) == 0
    _seed_scored_history(db_path, "KLAS", ef.EMOS_MIN_TRAIN - 1)
    with sqlite3.connect(db_path) as conn:
        # Thin history, still never scored or served -> awaiting (59 days).
        assert ef.awaiting_onboarding(conn, lv) == ef.EMOS_MIN_TRAIN - 1
    _seed_scored_history(db_path, "KLAS", 1, start=date(2025, 6, 1))
    with sqlite3.connect(db_path) as conn:
        # Enough history to serve, even before any row exists -> a target.
        assert ef.awaiting_onboarding(conn, lv) is None
    # Onboarded (one rolling-origin row) but history since lost -> an outage,
    # never "awaiting onboarding".
    db_path2 = tmp_path / "weather2.db"
    _mark_onboarded(db_path2, ["KLAS"])
    with sqlite3.connect(db_path2) as conn:
        assert ef.awaiting_onboarding(conn, lv) is None


def test_serve_rolling_excludes_never_onboarded_city_from_health(tmp_path, monkeypatch):
    # The deploy-day scenario: an onboarded city serves, a freshly registered
    # one has nothing yet. The serve must succeed (exit 0) and report the new
    # city as awaiting, not as an outage.
    import emos_forecast as ef

    calls: list[str] = []

    def fake_serve(conn, target, *, city=ef.DEFAULT_CITY, **kwargs):
        calls.append(city.slug)
        return (70.0, 3.0)

    monkeypatch.setattr(ef, "serve_live_emos", fake_serve)
    monkeypatch.setattr(ef, "fetch_live_model_forecasts_multi", lambda **_kw: {})
    db_path = tmp_path / "weather.db"
    _mark_onboarded(db_path, ["KSFO"])

    out = io.StringIO()
    with redirect_stdout(out):
        status = ef.main(["--db", str(db_path), "--serve-rolling", "--cities", "sfo,lv"])

    assert status == 0
    assert calls == ["sfo", "sfo", "sfo"]
    text = out.getvalue()
    assert "live EMOS [lv] awaiting onboarding backfill: 0/60 scored lead-1 days" in text
    assert "served=3 targets=3 cities=2 leads=0..2 awaiting=1" in text


def test_serve_rolling_twenty_cities_with_five_unbackfilled_is_healthy(tmp_path, monkeypatch):
    # Regression for the 2026-09-13 expansion review: with the five new
    # stations thin, `--cities all` exited 1 (45/60 served) and turned every
    # sfo-forecaster-refresh tick red until the deep backfill landed.
    import emos_forecast as ef

    new_slugs = {"lv", "min", "satx", "nola", "dc"}
    onboarded = [c.nws_station_id for c in ef.CITIES if c.slug not in new_slugs]
    assert len(onboarded) == 15

    served_slugs: list[str] = []

    def fake_serve(conn, target, *, city=ef.DEFAULT_CITY, **kwargs):
        served_slugs.append(city.slug)
        return (70.0, 3.0)

    monkeypatch.setattr(ef, "serve_live_emos", fake_serve)
    monkeypatch.setattr(ef, "fetch_live_model_forecasts_multi", lambda **_kw: {})
    db_path = tmp_path / "weather.db"
    _mark_onboarded(db_path, onboarded)

    out = io.StringIO()
    with redirect_stdout(out):
        status = ef.main(["--db", str(db_path), "--serve-rolling", "--cities", "all"])

    assert status == 0
    assert len(served_slugs) == 45
    assert set(served_slugs).isdisjoint(new_slugs)
    assert "served=45 targets=45 cities=20 leads=0..2 awaiting=5" in out.getvalue()


def test_serve_rolling_real_serve_path_with_one_fresh_city_exits_zero(tmp_path, monkeypatch):
    # No fake serve: SFO has 70 scored days at leads 1 and 2 and really serves
    # three live rows; LV is a fresh registry row. The scheduled all-city serve
    # (what sfo-forecaster-refresh runs) must exit 0 and write SFO's rows.
    import emos_forecast as ef

    db_path = tmp_path / "weather.db"
    _seed_scored_history(db_path, "KSFO", 70, lead_days=1)
    _seed_scored_history(db_path, "KSFO", 70, lead_days=2)
    today = ef._settlement_today(ef.get_city("sfo"))

    def fake_multi(*, city=ef.DEFAULT_CITY, models=ef.NWP_MODELS):
        return {
            today + timedelta(days=k): {m: 65.0 + 0.2 * j for j, m in enumerate(models)}
            for k in range(ef.ROLLING_SERVE_DAYS)
        }

    monkeypatch.setattr(ef, "fetch_live_model_forecasts_multi", fake_multi)

    out = io.StringIO()
    with redirect_stdout(out):
        status = ef.main(["--db", str(db_path), "--serve-rolling", "--cities", "sfo,lv"])

    assert status == 0
    assert "served=3 targets=3 cities=2 leads=0..2 awaiting=1" in out.getvalue()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT station_id, COUNT(*) FROM forecast_emos_daily_high "
            "WHERE source = 'live' GROUP BY station_id"
        ).fetchall()
    assert rows == [("KSFO", 3)]


def test_serve_rolling_previously_onboarded_city_below_floor_is_an_outage(tmp_path, monkeypatch):
    # A station that has an EMOS row (was scored/served before) but now lacks
    # the training floor is a real outage: it stays a target and fails the run.
    import emos_forecast as ef

    monkeypatch.setattr(ef, "fetch_live_model_forecasts_multi", lambda **_kw: {})
    db_path = tmp_path / "weather.db"
    _mark_onboarded(db_path, ["KLAS"])

    out = io.StringIO()
    with redirect_stdout(out):
        status = ef.main(["--db", str(db_path), "--serve-rolling", "--cities", "lv"])

    assert status == 1
    text = out.getvalue()
    assert "awaiting onboarding" not in text
    assert "unavailable (already settled or thin coverage)" in text
    assert "served=0 targets=3 cities=1 leads=0..2 awaiting=0" in text


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
    _mark_onboarded(db_path, ["KSFO"])

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


def test_serve_rolling_fails_when_any_requested_target_is_unserved(tmp_path, monkeypatch):
    import emos_forecast as ef

    calls = 0

    def partial_serve(*args, **kwargs):
        nonlocal calls
        calls += 1
        return None if calls == 2 else (70.0, 3.0)

    monkeypatch.setattr(ef, "serve_live_emos", partial_serve)
    db_path = tmp_path / "weather.db"
    _mark_onboarded(db_path, ["KSFO"])

    out = io.StringIO()
    with redirect_stdout(out):
        status = ef.main(["--db", str(db_path), "--serve-rolling", "--cities", "sfo"])

    assert status == 1
    assert "served=2 targets=3 cities=1 leads=0..2" in out.getvalue()


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
    _mark_onboarded(db_path, [city.nws_station_id for city in ef.CITIES])

    status = ef.main(["--db", str(db_path), "--serve-rolling", "--cities", "all"])

    assert status == 0
    assert len(calls) == len(ef.CITIES) == 20
    assert len(served_models) == 60
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
    # Dispersed fixture on purpose: with _seed the served sigma lands on the
    # 1.5 F floor, and a floor-clamped serve cannot distinguish a working
    # horizon rescale from a no-op. Monkeypatching ef.SIGMA_FLOOR_F would not
    # fix that -- postproc_models.apply_emos imports its own copy and floors
    # first -- so the fixture, not the floor, has to carry the dispersion.
    _seed_dispersed(conn)  # lead-1 history: 140 settled days before the target
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
    assert abs(row[2] - result[1]) < 1e-9
    # Identical inputs at lead 1: same fit, so the same MEAN -- but FC-2, the
    # same-day serve must NOT inherit the day-ahead dispersion.
    lead1 = serve_live_emos(conn, target, lead_days=1, live_models=live)
    assert lead1 is not None
    assert abs(lead1[0] - result[0]) < 1e-9
    scale = ef._borrowed_lead_sigma_scale(ef.DEFAULT_CITY.nws_station_id, 0, 1)
    assert scale < 1.0
    # The production floor is in force here: assert it is NOT what produced the
    # difference, or this test silently stops covering FC-2.
    assert lead1[1] > ef.SIGMA_FLOOR_F
    assert result[1] > ef.SIGMA_FLOOR_F
    assert result[1] < lead1[1]
    assert abs(result[1] - lead1[1] * scale) < 1e-9


def test_lead0_sigma_rescale_is_bounded_and_only_applies_to_a_borrowed_fit():
    import emos_forecast as ef

    station = ef.DEFAULT_CITY.nws_station_id
    # A serve fit at its own lead is untouched, in either direction.
    assert ef._borrowed_lead_sigma_scale(station, 1, 1) == 1.0
    assert ef._borrowed_lead_sigma_scale(station, 2, 2) == 1.0
    assert ef._borrowed_lead_sigma_scale(station, 0, 0) == 1.0
    # Only the same-day serve borrows, and it only ever sharpens.
    scale = ef._borrowed_lead_sigma_scale(station, 0, 1)
    low, high = ef.LEAD0_SIGMA_SCALE_BOUNDS
    assert low <= scale <= high < 1.0 + 1e-12
    assert scale == ef.LEAD0_SIGMA_SCALE_BY_STATION[station]


def test_lead0_sigma_scale_never_sharpens_an_already_confident_station():
    """The pooled constant this replaced pushed KOKC/KBOS/KHOU/KNYC/KSFO from
    mildly over-confident into materially over-confident. A station measured at
    or past calibration must come out of the table as an exact identity, and no
    station may be widened."""

    import emos_forecast as ef

    low, high = ef.LEAD0_SIGMA_SCALE_BOUNDS
    assert high == 1.0
    for station, scale in ef.LEAD0_SIGMA_SCALE_BY_STATION.items():
        assert 0.0 < scale <= 1.0, station
        effective = ef._borrowed_lead_sigma_scale(station, 0, 1)
        assert low <= effective <= 1.0, station
    # Stations whose measured same-day z^2 was already >= 1 are exact no-ops.
    for station in ("KBOS", "KHOU", "KOKC"):
        assert ef._borrowed_lead_sigma_scale(station, 0, 1) == 1.0
    # An unmeasured station keeps the borrowed width (over-dispersed, under-sized).
    assert ef._borrowed_lead_sigma_scale("KZZZ", 0, 1) == ef.LEAD0_SIGMA_SCALE_DEFAULT
    assert ef.LEAD0_SIGMA_SCALE_DEFAULT == 1.0


def test_lead0_sigma_rescale_respects_the_sigma_floor(monkeypatch):
    """The horizon correction may sharpen the same-day Gaussian but not past
    the absolute floor every other serve path respects."""

    import emos_forecast as ef

    conn = sqlite3.connect(":memory:")
    _seed(conn)  # agreeing members -> the fit lands on the floor by itself
    target = date(2024, 6, 1)
    monkeypatch.setattr(ef, "_settlement_today", lambda city=ef.DEFAULT_CITY: target)
    live = {"gfs_seamless": 71.0, "ecmwf_ifs025": 70.0, "ncep_nbm_conus": 72.0}
    result = serve_live_emos(conn, target, lead_days=1, store_lead_days=0, live_models=live)
    assert result is not None
    assert result[1] >= ef.SIGMA_FLOOR_F
    # ... and on this fixture the floor is exactly what binds: the same-day serve
    # is a no-op against the lead-1 serve, which is what production sees on the
    # ~34% of same-day serves that sit on the floor.
    lead1 = serve_live_emos(conn, target, lead_days=1, live_models=live)
    assert lead1 is not None
    assert abs(result[1] - lead1[1]) < 1e-12
    assert abs(result[1] - ef.SIGMA_FLOOR_F) < 1e-12


def test_model_spread_is_invariant_to_a_pure_model_bias():
    """FC-1: the published disagreement statistic is taken over debiased
    members, so a chronically offset member does not read as an uncertain day."""

    import emos_forecast as ef

    forecasts = {"gfs_seamless": 71.0, "ecmwf_ifs025": 70.0, "ncep_nbm_conus": 72.0}
    biases = {"gfs_seamless": 1.0, "ecmwf_ifs025": 0.0, "ncep_nbm_conus": 2.0}
    spread = ef._model_spread_f(forecasts, biases)
    assert spread == 0.0  # 70 / 70 / 70 once corrected
    # Shift one member by a constant that is also in its bias: nothing moves.
    assert ef._model_spread_f(
        {**forecasts, "ecmwf_ifs025": 58.0}, {**biases, "ecmwf_ifs025": -12.0}
    ) == spread
    # Left uncorrected, that same 12F offset reads as a 14F "uncertain day":
    # the raw range (14F) and the debiased range (12F) are different statistics.
    assert ef._model_spread_f({**forecasts, "ecmwf_ifs025": 58.0}, biases) == 12.0


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


def test_apple_nudge_is_fixed_capped_recorded_and_scored(monkeypatch):
    import emos_forecast as ef

    conn = sqlite3.connect(":memory:")
    _seed(conn)
    target = date(2024, 6, 1)
    monkeypatch.setattr(ef, "_settlement_today", lambda city=ef.DEFAULT_CITY: target - timedelta(days=1))
    live = {"gfs_seamless": 71.0, "ecmwf_ifs025": 70.0, "ncep_nbm_conus": 72.0}
    base, sigma = serve_live_emos(conn, target, live_models=live, recalibrate=False)

    # A 7F+ disagreement leaves the forecast alone; a 4F gap moves it 50% (2.0F).
    blocked, _ = serve_live_emos(conn, target, live_models=live, recalibrate=False, apple_high_f=base + 20)
    nudged, nudged_sigma = serve_live_emos(conn, target, live_models=live, recalibrate=False, apple_high_f=base + 4)
    assert blocked == base
    assert abs(nudged - (base + 2.0)) < 1e-9 and nudged_sigma == sigma
    # Once the target is today (e.g. the run crossed midnight), Apple is ignored.
    monkeypatch.setattr(ef, "_settlement_today", lambda city=ef.DEFAULT_CITY: target)
    same_day, _ = serve_live_emos(conn, target, live_models=live, recalibrate=False, apple_high_f=base + 4)
    assert same_day == base
    assert ef.apple_weatherkit.apple_nudge_f(70.0, float("nan")) is None  # bad input never moves mu

    truth = round(base) + 2
    city_truth.upsert_settlement(conn, "KSFO", target.isoformat(), truth)
    (row,) = ef.apple_score(conn).values()
    # One station-day; its two Apple vintages (skipped, nudged) are averaged.
    assert row["n"] == 1 and row["applied"] == 1
    assert abs(row["mae_without"] - abs(base - truth)) < 1e-9
    assert abs(row["mae_with"] - (abs(base - truth) + abs(base + 2.0 - truth)) / 2) < 1e-9


def test_serve_rolling_passes_apple_highs_and_survives_failure(tmp_path, monkeypatch):
    import apple_weatherkit
    import emos_forecast as ef

    calls: list[tuple] = []
    monkeypatch.setattr(
        ef, "serve_live_emos",
        lambda conn, target, *, store_lead_days=None, apple_high_f=None, **kw: (
            calls.append((store_lead_days, apple_high_f)) or (70.0, 3.0)
        ),
    )
    monkeypatch.setattr(apple_weatherkit, "client_from_env", lambda **kw: object())
    monkeypatch.setattr(ef, "fetch_live_model_forecasts_multi", lambda **_kw: {})
    db_path = tmp_path / "weather.db"
    _mark_onboarded(db_path, ["KSFO"])
    today = ef._settlement_today()
    highs = {(today + timedelta(days=d)).isoformat(): 80.0 + d for d in range(3)}

    monkeypatch.setattr(apple_weatherkit, "live_highs", lambda client, city: highs)
    out = io.StringIO()
    with redirect_stdout(out):
        assert ef.main(["--db", str(db_path), "--serve-rolling", "--cities", "sfo"]) == 0
    assert calls == [(0, 80.0), (1, 81.0), (2, 82.0)]  # serve_live_emos drops same-day
    assert "apple_cities=1" in out.getvalue()

    def fail(client, city):
        raise apple_weatherkit.WeatherKitError("down")

    calls.clear()
    monkeypatch.setattr(apple_weatherkit, "live_highs", fail)
    with redirect_stdout(io.StringIO()):
        assert ef.main(["--db", str(db_path), "--serve-rolling", "--cities", "sfo"]) == 0
    assert calls == [(0, None), (1, None), (2, None)]


def test_apple_budget_counts_only_apple_time_not_nwp_time(tmp_path, monkeypatch):
    import apple_weatherkit
    import emos_forecast as ef

    clock = [0.0]
    monkeypatch.setattr(ef.time, "monotonic", lambda: clock[0])

    def slow_nwp(**_kw):
        clock[0] += 10_000  # far beyond the Apple budget
        return {}

    calls: list[float | None] = []
    monkeypatch.setattr(
        ef, "serve_live_emos",
        lambda conn, target, *, apple_high_f=None, **kw: calls.append(apple_high_f) or (70.0, 3.0),
    )
    monkeypatch.setattr(apple_weatherkit, "client_from_env", lambda **kw: object())
    monkeypatch.setattr(ef, "fetch_live_model_forecasts_multi", slow_nwp)
    monkeypatch.setattr(apple_weatherkit, "live_highs", lambda client, city: {
        (ef._settlement_today() + timedelta(days=1)).isoformat(): 80.0
    })
    db_path = tmp_path / "weather.db"
    _mark_onboarded(db_path, ["KSFO"])
    with redirect_stdout(io.StringIO()):
        assert ef.main(["--db", str(db_path), "--serve-rolling", "--cities", "sfo"]) == 0
    assert 80.0 in calls

# --- evening carry-over -----------------------------------------------------

CARRY_TARGET = date(2024, 6, 1)  # next day; "today" is 2024-05-31 (KSFO, UTC-8)
CARRY_NOW = "2024-06-01T02:00:00+00:00"  # 18:00 local standard time on 2024-05-31
LIVE = {"gfs_seamless": 71.0, "ecmwf_ifs025": 70.0, "ncep_nbm_conus": 72.0}


def _carry_setup(monkeypatch, *, today_bump=4.0, obs_from=date(2024, 3, 1), yesterday=True):
    """Hourly obs peaking at 70F (hour 15) on past days, CLI final 72F (offset +2);
    today peaks at 70+today_bump. Yesterday's lead-1 serve for today is stored
    with an Apple nudge of +2, which the carry must subtract."""
    import emos_forecast as ef
    from datetime import datetime, timezone
    from nws_ground_truth import init_db

    conn = sqlite3.connect(":memory:")
    _seed(conn)
    init_db(conn)
    city_truth.ensure_schema(conn)
    tz =timezone(timedelta(hours=-8))
    day = obs_from
    while day <= date(2024, 5, 31):
        bump = today_bump if day == date(2024, 5, 31) else 0.0
        for hour in range(24):
            at = datetime(day.year, day.month, day.day, hour, tzinfo=tz).astimezone(timezone.utc)
            conn.execute(
                "INSERT INTO nws_station_observations VALUES ('KSFO','KSFO',?,?,?,NULL,NULL,'{}','x')",
                (at.isoformat(), day.isoformat(), 70.0 - abs(hour - 15) + bump),
            )
        if day < date(2024, 5, 31):
            city_truth.upsert_settlement(conn, "KSFO", day.isoformat(), 72)
        day += timedelta(days=1)
    conn.commit()
    today = date(2024, 5, 31)
    monkeypatch.setattr(ef, "_settlement_today", lambda city=ef.DEFAULT_CITY: today - timedelta(days=1))
    mu_today = None
    if yesterday:
        stamp = "2024-05-31T02:00:00+00:00"  # 18:00 LST on 5/30 -> true lead 1
        mu_today, _ = serve_live_emos(
            conn, today, live_models=LIVE, recalibrate=False, fetched_at=stamp, now=_dt(stamp)
        )
        serve_live_emos(
            conn, today, live_models=LIVE, recalibrate=False, apple_high_f=mu_today + 4,
            fetched_at="2024-05-31T02:01:00+00:00", now=_dt(stamp),
        )
    monkeypatch.setattr(ef, "_settlement_today", lambda city=ef.DEFAULT_CITY: today)
    return conn, mu_today


def _dt(iso):
    from datetime import datetime

    return datetime.fromisoformat(iso)


def _carry_serve(conn, now=CARRY_NOW, **kwargs):
    kwargs.setdefault("target", CARRY_TARGET)
    target = kwargs.pop("target")
    result = serve_live_emos(
        conn, target, live_models=LIVE, recalibrate=False, fetched_at=now, now=_dt(now), **kwargs
    )
    (policy,) = conn.execute(
        "SELECT serve_policy_json FROM forecast_emos_live_vintages WHERE target_date=? "
        "ORDER BY recorded_at DESC LIMIT 1", (target.isoformat(),)
    ).fetchone()
    return result[0], json.loads(policy)


def test_evening_carry_adjusts_next_day_mean_and_is_scored(monkeypatch):
    import emos_forecast as ef

    conn, mu_today = _carry_setup(monkeypatch)
    base, skipped = _carry_serve(conn, now="2024-06-01T00:00:00+00:00")  # 16:00 LST
    assert skipped["carry_action"] == "skipped_before_17_lst" and skipped["carry_adjustment_f"] == 0.0

    mu, policy = _carry_serve(conn)
    # hsf 74 (peak at hour 15 on a +4 day), offset 72 - 70 = 2, Apple's +2 removed from yesterday's mean.
    e = mu_today - 76.0
    assert policy["carry_action"] == "applied"
    assert abs(policy["carry_e_f"] - e) < 1e-9 and policy["carry_hsf_f"] == 74.0
    assert abs(policy["carry_offset_f"] - 2.0) < 1e-9 and abs(policy["carry_today_mu_f"] - mu_today) < 1e-9
    assert policy["carry_hour_lst"] == 18.0
    assert abs(mu - (base - 0.2 * e)) < 1e-9 and abs(policy["carry_adjustment_f"] + 0.2 * e) < 1e-9

    truth = round(base) + 3
    city_truth.upsert_settlement(conn, "KSFO", CARRY_TARGET.isoformat(), truth)
    (row,) = ef.apple_score(conn, "carry_adjustment_f").values()
    # Two vintages (skipped, applied) for one station-day; "without" is mu minus the carry.
    assert row["n"] == 1 and row["applied"] == 1
    assert abs(row["mae_without"] - abs(base - truth)) < 1e-9
    assert abs(row["mae_with"] - (abs(base - truth) + abs(mu - truth)) / 2) < 1e-9


def test_evening_carry_is_clipped(monkeypatch):
    conn, _ = _carry_setup(monkeypatch, today_bump=30.0)
    base, _ = _carry_serve(conn, now="2024-06-01T00:00:00+00:00")
    mu, policy = _carry_serve(conn)
    assert policy["carry_adjustment_f"] == 1.5 and abs(mu - (base + 1.5)) < 1e-9


def test_evening_carry_skip_reasons_never_change_the_mean(monkeypatch):
    conn, _ = _carry_setup(monkeypatch, yesterday=False)
    base, _ = _carry_serve(conn, now="2024-06-01T00:00:00+00:00")
    mu, policy = _carry_serve(conn)
    assert mu == base and policy["carry_action"] == "skipped_no_yesterday_forecast"

    conn, _ = _carry_setup(monkeypatch, obs_from=date(2024, 5, 20))  # 11 prior days < 20
    base, _ = _carry_serve(conn, now="2024-06-01T00:00:00+00:00")
    mu, policy = _carry_serve(conn)
    assert mu == base and policy["carry_action"] == "skipped_thin_offset_history"

    conn, _ = _carry_setup(monkeypatch)
    conn.execute("DELETE FROM nws_station_observations WHERE local_date='2024-05-31'")
    base, _ = _carry_serve(conn, now="2024-06-01T00:00:00+00:00")
    mu, policy = _carry_serve(conn)
    assert mu == base and policy["carry_action"] == "skipped_thin_observations"

    # Lead 0 (today) and lead 2 are never carried.
    conn, _ = _carry_setup(monkeypatch)
    for target, stored in ((date(2024, 5, 31), 0), (date(2024, 6, 2), 2), (date(2024, 6, 2), 1)):
        _, policy = _carry_serve(conn, target=target, store_lead_days=stored)
        assert policy["carry_action"] == "skipped_not_next_day"
        assert policy["carry_adjustment_f"] == 0.0


def _edit_vintages(conn, sql):
    # The vintage table is append-only by trigger; tests that need a corrupt row drop them.
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='forecast_emos_live_vintages'"
    ).fetchall():
        conn.execute(f"DROP TRIGGER {name}")
    conn.execute(sql)


def test_evening_carry_strips_yesterdays_own_carry(monkeypatch):
    conn, mu_today = _carry_setup(monkeypatch)
    # Yesterday's vintage was served with Apple +2 (already stripped) and a carry of -1.0.
    _edit_vintages(
        conn,
        "UPDATE forecast_emos_live_vintages SET predicted_high_f=predicted_high_f-1.0, "
        "serve_policy_json=json_set(serve_policy_json,'$.carry_adjustment_f',-1.0) "
        "WHERE target_date='2024-05-31' AND recorded_at='2024-05-31T02:01:00+00:00'",
    )
    _, policy = _carry_serve(conn)
    assert abs(policy["carry_today_mu_f"] - mu_today) < 1e-9


def test_evening_carry_data_error_skips_without_dropping_the_serve(monkeypatch):
    conn, _ = _carry_setup(monkeypatch)
    base, _ = _carry_serve(conn, now="2024-06-01T00:00:00+00:00")
    _edit_vintages(
        conn, "UPDATE forecast_emos_live_vintages SET serve_policy_json='not json' WHERE target_date='2024-05-31'"
    )
    mu, policy = _carry_serve(conn)
    assert mu == base and policy["carry_action"] == "skipped_data_error"

    conn, _ = _carry_setup(monkeypatch)
    conn.execute("UPDATE nws_station_observations SET observed_at='garbage' WHERE rowid=(SELECT MIN(rowid) FROM nws_station_observations WHERE local_date='2024-05-31')")
    mu, policy = _carry_serve(conn)
    assert mu == base and policy["carry_action"] == "skipped_data_error"
