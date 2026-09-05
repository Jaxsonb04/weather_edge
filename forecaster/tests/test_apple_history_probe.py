"""A historical capability query must never become a training-data archive."""

import json
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from apple_history_probe import probe_history, summarize_history
from apple_weatherkit import WeatherKitError
from cities import get_city


NOW = datetime(2026, 9, 5, 6, tzinfo=timezone.utc)
TARGET = date(2025, 7, 19)
START = datetime(2025, 7, 19, 8, tzinfo=timezone.utc)
CITY = get_city("sfo")


def payload():
    return {"forecastHourly": {
        "metadata": {"latitude": CITY.latitude, "longitude": CITY.longitude,
                     "units": "m", "version": 1,
                     "readTime": "2026-09-05T06:00:00Z",
                     "expireTime": "2026-09-05T07:00:00Z"},
        "hours": [{"forecastStart": (START + timedelta(hours=i)).isoformat(),
                   "temperature": 17.123456789} for i in range(24)],
    }}


def summarize(value):
    return summarize_history(value, city=CITY, target=TARGET, now=NOW)


def test_one_request_exact_standard_time_window_no_weather_output():
    calls = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, size): return json.dumps(payload()).encode()

    def transport(request, *, timeout):
        calls.append(request)
        assert timeout == 15
        return Response()

    report = probe_history(city=CITY, target=TARGET, now=NOW,
                           token_provider=lambda: "private-test-token", transport=transport)
    assert len(calls) == 1
    url = urlparse(calls[0].full_url)
    assert url.scheme == "https" and url.hostname == "weatherkit.apple.com"
    query = parse_qs(url.query)
    assert query["hourlyStart"] == ["2025-07-19T08:00:00Z"]
    assert query["hourlyEnd"] == ["2025-07-20T08:00:00Z"]
    assert query["timezone"] == ["Etc/GMT+8"]
    assert query["dataSets"] == ["forecastHourly"]
    assert report["complete_requested_day"] is True
    assert report["historical_forecast_vintage_verified"] is False
    assert report["weather_values_retained"] is False
    assert "17.123456789" not in json.dumps(report)
    assert "private-test-token" not in json.dumps(report)


@pytest.mark.parametrize("target", [date(2021, 7, 31), date(2026, 9, 4), date(2026, 9, 5)])
def test_invalid_target_never_requests_token_or_weather(target):
    def unexpected(): raise AssertionError("must validate before authentication")
    with pytest.raises(WeatherKitError, match="completed day"):
        probe_history(city=CITY, target=target, now=NOW, token_provider=unexpected)


def test_coverage_rejects_partial_or_ignored_date_ranges():
    p = payload()
    p["forecastHourly"]["hours"].pop()
    assert summarize(p)["complete_requested_day"] is False
    p = payload()
    p["forecastHourly"]["hours"].append({"forecastStart": "2026-09-05T06:00:00Z", "temperature": 22})
    result = summarize(p)
    assert result["outside_window_rows"] == 1
    assert result["complete_requested_day"] is False


def test_duplicate_hour_and_nonfinite_temperature_are_invalid():
    p = payload()
    p["forecastHourly"]["hours"].append(p["forecastHourly"]["hours"][0])
    with pytest.raises(WeatherKitError, match="duplicate"):
        summarize(p)
    p = payload()
    p["forecastHourly"]["hours"][0]["temperature"] = float("nan")
    with pytest.raises(WeatherKitError, match="invalid temperature"):
        summarize(p)


def test_failed_request_is_sanitized_and_has_no_retry():
    calls = []
    def transport(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("private-test-token and upstream weather body")
    with pytest.raises(WeatherKitError) as caught:
        probe_history(city=CITY, target=TARGET, now=NOW,
                      token_provider=lambda: "private-test-token", transport=transport)
    assert len(calls) == 1
    assert str(caught.value) == "WeatherKit single-day history request failed"
    assert caught.value.__suppress_context__ is True
