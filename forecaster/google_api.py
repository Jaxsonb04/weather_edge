#!/usr/bin/env python3
"""Google Weather API fetch, parse, cache, and paid-event budget mechanics."""

import json
import math
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from settlement_calendar import (
    local_standard_date,
    today_local_standard,
    utc_window_for_local_standard_date,
)
from weather_cache_config import (
    API_KEY_ENV,
    CACHE_PATH,
    CURRENT_API_URL,
    DAILY_API_URL,
    ENABLE_GOOGLE_CURRENT_CONDITIONS,
    ENABLE_GOOGLE_DAILY_FORECAST,
    GOOGLE_DAILY_DISAGREEMENT_WARN_F,
    GOOGLE_DAILY_INTERNAL_WEIGHT,
    GOOGLE_WEATHER_DAILY_EVENT_BUDGET,
    GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET,
    GOOGLE_WEATHER_MONTHLY_FREE_CAP,
    HOURLY_API_URL,
    HOURLY_LOOKAHEAD_HOURS,
    HOURLY_PAGE_SIZE,
    MIN_HOURS_FOR_DAILY_HIGH,
    SFO_POINT,
    SFO_TZ,
    USAGE_PATH,
)


class GoogleFetchError(RuntimeError):
    """A safe Google fetch failure with conservative dispatch accounting."""

    def __init__(self, endpoint, dispatched_events):
        self.endpoint = endpoint
        self.dispatched_events = max(0, int(dispatched_events))
        super().__init__(f"Google Weather {endpoint} request failed")


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


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


def _fetch_google_json(endpoint, base_url, params):
    dispatched_events = 0
    try:
        query = urlencode(params)
        request_url = f"{base_url}?{query}"
        dispatched_events += 1
        with urlopen(request_url, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Google Weather response was not a JSON object")
        return payload
    except Exception:
        # Transport errors may embed the API key and full request URL. Keep the
        # public exception entirely detached from that unsafe exception chain.
        failure = GoogleFetchError(endpoint, dispatched_events)
    raise failure


def fetch_hourly_page(key, page_token=None):
    return _fetch_google_json(
        "hourly forecast",
        HOURLY_API_URL,
        {
            "key": key,
            "location.latitude": f"{SFO_POINT['lat']:.4f}",
            "location.longitude": f"{SFO_POINT['lon']:.4f}",
            "hours": str(HOURLY_LOOKAHEAD_HOURS),
            "pageSize": str(HOURLY_PAGE_SIZE),
            "unitsSystem": "IMPERIAL",
            **({"pageToken": page_token} if page_token else {}),
        },
    )


def fetch_daily_forecast(key):
    return _fetch_google_json(
        "daily forecast",
        DAILY_API_URL,
        {
            "key": key,
            "location.latitude": f"{SFO_POINT['lat']:.4f}",
            "location.longitude": f"{SFO_POINT['lon']:.4f}",
            "days": "3",
            "unitsSystem": "IMPERIAL",
        },
    )


def fetch_current_conditions(key):
    return _fetch_google_json(
        "current conditions",
        CURRENT_API_URL,
        {
            "key": key,
            "location.latitude": f"{SFO_POINT['lat']:.4f}",
            "location.longitude": f"{SFO_POINT['lon']:.4f}",
            "unitsSystem": "IMPERIAL",
        },
    )


def fetch_google_forecast(key):
    hours = []
    time_zone = None
    page_token = None
    events_used = 0

    # Hard cap on paid hourly pages. The budget reserves ~3 events per refresh; a
    # misbehaving API that keeps returning a nextPageToken with few/no new hours
    # could otherwise spend dozens of paid events in one refresh (and the budget
    # reconciliation only runs AFTER, so it can't stop the current run). At ~24
    # hours/page, the 72h lookahead needs ~3-4 pages; cap well above that and
    # break on no forward progress.
    max_hourly_pages = max(4, HOURLY_LOOKAHEAD_HOURS // 24 + 2)
    while True:
        try:
            payload = fetch_hourly_page(key, page_token)
        except GoogleFetchError as exc:
            exc.dispatched_events += events_used
            raise
        events_used += 1
        new_hours = payload.get("forecastHours") or []
        hours.extend(new_hours)
        time_zone = time_zone or payload.get("timeZone")
        page_token = payload.get("nextPageToken")
        if not page_token or len(hours) >= HOURLY_LOOKAHEAD_HOURS:
            break
        if events_used >= max_hourly_pages:
            print(
                f"[google] hourly pagination hit hard cap of {max_hourly_pages} pages "
                f"({len(hours)} hours); stopping to protect the paid-event budget"
            )
            break
        if not new_hours:
            print("[google] hourly page returned no new hours; stopping to avoid a paid-call loop")
            break

    daily_forecast = None
    if ENABLE_GOOGLE_DAILY_FORECAST:
        try:
            daily_forecast = fetch_daily_forecast(key)
        except GoogleFetchError as exc:
            exc.dispatched_events += events_used
            raise
        events_used += 1

    current_conditions = None
    if ENABLE_GOOGLE_CURRENT_CONDITIONS:
        try:
            current_conditions = fetch_current_conditions(key)
        except GoogleFetchError as exc:
            exc.dispatched_events += events_used
            raise
        events_used += 1

    return {
        "forecastHours": hours,
        "timeZone": time_zone,
        "dailyForecast": daily_forecast,
        "currentConditions": current_conditions,
        "google_weather_events_used": events_used,
    }
