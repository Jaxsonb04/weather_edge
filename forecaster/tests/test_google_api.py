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
