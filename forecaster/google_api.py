#!/usr/bin/env python3
"""Google Weather API fetch, parse, cache, and paid-event budget mechanics."""

import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from cities import CityConfig
from google_weather_store import GoogleRuntimeStore, GoogleUsageLedger
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
    GOOGLE_HOURLY_MAX_PAGES,
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

    @classmethod
    def from_unrecognized(cls, endpoint, prior_events, exc):
        try:
            current_events = getattr(exc, "dispatched_events", 1)
        except Exception:
            current_events = 1
        if type(current_events) is not int or current_events < 0:
            current_events = 1
        return cls(endpoint, prior_events + current_events)


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
    actual_events = max(0, actual_events)
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

    # Hard cap on paid hourly pages. The budget reserves 3 events per refresh; a
    # misbehaving API that keeps returning a nextPageToken with few/no new hours
    # could otherwise spend dozens of paid events in one refresh (and the budget
    # reconciliation only runs AFTER, so it can't stop the current run). Three
    # full 24-hour pages cover the 72-hour lookahead; underfilled responses stop
    # at the same official ceiling and are marked incomplete by their row count.
    max_hourly_pages = GOOGLE_HOURLY_MAX_PAGES
    while events_used < max_hourly_pages:
        failure = None
        try:
            payload = fetch_hourly_page(key, page_token)
        except GoogleFetchError as exc:
            exc.dispatched_events += events_used
            raise
        except Exception as exc:
            failure = GoogleFetchError.from_unrecognized(
                "hourly forecast",
                events_used,
                exc,
            )
        if failure is not None:
            raise failure

        events_used += 1
        try:
            new_hours = payload.get("forecastHours") or []
            if not isinstance(new_hours, list) or not all(
                isinstance(hour, dict) for hour in new_hours
            ):
                raise ValueError("Google hourly forecast rows must be objects")
            hours.extend(new_hours)
            time_zone = time_zone or payload.get("timeZone")
            page_token = payload.get("nextPageToken")
            if page_token is not None and not isinstance(page_token, str):
                raise ValueError("Google hourly page token must be a string")
        except Exception:
            failure = GoogleFetchError("hourly forecast", events_used)
        if failure is not None:
            raise failure

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
        failure = None
        try:
            daily_forecast = fetch_daily_forecast(key)
        except GoogleFetchError as exc:
            exc.dispatched_events += events_used
            raise
        except Exception as exc:
            failure = GoogleFetchError.from_unrecognized(
                "daily forecast",
                events_used,
                exc,
            )
        if failure is not None:
            raise failure
        events_used += 1

    current_conditions = None
    if ENABLE_GOOGLE_CURRENT_CONDITIONS:
        failure = None
        try:
            current_conditions = fetch_current_conditions(key)
        except GoogleFetchError as exc:
            exc.dispatched_events += events_used
            raise
        except Exception as exc:
            failure = GoogleFetchError.from_unrecognized(
                "current conditions",
                events_used,
                exc,
            )
        if failure is not None:
            raise failure
        events_used += 1

    return {
        "forecastHours": hours,
        "timeZone": time_zone,
        "dailyForecast": daily_forecast,
        "currentConditions": current_conditions,
        "google_weather_events_used": events_used,
    }


# ---------------------------------------------------------------------------
# Task 4: city-aware requests, strict pagination, and per-page event
# accounting through the transactional usage ledger and the TTL-enforced
# runtime store. Every function below is explicit about which of the 15
# configured cities it is fetching for; none of it depends on SFO_TZ,
# SFO_POINT, or the legacy JSON usage/cache files above.
# ---------------------------------------------------------------------------


class GoogleCityFetchError(RuntimeError):
    """A safe, city-aware Google fetch failure with no embedded secrets.

    Only the city slug, endpoint name, and page number are ever included in
    the message -- never the API key, the full request URL, or the original
    transport/parse exception (see ``_dispatch_google_request``).
    """

    def __init__(self, *, city_slug, endpoint, page_number):
        self.city_slug = city_slug
        self.endpoint = endpoint
        self.page_number = page_number
        super().__init__(
            f"Google Weather {endpoint} request failed for {city_slug} page {page_number}"
        )


@dataclass(frozen=True)
class GoogleHourlyRow:
    valid_at: datetime
    temperature_f: float
    # The station's fixed-standard (never civil/DST) settlement day for this
    # reading -- see settlement_calendar.local_standard_date.
    station_date: date


@dataclass(frozen=True)
class GoogleDailyRow:
    target_date: str
    high_f: float


@dataclass(frozen=True)
class GoogleCurrentRow:
    observed_at: datetime
    temperature_f: float


@dataclass(frozen=True)
class GoogleFetchResult:
    city_slug: str
    station_id: str
    issued_at: datetime
    hourly_rows: tuple[GoogleHourlyRow, ...]
    daily_rows: tuple[GoogleDailyRow, ...]
    current_row: GoogleCurrentRow | None
    dispatched_events: int


def _resolve_instant(now):
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(timezone.utc)


def _open_google_request(base_url, params, *, transport):
    """Perform one Google Weather HTTP GET and return the raw response body.

    This is the ONLY place that ever holds the live request URL (which
    embeds the API key): it never logs it, never returns it, and any
    exception raised here is classified and re-raised as a sanitized
    ``GoogleCityFetchError`` by ``_dispatch_google_request`` -- the original
    exception (and the URL/key it may embed) never escapes that boundary.
    """

    query = urlencode(params)
    request_url = f"{base_url}?{query}"
    with transport(request_url, timeout=20) as response:
        return response.read()


def _decode_google_json(raw):
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("Google Weather response was not a JSON object")
    return payload


def _dispatch_google_request(*, usage, city, endpoint, page_number, request, parse, now=None):
    """Reserve, dispatch, and classify one Google Weather request.

    Reserves ledger budget before dispatch, marks the reservation dispatched
    immediately before the transport call, and completes it as success or a
    classified failure afterward -- no path leaves a reservation dangling. On
    any failure only a sanitized ``GoogleCityFetchError`` escapes; it is
    raised after the classification try/except blocks have fully unwound, so
    it never chains to the original (possibly secret-bearing) exception.
    """

    event = usage.reserve_event(
        city_slug=city.slug,
        station_id=city.nws_station_id,
        endpoint=endpoint,
        page_number=page_number,
        now=now,
    )
    try:
        usage.mark_dispatched(event, now=now)
    except Exception:
        usage.cancel_before_dispatch(event, now=now)
        raise

    error_kind = None
    response_status_class = None
    raw = None
    try:
        raw = request()
    except HTTPError as exc:
        error_kind = "http"
        code = getattr(exc, "code", None)
        response_status_class = code // 100 if isinstance(code, int) else None
    except TimeoutError:
        error_kind = "timeout"
    except URLError:
        error_kind = "transport"
    except Exception:
        error_kind = "unknown"

    result = None
    if error_kind is None:
        try:
            result = parse(raw)
        except Exception:
            error_kind = "parse"

    if error_kind is None:
        usage.complete_event(event, success=True, now=now)
        return result

    usage.complete_event(
        event,
        success=False,
        error_kind=error_kind,
        response_status_class=response_status_class,
        now=now,
    )
    raise GoogleCityFetchError(city_slug=city.slug, endpoint=endpoint, page_number=page_number)


def _city_hour_datetime(city, hour):
    start = hour.get("interval", {}).get("startTime")
    parsed = parse_google_timestamp(start)
    if parsed:
        return parsed.astimezone(timezone.utc)

    display = hour.get("displayDateTime") or {}
    year = display.get("year")
    month = display.get("month")
    day_num = display.get("day")
    if not all([year, month, day_num]):
        return None
    civil_tz = ZoneInfo(city.civil_tz_name)
    local = datetime(
        int(year),
        int(month),
        int(day_num),
        int(display.get("hours", display.get("hour", 0))),
        int(display.get("minutes", display.get("minute", 0))),
        tzinfo=civil_tz,
    )
    return local.astimezone(timezone.utc)


def _parse_hourly_page_rows(city, payload):
    raw_hours = payload.get("forecastHours")
    if raw_hours is None:
        raw_hours = []
    if not isinstance(raw_hours, list) or not all(isinstance(hour, dict) for hour in raw_hours):
        raise ValueError("Google hourly forecast rows must be objects")

    rows = []
    for hour in raw_hours:
        valid_at = _city_hour_datetime(city, hour)
        temperature_f = temp_to_f(hour.get("temperature") or {})
        if valid_at is None or temperature_f is None:
            continue
        rows.append(
            GoogleHourlyRow(
                valid_at=valid_at,
                temperature_f=round(temperature_f, 2),
                station_date=local_standard_date(valid_at, city.fixed_standard_timezone()),
            )
        )
    return tuple(rows)


def _google_civil_display_date(city, payload):
    display = payload.get("displayDate") or {}
    year, month, day = display.get("year"), display.get("month"), display.get("day")
    if not all([year, month, day]):
        return None
    try:
        return (
            datetime(int(year), int(month), int(day), tzinfo=ZoneInfo(city.civil_tz_name))
            .date()
            .isoformat()
        )
    except ValueError:
        return None


def _parse_daily_rows(city, payload):
    raw_days = payload.get("forecastDays")
    if raw_days is None:
        raw_days = []
    if not isinstance(raw_days, list) or not all(isinstance(day, dict) for day in raw_days):
        raise ValueError("Google daily forecast rows must be objects")

    rows = []
    for day in raw_days:
        target_date_iso = _google_civil_display_date(city, day)
        high_f = temp_to_f(day.get("maxTemperature") or {})
        if target_date_iso is None or high_f is None:
            continue
        rows.append(GoogleDailyRow(target_date=target_date_iso, high_f=round(high_f, 2)))
    return tuple(rows)


def _parse_current_row(payload, fetch_instant):
    temperature_f = temp_to_f(payload.get("temperature") or {})
    if temperature_f is None:
        return None
    observed_at = parse_google_timestamp(payload.get("currentTime")) or fetch_instant
    return GoogleCurrentRow(
        observed_at=observed_at.astimezone(timezone.utc),
        temperature_f=round(temperature_f, 2),
    )


def fetch_city_hourly(
    city: CityConfig,
    *,
    key: str,
    usage: GoogleUsageLedger,
    runtime: GoogleRuntimeStore,
    max_pages: int = 3,
    transport=urlopen,
    now: datetime | None = None,
) -> tuple[GoogleHourlyRow, ...]:
    """Fetch, account for, and durably publish one city's paginated hourly forecast.

    Reserves ledger budget before every page, marks the reservation
    dispatched immediately before the transport call, and completes it as
    success or a classified failure afterward. ``GOOGLE_HOURLY_MAX_PAGES``
    (an official hard ceiling of 3) always wins over a larger ``max_pages``;
    a ceiling of zero makes no hourly request at all.

    The complete multi-page generation is written to the runtime store in
    one atomic call only if every dispatched page in this attempt parsed
    successfully. Any failure aborts the whole attempt and writes nothing --
    a partial or empty write would wipe or falsely refresh whatever
    generation is already active for this city
    (see ``google_weather_store.GoogleRuntimeStore.replace_hourly_generation``).
    """

    effective_max_pages = min(max_pages, GOOGLE_HOURLY_MAX_PAGES)
    if effective_max_pages <= 0:
        return ()

    fetch_instant = _resolve_instant(now)
    collected: list[GoogleHourlyRow] = []
    page_token = None

    for page_number in range(1, effective_max_pages + 1):
        requested_token = page_token

        def _request(_token=requested_token):
            return _open_google_request(
                HOURLY_API_URL,
                {
                    "key": key,
                    "location.latitude": f"{city.latitude:.4f}",
                    "location.longitude": f"{city.longitude:.4f}",
                    "hours": str(HOURLY_LOOKAHEAD_HOURS),
                    "pageSize": str(HOURLY_PAGE_SIZE),
                    "unitsSystem": "IMPERIAL",
                    **({"pageToken": _token} if _token else {}),
                },
                transport=transport,
            )

        def _parse(raw):
            payload = _decode_google_json(raw)
            rows = _parse_hourly_page_rows(city, payload)
            next_token = payload.get("nextPageToken")
            if next_token is not None and not isinstance(next_token, str):
                raise ValueError("Google hourly page token must be a string")
            return rows, next_token

        rows, next_token = _dispatch_google_request(
            usage=usage,
            city=city,
            endpoint="hourly",
            page_number=page_number,
            request=_request,
            parse=_parse,
            now=now,
        )
        collected.extend(rows)
        page_token = next_token
        if not page_token:
            break

    temperatures_by_valid_at = {row.valid_at: row.temperature_f for row in collected}
    if temperatures_by_valid_at:
        runtime.replace_hourly_generation(
            city_slug=city.slug,
            station_id=city.nws_station_id,
            issued_at=fetch_instant,
            temperatures_by_valid_at=temperatures_by_valid_at,
            stored_at=fetch_instant,
        )
    return tuple(collected)


def fetch_city_daily(
    city: CityConfig,
    *,
    key: str,
    usage: GoogleUsageLedger,
    runtime: GoogleRuntimeStore,
    transport=urlopen,
    now: datetime | None = None,
) -> tuple[GoogleDailyRow, ...]:
    """Fetch, account for, and durably publish one city's daily forecast."""

    fetch_instant = _resolve_instant(now)

    def _request():
        return _open_google_request(
            DAILY_API_URL,
            {
                "key": key,
                "location.latitude": f"{city.latitude:.4f}",
                "location.longitude": f"{city.longitude:.4f}",
                "days": "3",
                "unitsSystem": "IMPERIAL",
            },
            transport=transport,
        )

    def _parse(raw):
        payload = _decode_google_json(raw)
        return _parse_daily_rows(city, payload)

    rows = _dispatch_google_request(
        usage=usage,
        city=city,
        endpoint="daily",
        page_number=1,
        request=_request,
        parse=_parse,
        now=now,
    )

    highs_by_target_date = {row.target_date: row.high_f for row in rows}
    if highs_by_target_date:
        runtime.replace_daily_generation(
            city_slug=city.slug,
            station_id=city.nws_station_id,
            issued_at=fetch_instant,
            highs_by_target_date=highs_by_target_date,
            stored_at=fetch_instant,
        )
    return rows


def fetch_city_current(
    city: CityConfig,
    *,
    key: str,
    usage: GoogleUsageLedger,
    runtime: GoogleRuntimeStore,
    transport=urlopen,
    now: datetime | None = None,
) -> GoogleCurrentRow | None:
    """Fetch, account for, and durably publish one city's current conditions."""

    fetch_instant = _resolve_instant(now)

    def _request():
        return _open_google_request(
            CURRENT_API_URL,
            {
                "key": key,
                "location.latitude": f"{city.latitude:.4f}",
                "location.longitude": f"{city.longitude:.4f}",
                "unitsSystem": "IMPERIAL",
            },
            transport=transport,
        )

    def _parse(raw):
        payload = _decode_google_json(raw)
        return _parse_current_row(payload, fetch_instant)

    row = _dispatch_google_request(
        usage=usage,
        city=city,
        endpoint="current",
        page_number=1,
        request=_request,
        parse=_parse,
        now=now,
    )

    if row is not None:
        runtime.replace_current_generation(
            city_slug=city.slug,
            station_id=city.nws_station_id,
            issued_at=fetch_instant,
            observed_at=row.observed_at,
            temperature_f=row.temperature_f,
            stored_at=fetch_instant,
        )
    return row


def fetch_city_weather(
    city: CityConfig,
    *,
    key: str,
    usage: GoogleUsageLedger,
    runtime: GoogleRuntimeStore,
    include_daily: bool = True,
    include_current: bool = True,
    max_hourly_pages: int = 3,
    transport=urlopen,
    now: datetime | None = None,
) -> GoogleFetchResult:
    """Fetch every requested endpoint for one city as one bundled result.

    Each endpoint is independently reserved, dispatched, and completed
    through ``fetch_city_hourly``/``fetch_city_daily``/``fetch_city_current``;
    a failure in one endpoint's ledger/store state never contaminates
    another regardless of call order. This wrapper does not swallow
    failures -- it propagates the first one it hits; callers that need
    partial-failure tolerance across endpoints should call the per-endpoint
    functions directly (as the multi-city refresh orchestrator does).
    """

    fetch_instant = _resolve_instant(now)
    before = usage.usage(now=fetch_instant).monthly_events

    hourly_rows = fetch_city_hourly(
        city,
        key=key,
        usage=usage,
        runtime=runtime,
        max_pages=max_hourly_pages,
        transport=transport,
        now=now,
    )
    daily_rows = (
        fetch_city_daily(city, key=key, usage=usage, runtime=runtime, transport=transport, now=now)
        if include_daily
        else ()
    )
    current_row = (
        fetch_city_current(city, key=key, usage=usage, runtime=runtime, transport=transport, now=now)
        if include_current
        else None
    )

    after = usage.usage(now=fetch_instant).monthly_events
    return GoogleFetchResult(
        city_slug=city.slug,
        station_id=city.nws_station_id,
        issued_at=fetch_instant,
        hourly_rows=hourly_rows,
        daily_rows=daily_rows,
        current_row=current_row,
        dispatched_events=after - before,
    )
