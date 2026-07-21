import json
import sqlite3
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sfo_kalshi_quant.cli import _enforce_live_forecast_freshness
from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.config import SFO_TZ, StrategyConfig
from sfo_kalshi_quant.forecast import (
    ForecastDataError,
    SfoForecasterAdapter,
    has_forecaster_observed_high_adjustment,
)
from sfo_kalshi_quant.models import ForecastSnapshot, IntradaySnapshot


def test_latest_blend_reads_extended_forecaster_metadata():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_path = root / "weather.db"
        details = {
            "blend_weighting": {"mode": "base"},
            "observed_high_decision": {"mode": "lock", "highF": 69.8, "reason": "test"},
            "google_weather_api": {
                "daily_events_used": 11,
                "daily_event_budget": 260,
                "monthly_events_used": 11,
                "monthly_event_budget": 8000,
            },
            "sources": {
                "google": {
                    "components": {
                        "hourly_local_day_high_f": 65.3,
                        "daily_endpoint_high_f": 65.3,
                        "daily_minus_hourly_gap_f": 0.0,
                    },
                    "warning": None,
                }
            },
        }
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE forecast_blend_daily_high (
                    fetched_at TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    lead_hours REAL,
                    method TEXT NOT NULL,
                    predicted_high_f REAL NOT NULL,
                    google_high_f REAL,
                    nws_high_f REAL,
                    open_meteo_high_f REAL,
                    history_high_f REAL,
                    google_weight REAL,
                    nws_weight REAL,
                    open_meteo_weight REAL,
                    history_weight REAL,
                    station_adjustment_f REAL,
                    fresh_station_count INTEGER,
                    source_count INTEGER,
                    time_zone TEXT,
                    max_calls_per_day INTEGER,
                    calls_used_today INTEGER,
                    details_json TEXT,
                    actual_high_f REAL,
                    abs_error_f REAL,
                    scored_at TEXT,
                    PRIMARY KEY (fetched_at, target_date)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO forecast_blend_daily_high (
                    fetched_at, target_date, lead_hours, method, predicted_high_f,
                    google_high_f, nws_high_f, open_meteo_high_f, history_high_f,
                    google_weight, nws_weight, open_meteo_weight, history_weight,
                    station_adjustment_f, fresh_station_count, source_count,
                    max_calls_per_day, calls_used_today, details_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-06-03T22:13:44+00:00",
                    "2026-06-04",
                    23.77,
                    "weighted blend",
                    66.45,
                    65.3,
                    66.0,
                    68.5,
                    69.7,
                    0.38,
                    0.36,
                    0.18,
                    0.08,
                    -0.03,
                    6,
                    4,
                    6,
                    5,
                    json.dumps(details),
                ),
            )

        forecast = SfoForecasterAdapter(root).latest_blend(date(2026, 6, 4))

        assert forecast.predicted_high_f == 66.45
        assert forecast.lead_hours == 23.77
        assert forecast.google_weight == 0.38
        assert forecast.fresh_station_count == 6
        assert forecast.calls_used_today == 5
        assert forecast.raw["blend_weighting"]["mode"] == "base"
        assert forecast.raw["observed_high_decision"]["highF"] == 69.8
        assert forecast.raw["google_weather_api"]["monthly_event_budget"] == 8000
        assert forecast.raw["google_components"]["hourly_local_day_high_f"] == 65.3


def test_intraday_snapshot_prefers_official_daily_high_table():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_path = root / "weather.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE nws_daily_high_ground_truth (
                    station_id TEXT NOT NULL,
                    noaa_station_id TEXT,
                    local_date TEXT NOT NULL,
                    high_f REAL,
                    high_observed_at TEXT,
                    observation_count INTEGER NOT NULL,
                    first_observed_at TEXT,
                    last_observed_at TEXT,
                    is_complete INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    PRIMARY KEY (station_id, local_date)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE nws_station_observations (
                    station_id TEXT,
                    local_date TEXT,
                    observed_at TEXT,
                    temp_f REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE forecast_google_hourly (
                    fetched_at TEXT,
                    target_date TEXT,
                    forecast_hour_utc TEXT,
                    temperature_f REAL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO nws_station_observations
                VALUES ('KSFO', '2026-06-03', '2026-06-03T20:00:00+00:00', 68.0)
                """
            )
            conn.execute(
                """
                INSERT INTO nws_daily_high_ground_truth (
                    station_id, noaa_station_id, local_date, high_f, high_observed_at,
                    observation_count, is_complete, updated_at, source
                )
                VALUES ('KSFO', 'USW00023234', '2026-06-03', 69.8,
                    '2026-06-03T21:15:00+00:00', 200, 0,
                    '2026-06-03T22:13:38+00:00', 'NWS KSFO observed daily high')
                """
            )

        intraday = SfoForecasterAdapter(root).intraday_snapshot(date(2026, 6, 3))

        assert intraday is not None
        assert intraday.observed_high_f == 69.8
        assert intraday.latest_observed_at == "2026-06-03T21:15:00+00:00"
        assert intraday.observation_count == 200
        assert intraday.observed_high_source == "NWS KSFO observed daily high"


def test_live_forecast_freshness_rejects_stale_snapshot():
    stale_fetched_at = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    target = datetime.now(SFO_TZ).date() + timedelta(days=1)
    forecast = ForecastSnapshot(
        target_date=target,
        predicted_high_f=70.0,
        fetched_at=stale_fetched_at,
        method="test",
    )

    try:
        _enforce_live_forecast_freshness(forecast, StrategyConfig(max_forecast_age_hours=30.0))
    except ForecastDataError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("stale live forecasts must be rejected")


def _write_sfo_live_forecast_sources(
    root: Path,
    target: date,
    *,
    blend_fetched_at: str,
    emos_fetched_at: str,
) -> None:
    with sqlite3.connect(root / "weather.db") as conn:
        conn.executescript(
            """
            CREATE TABLE forecast_blend_daily_high (
                fetched_at TEXT NOT NULL,
                target_date TEXT NOT NULL,
                lead_hours REAL,
                method TEXT NOT NULL,
                predicted_high_f REAL NOT NULL,
                google_high_f REAL,
                nws_high_f REAL,
                open_meteo_high_f REAL,
                history_high_f REAL,
                google_weight REAL,
                nws_weight REAL,
                open_meteo_weight REAL,
                history_weight REAL,
                station_adjustment_f REAL,
                fresh_station_count INTEGER,
                source_count INTEGER,
                time_zone TEXT,
                max_calls_per_day INTEGER,
                calls_used_today INTEGER,
                details_json TEXT,
                PRIMARY KEY (fetched_at, target_date)
            );
            CREATE TABLE forecast_emos_daily_high (
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                lead_days INTEGER NOT NULL,
                predicted_high_f REAL NOT NULL,
                sigma_f REAL NOT NULL,
                n_models INTEGER NOT NULL,
                model_spread_f REAL,
                fetched_at TEXT NOT NULL,
                method TEXT,
                source TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO forecast_blend_daily_high (
                fetched_at, target_date, lead_hours, method, predicted_high_f,
                google_high_f, nws_high_f, open_meteo_high_f, history_high_f,
                google_weight, nws_weight, open_meteo_weight, history_weight,
                station_adjustment_f, fresh_station_count, source_count,
                time_zone, max_calls_per_day, calls_used_today, details_json
            ) VALUES (?, ?, 24, 'legacy SFO blend', 68.0, 68.0, 69.0, 67.0,
                66.0, 0.4, 0.3, 0.2, 0.1, 0, 4, 4, 'Etc/GMT+8', 260, 5, '{}')
            """,
            (blend_fetched_at, target.isoformat()),
        )
        conn.execute(
            """
            INSERT INTO forecast_emos_daily_high (
                station_id, target_date, lead_days, predicted_high_f, sigma_f,
                n_models, model_spread_f, fetched_at, method, source
            ) VALUES ('KSFO', ?, 1, 71.5, 2.4, 8, 3.2, ?, 'emos-wmean', 'live')
            """,
            (target.isoformat(), emos_fetched_at),
        )


def test_sfo_live_forecast_falls_back_to_fresh_emos_when_legacy_blend_is_stale(
    tmp_path,
):
    now = datetime(2026, 7, 20, 6, 0, tzinfo=UTC)
    target = date(2026, 7, 20)
    _write_sfo_live_forecast_sources(
        tmp_path,
        target,
        blend_fetched_at="2026-07-19T09:44:30+00:00",
        emos_fetched_at="2026-07-20T05:41:48+00:00",
    )

    forecast = SfoForecasterAdapter(tmp_path).latest_live_forecast(
        target,
        max_age_hours=12.0,
        now=now,
    )

    assert forecast.predicted_high_f == 71.5
    assert forecast.fetched_at == "2026-07-20T05:41:48+00:00"
    assert forecast.raw["source"] == "forecast_emos_daily_high"
    assert forecast.raw["operational_fallback"] == {
        "reason": "legacy_sfo_blend_stale",
        "legacy_blend_fetched_at": "2026-07-19T09:44:30+00:00",
        "max_age_hours": 12.0,
    }


def test_sfo_live_forecast_keeps_fresh_legacy_blend_bitwise_unchanged(tmp_path):
    now = datetime(2026, 7, 20, 6, 0, tzinfo=UTC)
    target = date(2026, 7, 20)
    _write_sfo_live_forecast_sources(
        tmp_path,
        target,
        blend_fetched_at="2026-07-20T05:30:00+00:00",
        emos_fetched_at="2026-07-20T05:41:48+00:00",
    )
    adapter = SfoForecasterAdapter(tmp_path)

    forecast = adapter.latest_live_forecast(target, max_age_hours=12.0, now=now)

    assert forecast == adapter.latest_blend(target)
    assert forecast.predicted_high_f == 68.0
    assert "operational_fallback" not in forecast.raw


def test_sfo_live_forecast_uses_fresh_emos_when_legacy_blend_row_is_missing(tmp_path):
    now = datetime(2026, 7, 20, 6, 0, tzinfo=UTC)
    target = date(2026, 7, 21)
    _write_sfo_live_forecast_sources(
        tmp_path,
        target,
        blend_fetched_at="2026-07-20T05:30:00+00:00",
        emos_fetched_at="2026-07-20T05:41:48+00:00",
    )
    with sqlite3.connect(tmp_path / "weather.db") as conn:
        conn.execute("DELETE FROM forecast_blend_daily_high")

    forecast = SfoForecasterAdapter(tmp_path).latest_live_forecast(
        target,
        max_age_hours=12.0,
        now=now,
    )

    assert forecast.predicted_high_f == 71.5
    assert forecast.raw["operational_fallback"]["reason"] == "legacy_sfo_blend_missing"
    assert forecast.raw["operational_fallback"]["legacy_blend_fetched_at"] is None


def test_sfo_live_forecast_remains_fail_closed_when_blend_and_emos_are_stale(tmp_path):
    now = datetime(2026, 7, 20, 6, 0, tzinfo=UTC)
    target = date(2026, 7, 20)
    _write_sfo_live_forecast_sources(
        tmp_path,
        target,
        blend_fetched_at="2026-07-19T09:44:30+00:00",
        emos_fetched_at="2026-07-19T10:00:00+00:00",
    )
    config = StrategyConfig(max_forecast_age_hours=12.0)

    forecast = SfoForecasterAdapter(tmp_path).latest_live_forecast(
        target,
        max_age_hours=config.max_forecast_age_hours,
        now=now,
    )

    assert forecast.method == "legacy SFO blend"
    assert forecast.age_hours(now) > config.max_forecast_age_hours


def test_observed_high_lock_is_detected_before_second_intraday_adjustment():
    forecast = ForecastSnapshot(
        target_date=date(2026, 6, 4),
        predicted_high_f=70.0,
        raw={"observed_high_decision": {"mode": "lock"}},
    )
    assert has_forecaster_observed_high_adjustment(forecast)


def test_apply_intraday_update_uses_adapter_city_fixed_standard_time():
    forecast = ForecastSnapshot(
        target_date=date(2026, 7, 10),
        predicted_high_f=74.0,
        method="test",
    )
    intraday = IntradaySnapshot(
        target_date=date(2026, 7, 10),
        observed_high_f=68.0,
        latest_temp_f=None,
        latest_observed_at="2026-07-10T18:00:00+00:00",
        remaining_forecast_high_f=72.0,
        forecast_fetched_at=None,
    )

    nyc = SfoForecasterAdapter(Path("."), city=get_city("nyc")).apply_intraday_update(
        forecast, intraday
    )
    sfo = SfoForecasterAdapter(Path("."), city=get_city("sfo")).apply_intraday_update(
        forecast, intraday
    )

    assert nyc.raw["intraday_update"]["intraday_weight"] == 0.65
    assert nyc.predicted_high_f == 72.7
    assert sfo.raw["intraday_update"]["intraday_weight"] == 0.5
    assert sfo.predicted_high_f == 73.0


# ---------------------------------------------------------------------------
# T7-1: the legacy raw-JSON google_weather_cache.json fallback is removed.
#
# Before Task 7, a missing/row-less weather.db silently degraded to reading
# highF/method from the legacy raw Google JSON cache. That fallback is
# removed: no consumer may read Google content from google_weather_cache.json
# any more, and a weather.db gap is now an explicit ForecastDataError rather
# than a silent degrade.
# ---------------------------------------------------------------------------


def test_missing_weather_db_raises_instead_of_reading_the_legacy_json_cache(tmp_path):
    (tmp_path / "google_weather_cache.json").write_text(
        json.dumps({"target_date": "2026-06-04", "highF": 66.45, "method": "legacy_cache"})
    )

    try:
        SfoForecasterAdapter(tmp_path).latest_blend(date(2026, 6, 4))
    except ForecastDataError as exc:
        assert "google_weather_cache.json" not in str(exc) or "removed" in str(exc)
    else:
        raise AssertionError(
            "latest_blend must fail closed, not silently serve the legacy JSON cache"
        )


def test_weather_db_without_a_matching_row_raises_instead_of_reading_the_legacy_json_cache(
    tmp_path,
):
    # A real, fully-shaped (but empty) forecast_blend_daily_high table -- so
    # this genuinely exercises the "table exists, no matching row" path
    # rather than an unrelated malformed-schema SQL error.
    db_path = tmp_path / "weather.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE forecast_blend_daily_high (
                fetched_at TEXT NOT NULL,
                target_date TEXT NOT NULL,
                lead_hours REAL,
                method TEXT NOT NULL,
                predicted_high_f REAL NOT NULL,
                google_high_f REAL,
                nws_high_f REAL,
                open_meteo_high_f REAL,
                history_high_f REAL,
                google_weight REAL,
                nws_weight REAL,
                open_meteo_weight REAL,
                history_weight REAL,
                station_adjustment_f REAL,
                fresh_station_count INTEGER,
                source_count INTEGER,
                time_zone TEXT,
                max_calls_per_day INTEGER,
                calls_used_today INTEGER,
                details_json TEXT,
                actual_high_f REAL,
                abs_error_f REAL,
                scored_at TEXT,
                PRIMARY KEY (fetched_at, target_date)
            )
            """
        )
    (tmp_path / "google_weather_cache.json").write_text(
        json.dumps({"target_date": "2026-06-04", "highF": 66.45, "method": "legacy_cache"})
    )

    try:
        SfoForecasterAdapter(tmp_path).latest_blend(date(2026, 6, 4))
    except ForecastDataError as exc:
        assert "google_weather_cache.json" not in str(exc) or "removed" in str(exc)
    else:
        raise AssertionError(
            "latest_blend must fail closed, not silently serve the legacy JSON cache"
        )


def test_adapter_no_longer_exposes_a_google_cache_path_attribute(tmp_path):
    adapter = SfoForecasterAdapter(tmp_path)

    assert not hasattr(adapter, "google_cache_path")


# ---------------------------------------------------------------------------
# Task 7: reading the forecaster-owned durable paired-evidence table.
#
# Pure SQL read (mirrors _latest_blend_row/_latest_emos_snapshot) -- never a
# Python import of forecaster's google_runtime_blend/google_paired_evidence
# modules (the two projects deliberately do not import each other).
# ---------------------------------------------------------------------------


def _write_paired_evidence_row(db_path, **overrides):
    row = {
        "station_id": "KSFO",
        "target_date": "2026-07-19",
        "issued_at": "2026-07-18T19:00:00+00:00",
        "policy_version": "google-runtime-fixed-v1",
        "baseline_mu": 80.0,
        "baseline_sigma": 3.0,
        "challenger_mu": 80.45,
        "challenger_sigma": 3.0,
        "action": "forecast",
    }
    row.update(overrides)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS google_challenger_research_baseline (
                station_id TEXT NOT NULL,
                target_date TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                baseline_mu REAL NOT NULL,
                baseline_sigma REAL NOT NULL,
                challenger_mu REAL,
                challenger_sigma REAL NOT NULL,
                action TEXT NOT NULL,
                PRIMARY KEY(station_id, target_date, issued_at, policy_version)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO google_challenger_research_baseline (
                station_id, target_date, issued_at, policy_version,
                baseline_mu, baseline_sigma, challenger_mu, challenger_sigma, action
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["station_id"], row["target_date"], row["issued_at"],
                row["policy_version"], row["baseline_mu"], row["baseline_sigma"],
                row["challenger_mu"], row["challenger_sigma"], row["action"],
            ),
        )
    return row


def test_latest_google_challenger_baseline_returns_none_without_weather_db(tmp_path):
    adapter = SfoForecasterAdapter(tmp_path)

    assert adapter.latest_google_challenger_baseline(date(2026, 7, 19)) is None


def test_latest_google_challenger_baseline_returns_none_without_the_table(tmp_path):
    db_path = tmp_path / "weather.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
    adapter = SfoForecasterAdapter(tmp_path)

    assert adapter.latest_google_challenger_baseline(date(2026, 7, 19)) is None


def test_latest_google_challenger_baseline_reads_the_freshest_matching_row(tmp_path):
    db_path = tmp_path / "weather.db"
    _write_paired_evidence_row(db_path, issued_at="2026-07-18T19:00:00+00:00")
    _write_paired_evidence_row(db_path, issued_at="2026-07-18T20:00:00+00:00", baseline_mu=81.0)
    adapter = SfoForecasterAdapter(tmp_path)

    row = adapter.latest_google_challenger_baseline(date(2026, 7, 19))

    assert row is not None
    assert row["issued_at"] == "2026-07-18T20:00:00+00:00"
    assert row["baseline_mu"] == 81.0
    assert row["station_id"] == "KSFO"
    assert row["action"] == "forecast"
