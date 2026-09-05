"""One-day WeatherKit history capability check, entirely in memory.

This is deliberately not an archive downloader or an accuracy backtest. Apple's
historical conditions are not forecasts issued before a historical decision.
Only structural availability is returned; temperatures and response bodies are
never written, logged, or added to training data. See docs/APPLE-WEATHERKIT.md.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request

from apple_weatherkit import (
    WEATHERKIT_API_ROOT,
    WEATHERKIT_HTTP_TIMEOUT_SECONDS,
    WEATHERKIT_MAX_RESPONSE_BYTES,
    WeatherKitError,
    WeatherKitTokenProvider,
    _credentials_from_env,
    _env_enabled,
    _finite_number,
    _parse_datetime,
    _product,
    _weatherkit_urlopen,
)
from cities import CityConfig, get_city
from settlement_calendar import utc_window_for_local_standard_date


HISTORY_START = date(2021, 8, 1)


def _validate_day(city: CityConfig, target: date, now: datetime) -> None:
    if now.tzinfo is None:
        raise WeatherKitError("History probe requires an aware current time")
    today = now.astimezone(city.fixed_standard_timezone()).date()
    if target < HISTORY_START or target >= today:
        raise WeatherKitError("History probe requires one completed day since 2021-08-01")


def summarize_history(
    payload: dict[str, Any], *, city: CityConfig, target: date, now: datetime
) -> dict[str, Any]:
    """Validate exact 24-hour coverage without returning any weather values."""

    _validate_day(city, target, now)
    start, end = utc_window_for_local_standard_date(target, city.fixed_standard_timezone())
    hourly, _read_at, _expires_at = _product(payload, "forecastHourly", city=city)
    rows = hourly.get("hours")
    if not isinstance(rows, list):
        raise WeatherKitError("History response has invalid hourly rows")
    expected = {start + timedelta(hours=i) for i in range(24)}
    seen: set[datetime] = set()
    outside_window = 0
    for row in rows:
        if not isinstance(row, dict):
            raise WeatherKitError("History response has invalid hourly row")
        timestamp = _parse_datetime(row.get("forecastStart"), field="forecastStart")
        _finite_number(row.get("temperature"), field="temperature")
        if timestamp not in expected:
            outside_window += 1
            continue
        if timestamp in seen:
            raise WeatherKitError("History response has duplicate hourly timestamp")
        seen.add(timestamp)
    return {
        "purpose": "historical_conditions_capability_only",
        "city": city.slug,
        "target_date": target.isoformat(),
        "requested_start": start.isoformat(),
        "requested_end_exclusive": end.isoformat(),
        "covered_hours": len(seen),
        "outside_window_rows": outside_window,
        "complete_requested_day": seen == expected and outside_window == 0,
        "historical_forecast_vintage_verified": False,
        "weather_values_retained": False,
    }


def probe_history(
    *,
    city: CityConfig,
    target: date,
    now: datetime,
    token_provider: Callable[[], str],
    transport=_weatherkit_urlopen,
) -> dict[str, Any]:
    """Make exactly one authenticated GET for one past station-day; no retries."""

    _validate_day(city, target, now)
    start, end = utc_window_for_local_standard_date(target, city.fixed_standard_timezone())
    query = urlencode({
        "countryCode": "US",
        "dataSets": "forecastHourly",
        "timezone": city.settlement_tz_name,
        "hourlyStart": start.isoformat().replace("+00:00", "Z"),
        "hourlyEnd": end.isoformat().replace("+00:00", "Z"),
    })
    url = f"{WEATHERKIT_API_ROOT}/en-US/{city.latitude:.4f}/{city.longitude:.4f}?{query}"
    try:
        token = token_provider()
        if not isinstance(token, str) or not token:
            raise ValueError("empty token")
        request = Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "WeatherEdge/1.0",
        }, method="GET")
        with transport(request, timeout=WEATHERKIT_HTTP_TIMEOUT_SECONDS) as response:
            body = response.read(WEATHERKIT_MAX_RESPONSE_BYTES + 1)
        if len(body) > WEATHERKIT_MAX_RESPONSE_BYTES:
            raise ValueError("oversized response")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("invalid payload")
        return summarize_history(payload, city=city, target=target, now=now)
    except Exception:
        raise WeatherKitError("WeatherKit single-day history request failed") from None


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe one past WeatherKit day without retaining weather data")
    parser.add_argument("--city", required=True, help="One configured city slug")
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="One completed YYYY-MM-DD")
    args = parser.parse_args(argv)
    if not _env_enabled("ENABLE_APPLE_WEATHER"):
        print("Apple Weather is disabled; no history request dispatched")
        return 2
    credentials = _credentials_from_env()
    if credentials is None:
        print("Apple Weather credentials are incomplete; no history request dispatched")
        return 2
    try:
        report = probe_history(
            city=get_city(args.city), target=args.date,
            now=datetime.now(timezone.utc),
            token_provider=WeatherKitTokenProvider(credentials).token,
        )
    except (WeatherKitError, KeyError, ValueError):
        print("Apple Weather history probe failed; no weather data retained")
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0 if report["complete_requested_day"] else 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
