#!/usr/bin/env python3
"""Fetch and cache the Google Weather hourly high for the dashboard."""

import argparse
import json
import math
import re
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from forecast_scoring import is_clean_next_day_forecast
from settlement_calendar import (
    integer_settlement_high_f,
    local_standard_date,
    today_local_standard,
    utc_window_for_local_standard_date,
)


def env_int(name, default):
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name, default):
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_bool(name, default):
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


SFO_TZ = ZoneInfo("America/Los_Angeles")
SFO_POINT = {"lat": 37.6213, "lon": -122.3790}
HOURLY_API_URL = "https://weather.googleapis.com/v1/forecast/hours:lookup"
DAILY_API_URL = "https://weather.googleapis.com/v1/forecast/days:lookup"
CURRENT_API_URL = "https://weather.googleapis.com/v1/currentConditions:lookup"
NWS_API_URL = "https://api.weather.gov"
OPEN_METEO_API_URL = "https://api.open-meteo.com/v1/forecast"
API_KEY_ENV = "GOOGLE_WEATHER_API_KEY"
CACHE_PATH = Path("google_weather_cache.json")
USAGE_PATH = Path(".google_weather_usage.json")
DB_PATH = Path("weather.db")
FORECAST_DATA_PATH = Path("forecast_data.json")
SFO_WEATHER_STATION_ID = "USW00023234"
GOOGLE_WEATHER_MONTHLY_FREE_CAP = 10000
GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET = env_int("GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET", 8000)
GOOGLE_WEATHER_DAILY_EVENT_BUDGET = env_int("GOOGLE_WEATHER_DAILY_EVENT_BUDGET", 260)
ENABLE_GOOGLE_DAILY_FORECAST = env_bool("ENABLE_GOOGLE_DAILY_FORECAST", True)
ENABLE_GOOGLE_CURRENT_CONDITIONS = env_bool("ENABLE_GOOGLE_CURRENT_CONDITIONS", True)
GOOGLE_DAILY_INTERNAL_WEIGHT = env_float("GOOGLE_DAILY_INTERNAL_WEIGHT", 0.15)
GOOGLE_DAILY_DISAGREEMENT_WARN_F = env_float("GOOGLE_DAILY_DISAGREEMENT_WARN_F", 2.5)
HOURLY_LOOKAHEAD_HOURS = 72
HOURLY_PAGE_SIZE = 24
MIN_HOURS_FOR_DAILY_HIGH = 18
NWS_USER_AGENT = "SFO Weather Forecaster student project"
FRESH_OBSERVATION_MINUTES = 180
BLEND_WEIGHTS = {
    "google": 0.38,
    "nws": 0.36,
    "open_meteo": 0.18,
    "history": 0.08,
}
# 5 scored days was too little evidence to shift weights for a trading edge;
# learning now also has to beat the base blend on a walk-forward holdout.
ADAPTIVE_WEIGHT_MIN_SCORED_DAYS = 15
ADAPTIVE_WEIGHT_MAX_LEARNED_SHARE = 0.60
ADAPTIVE_WEIGHT_HOLDOUT_MIN_DAYS = 5
ADAPTIVE_SOURCE_COLUMNS = {
    "google": "google_high_f",
    "nws": "nws_high_f",
    "open_meteo": "open_meteo_high_f",
    "history": "history_high_f",
}
AIRPORT_STATIONS = ("KSFO", "KOAK", "KSJC", "KSQL", "KPAO", "KHAF")
DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


def read_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2) + "\n")


def local_midnight_utc(target_iso):
    start_utc, end_utc = utc_window_for_local_standard_date(target_iso)
    return (
        start_utc.strftime("%Y-%m-%d %H:%M:%S"),
        end_utc.strftime("%Y-%m-%d %H:%M:%S"),
    )


def table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def daily_archive_columns(conn):
    if not table_exists(conn, "forecast_google_daily_high"):
        return {}
    rows = conn.execute("PRAGMA table_info(forecast_google_daily_high)").fetchall()
    return {row[1]: row for row in rows}


def create_daily_archive_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS forecast_google_daily_high (
            fetched_at TEXT NOT NULL,
            target_date TEXT NOT NULL,
            lead_hours REAL,
            source TEXT NOT NULL,
            method TEXT NOT NULL,
            predicted_high_f REAL NOT NULL,
            peak_hour_local TEXT,
            hours_used INTEGER,
            forecast_start_local TEXT,
            forecast_end_local TEXT,
            condition TEXT,
            precipitation_probability_pct REAL,
            time_zone TEXT,
            max_calls_per_day INTEGER,
            calls_used_today INTEGER,
            actual_high_f REAL,
            abs_error_f REAL,
            scored_at TEXT,
            PRIMARY KEY (fetched_at, target_date)
        )
        """
    )


def create_blend_archive_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS forecast_blend_daily_high (
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


def migrate_daily_archive(conn):
    columns = daily_archive_columns(conn)
    if not columns:
        create_daily_archive_table(conn)
        return

    pk_cols = [row[1] for row in sorted(columns.values(), key=lambda row: row[5]) if row[5]]
    if pk_cols == ["fetched_at", "target_date"] and "lead_hours" in columns:
        return

    old_col_names = set(columns)
    conn.execute("ALTER TABLE forecast_google_daily_high RENAME TO forecast_google_daily_high_old")
    create_daily_archive_table(conn)
    copied_cols = [
        "fetched_at",
        "target_date",
        "source",
        "method",
        "predicted_high_f",
        "peak_hour_local",
        "hours_used",
        "forecast_start_local",
        "forecast_end_local",
        "condition",
        "precipitation_probability_pct",
        "time_zone",
        "max_calls_per_day",
        "calls_used_today",
        "actual_high_f",
        "abs_error_f",
        "scored_at",
    ]
    selected = ", ".join(col for col in copied_cols if col in old_col_names)
    inserted = ", ".join(col for col in copied_cols if col in old_col_names)
    if selected:
        conn.execute(
            f"""
            INSERT OR IGNORE INTO forecast_google_daily_high ({inserted})
            SELECT {selected}
            FROM forecast_google_daily_high_old
            """
        )
    conn.execute("DROP TABLE forecast_google_daily_high_old")


def hourly_archive_columns(conn):
    if not table_exists(conn, "forecast_google_hourly"):
        return {}
    rows = conn.execute("PRAGMA table_info(forecast_google_hourly)").fetchall()
    return {row[1]: row for row in rows}


def init_archive(conn):
    migrate_daily_archive(conn)
    create_blend_archive_table(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS forecast_google_hourly (
            fetched_at TEXT NOT NULL,
            target_date TEXT NOT NULL,
            forecast_hour_utc TEXT NOT NULL,
            forecast_hour_local TEXT NOT NULL,
            lead_hours REAL,
            temperature_f REAL,
            condition TEXT,
            precipitation_probability_pct REAL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (fetched_at, forecast_hour_utc)
        )
        """
    )
    hourly_columns = hourly_archive_columns(conn)
    if "lead_hours" not in hourly_columns:
        conn.execute("ALTER TABLE forecast_google_hourly ADD COLUMN lead_hours REAL")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_google_daily_target
        ON forecast_google_daily_high(target_date)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_google_hourly_target
        ON forecast_google_hourly(target_date)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_blend_daily_target
        ON forecast_blend_daily_high(target_date)
        """
    )


def archive_summary(conn, summary):
    if not summary.get("available") or not summary.get("fetched_at"):
        return 0

    summaries = summary.get("daily_highs") or [summary]
    count = 0
    for row in summaries:
        if not row.get("available") or not row.get("target_date") or row.get("highF") is None:
            continue
        conn.execute(
            """
            INSERT INTO forecast_google_daily_high (
                fetched_at,
                target_date,
                lead_hours,
                source,
                method,
                predicted_high_f,
                peak_hour_local,
                hours_used,
                forecast_start_local,
                forecast_end_local,
                condition,
                precipitation_probability_pct,
                time_zone,
                max_calls_per_day,
                calls_used_today
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fetched_at, target_date) DO UPDATE SET
                lead_hours = excluded.lead_hours,
                source = excluded.source,
                method = excluded.method,
                predicted_high_f = excluded.predicted_high_f,
                peak_hour_local = excluded.peak_hour_local,
                hours_used = excluded.hours_used,
                forecast_start_local = excluded.forecast_start_local,
                forecast_end_local = excluded.forecast_end_local,
                condition = excluded.condition,
                precipitation_probability_pct = excluded.precipitation_probability_pct,
                time_zone = excluded.time_zone,
                max_calls_per_day = excluded.max_calls_per_day,
                calls_used_today = excluded.calls_used_today
            """,
            (
                row.get("fetched_at", summary["fetched_at"]),
                row["target_date"],
                row.get("lead_hours"),
                row.get("source", summary.get("source", "Google Weather API")),
                row.get("method", summary.get("method", "cached Google forecast high")),
                row["highF"],
                row.get("peak_hour_local"),
                row.get("hours_used"),
                row.get("forecast_start_local"),
                row.get("forecast_end_local"),
                row.get("condition"),
                row.get("precipitation_probability_pct"),
                row.get("time_zone", summary.get("time_zone")),
                row.get("max_calls_per_day", summary.get("max_calls_per_day")),
                row.get("calls_used_today", summary.get("calls_used_today")),
            ),
        )
        count += 1
    return count


def archive_blend_summary(conn, blend):
    if not blend:
        return 0
    if isinstance(blend, list):
        return sum(archive_blend_summary(conn, row) for row in blend if row)

    conn.execute(
        """
        INSERT INTO forecast_blend_daily_high (
            fetched_at,
            target_date,
            lead_hours,
            method,
            predicted_high_f,
            google_high_f,
            nws_high_f,
            open_meteo_high_f,
            history_high_f,
            google_weight,
            nws_weight,
            open_meteo_weight,
            history_weight,
            station_adjustment_f,
            fresh_station_count,
            source_count,
            time_zone,
            max_calls_per_day,
            calls_used_today,
            details_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fetched_at, target_date) DO UPDATE SET
            lead_hours = excluded.lead_hours,
            method = excluded.method,
            predicted_high_f = excluded.predicted_high_f,
            google_high_f = excluded.google_high_f,
            nws_high_f = excluded.nws_high_f,
            open_meteo_high_f = excluded.open_meteo_high_f,
            history_high_f = excluded.history_high_f,
            google_weight = excluded.google_weight,
            nws_weight = excluded.nws_weight,
            open_meteo_weight = excluded.open_meteo_weight,
            history_weight = excluded.history_weight,
            station_adjustment_f = excluded.station_adjustment_f,
            fresh_station_count = excluded.fresh_station_count,
            source_count = excluded.source_count,
            time_zone = excluded.time_zone,
            max_calls_per_day = excluded.max_calls_per_day,
            calls_used_today = excluded.calls_used_today,
            details_json = excluded.details_json
        """,
        (
            blend["fetched_at"],
            blend["target_date"],
            blend.get("lead_hours"),
            blend["method"],
            blend["predicted_high_f"],
            blend.get("google_high_f"),
            blend.get("nws_high_f"),
            blend.get("open_meteo_high_f"),
            blend.get("history_high_f"),
            blend.get("google_weight"),
            blend.get("nws_weight"),
            blend.get("open_meteo_weight"),
            blend.get("history_weight"),
            blend.get("station_adjustment_f"),
            blend.get("fresh_station_count"),
            blend.get("source_count"),
            blend.get("time_zone"),
            blend.get("max_calls_per_day"),
            blend.get("calls_used_today"),
            json.dumps(blend.get("details", {}), separators=(",", ":")),
        ),
    )
    return 1


def archive_hourly_rows(conn, payload, summary):
    fetched_at = summary.get("fetched_at")
    fetched_time = parse_google_timestamp(fetched_at)
    if not fetched_at or not fetched_time:
        return 0

    rows = []
    for hour in payload.get("forecastHours") or []:
        local_time = hour_local_datetime(hour)
        temp_f = temp_to_f(hour.get("temperature") or {})
        if not local_time or temp_f is None:
            continue
        hour_utc = local_time.astimezone(timezone.utc).isoformat()
        lead_hours = round((local_time.astimezone(timezone.utc) - fetched_time).total_seconds() / 3600, 2)
        rows.append(
            (
                fetched_at,
                # Settlement-day bucketing (fixed PST), matching
                # daily_high_summaries; civil dates would file the last PST
                # hour of each day under the next target during DST.
                local_standard_date(local_time).isoformat(),
                hour_utc,
                local_time.strftime("%Y-%m-%d %H:%M %Z"),
                lead_hours,
                temp_f,
                condition_text(hour),
                precip_probability(hour),
                json.dumps(hour, separators=(",", ":")),
            )
        )

    conn.executemany(
        """
        INSERT OR IGNORE INTO forecast_google_hourly (
            fetched_at,
            target_date,
            forecast_hour_utc,
            forecast_hour_local,
            lead_hours,
            temperature_f,
            condition,
            precipitation_probability_pct,
            raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


def update_scores_for_table(conn, table_name):
    rows = conn.execute(
        f"""
        SELECT fetched_at, target_date, predicted_high_f
        FROM {table_name}
        WHERE actual_high_f IS NULL
        """
    ).fetchall()
    scored = 0

    for fetched_at, target_iso, predicted_high_f in rows:
        actual = actual_high_from_ground_truth(conn, target_iso)
        if actual is None:
            actual = actual_high_from_history(conn, target_iso)
        if actual is None:
            continue
        settlement_actual = integer_settlement_high_f(actual)
        if settlement_actual is None:
            continue

        conn.execute(
            f"""
            UPDATE {table_name}
            SET actual_high_f = ?,
                abs_error_f = ?,
                scored_at = ?
            WHERE fetched_at = ?
              AND target_date = ?
            """,
            (
                settlement_actual,
                round(abs(float(predicted_high_f) - settlement_actual), 2),
                datetime.now(timezone.utc).isoformat(),
                fetched_at,
                target_iso,
            ),
        )
        scored += 1

    return scored


def update_scores(conn):
    scored = update_scores_for_table(conn, "forecast_google_daily_high")
    scored += update_scores_for_table(conn, "forecast_blend_daily_high")
    return scored


def actual_high_from_ground_truth(conn, target_iso):
    if not table_exists(conn, "nws_daily_high_ground_truth"):
        return None

    row = conn.execute(
        """
        SELECT high_f
        FROM nws_daily_high_ground_truth
        WHERE station_id = 'KSFO'
          AND local_date = ?
          AND is_complete = 1
          AND high_f IS NOT NULL
        """,
        (target_iso,),
    ).fetchone()
    return integer_settlement_high_f(row[0]) if row else None


def actual_high_from_history(conn, target_iso):
    if not table_exists(conn, "weather"):
        return None

    start_utc, end_utc = local_midnight_utc(target_iso)
    return conn.execute(
        """
        SELECT MAX(temp_f)
        FROM weather
        WHERE station_id = ?
          AND timestamp >= ?
          AND timestamp < ?
        """,
        (SFO_WEATHER_STATION_ID, start_utc, end_utc),
    ).fetchone()[0]


def archive_forecast(summary, payload=None, blend=None):
    with sqlite3.connect(DB_PATH) as conn:
        init_archive(conn)
        daily_rows = archive_summary(conn, summary)
        blend_rows = archive_blend_summary(conn, blend)
        hourly_rows = 0
        if payload:
            hourly_rows = archive_hourly_rows(conn, payload, summary)
        scored = update_scores(conn)
        conn.commit()
    return {
        "daily_rows": daily_rows,
        "blend_rows": blend_rows,
        "hourly_rows": hourly_rows,
        "scored": scored,
    }


def score_archive():
    with sqlite3.connect(DB_PATH) as conn:
        init_archive(conn)
        scored = update_scores(conn)
        conn.commit()
    return {"daily_rows": 0, "blend_rows": 0, "hourly_rows": 0, "scored": scored}


def load_dotenv_key():
    env_path = Path(".env")
    if not env_path.exists():
        return None

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == API_KEY_ENV:
            return value.strip().strip("\"'")
    return None


def api_key():
    return os.environ.get(API_KEY_ENV) or load_dotenv_key()


def now_sfo():
    return datetime.now(SFO_TZ)


def target_date(now=None):
    now = now or now_sfo()
    # Settlement "tomorrow" on the fixed-PST clock (the NWS/Kalshi report date
    # the trader settles on), not the civil calendar day. During the DST
    # 00:00-01:00 window these disagree, and the forecaster-refresh timer fires
    # at 00:40 every summer night, which previously filed the snapshot under the
    # wrong settlement day so the trader found no matching blend.
    return (local_standard_date(now) + timedelta(days=1)).isoformat()


def settlement_today_iso(now=None):
    """Today's NWS/Kalshi settlement date (fixed-PST), as an ISO string."""
    return today_local_standard(now).isoformat()


def local_usage_date(now=None):
    # Google event-budget window stays on civil local time (billing boundary),
    # deliberately separate from the settlement clock used for target dates.
    return (now or now_sfo()).date().isoformat()


def local_usage_month(now=None):
    return (now or now_sfo()).strftime("%Y-%m")


def hourly_events_per_refresh():
    return math.ceil(HOURLY_LOOKAHEAD_HOURS / HOURLY_PAGE_SIZE)


def estimated_google_weather_events_per_refresh():
    return (
        hourly_events_per_refresh()
        + (1 if ENABLE_GOOGLE_DAILY_FORECAST else 0)
        + (1 if ENABLE_GOOGLE_CURRENT_CONDITIONS else 0)
    )


def load_usage(now=None):
    today = local_usage_date(now)
    month = local_usage_month(now)
    usage = read_json(USAGE_PATH, {})
    legacy_today = usage.get("date") == today and usage.get("month") is None
    old_daily_count = int(usage.get("daily_events", usage.get("refreshes", usage.get("calls", 0))) or 0)
    old_monthly_count = int(usage.get("monthly_events", usage.get("calls", usage.get("refreshes", 0))) or 0)

    if usage.get("month") != month and not legacy_today:
        usage = {"month": month, "monthly_events": 0}
    else:
        usage["month"] = month
        usage["monthly_events"] = old_monthly_count

    if usage.get("date") != today:
        usage["date"] = today
        usage["daily_events"] = 0
        usage["refreshes"] = 0
    else:
        usage["daily_events"] = old_daily_count
        usage["refreshes"] = int(usage.get("refreshes", 0) or 0)

    usage["monthly_free_cap"] = GOOGLE_WEATHER_MONTHLY_FREE_CAP
    usage["monthly_event_budget"] = min(
        GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET,
        GOOGLE_WEATHER_MONTHLY_FREE_CAP,
    )
    usage["daily_event_budget"] = GOOGLE_WEATHER_DAILY_EVENT_BUDGET
    usage["estimated_events_per_refresh"] = estimated_google_weather_events_per_refresh()
    usage["limit"] = usage["daily_event_budget"]
    usage["calls"] = usage["daily_events"]
    return usage


def usage_has_budget(usage, events_needed):
    daily_remaining = usage["daily_event_budget"] - usage.get("daily_events", 0)
    monthly_remaining = usage["monthly_event_budget"] - usage.get("monthly_events", 0)
    return daily_remaining >= events_needed and monthly_remaining >= events_needed


def reserve_google_weather_events(usage, events_reserved):
    usage = dict(usage)
    usage["daily_events"] = usage.get("daily_events", 0) + events_reserved
    usage["monthly_events"] = usage.get("monthly_events", 0) + events_reserved
    usage["refreshes"] = usage.get("refreshes", 0) + 1
    usage["calls"] = usage["daily_events"]
    usage["last_reserved_events"] = events_reserved
    usage["last_refresh_at"] = datetime.now(timezone.utc).isoformat()
    return usage


def adjust_reserved_google_weather_events(usage, reserved_events, actual_events):
    usage = dict(usage)
    delta = actual_events - reserved_events
    if delta:
        usage["daily_events"] = max(0, usage.get("daily_events", 0) + delta)
        usage["monthly_events"] = max(0, usage.get("monthly_events", 0) + delta)
    usage["calls"] = usage["daily_events"]
    usage["last_refresh_events"] = actual_events
    return usage


def cache_matches(cache, target_iso):
    return (
        cache.get("available")
        and cache.get("target_date") == target_iso
        and cache.get("source") == "Google Weather API forecast.hours"
    )


def temp_to_f(temp):
    degrees = temp.get("degrees")
    if degrees is None:
        return None
    unit = str(temp.get("unit", "CELSIUS")).upper()
    return float(degrees) * 9 / 5 + 32 if unit == "CELSIUS" else float(degrees)


def parse_google_timestamp(raw):
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def hour_local_datetime(hour):
    start = hour.get("interval", {}).get("startTime")
    parsed = parse_google_timestamp(start)
    if parsed:
        return parsed.astimezone(SFO_TZ)

    display = hour.get("displayDateTime") or {}
    year = display.get("year")
    month = display.get("month")
    day_num = display.get("day")
    if not all([year, month, day_num]):
        return None
    return datetime(
        int(year),
        int(month),
        int(day_num),
        int(display.get("hours", display.get("hour", 0))),
        int(display.get("minutes", display.get("minute", 0))),
        tzinfo=SFO_TZ,
    )


def condition_text(hour):
    return (
        hour.get("weatherCondition", {})
        .get("description", {})
        .get("text")
    )


def precip_probability(hour):
    return (
        hour.get("precipitation", {})
        .get("probability", {})
        .get("percent")
    )


def google_display_date(payload):
    display = payload.get("displayDate") or {}
    year = display.get("year")
    month = display.get("month")
    day = display.get("day")
    if not all([year, month, day]):
        return None
    try:
        return datetime(int(year), int(month), int(day), tzinfo=SFO_TZ).date().isoformat()
    except ValueError:
        return None


def google_daily_api_high_rows(payload):
    rows = []
    for day in payload.get("forecastDays") or []:
        target_iso = google_display_date(day)
        high_f = temp_to_f(day.get("maxTemperature") or {})
        if not target_iso or high_f is None:
            continue
        rows.append(
            {
                "target_date": target_iso,
                "highF": round(high_f, 2),
                "source": "Google Weather API forecast.days",
                "condition": (
                    day.get("daytimeForecast", {})
                    .get("weatherCondition", {})
                    .get("description", {})
                    .get("text")
                ),
                "precipitation_probability_pct": (
                    day.get("daytimeForecast", {})
                    .get("precipitation", {})
                    .get("probability", {})
                    .get("percent")
                ),
            }
        )
    return rows


def google_daily_api_high_for(summary, target_iso):
    for row in summary.get("google_daily_forecast_highs") or []:
        if row.get("target_date") == target_iso and finite(row.get("highF")):
            return row
    return None


def google_current_conditions_summary(payload):
    if not payload:
        return None
    current_temp = temp_to_f(payload.get("temperature") or {})
    feels_like = temp_to_f(payload.get("feelsLikeTemperature") or {})
    history = payload.get("currentConditionsHistory") or {}
    history_max = temp_to_f(history.get("maxTemperature") or {})
    history_min = temp_to_f(history.get("minTemperature") or {})
    temp_change = temp_to_f(history.get("temperatureChange") or {})
    return {
        "source": "Google Weather API currentConditions",
        "current_temp_f": round(current_temp, 2) if current_temp is not None else None,
        "feels_like_f": round(feels_like, 2) if feels_like is not None else None,
        "last_24h_max_temp_f": round(history_max, 2) if history_max is not None else None,
        "last_24h_min_temp_f": round(history_min, 2) if history_min is not None else None,
        "last_24h_temp_change_f": round(temp_change, 2) if temp_change is not None else None,
        "condition": (
            payload.get("weatherCondition", {})
            .get("description", {})
            .get("text")
        ),
        "precipitation_probability_pct": (
            payload.get("precipitation", {})
            .get("probability", {})
            .get("percent")
        ),
        "relative_humidity_pct": payload.get("relativeHumidity"),
        "cloud_cover_pct": payload.get("cloudCover"),
    }


def google_hour_rows(payload):
    rows = []
    for hour in payload.get("forecastHours") or []:
        local_time = hour_local_datetime(hour)
        temp_f = temp_to_f(hour.get("temperature") or {})
        if not local_time or temp_f is None:
            continue
        rows.append({"time": local_time, "temp_f": temp_f, "hour": hour})
    return sorted(rows, key=lambda row: row["time"])


def min_hours_for_daily_summary(local_date, fetched_local_date):
    if local_date == fetched_local_date:
        return 1
    return MIN_HOURS_FOR_DAILY_HIGH


def daily_high_summaries(payload, usage, fetched_time):
    rows = google_hour_rows(payload)
    groups = defaultdict(list)
    fetched_local_date = local_standard_date(fetched_time)
    fetched_at = fetched_time.isoformat()

    for row in rows:
        local_date = local_standard_date(row["time"])
        if local_date < fetched_local_date:
            continue
        groups[local_date].append(row)

    summaries = []
    for local_date, day_rows in sorted(groups.items()):
        if len(day_rows) < min_hours_for_daily_summary(local_date, fetched_local_date):
            continue

        peak = max(day_rows, key=lambda row: row["temp_f"])
        peak_time = peak["time"]
        lead_hours = (peak_time.astimezone(timezone.utc) - fetched_time).total_seconds() / 3600
        method = (
            "max hourly temperature across remaining target SFO local date"
            if local_date == fetched_local_date
            else "max hourly temperature across the target SFO local date"
        )
        summaries.append(
            {
                "available": True,
                "source": "Google Weather API forecast.hours",
                "method": method,
                "target_date": local_date.isoformat(),
                "lead_hours": round(lead_hours, 2),
                "highF": round(peak["temp_f"], 2),
                "peak_hour_local": peak_time.strftime("%Y-%m-%d %H:%M %Z"),
                "hours_used": len(day_rows),
                "forecast_start_local": day_rows[0]["time"].strftime("%Y-%m-%d %H:%M %Z"),
                "forecast_end_local": day_rows[-1]["time"].strftime("%Y-%m-%d %H:%M %Z"),
                "condition": condition_text(peak["hour"]),
                "precipitation_probability_pct": precip_probability(peak["hour"]),
                "fetched_at": fetched_at,
                "time_zone": payload.get("timeZone", {}).get("id"),
                "max_calls_per_day": usage.get("daily_event_budget"),
                "calls_used_today": usage.get("daily_events"),
                "max_google_events_per_month": usage.get("monthly_event_budget"),
                "google_events_used_month": usage.get("monthly_events"),
                "google_refreshes_today": usage.get("refreshes"),
            }
        )
    return summaries


def summarize_forecast(payload, target_iso, usage):
    fetched_time = datetime.now(timezone.utc)
    summaries = daily_high_summaries(payload, usage, fetched_time)
    summary = next((row for row in summaries if row["target_date"] == target_iso), None)

    if not summary:
        raise ValueError(f"Google hourly forecast did not include {target_iso}")

    summary = dict(summary)
    summary["daily_highs"] = [dict(row) for row in summaries]
    summary["google_daily_forecast_highs"] = google_daily_api_high_rows(payload.get("dailyForecast") or {})
    summary["google_current_conditions"] = google_current_conditions_summary(
        payload.get("currentConditions") or {}
    )
    summary["google_weather_events_used"] = payload.get("google_weather_events_used")
    summary["max_google_events_per_month"] = usage.get("monthly_event_budget")
    summary["google_events_used_month"] = usage.get("monthly_events")
    summary["google_refreshes_today"] = usage.get("refreshes")
    return summary


def fetch_hourly_page(key, page_token=None):
    params = urlencode(
        {
            "key": key,
            "location.latitude": f"{SFO_POINT['lat']:.4f}",
            "location.longitude": f"{SFO_POINT['lon']:.4f}",
            "hours": str(HOURLY_LOOKAHEAD_HOURS),
            "pageSize": str(HOURLY_PAGE_SIZE),
            "unitsSystem": "IMPERIAL",
            **({"pageToken": page_token} if page_token else {}),
        }
    )
    with urlopen(f"{HOURLY_API_URL}?{params}", timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_daily_forecast(key):
    params = urlencode(
        {
            "key": key,
            "location.latitude": f"{SFO_POINT['lat']:.4f}",
            "location.longitude": f"{SFO_POINT['lon']:.4f}",
            "days": "3",
            "unitsSystem": "IMPERIAL",
        }
    )
    with urlopen(f"{DAILY_API_URL}?{params}", timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_current_conditions(key):
    params = urlencode(
        {
            "key": key,
            "location.latitude": f"{SFO_POINT['lat']:.4f}",
            "location.longitude": f"{SFO_POINT['lon']:.4f}",
            "unitsSystem": "IMPERIAL",
        }
    )
    with urlopen(f"{CURRENT_API_URL}?{params}", timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_google_forecast(key):
    hours = []
    time_zone = None
    page_token = None
    events_used = 0

    while True:
        payload = fetch_hourly_page(key, page_token)
        events_used += 1
        hours.extend(payload.get("forecastHours") or [])
        time_zone = time_zone or payload.get("timeZone")
        page_token = payload.get("nextPageToken")
        if not page_token or len(hours) >= HOURLY_LOOKAHEAD_HOURS:
            break

    daily_forecast = None
    if ENABLE_GOOGLE_DAILY_FORECAST:
        daily_forecast = fetch_daily_forecast(key)
        events_used += 1

    current_conditions = None
    if ENABLE_GOOGLE_CURRENT_CONDITIONS:
        current_conditions = fetch_current_conditions(key)
        events_used += 1

    return {
        "forecastHours": hours,
        "timeZone": time_zone,
        "dailyForecast": daily_forecast,
        "currentConditions": current_conditions,
        "google_weather_events_used": events_used,
    }


def read_nws_json(url):
    request = Request(url, headers={"User-Agent": NWS_USER_AGENT})
    with urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def read_public_json(url):
    with urlopen(url, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_iso_duration(raw):
    match = DURATION_RE.match(raw or "PT1H")
    if not match:
        return timedelta(hours=1)
    days = int(match.group(1) or 0)
    hours = int(match.group(2) or 0)
    minutes = int(match.group(3) or 0)
    return timedelta(days=days, hours=hours, minutes=minutes)


def target_window_utc(target_iso):
    return utc_window_for_local_standard_date(target_iso)


def interval_touches_date(valid_time, target_iso):
    start_raw, _, duration_raw = valid_time.partition("/")
    start = parse_google_timestamp(start_raw)
    if not start:
        return False
    end = start + parse_iso_duration(duration_raw)
    target_start, target_end = target_window_utc(target_iso)
    return start.astimezone(timezone.utc) < target_end and end.astimezone(timezone.utc) > target_start


def nws_value_to_f(value, unit):
    if value is None:
        return None
    value = float(value)
    return value * 9 / 5 + 32 if "degC" in str(unit) else value


def load_nws_forecast_high(target_iso):
    point_url = f"{NWS_API_URL}/points/{SFO_POINT['lat']:.4f},{SFO_POINT['lon']:.4f}"
    point = read_nws_json(point_url)
    props = point.get("properties") or {}

    if props.get("forecastGridData"):
        grid = read_nws_json(props["forecastGridData"])
        layer = (grid.get("properties") or {}).get("maxTemperature") or {}
        highs = [
            nws_value_to_f(row.get("value"), layer.get("uom"))
            for row in layer.get("values") or []
            if interval_touches_date(row.get("validTime", ""), target_iso)
        ]
        highs = [value for value in highs if value is not None]
        if highs:
            return {
                "highF": round(max(highs), 2),
                "source": "NWS forecastGridData maxTemperature",
                "detail": props.get("gridId")
                and f"{props['gridId']} grid {props.get('gridX')},{props.get('gridY')}",
            }

    if props.get("forecastHourly"):
        hourly = read_nws_json(props["forecastHourly"])
        highs = []
        for period in (hourly.get("properties") or {}).get("periods") or []:
            start = parse_google_timestamp(period.get("startTime"))
            if not start or local_standard_date(start).isoformat() != target_iso:
                continue
            unit = period.get("temperatureUnit")
            temp = float(period["temperature"])
            highs.append(temp * 9 / 5 + 32 if unit == "C" else temp)
        if highs:
            return {
                "highF": round(max(highs), 2),
                "source": "NWS hourly forecast",
                "detail": "Hourly forecast fallback",
            }

    return {"highF": None, "source": "NWS", "error": "NWS forecast did not include target high"}


def load_open_meteo_forecast_high(target_iso):
    params = urlencode(
        {
            "latitude": f"{SFO_POINT['lat']:.4f}",
            "longitude": f"{SFO_POINT['lon']:.4f}",
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            # Fixed PST (IANA POSIX sign) so the daily max covers the same
            # window as the NWS/Kalshi settlement day.
            "timezone": "Etc/GMT+8",
            "forecast_days": "4",
        }
    )
    data = read_public_json(f"{OPEN_METEO_API_URL}?{params}")
    dates = (data.get("daily") or {}).get("time") or []
    highs = (data.get("daily") or {}).get("temperature_2m_max") or []
    if target_iso in dates:
        value = highs[dates.index(target_iso)]
        if value is not None:
            return {
                "highF": round(float(value), 2),
                "source": "Open-Meteo daily forecast",
                "detail": "SFO coordinate daily high",
            }
    return {"highF": None, "source": "Open-Meteo", "error": "Open-Meteo did not include target high"}


def load_history_high(target_iso):
    if not FORECAST_DATA_PATH.exists():
        return {"highF": None, "source": "SFO history", "error": "forecast_data.json missing"}
    data = read_json(FORECAST_DATA_PATH, {})
    row = (data.get("table") or {}).get(target_iso[5:])
    if not row or row.get("mean") is None:
        return {"highF": None, "source": "SFO history", "error": "No climatology row"}
    return {
        "highF": round(float(row["mean"]), 2),
        "source": "SFO historical climatology",
        "detail": f"{data.get('n_years')} years, {row.get('n')} nearby-date samples",
    }


def load_station_observation(station_id):
    data = read_nws_json(f"{NWS_API_URL}/stations/{station_id}/observations/latest")
    props = data.get("properties") or {}
    temp = props.get("temperature") or {}
    observed_at = parse_google_timestamp(props.get("timestamp"))
    return {
        "station_id": station_id,
        "temp_f": nws_value_to_f(temp.get("value"), temp.get("unitCode")),
        "observed_at": observed_at,
    }


def station_adjustment():
    observations = []
    for station_id in AIRPORT_STATIONS:
        try:
            observations.append(load_station_observation(station_id))
        except Exception:
            continue

    now = datetime.now(timezone.utc)
    fresh = []
    for obs in observations:
        observed_at = obs.get("observed_at")
        if not observed_at or not finite(obs.get("temp_f")):
            continue
        age_minutes = (now - observed_at.astimezone(timezone.utc)).total_seconds() / 60
        if 0 <= age_minutes <= FRESH_OBSERVATION_MINUTES:
            fresh.append(obs)

    sfo = next((obs for obs in fresh if obs["station_id"] == "KSFO"), None)
    neighbors = [obs for obs in fresh if obs["station_id"] != "KSFO"]
    if not sfo or not neighbors:
        return {"value": 0.0, "fresh_station_count": len(fresh), "detail": "No fresh SFO plus neighbor context"}

    neighbor_avg = sum(obs["temp_f"] for obs in neighbors) / len(neighbors)
    offset = sfo["temp_f"] - neighbor_avg
    value = max(-1.0, min(1.0, offset * 0.04))
    return {
        "value": round(value, 2),
        "fresh_station_count": len(fresh),
        "detail": f"SFO offset {offset:.1f}F against {len(neighbors)} neighbors",
    }


def safe_source(loader, fallback_name):
    try:
        return loader()
    except Exception as exc:
        return {"highF": None, "source": fallback_name, "error": type(exc).__name__}


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def normalize_weights(weights):
    total = sum(value for value in weights.values() if finite(value) and value > 0)
    if total <= 0:
        return dict(BLEND_WEIGHTS)
    return {
        key: (float(value) / total if finite(value) and value > 0 else 0.0)
        for key, value in weights.items()
    }


def latest_scored_blend_rows():
    if not DB_PATH.exists():
        return []
    try:
        with sqlite3.connect(DB_PATH) as conn:
            if not table_exists(conn, "forecast_blend_daily_high"):
                return []
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT b.target_date,
                       b.actual_high_f,
                       b.google_high_f,
                       b.nws_high_f,
                       b.open_meteo_high_f,
                       b.history_high_f,
                       b.fetched_at,
                       b.details_json
                FROM forecast_blend_daily_high b
                WHERE b.actual_high_f IS NOT NULL
                  AND b.abs_error_f IS NOT NULL
                ORDER BY b.target_date, b.fetched_at
                """
            ).fetchall()
            eligible = [
                row
                for row in rows
                if is_clean_next_day_forecast(
                    row["target_date"],
                    row["fetched_at"],
                    row["details_json"],
                )
            ]
            latest_by_day = {}
            for row in eligible:
                current = latest_by_day.get(row["target_date"])
                if current is None or row["fetched_at"] > current["fetched_at"]:
                    latest_by_day[row["target_date"]] = row
            return list(latest_by_day.values())
    except sqlite3.Error:
        return []


def adaptive_blend_weights():
    cached = getattr(adaptive_blend_weights, "_cached", None)
    if cached is not None:
        return cached

    base = dict(BLEND_WEIGHTS)
    rows = latest_scored_blend_rows()
    scored_days = len({row["target_date"] for row in rows})
    metadata = {
        "mode": "base",
        "reason": (
            f"collecting clean next-day scored blend days; need {ADAPTIVE_WEIGHT_MIN_SCORED_DAYS}, "
            f"have {scored_days}"
        ),
        "scored_days": scored_days,
        "eligibility": "last pre-midnight SFO snapshot from the day before target; excludes observed lock/floor rows",
        "base_weights": base,
        "weights": base,
        "source_mae_f": {},
        "source_counts": {},
        "learned_share": 0.0,
    }

    if scored_days < ADAPTIVE_WEIGHT_MIN_SCORED_DAYS:
        result = (base, metadata)
        adaptive_blend_weights._cached = result
        return result

    # Walk-forward gate: weights learned from the older days must beat the
    # base blend on the most recent days before they get any live share.
    ordered_days = sorted({row["target_date"] for row in rows})
    holdout_count = max(ADAPTIVE_WEIGHT_HOLDOUT_MIN_DAYS, len(ordered_days) // 3)
    holdout_days = set(ordered_days[-holdout_count:])
    train_rows = [row for row in rows if row["target_date"] not in holdout_days]
    holdout_rows = [row for row in rows if row["target_date"] in holdout_days]

    learned_share = min(
        ADAPTIVE_WEIGHT_MAX_LEARNED_SHARE,
        0.25 + (scored_days - ADAPTIVE_WEIGHT_MIN_SCORED_DAYS) * 0.02,
    )

    train_learned, train_mae, train_counts = learned_source_weights(train_rows, base)
    if train_learned is None:
        metadata.update(
            {
                "reason": "not enough per-source scored samples to learn weights safely",
                "source_mae_f": {key: round(value, 2) for key, value in train_mae.items()},
                "source_counts": train_counts,
            }
        )
        result = (base, metadata)
        adaptive_blend_weights._cached = result
        return result

    candidate = normalize_weights(
        {
            key: base[key] * (1 - learned_share) + train_learned[key] * learned_share
            for key in base
        }
    )
    base_holdout_mae = blended_mae(holdout_rows, base)
    candidate_holdout_mae = blended_mae(holdout_rows, candidate)
    holdout_report = {
        "holdout_days": holdout_count,
        "base_mae_f": None if base_holdout_mae is None else round(base_holdout_mae, 3),
        "candidate_mae_f": None if candidate_holdout_mae is None else round(candidate_holdout_mae, 3),
    }
    if (
        base_holdout_mae is None
        or candidate_holdout_mae is None
        or candidate_holdout_mae >= base_holdout_mae
    ):
        metadata.update(
            {
                "reason": (
                    "learned weights did not improve walk-forward holdout blend error; "
                    "keeping base weights"
                ),
                "source_mae_f": {key: round(value, 2) for key, value in train_mae.items()},
                "source_counts": train_counts,
                "holdout": holdout_report,
            }
        )
        result = (base, metadata)
        adaptive_blend_weights._cached = result
        return result

    # Methodology survived the holdout; refit on all scored days for the
    # weights that actually go live.
    learned, source_mae, source_counts = learned_source_weights(rows, base)
    if learned is None:  # pragma: no cover - train superset cannot lose sources
        result = (base, metadata)
        adaptive_blend_weights._cached = result
        return result
    mixed = normalize_weights(
        {
            key: base[key] * (1 - learned_share) + learned[key] * learned_share
            for key in base
        }
    )
    metadata.update(
        {
            "mode": "adaptive",
            "reason": (
                "weights nudged toward lower-MAE sources after beating the base "
                "blend on a walk-forward holdout"
            ),
            "source_mae_f": {key: round(value, 2) for key, value in source_mae.items()},
            "source_counts": source_counts,
            "learned_share": round(learned_share, 3),
            "learned_weights": {key: round(value, 4) for key, value in learned.items()},
            "weights": {key: round(value, 4) for key, value in mixed.items()},
            "holdout": holdout_report,
        }
    )
    result = (mixed, metadata)
    adaptive_blend_weights._cached = result
    return result


def learned_source_weights(rows, base):
    """Inverse-MAE weights from scored rows, or (None, mae, counts) if unsafe."""

    source_errors = {key: [] for key in ADAPTIVE_SOURCE_COLUMNS}
    for row in rows:
        actual = row["actual_high_f"]
        if not finite(actual):
            continue
        for key, column in ADAPTIVE_SOURCE_COLUMNS.items():
            value = row[column]
            if finite(value):
                source_errors[key].append(abs(float(value) - float(actual)))

    scored_days = len(rows)
    min_source_samples = max(3, min(ADAPTIVE_WEIGHT_MIN_SCORED_DAYS, scored_days // 2))
    source_mae = {
        key: sum(errors) / len(errors)
        for key, errors in source_errors.items()
        if len(errors) >= min_source_samples
    }
    source_counts = {key: len(errors) for key, errors in source_errors.items()}
    if len(source_mae) < 2:
        return None, source_mae, source_counts

    inverse_scores = {key: 1 / max(mae, 0.5) for key, mae in source_mae.items()}
    learned_for_scored = normalize_weights(inverse_scores)
    missing_mass = sum(base[key] for key in base if key not in learned_for_scored)
    learned = {}
    for key in base:
        if key in learned_for_scored:
            learned[key] = learned_for_scored[key] * max(0.0, 1 - missing_mass)
        else:
            learned[key] = base[key]
    return normalize_weights(learned), source_mae, source_counts


def blended_mae(rows, weights):
    """MAE of the weighted source blend over scored rows, or None if empty."""

    errors = []
    for row in rows:
        actual = row["actual_high_f"]
        if not finite(actual):
            continue
        total = 0.0
        weight_sum = 0.0
        for key, column in ADAPTIVE_SOURCE_COLUMNS.items():
            value = row[column]
            weight = weights.get(key, 0.0)
            if finite(value) and weight > 0:
                total += weight * float(value)
                weight_sum += weight
        if weight_sum <= 0:
            continue
        errors.append(abs(total / weight_sum - float(actual)))
    if not errors:
        return None
    return sum(errors) / len(errors)


def target_summary(summary, target_iso):
    for row in summary.get("daily_highs") or []:
        if row.get("target_date") == target_iso:
            return row
    return summary if summary.get("target_date") == target_iso else None


def build_blend_snapshot(summary, target_iso):
    google_row = target_summary(summary, target_iso)
    if not google_row or not finite(google_row.get("highF")):
        return None

    fetched_at = google_row.get("fetched_at") or summary.get("fetched_at")
    google_hourly_high = float(google_row["highF"])
    google_daily_row = google_daily_api_high_for(summary, target_iso)
    google_daily_high = (
        float(google_daily_row["highF"])
        if google_daily_row and finite(google_daily_row.get("highF"))
        else None
    )
    google_internal_weight = (
        max(0.0, min(0.40, GOOGLE_DAILY_INTERNAL_WEIGHT))
        if google_daily_high is not None
        else 0.0
    )
    google_composite_high = (
        google_hourly_high * (1 - google_internal_weight)
        + google_daily_high * google_internal_weight
        if google_daily_high is not None
        else google_hourly_high
    )
    google_internal_gap = (
        round(google_daily_high - google_hourly_high, 2)
        if google_daily_high is not None
        else None
    )
    google_components = {
        "hourly_local_day_high_f": round(google_hourly_high, 2),
        "daily_endpoint_high_f": round(google_daily_high, 2) if google_daily_high is not None else None,
        "daily_internal_weight": google_internal_weight,
        "daily_minus_hourly_gap_f": google_internal_gap,
        "gap_warning_f": GOOGLE_DAILY_DISAGREEMENT_WARN_F,
        "current_conditions": summary.get("google_current_conditions"),
        "weather_events_used": summary.get("google_weather_events_used"),
    }
    google_detail = google_row.get("peak_hour_local")
    if google_daily_high is not None:
        google_detail = (
            f"{google_row.get('peak_hour_local')}; "
            f"daily endpoint {google_daily_high:.1f}F"
        )
    sources = {
        "google": {
            "highF": round(google_composite_high, 2),
            "lockHighF": round(max(google_hourly_high, google_daily_high or google_hourly_high), 2),
            "source": "Google Weather API forecast.hours + forecast.days",
            "detail": google_detail,
            "components": google_components,
            "warning": (
                f"Google hourly/daily gap {abs(google_internal_gap):.1f}F exceeds "
                f"{GOOGLE_DAILY_DISAGREEMENT_WARN_F:.1f}F"
                if google_internal_gap is not None
                and abs(google_internal_gap) > GOOGLE_DAILY_DISAGREEMENT_WARN_F
                else None
            ),
        },
        "nws": safe_source(lambda: load_nws_forecast_high(target_iso), "NWS"),
        "open_meteo": safe_source(lambda: load_open_meteo_forecast_high(target_iso), "Open-Meteo"),
        "history": load_history_high(target_iso),
    }
    adjustment = station_adjustment()
    blend_weights, weight_metadata = adaptive_blend_weights()

    available = {
        key: row
        for key, row in sources.items()
        if finite(row.get("highF")) and blend_weights.get(key, 0) > 0
    }
    if not available:
        return None

    total_weight = sum(blend_weights[key] for key in available)
    normalized_weights = {
        key: blend_weights[key] / total_weight
        for key in sources
        if key in available
    }
    weighted_high = sum(sources[key]["highF"] * normalized_weights[key] for key in available)
    raw_predicted = weighted_high + adjustment["value"]
    observed_decision = observed_high_decision(target_iso, sources)
    predicted = raw_predicted
    method = "weighted Google + NWS + Open-Meteo + SFO history with capped station adjustment"
    if observed_decision:
        observed_high = observed_decision["highF"]
        if observed_decision["mode"] == "lock":
            predicted = observed_high
            method = f"official NWS observed high ({observed_decision['reason']})"
        else:
            predicted = max(raw_predicted, observed_high)
            method = "weighted blend floored by NWS observed high-so-far"

    return {
        "fetched_at": fetched_at,
        "target_date": target_iso,
        "lead_hours": google_row.get("lead_hours"),
        "method": method,
        "predicted_high_f": round(predicted, 2),
        "google_high_f": sources["google"].get("highF"),
        "nws_high_f": sources["nws"].get("highF"),
        "open_meteo_high_f": sources["open_meteo"].get("highF"),
        "history_high_f": sources["history"].get("highF"),
        "google_weight": round(normalized_weights.get("google", 0), 4),
        "nws_weight": round(normalized_weights.get("nws", 0), 4),
        "open_meteo_weight": round(normalized_weights.get("open_meteo", 0), 4),
        "history_weight": round(normalized_weights.get("history", 0), 4),
        "station_adjustment_f": adjustment["value"],
        "fresh_station_count": adjustment["fresh_station_count"],
        "source_count": len(available),
        "time_zone": google_row.get("time_zone") or summary.get("time_zone"),
        "max_calls_per_day": google_row.get("max_calls_per_day") or summary.get("max_calls_per_day"),
        "calls_used_today": google_row.get("calls_used_today") or summary.get("calls_used_today"),
        "details": {
            "sources": sources,
            "google_weather_api": {
                "monthly_free_cap": GOOGLE_WEATHER_MONTHLY_FREE_CAP,
                "monthly_event_budget": summary.get("max_google_events_per_month"),
                "monthly_events_used": summary.get("google_events_used_month"),
                "daily_event_budget": google_row.get("max_calls_per_day"),
                "daily_events_used": google_row.get("calls_used_today"),
                "refreshes_today": summary.get("google_refreshes_today"),
                "enabled_daily_forecast": ENABLE_GOOGLE_DAILY_FORECAST,
                "enabled_current_conditions": ENABLE_GOOGLE_CURRENT_CONDITIONS,
            },
            "station_adjustment": adjustment,
            "base_weights": BLEND_WEIGHTS,
            "blend_weighting": weight_metadata,
            "raw_weighted_prediction_f": round(raw_predicted, 2),
            "observed_high_decision": observed_decision,
        },
    }


def load_nws_observed_high(target_iso):
    if not DB_PATH.exists():
        return None
    try:
        with sqlite3.connect(DB_PATH) as conn:
            if not table_exists(conn, "nws_daily_high_ground_truth"):
                return None
            row = conn.execute(
                """
                SELECT high_f,
                       high_observed_at,
                       observation_count,
                       is_complete,
                       updated_at
                FROM nws_daily_high_ground_truth
                WHERE station_id = 'KSFO'
                  AND local_date = ?
                  AND high_f IS NOT NULL
                """,
                (target_iso,),
            ).fetchone()
    except sqlite3.Error:
        return None

    if not row:
        return None
    return {
        "highF": round(float(row[0]), 2),
        "high_observed_at": row[1],
        "observation_count": row[2],
        "is_complete": bool(row[3]),
        "updated_at": row[4],
        "source": "NWS KSFO observed daily high",
    }


def observed_high_decision(target_iso, sources):
    # Same-day check on the settlement clock, so the observed-high lock/floor
    # applies to the right Kalshi day during the DST 00:00-01:00 window.
    if target_iso != settlement_today_iso():
        return None

    observed = load_nws_observed_high(target_iso)
    if not observed or not finite(observed.get("highF")):
        return None

    live_values = [
        row.get("lockHighF", row.get("highF"))
        for key, row in sources.items()
        if key != "history" and finite(row.get("lockHighF", row.get("highF")))
    ]
    max_live_forecast = max(live_values) if live_values else None

    decision = dict(observed)
    decision["max_live_forecast_f"] = (
        round(float(max_live_forecast), 2)
        if max_live_forecast is not None
        else None
    )

    if observed["is_complete"]:
        decision.update(
            {
                "mode": "lock",
                "reason": "completed local day",
            }
        )
        return decision

    if max_live_forecast is not None and observed["highF"] >= max_live_forecast - 0.25:
        decision.update(
            {
                "mode": "lock",
                "reason": "NWS high-so-far meets or exceeds live forecast highs",
            }
        )
        return decision

    decision.update(
        {
            "mode": "floor",
            "reason": "same-day forecast cannot go below observed KSFO high-so-far",
        }
    )
    return decision


def blend_targets(summary, primary_target_iso):
    targets = []
    today = settlement_today_iso()
    for row in summary.get("daily_highs") or []:
        target_iso = row.get("target_date")
        if target_iso in {today, primary_target_iso} and target_iso not in targets:
            targets.append(target_iso)
    if primary_target_iso not in targets:
        targets.append(primary_target_iso)
    return targets


def unavailable(reason):
    usage = load_usage()
    return {
        "available": False,
        "reason": reason,
        "target_date": target_date(),
        "max_calls_per_day": usage.get("daily_event_budget"),
        "calls_used_today": usage.get("daily_events"),
        "max_google_events_per_month": usage.get("monthly_event_budget"),
        "google_events_used_month": usage.get("monthly_events"),
        "fetched_at": None,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="fetch a fresh Google forecast")
    parser.add_argument("--force", action="store_true", help="ignore a valid cache")
    args = parser.parse_args()

    target_iso = target_date()
    cache = read_json(CACHE_PATH, {})
    archived_cache = False
    archive_stats = {"daily_rows": 0, "blend_rows": 0, "hourly_rows": 0, "scored": 0}
    if cache.get("available") and cache.get("source") == "Google Weather API forecast.hours":
        archive_stats = archive_forecast(cache)
        archived_cache = True
    else:
        archive_stats = score_archive()

    if cache_matches(cache, target_iso) and not args.force and not args.refresh:
        blends = [
            build_blend_snapshot(cache, blend_target)
            for blend_target in blend_targets(cache, target_iso)
        ]
        cache["blend_snapshots"] = [blend for blend in blends if blend]
        cache["blend_generated_at"] = datetime.now(timezone.utc).isoformat()
        archive_stats = archive_forecast(cache, None, blends)
        write_json(CACHE_PATH, cache)
        print(
            f"reblended cached Google forecast for {target_iso}; "
            f"daily rows {archive_stats['daily_rows']}, "
            f"blend rows {archive_stats['blend_rows']}, "
            f"hourly rows {archive_stats['hourly_rows']}, "
            f"scored {archive_stats['scored']}"
        )
        return

    key = api_key()
    if not key:
        if not cache_matches(cache, target_iso):
            write_json(CACHE_PATH, unavailable("Google Weather cache unavailable."))
        archived = "; archived previous cache" if archived_cache else ""
        print(f"missing {API_KEY_ENV}; Google cache not refreshed{archived}; scored {archive_stats['scored']}")
        return

    usage = load_usage()
    estimated_events = estimated_google_weather_events_per_refresh()
    if not usage_has_budget(usage, estimated_events):
        if not cache_matches(cache, target_iso):
            write_json(CACHE_PATH, unavailable("Google Weather event budget reached."))
        print(
            "Google Weather event budget reached "
            f"(daily {usage.get('daily_events')}/{usage.get('daily_event_budget')}, "
            f"monthly {usage.get('monthly_events')}/{usage.get('monthly_event_budget')}); "
            f"scored {archive_stats['scored']}"
        )
        return

    usage = reserve_google_weather_events(usage, estimated_events)
    write_json(USAGE_PATH, usage)
    try:
        raw = fetch_google_forecast(key)
        usage = adjust_reserved_google_weather_events(
            usage,
            estimated_events,
            int(raw.get("google_weather_events_used") or estimated_events),
        )
        write_json(USAGE_PATH, usage)
        summary = summarize_forecast(raw, target_iso, usage)
    except Exception as exc:
        if not cache_matches(cache, target_iso):
            write_json(CACHE_PATH, unavailable("Google Weather request failed."))
        print(
            f"Google Weather request failed without saving a URL: {type(exc).__name__}; "
            f"scored {archive_stats['scored']}"
        )
        return

    blends = [
        build_blend_snapshot(summary, blend_target)
        for blend_target in blend_targets(summary, target_iso)
    ]
    summary["blend_snapshots"] = [blend for blend in blends if blend]
    summary["blend_generated_at"] = datetime.now(timezone.utc).isoformat()
    archive_stats = archive_forecast(summary, raw, blends)
    write_json(CACHE_PATH, summary)
    write_json(USAGE_PATH, usage)
    print(
        f"wrote {CACHE_PATH} and archived to {DB_PATH} for {target_iso}; "
        f"Google Weather events today: {usage['daily_events']}/{usage['daily_event_budget']}; "
        f"month: {usage['monthly_events']}/{usage['monthly_event_budget']}; "
        f"daily rows {archive_stats['daily_rows']}; "
        f"blend rows {archive_stats['blend_rows']}; "
        f"hourly rows {archive_stats['hourly_rows']}; "
        f"scored {archive_stats['scored']}"
    )


if __name__ == "__main__":
    main()
