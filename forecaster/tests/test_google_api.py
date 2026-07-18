"""Network-free tests for Google Weather dispatch accounting and redaction."""

from __future__ import annotations

import json
import traceback
from urllib.error import HTTPError

import pytest

import google_api


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode("utf-8")


def _rendered_exception(exc):
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def test_predispatch_failure_reports_zero_events_without_exposing_key(
    monkeypatch, capsys
):
    key = "test-secret-key"
    monkeypatch.setattr(
        google_api,
        "urlencode",
        lambda _params: (_ for _ in ()).throw(ValueError(f"invalid key {key}")),
    )
    monkeypatch.setattr(
        google_api,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("transport must not be dispatched"),
    )

    with pytest.raises(google_api.GoogleFetchError) as raised:
        google_api.fetch_hourly_page(key)

    assert raised.value.dispatched_events == 0
    assert raised.value.endpoint == "hourly forecast"
    assert key not in _rendered_exception(raised.value)
    captured = capsys.readouterr()
    assert key not in captured.out + captured.err


def test_second_hourly_page_failure_reports_both_dispatched_events(monkeypatch):
    key = "test-secret-key"
    full_url = f"{google_api.HOURLY_API_URL}?key={key}&pageToken=second"
    calls = []

    def fake_urlopen(url, **_kwargs):
        calls.append(url)
        if len(calls) == 1:
            return _Response(
                {
                    "forecastHours": [{"id": "first-page"}],
                    "nextPageToken": "second",
                }
            )
        raise TimeoutError(f"timed out requesting {full_url}")

    monkeypatch.setattr(google_api, "urlopen", fake_urlopen)
    monkeypatch.setattr(google_api, "ENABLE_GOOGLE_DAILY_FORECAST", False)
    monkeypatch.setattr(google_api, "ENABLE_GOOGLE_CURRENT_CONDITIONS", False)

    with pytest.raises(google_api.GoogleFetchError) as raised:
        google_api.fetch_google_forecast(key)

    assert len(calls) == 2
    assert raised.value.dispatched_events == 2
    assert raised.value.endpoint == "hourly forecast"
    rendered = _rendered_exception(raised.value)
    assert key not in rendered
    assert full_url not in rendered


@pytest.mark.parametrize("failure_kind", ["timeout", "http", "parse", "shape"])
def test_failure_after_dispatch_counts_event_and_redacts_request(
    failure_kind, monkeypatch, capsys
):
    key = "test-secret-key"
    full_url = f"{google_api.HOURLY_API_URL}?key={key}&location.latitude=37.6213"

    def fake_urlopen(_url, **_kwargs):
        if failure_kind == "timeout":
            raise TimeoutError(f"timed out requesting {full_url}")
        if failure_kind == "http":
            raise HTTPError(full_url, 500, f"server rejected {key}", {}, None)
        if failure_kind == "shape":
            return _Response([])
        return _Response(b"not-json")

    monkeypatch.setattr(google_api, "urlopen", fake_urlopen)

    with pytest.raises(google_api.GoogleFetchError) as raised:
        google_api.fetch_hourly_page(key)

    assert raised.value.dispatched_events == 1
    assert raised.value.endpoint == "hourly forecast"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    rendered = _rendered_exception(raised.value)
    assert key not in rendered
    assert full_url not in rendered
    captured = capsys.readouterr()
    assert key not in captured.out + captured.err
    assert full_url not in captured.out + captured.err


def test_successful_forecast_event_count_is_unchanged(monkeypatch):
    pages = iter(
        [
            {"forecastHours": [{}] * 24, "nextPageToken": "second"},
            {"forecastHours": [{}] * 24, "nextPageToken": "third"},
            {"forecastHours": [{}] * 24},
        ]
    )
    monkeypatch.setattr(
        google_api,
        "fetch_hourly_page",
        lambda _key, _page_token=None: next(pages),
    )
    monkeypatch.setattr(google_api, "fetch_daily_forecast", lambda _key: {})
    monkeypatch.setattr(google_api, "fetch_current_conditions", lambda _key: {})
    monkeypatch.setattr(google_api, "ENABLE_GOOGLE_DAILY_FORECAST", True)
    monkeypatch.setattr(google_api, "ENABLE_GOOGLE_CURRENT_CONDITIONS", True)

    result = google_api.fetch_google_forecast("test-key")

    assert result["google_weather_events_used"] == 5


def test_hourly_transport_stops_after_three_underfilled_pages(monkeypatch):
    calls = []

    def fake_urlopen(url, **_kwargs):
        calls.append(url)
        page_number = len(calls)
        return _Response(
            {
                "forecastHours": [{"id": f"page-{page_number}"}],
                "nextPageToken": f"page-{page_number + 1}",
            }
        )

    monkeypatch.setattr(google_api, "urlopen", fake_urlopen)
    monkeypatch.setattr(google_api, "ENABLE_GOOGLE_DAILY_FORECAST", False)
    monkeypatch.setattr(google_api, "ENABLE_GOOGLE_CURRENT_CONDITIONS", False)

    result = google_api.fetch_google_forecast("test-key")

    assert len(calls) == 3
    assert result["google_weather_events_used"] == 3
    assert [row["id"] for row in result["forecastHours"]] == [
        "page-1",
        "page-2",
        "page-3",
    ]


@pytest.mark.parametrize(
    "forecast_hours",
    [1, {"unexpected": "mapping"}, "unexpected string", [1]],
)
def test_invalid_nested_hourly_shape_counts_dispatched_page_safely(
    forecast_hours, monkeypatch
):
    key = "test-secret-key"
    full_url = f"{google_api.HOURLY_API_URL}?key={key}"
    monkeypatch.setattr(
        google_api,
        "fetch_hourly_page",
        lambda _key, _page_token=None: {"forecastHours": forecast_hours},
    )
    monkeypatch.setattr(google_api, "ENABLE_GOOGLE_DAILY_FORECAST", False)
    monkeypatch.setattr(google_api, "ENABLE_GOOGLE_CURRENT_CONDITIONS", False)

    with pytest.raises(google_api.GoogleFetchError) as raised:
        google_api.fetch_google_forecast(key)

    assert raised.value.dispatched_events == 1
    assert raised.value.endpoint == "hourly forecast"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    rendered = _rendered_exception(raised.value)
    assert key not in rendered
    assert full_url not in rendered


def test_invalid_second_page_shape_counts_both_dispatched_pages(monkeypatch):
    pages = iter(
        [
            {"forecastHours": [{"id": "first"}], "nextPageToken": "second"},
            {"forecastHours": {"unexpected": "mapping"}},
        ]
    )
    monkeypatch.setattr(
        google_api,
        "fetch_hourly_page",
        lambda _key, _page_token=None: next(pages),
    )
    monkeypatch.setattr(google_api, "ENABLE_GOOGLE_DAILY_FORECAST", False)
    monkeypatch.setattr(google_api, "ENABLE_GOOGLE_CURRENT_CONDITIONS", False)

    with pytest.raises(google_api.GoogleFetchError) as raised:
        google_api.fetch_google_forecast("test-key")

    assert raised.value.dispatched_events == 2


def test_unrecognized_tagged_endpoint_failure_is_sanitized_without_double_count(
    monkeypatch
):
    key = "test-secret-key"
    full_url = f"{google_api.HOURLY_API_URL}?key={key}&pageToken=second"
    calls = 0

    class UnknownEndpointError(RuntimeError):
        dispatched_events = 1

    def fetch_page(_key, _page_token=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "forecastHours": [{"id": "first"}],
                "nextPageToken": "second",
            }
        raise UnknownEndpointError(f"request failed for {full_url}")

    monkeypatch.setattr(google_api, "fetch_hourly_page", fetch_page)
    monkeypatch.setattr(google_api, "ENABLE_GOOGLE_DAILY_FORECAST", False)
    monkeypatch.setattr(google_api, "ENABLE_GOOGLE_CURRENT_CONDITIONS", False)

    with pytest.raises(google_api.GoogleFetchError) as raised:
        google_api.fetch_google_forecast(key)

    assert raised.value.dispatched_events == 2
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    rendered = _rendered_exception(raised.value)
    assert key not in rendered
    assert full_url not in rendered


@pytest.mark.parametrize("successful_prior_pages", [0, 1])
def test_unrecognized_untagged_endpoint_failure_is_counted_conservatively(
    successful_prior_pages, monkeypatch
):
    key = "test-secret-key"
    full_url = f"{google_api.HOURLY_API_URL}?key={key}"
    calls = 0

    def fetch_page(_key, _page_token=None):
        nonlocal calls
        calls += 1
        if calls <= successful_prior_pages:
            return {
                "forecastHours": [{"id": f"page-{calls}"}],
                "nextPageToken": "next",
            }
        raise RuntimeError(f"unexpected failure for {full_url}")

    monkeypatch.setattr(google_api, "fetch_hourly_page", fetch_page)
    monkeypatch.setattr(google_api, "ENABLE_GOOGLE_DAILY_FORECAST", False)
    monkeypatch.setattr(google_api, "ENABLE_GOOGLE_CURRENT_CONDITIONS", False)

    with pytest.raises(google_api.GoogleFetchError) as raised:
        google_api.fetch_google_forecast(key)

    assert raised.value.dispatched_events == successful_prior_pages + 1
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    rendered = _rendered_exception(raised.value)
    assert key not in rendered
    assert full_url not in rendered
