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
