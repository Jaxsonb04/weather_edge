"""Network-free security regressions for NWS forecast-link fetching."""

from __future__ import annotations

import json
import urllib.request

import pytest

import blend_sources


class _JsonResponse:
    headers = {}

    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")
        self.requested_bytes = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        self.requested_bytes = size
        return self.body


class _StaticOpener:
    def __init__(self, response):
        self.response = response

    def open(self, _request, *, timeout):
        assert timeout == 25
        return self.response


@pytest.mark.parametrize(
    "url",
    (
        "file:///etc/passwd",
        "ftp://api.weather.gov/gridpoints/MTR/88,126",
    ),
)
def test_nws_json_rejects_non_https_urls_before_transport(monkeypatch, url):
    def unexpected_transport(*_args, **_kwargs):
        raise AssertionError("unsafe URL reached the transport")

    monkeypatch.setattr(urllib.request, "build_opener", unexpected_transport)

    with pytest.raises(ValueError, match="HTTPS"):
        blend_sources.read_nws_json(url)


@pytest.mark.parametrize(
    "url",
    (
        "https://127.0.0.1/private",
        "https://169.254.169.254/latest/meta-data/",
        "https://example.com/forecast",
        "https://api.weather.gov.example.com/forecast",
    ),
)
def test_nws_json_rejects_every_host_except_api_weather_gov(monkeypatch, url):
    def unexpected_transport(*_args, **_kwargs):
        raise AssertionError("untrusted host reached the transport")

    monkeypatch.setattr(urllib.request, "build_opener", unexpected_transport)

    with pytest.raises(ValueError, match="api.weather.gov"):
        blend_sources.read_nws_json(url)


@pytest.mark.parametrize(
    "url",
    (
        "https://reader@api.weather.gov/gridpoints/MTR/88,126",
        "https://reader:secret@api.weather.gov/gridpoints/MTR/88,126",
        "https://api.weather.gov:8443/gridpoints/MTR/88,126",
    ),
)
def test_nws_json_rejects_credentials_and_nonstandard_ports(monkeypatch, url):
    def unexpected_transport(*_args, **_kwargs):
        raise AssertionError("unsafe authority reached the transport")

    monkeypatch.setattr(urllib.request, "build_opener", unexpected_transport)

    with pytest.raises(ValueError, match="credentials|port"):
        blend_sources.read_nws_json(url)


def test_nws_json_rejects_cross_origin_redirect_before_following(monkeypatch):
    target = "https://169.254.169.254/latest/meta-data/"

    class RedirectingOpener:
        def __init__(self, redirect_handler):
            self.redirect_handler = redirect_handler

        def open(self, request, *, timeout):
            return self.redirect_handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                target,
            )

    def fake_build_opener(*handlers):
        redirect_handler = next(
            handler
            for handler in handlers
            if isinstance(handler, urllib.request.HTTPRedirectHandler)
        )
        return RedirectingOpener(redirect_handler)

    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)

    with pytest.raises(ValueError, match="api.weather.gov"):
        blend_sources.read_nws_json(
            "https://api.weather.gov/points/37.6213,-122.3790"
        )


@pytest.mark.parametrize(
    ("field", "advertised_url", "message"),
    (
        ("forecastGridData", "file:///etc/passwd", "HTTPS"),
        (
            "forecastHourly",
            "https://169.254.169.254/latest/meta-data/",
            "api.weather.gov",
        ),
    ),
)
def test_nws_forecast_rejects_untrusted_advertised_links_before_second_request(
    monkeypatch,
    field,
    advertised_url,
    message,
):
    point_response = _JsonResponse(
        {"properties": {field: advertised_url}}
    )
    opener_calls = 0

    def fake_build_opener(*_handlers):
        nonlocal opener_calls
        opener_calls += 1
        if opener_calls > 1:
            raise AssertionError("untrusted advertised URL reached the transport")
        return _StaticOpener(point_response)

    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)

    with pytest.raises(ValueError, match=message):
        blend_sources.load_nws_forecast_high("2026-07-27")

    assert opener_calls == 1


@pytest.mark.parametrize(
    "url",
    (
        "https://api.weather.gov/points/37.6213,-122.3790",
        "https://api.weather.gov:443/gridpoints/MTR/88,126",
    ),
)
def test_nws_json_accepts_api_weather_gov_and_bounds_the_read(monkeypatch, url):
    response = _JsonResponse({"properties": {"gridId": "MTR"}})

    class InspectingOpener(_StaticOpener):
        def open(self, request, *, timeout):
            assert request.full_url == url
            return super().open(request, timeout=timeout)

    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_handlers: InspectingOpener(response),
    )

    payload = blend_sources.read_nws_json(url)

    assert payload == {"properties": {"gridId": "MTR"}}
    assert 1 < response.requested_bytes <= 8 * 1024 * 1024 + 1


def test_nws_json_rejects_an_oversized_response(monkeypatch):
    class OversizedResponse:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size=-1):
            assert size > 1
            return b"x" * size

    class StaticOpener:
        def open(self, _request, *, timeout):
            assert timeout == 25
            return OversizedResponse()

    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_handlers: StaticOpener(),
    )

    with pytest.raises(ValueError, match="size limit"):
        blend_sources.read_nws_json(
            "https://api.weather.gov/gridpoints/MTR/88,126"
        )
