"""Behavior tests for the temporary Apple WeatherKit forecast source."""

from __future__ import annotations

import json
import sqlite3
import stat
import threading
import traceback
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from apple_weatherkit import (
    AppleHighSnapshot,
    AppleRuntimeCache,
    _NoRedirectHandler,
    WeatherKitError,
    WeatherKitCredentials,
    WeatherKitClient,
    WeatherKitTokenProvider,
    refresh_apple_weather,
    run_cli,
)
from cities import CITIES, get_city
from settlement_calendar import utc_window_for_local_standard_date


NOW = datetime(2026, 7, 18, 19, 0, tzinfo=timezone.utc)


def _write_test_private_key(path) -> object:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return private_key


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int = -1) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _weatherkit_payload() -> dict:
    # SFO's fixed-standard 2026-07-19 settlement window is
    # [2026-07-19 08:00Z, 2026-07-20 08:00Z). The 99C row immediately before
    # that window must not contaminate the derived market-day high.
    hours = [
        {
            "forecastStart": "2026-07-19T07:00:00Z",
            "temperature": 99.0,
        }
    ]
    start = datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc)
    hours.extend(
        {
            "forecastStart": (start + timedelta(hours=index))
            .isoformat()
            .replace("+00:00", "Z"),
            "temperature": 20.0 if index == 11 else 15.0,
        }
        for index in range(24)
    )
    return {
        "forecastHourly": {
            "metadata": {
                "readTime": "2026-07-18T18:58:00Z",
                "expireTime": "2026-07-18T20:00:00Z",
                "latitude": 37.62,
                "longitude": -122.38,
                "units": "m",
                "version": 1,
            },
            "hours": hours,
        },
        "forecastDaily": {
            "metadata": {
                "readTime": "2026-07-18T18:58:00Z",
                "expireTime": "2026-07-18T20:30:00Z",
                "latitude": 37.62,
                "longitude": -122.38,
                "units": "m",
                "version": 1,
            },
            "days": [
                {
                    "forecastStart": "2026-07-19T08:00:00Z",
                    "forecastEnd": "2026-07-20T08:00:00Z",
                    "temperatureMax": 21.0,
                    "temperatureMin": 11.0,
                }
            ],
        },
    }


def _weatherkit_payload_for_city(city) -> dict:
    target = date(2026, 7, 19)
    start, end = utc_window_for_local_standard_date(
        target, city.fixed_standard_timezone()
    )
    metadata = {
        "readTime": "2026-07-18T18:58:00Z",
        "expireTime": "2026-07-18T20:00:00Z",
        "latitude": city.latitude,
        "longitude": city.longitude,
        "units": "m",
        "version": 1,
    }
    return {
        "forecastHourly": {
            "metadata": metadata,
            "hours": [
                {
                    "forecastStart": (start + timedelta(hours=index))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "temperature": 20.0,
                }
                for index in range(24)
            ],
        },
        "forecastDaily": {
            "metadata": metadata,
            "days": [
                {
                    "forecastStart": start.isoformat().replace("+00:00", "Z"),
                    "forecastEnd": end.isoformat().replace("+00:00", "Z"),
                    "temperatureMax": 21.0,
                    "temperatureMin": 11.0,
                }
            ],
        },
    }


def test_refresh_caches_one_station_aligned_forecast_generation(tmp_path) -> None:
    city = get_city("sfo")
    requests = []

    def transport(request, timeout=15):
        requests.append((request, timeout))
        return _Response(_weatherkit_payload())

    cache = AppleRuntimeCache(tmp_path / "apple_weather_runtime.json")
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=transport,
    )

    report = refresh_apple_weather(
        (city,),
        client=client,
        cache=cache,
        now=NOW,
    )

    assert report.refreshed == ("sfo",)
    assert report.failed == ()
    assert len(requests) == 1
    request, timeout = requests[0]
    parsed_url = urlparse(request.full_url)
    query = parse_qs(parsed_url.query)
    assert parsed_url.scheme == "https"
    assert parsed_url.netloc == "weatherkit.apple.com"
    assert parsed_url.path == "/api/v1/weather/en-US/37.6200/-122.3800"
    assert query["dataSets"] == ["forecastHourly,forecastDaily"]
    assert query["timezone"] == [city.settlement_tz_name]
    assert request.get_header("Authorization") == "Bearer signed-test-token"
    assert timeout == 15

    active = cache.active_highs(city_slug="sfo", now=NOW)
    assert len(active) == 1
    snapshot = active[0]
    assert snapshot.station_id == "KSFO"
    assert snapshot.target_date == "2026-07-19"
    assert snapshot.covered_hours == 24
    assert snapshot.complete is True
    assert snapshot.hourly_high_f == pytest.approx(68.0)
    assert snapshot.daily_high_f == pytest.approx(69.8)
    # The shorter hourly product lifetime governs the combined cache entry.
    assert snapshot.expires_at == datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)


def test_refresh_uses_the_canonical_registry_for_all_fifteen_cities(tmp_path) -> None:
    payloads = iter(_weatherkit_payload_for_city(city) for city in CITIES)
    cache = AppleRuntimeCache(tmp_path / "apple_weather_runtime.json")
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=lambda _request, timeout=15: _Response(next(payloads)),
    )

    report = refresh_apple_weather(CITIES, client=client, cache=cache, now=NOW)

    assert report.refreshed == tuple(city.slug for city in CITIES)
    assert report.failed == ()
    for city in CITIES:
        active = cache.active_highs(city_slug=city.slug, now=NOW)
        assert len(active) == 1
        assert active[0].station_id == city.nws_station_id
        assert active[0].target_date == "2026-07-19"


def test_runtime_cache_is_private_and_purges_data_at_apple_expiry(tmp_path) -> None:
    city = get_city("sfo")
    cache_path = tmp_path / "apple_weather_runtime.json"
    cache = AppleRuntimeCache(cache_path)
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=lambda _request, timeout=15: _Response(_weatherkit_payload()),
    )
    refresh_apple_weather((city,), client=client, cache=cache, now=NOW)

    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600
    expired_at = datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)
    assert cache.active_highs(city_slug="sfo", now=expired_at) == ()
    assert not cache_path.exists()

    purged = cache.purge_expired(now=expired_at)

    assert purged == 0
    assert not cache_path.exists()


def test_refresh_failure_preserves_the_prior_generation_only_until_expiry(tmp_path) -> None:
    city = get_city("sfo")
    calls = 0

    def transport(_request, timeout=15):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Response(_weatherkit_payload())
        raise TimeoutError("upstream timeout with sensitive request details")

    cache = AppleRuntimeCache(tmp_path / "apple_weather_runtime.json")
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=transport,
    )
    first = refresh_apple_weather((city,), client=client, cache=cache, now=NOW)
    retry_at = NOW + timedelta(minutes=30)

    second = refresh_apple_weather((city,), client=client, cache=cache, now=retry_at)

    assert first.refreshed == ("sfo",)
    assert second.failed == ("sfo",)
    assert len(cache.active_highs(city_slug="sfo", now=retry_at)) == 1
    assert cache.active_highs(
        city_slug="sfo",
        now=datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc),
    ) == ()


def test_hourly_market_high_remains_available_when_daily_product_is_temporarily_unavailable(
    tmp_path,
) -> None:
    city = get_city("sfo")
    payload = deepcopy(_weatherkit_payload())
    payload["forecastDaily"] = {
        "metadata": {
            "readTime": "2026-07-18T18:58:00Z",
            "expireTime": "2026-07-18T20:30:00Z",
            "temporarilyUnavailable": True,
            "latitude": 37.62,
            "longitude": -122.38,
            "units": "m",
            "version": 1,
        },
        "days": [],
    }
    cache = AppleRuntimeCache(tmp_path / "apple_weather_runtime.json")
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=lambda _request, timeout=15: _Response(payload),
    )

    report = refresh_apple_weather((city,), client=client, cache=cache, now=NOW)

    assert report.refreshed == ("sfo",)
    snapshot = cache.active_highs(city_slug="sfo", now=NOW)[0]
    assert snapshot.hourly_high_f == pytest.approx(68.0)
    assert snapshot.daily_high_f is None
    assert snapshot.expires_at == datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)


def test_expired_daily_diagnostic_does_not_hide_fresh_hourly_high(tmp_path) -> None:
    city = get_city("sfo")
    payload = deepcopy(_weatherkit_payload())
    payload["forecastDaily"]["metadata"]["expireTime"] = NOW.isoformat()
    cache = AppleRuntimeCache(tmp_path / "apple_weather_runtime.json")
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=lambda _request, timeout=15: _Response(payload),
    )

    report = refresh_apple_weather((city,), client=client, cache=cache, now=NOW)

    assert report.refreshed == ("sfo",)
    snapshot = cache.active_highs(city_slug="sfo", now=NOW)[0]
    assert snapshot.hourly_high_f == pytest.approx(68.0)
    assert snapshot.daily_high_f is None
    assert snapshot.expires_at == datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)


def test_default_http_handler_refuses_redirects_before_forwarding_authorization() -> None:
    handler = _NoRedirectHandler()

    redirected = handler.redirect_request(
        request=None,
        fp=None,
        code=302,
        msg="Found",
        headers={},
        newurl="https://example.invalid/steal-token",
    )

    assert redirected is None


def test_request_failures_do_not_render_tokens_or_upstream_details() -> None:
    secret = "signed-secret-token"
    client = WeatherKitClient(
        token_provider=lambda: secret,
        transport=lambda _request, timeout=15: (_ for _ in ()).throw(
            RuntimeError(f"upstream leaked {secret}")
        ),
    )

    with pytest.raises(WeatherKitError) as captured:
        client.fetch(get_city("sfo"), now=NOW)

    rendered = "".join(
        traceback.format_exception(
            type(captured.value), captured.value, captured.value.__traceback__
        )
    )
    assert secret not in rendered
    assert "upstream leaked" not in rendered


def test_token_provider_signs_exact_weatherkit_claims_with_the_private_p8(tmp_path) -> None:
    import jwt

    key_path = tmp_path / "AuthKey_TESTKEY123.p8"
    private_key = _write_test_private_key(key_path)
    credentials = WeatherKitCredentials(
        team_id="TEAMID1234",
        service_id="com.example.weatheredge",
        key_id="TESTKEY123",
        private_key_path=key_path,
    )
    provider = WeatherKitTokenProvider(
        credentials,
        now_provider=lambda: NOW,
        ttl=timedelta(minutes=15),
    )

    token = provider.token()

    header = jwt.get_unverified_header(token)
    assert header == {
        "alg": "ES256",
        "id": "TEAMID1234.com.example.weatheredge",
        "kid": "TESTKEY123",
    }
    claims = jwt.decode(
        token,
        private_key.public_key(),
        algorithms=["ES256"],
        options={"verify_aud": False, "verify_exp": False},
    )
    assert claims == {
        "iss": "TEAMID1234",
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(minutes=15)).timestamp()),
        "sub": "com.example.weatheredge",
    }
    assert provider.token() == token


def test_misaligned_apple_daily_rollup_is_not_attached_to_market_day(tmp_path) -> None:
    city = get_city("sfo")
    payload = deepcopy(_weatherkit_payload())
    payload["forecastDaily"]["days"][0]["forecastEnd"] = "2026-07-20T07:00:00Z"
    cache = AppleRuntimeCache(tmp_path / "apple_weather_runtime.json")
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=lambda _request, timeout=15: _Response(payload),
    )

    report = refresh_apple_weather((city,), client=client, cache=cache, now=NOW)

    assert report.refreshed == ("sfo",)
    snapshot = cache.active_highs(city_slug="sfo", now=NOW)[0]
    assert snapshot.hourly_high_f == pytest.approx(68.0)
    assert snapshot.daily_high_f is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("units", "us"), ("latitude", 40.0), ("longitude", -73.0)],
)
def test_response_metadata_must_match_metric_station_request(
    tmp_path, field, value
) -> None:
    city = get_city("sfo")
    payload = deepcopy(_weatherkit_payload())
    payload["forecastHourly"]["metadata"][field] = value
    cache_path = tmp_path / "apple_weather_runtime.json"
    cache = AppleRuntimeCache(cache_path)
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=lambda _request, timeout=15: _Response(payload),
    )

    report = refresh_apple_weather((city,), client=client, cache=cache, now=NOW)

    assert report.failed == ("sfo",)
    assert not cache_path.exists()


def test_cli_is_safe_off_without_explicit_enable_or_credentials(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("ENABLE_APPLE_WEATHER", raising=False)
    monkeypatch.setenv(
        "APPLE_WEATHER_RUNTIME_CACHE_PATH",
        str(tmp_path / "apple_weather_runtime.json"),
    )

    status = run_cli(
        ["--cities", "sfo"],
        transport=lambda *_args, **_kwargs: pytest.fail("network must remain disabled"),
        now_provider=lambda: NOW,
        runtime_root=tmp_path,
    )

    assert status == 0
    assert "disabled" in capsys.readouterr().out.lower()
    assert not (tmp_path / "apple_weather_runtime.json").exists()


def test_cli_reports_enabled_but_incomplete_credentials_as_failure(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("ENABLE_APPLE_WEATHER", "1")
    monkeypatch.setenv(
        "APPLE_WEATHER_RUNTIME_CACHE_PATH",
        str(tmp_path / "apple_weather_runtime.json"),
    )
    for name in (
        "APPLE_WEATHER_TEAM_ID",
        "APPLE_WEATHER_SERVICE_ID",
        "APPLE_WEATHER_KEY_ID",
        "APPLE_WEATHER_PRIVATE_KEY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)

    status = run_cli(
        ["--cities", "sfo"],
        transport=lambda *_args, **_kwargs: pytest.fail("network must remain disabled"),
        now_provider=lambda: NOW,
        runtime_root=tmp_path,
    )

    assert status == 2
    assert "incomplete" in capsys.readouterr().out.lower()


def test_refresh_physically_purges_expired_data_even_when_upstream_fails(tmp_path) -> None:
    city = get_city("sfo")
    calls = 0

    def transport(_request, timeout=15):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Response(_weatherkit_payload())
        raise TimeoutError("WeatherKit unavailable")

    cache_path = tmp_path / "apple_weather_runtime.json"
    cache = AppleRuntimeCache(cache_path)
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=transport,
    )
    refresh_apple_weather((city,), client=client, cache=cache, now=NOW)
    assert cache_path.exists()

    report = refresh_apple_weather(
        (city,),
        client=client,
        cache=cache,
        now=datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc),
    )

    assert report.failed == ("sfo",)
    assert not cache_path.exists()


def test_unverifiable_corrupt_cache_is_discarded_before_refresh(tmp_path) -> None:
    city = get_city("sfo")
    cache_path = tmp_path / "apple_weather_runtime.json"
    cache_path.write_text("not-json")
    cache_path.chmod(0o600)
    cache = AppleRuntimeCache(cache_path)
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=lambda _request, timeout=15: _Response(_weatherkit_payload()),
    )

    report = refresh_apple_weather((city,), client=client, cache=cache, now=NOW)

    assert report.refreshed == ("sfo",)
    assert len(cache.active_highs(city_slug="sfo", now=NOW)) == 1


def test_runtime_cache_minimizes_apple_data_to_complete_normalized_highs(tmp_path) -> None:
    city = get_city("sfo")
    cache_path = tmp_path / "apple_weather_runtime.json"
    cache = AppleRuntimeCache(cache_path)
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=lambda _request, timeout=15: _Response(_weatherkit_payload()),
    )

    refresh_apple_weather((city,), client=client, cache=cache, now=NOW)

    payload = json.loads(cache_path.read_text())
    assert len(payload["entries"]) == 1
    assert payload["entries"][0]["complete"] is True
    assert set(payload["entries"][0]) == {
        "city_slug",
        "station_id",
        "target_date",
        "fetched_at",
        "issued_at",
        "expires_at",
        "hourly_high_f",
        "daily_high_f",
        "covered_hours",
        "complete",
    }
    rendered = cache_path.read_text()
    assert "forecastHourly" not in rendered
    assert "forecastDaily" not in rendered
    assert "temperature" not in rendered


def test_runtime_cache_rejects_exposed_permissions_and_refresh_replaces_it_privately(
    tmp_path,
) -> None:
    city = get_city("sfo")
    cache_path = tmp_path / "apple_weather_runtime.json"
    cache = AppleRuntimeCache(cache_path)
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=lambda _request, timeout=15: _Response(_weatherkit_payload()),
    )
    refresh_apple_weather((city,), client=client, cache=cache, now=NOW)
    cache_path.chmod(0o644)

    with pytest.raises(WeatherKitError, match="permissions"):
        cache.active_highs(city_slug="sfo", now=NOW)

    report = refresh_apple_weather((city,), client=client, cache=cache, now=NOW)

    assert report.refreshed == ("sfo",)
    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600


def test_purge_only_discards_an_unverifiable_cache(
    tmp_path, monkeypatch, capsys
) -> None:
    cache_path = tmp_path / "apple_weather_runtime.json"
    cache_path.write_text("not-json")
    cache_path.chmod(0o600)
    monkeypatch.setenv("APPLE_WEATHER_RUNTIME_CACHE_PATH", str(cache_path))

    status = run_cli(
        ["--purge-only"], now_provider=lambda: NOW, runtime_root=tmp_path
    )

    assert status == 0
    assert not cache_path.exists()
    assert "discarded" in capsys.readouterr().out.lower()


def test_purge_only_discards_an_unreadable_cache_without_leaking_io_details(
    tmp_path, monkeypatch, capsys
) -> None:
    cache_path = tmp_path / "apple_weather_runtime.json"
    cache_path.write_text(json.dumps({"version": 1, "entries": []}))
    cache_path.chmod(0o600)
    monkeypatch.setenv("APPLE_WEATHER_RUNTIME_CACHE_PATH", str(cache_path))
    original_read_text = type(cache_path).read_text

    def unreadable(path, *args, **kwargs):
        if path == cache_path:
            raise PermissionError("sensitive filesystem detail")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(cache_path), "read_text", unreadable)

    status = run_cli(
        ["--purge-only"], now_provider=lambda: NOW, runtime_root=tmp_path
    )

    output = capsys.readouterr().out
    assert status == 0
    assert not cache_path.exists()
    assert "discarded" in output.lower()
    assert "sensitive filesystem detail" not in output


def test_refresh_fails_when_no_complete_settlement_day_is_available(tmp_path) -> None:
    city = get_city("sfo")
    payload = deepcopy(_weatherkit_payload())
    payload["forecastHourly"]["hours"] = payload["forecastHourly"]["hours"][:12]
    cache_path = tmp_path / "apple_weather_runtime.json"
    cache = AppleRuntimeCache(cache_path)
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=lambda _request, timeout=15: _Response(payload),
    )

    report = refresh_apple_weather((city,), client=client, cache=cache, now=NOW)

    assert report.failed == ("sfo",)
    assert not cache_path.exists()


def test_cache_serializes_concurrent_city_replace_operations(tmp_path) -> None:
    entered_read = threading.Event()
    release_read = threading.Event()
    second_done = threading.Event()

    class BlockingCache(AppleRuntimeCache):
        def _read(self):
            rows = super()._read()
            if threading.current_thread().name == "first-replace":
                entered_read.set()
                assert release_read.wait(timeout=2)
            return rows

    cache = BlockingCache(tmp_path / "apple_weather_runtime.json")
    expires_at = NOW + timedelta(hours=1)

    def snapshot(city_slug: str, station_id: str) -> AppleHighSnapshot:
        return AppleHighSnapshot(
            city_slug=city_slug,
            station_id=station_id,
            target_date="2026-07-19",
            fetched_at=NOW,
            issued_at=NOW,
            expires_at=expires_at,
            hourly_high_f=70.0,
            daily_high_f=None,
            covered_hours=24,
            complete=True,
        )

    first = threading.Thread(
        name="first-replace",
        target=lambda: cache.replace_city(
            "sfo", (snapshot("sfo", "KSFO"),), now=NOW
        ),
    )

    def replace_second() -> None:
        cache.replace_city("nyc", (snapshot("nyc", "KNYC"),), now=NOW)
        second_done.set()

    second = threading.Thread(name="second-replace", target=replace_second)
    first.start()
    assert entered_read.wait(timeout=2)
    second.start()
    second_was_blocked = not second_done.wait(timeout=0.1)
    release_read.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert second_was_blocked
    assert {
        row.city_slug for row in cache._read()
    } == {"sfo", "nyc"}


def test_refresh_rechecks_expiry_after_a_slow_response(tmp_path) -> None:
    city = get_city("sfo")
    cache = AppleRuntimeCache(tmp_path / "apple_weather_runtime.json")
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=lambda _request, timeout=15: _Response(_weatherkit_payload()),
    )
    times = iter(
        (
            NOW,
            NOW,
            datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc),
        )
    )

    report = refresh_apple_weather(
        (city,),
        client=client,
        cache=cache,
        now_provider=lambda: next(times),
    )

    assert report.failed == ("sfo",)
    assert not cache.path.exists()


def test_daily_expiry_crossed_during_request_does_not_drop_fresh_hourly_high(
    tmp_path,
) -> None:
    city = get_city("sfo")
    payload = deepcopy(_weatherkit_payload())
    payload["forecastDaily"]["metadata"]["expireTime"] = (
        NOW + timedelta(seconds=5)
    ).isoformat()
    cache = AppleRuntimeCache(tmp_path / "apple_weather_runtime.json")
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=lambda _request, timeout=15: _Response(payload),
    )
    received_at = NOW + timedelta(seconds=10)
    times = iter((NOW, NOW, received_at, received_at))

    report = refresh_apple_weather(
        (city,),
        client=client,
        cache=cache,
        now_provider=lambda: next(times),
    )

    assert report.refreshed == ("sfo",)
    snapshot = cache.active_highs(city_slug="sfo", now=received_at)[0]
    assert snapshot.daily_high_f is None
    assert snapshot.expires_at == datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("baseline_contents", [None, b"not a sqlite database"])
def test_enabled_cli_discards_corrupt_cache_before_refresh(
    tmp_path, monkeypatch, capsys, baseline_contents
) -> None:
    cache_path = tmp_path / "apple_weather_runtime.json"
    cache_path.write_text("not-json")
    cache_path.chmod(0o600)
    key_path = tmp_path / "AuthKey_TESTKEY123.p8"
    _write_test_private_key(key_path)
    monkeypatch.delenv("KALSHI_ENV", raising=False)
    monkeypatch.setenv("ENABLE_APPLE_WEATHER", "1")
    monkeypatch.setenv("APPLE_WEATHER_RUNTIME_CACHE_PATH", str(cache_path))
    monkeypatch.setenv("APPLE_WEATHER_TEAM_ID", "TEAMID1234")
    monkeypatch.setenv("APPLE_WEATHER_SERVICE_ID", "com.example.weatheredge")
    monkeypatch.setenv("APPLE_WEATHER_KEY_ID", "TESTKEY123")
    monkeypatch.setenv("APPLE_WEATHER_PRIVATE_KEY_PATH", str(key_path))
    monkeypatch.setenv("APPLE_WEATHER_HTTP_TIMEOUT_SECONDS", "60")
    timeouts = []

    def transport(_request, timeout=15):
        timeouts.append(timeout)
        return _Response(_weatherkit_payload())

    missing_baseline = tmp_path / "missing-baseline.db"
    if baseline_contents is not None:
        missing_baseline.write_bytes(baseline_contents)
    status = run_cli(
        ["--cities", "sfo", "--baseline-db", str(missing_baseline)],
        transport=transport,
        now_provider=lambda: NOW,
        runtime_root=tmp_path,
    )

    assert status == 0
    assert timeouts == [15]
    output = capsys.readouterr().out
    assert "1 refreshed" in output
    assert "paired=0, unpaired=1" in output
    assert "not an accuracy evaluation" in output
    if baseline_contents is None:
        assert not missing_baseline.exists()
    else:
        assert missing_baseline.read_bytes() == baseline_contents
    assert json.loads(cache_path.read_text())["entries"]


def test_cli_refuses_durable_cache_paths_even_outside_trading_prod(
    tmp_path, monkeypatch, capsys
) -> None:
    cache_path = tmp_path / "durable-apple-cache.json"
    monkeypatch.delenv("KALSHI_ENV", raising=False)
    monkeypatch.delenv("ENABLE_APPLE_WEATHER", raising=False)
    monkeypatch.setenv("APPLE_WEATHER_RUNTIME_CACHE_PATH", str(cache_path))

    status = run_cli(
        ["--cities", "sfo"],
        transport=lambda *_args, **_kwargs: pytest.fail("network must remain disabled"),
        now_provider=lambda: NOW,
    )

    assert status == 2
    assert "runtime configuration is invalid" in capsys.readouterr().out.lower()
    assert not cache_path.exists()


def test_cli_refuses_cache_path_collision_with_another_runtime_store(
    tmp_path, monkeypatch, capsys
) -> None:
    google_store = tmp_path / "google_runtime.db"
    google_store.write_bytes(b"other provider data")
    google_store.chmod(0o600)
    monkeypatch.setenv("APPLE_WEATHER_RUNTIME_CACHE_PATH", str(google_store))

    status = run_cli(
        ["--purge-only"],
        now_provider=lambda: NOW,
        runtime_root=tmp_path,
    )

    assert status == 2
    assert "runtime configuration is invalid" in capsys.readouterr().out.lower()
    assert google_store.read_bytes() == b"other provider data"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("complete", "false"),
        ("covered_hours", "24"),
        ("city_slug", "unknown"),
        ("station_id", "WRONG"),
        ("target_date", "not-a-date"),
    ),
)
def test_cache_rejects_coerced_or_mismatched_schema(
    tmp_path, field, value
) -> None:
    city = get_city("sfo")
    cache_path = tmp_path / "apple_weather_runtime.json"
    cache = AppleRuntimeCache(cache_path)
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=lambda _request, timeout=15: _Response(_weatherkit_payload()),
    )
    refresh_apple_weather((city,), client=client, cache=cache, now=NOW)
    payload = json.loads(cache_path.read_text())
    payload["entries"][0][field] = value
    cache_path.write_text(json.dumps(payload))
    cache_path.chmod(0o600)

    with pytest.raises(WeatherKitError):
        cache.active_highs(city_slug="sfo", now=NOW)


def test_cache_rejects_unknown_schema_version(tmp_path) -> None:
    cache_path = tmp_path / "apple_weather_runtime.json"
    cache_path.write_text(json.dumps({"version": 2, "entries": []}))
    cache_path.chmod(0o600)

    with pytest.raises(WeatherKitError):
        AppleRuntimeCache(cache_path).purge_expired(now=NOW)


def test_cache_rejects_unknown_top_level_data_that_could_evade_expiry(tmp_path) -> None:
    cache_path = tmp_path / "apple_weather_runtime.json"
    cache_path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [],
                "raw_apple": {"temperature": 72.0},
            }
        )
    )
    cache_path.chmod(0o600)

    with pytest.raises(WeatherKitError):
        AppleRuntimeCache(cache_path).purge_expired(now=NOW)


def test_retention_sweep_removes_interrupted_atomic_temp_files(tmp_path) -> None:
    cache_path = tmp_path / "apple_weather_runtime.json"
    orphan = tmp_path / ".apple_weather_runtime.json.tmp.interrupted"
    orphan.write_text("Apple forecast data that must not survive")
    orphan.chmod(0o600)
    cache = AppleRuntimeCache(cache_path)

    assert cache.purge_expired(now=NOW) == 0

    assert not orphan.exists()


def test_malformed_optional_daily_product_does_not_hide_hourly_high(tmp_path) -> None:
    city = get_city("sfo")
    payload = deepcopy(_weatherkit_payload())
    payload["forecastDaily"]["metadata"]["units"] = "invalid"
    cache = AppleRuntimeCache(tmp_path / "apple_weather_runtime.json")
    client = WeatherKitClient(
        token_provider=lambda: "signed-test-token",
        transport=lambda _request, timeout=15: _Response(payload),
    )

    report = refresh_apple_weather((city,), client=client, cache=cache, now=NOW)

    assert report.refreshed == ("sfo",)
    snapshot = cache.active_highs(city_slug="sfo", now=NOW)[0]
    assert snapshot.hourly_high_f == pytest.approx(68.0)
    assert snapshot.daily_high_f is None
    assert snapshot.expires_at == datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)


def _probe_cache(tmp_path):
    cache = AppleRuntimeCache(tmp_path / "apple_weather_runtime.json")
    refresh_apple_weather(
        (get_city("sfo"),),
        client=WeatherKitClient(
            token_provider=lambda: "signed-test-token",
            transport=lambda _request, timeout=15: _Response(_weatherkit_payload()),
        ),
        cache=cache,
        now=NOW,
    )
    return cache


def _probe_baseline(tmp_path, rows=None):
    path = tmp_path / "baseline.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE forecast_emos_daily_high (station_id TEXT, target_date TEXT, "
            "lead_days INTEGER, predicted_high_f REAL, sigma_f REAL, fetched_at TEXT, source TEXT)"
        )
        conn.executemany(
            "INSERT INTO forecast_emos_daily_high VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows if rows is not None else [
                ("KSFO", "2026-07-19", 1, 71.25, 2.5, NOW.isoformat(), "live")
            ],
        )
    return path


def test_runtime_probe_pairs_fresh_matching_baseline_without_writing_it(tmp_path):
    from apple_weatherkit import probe_baseline_compatibility

    cache = _probe_cache(tmp_path)
    baseline = _probe_baseline(tmp_path)
    before = baseline.read_bytes()

    report = probe_baseline_compatibility(
        (get_city("sfo"),), cache=cache, baseline_db=baseline, now_provider=lambda: NOW,
    )

    assert report.paired_targets == 1
    assert report.apple_targets == 1
    assert report.unpaired_targets == 0
    assert baseline.read_bytes() == before
    # The returned report cannot retain raw values or reversible comparisons.
    assert set(report.__dataclass_fields__) == {
        "requested_cities", "apple_targets", "paired_targets", "unpaired_targets",
        "unavailable_cities",
    }


@pytest.mark.parametrize("mutation", [
    {"station_id": "KNYC"}, {"target_date": "2026-07-20"}, {"lead_days": 2},
    {"source": "rolling_origin_v2"}, {"sigma_f": 0.0}, {"sigma_f": float("inf")},
    {"predicted_high_f": float("inf")}, {"fetched_at": "invalid"},
    {"fetched_at": (NOW - timedelta(hours=7)).isoformat()},
    {"fetched_at": (NOW + timedelta(seconds=1)).isoformat()},
])
def test_runtime_probe_rejects_mismatched_stale_or_invalid_baseline(tmp_path, mutation):
    from apple_weatherkit import probe_baseline_compatibility

    cache = _probe_cache(tmp_path)
    row = dict(station_id="KSFO", target_date="2026-07-19", lead_days=1,
               predicted_high_f=71.25, sigma_f=2.5, fetched_at=NOW.isoformat(), source="live")
    row.update(mutation)
    baseline = _probe_baseline(tmp_path, [tuple(row.values())])

    report = probe_baseline_compatibility(
        (get_city("sfo"),), cache=cache, baseline_db=baseline, now_provider=lambda: NOW,
    )

    assert report.apple_targets == 1
    assert report.paired_targets == 0
    assert report.unpaired_targets == 1


def test_runtime_probe_uses_latest_baseline_and_does_not_hide_its_invalid_sigma(tmp_path):
    from apple_weatherkit import probe_baseline_compatibility

    cache = _probe_cache(tmp_path)
    baseline = _probe_baseline(tmp_path, [
        ("KSFO", "2026-07-19", 1, 71.25, 2.5, (NOW - timedelta(minutes=5)).isoformat(), "live"),
        ("KSFO", "2026-07-19", 1, 71.25, 0.0, NOW.isoformat(), "live"),
    ])
    report = probe_baseline_compatibility(
        (get_city("sfo"),), cache=cache, baseline_db=baseline, now_provider=lambda: NOW,
    )
    assert report.paired_targets == 0


def test_runtime_probe_rechecks_apple_expiry_after_baseline_read(tmp_path):
    from apple_weatherkit import probe_baseline_compatibility

    cache = _probe_cache(tmp_path)
    baseline = _probe_baseline(tmp_path)
    times = iter((NOW, datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)))
    report = probe_baseline_compatibility(
        (get_city("sfo"),), cache=cache, baseline_db=baseline, now_provider=lambda: next(times),
    )
    assert report.apple_targets == 1
    assert report.paired_targets == 0
    assert not cache.path.exists()


def test_runtime_probe_counts_only_complete_lead_one_and_two_targets(tmp_path):
    from apple_weatherkit import probe_baseline_compatibility

    cache = _probe_cache(tmp_path)
    snapshot = cache.active_highs(city_slug="sfo", now=NOW)[0]
    cache.replace_city("sfo", [
        replace(snapshot, target_date="2026-07-18"),
        snapshot,
        replace(snapshot, target_date="2026-07-20"),
        replace(snapshot, target_date="2026-07-21"),
    ], now=NOW)
    baseline = _probe_baseline(tmp_path, [
        ("KSFO", "2026-07-19", 1, 71.25, 2.5, NOW.isoformat(), "live"),
        ("KSFO", "2026-07-20", 2, 71.25, 2.5, NOW.isoformat(), "live"),
    ])

    report = probe_baseline_compatibility(
        (get_city("sfo"), get_city("nyc")), cache=cache,
        baseline_db=baseline, now_provider=lambda: NOW,
    )

    assert report.apple_targets == 2
    assert report.paired_targets == 2
    assert report.requested_cities == 2
    assert report.unavailable_cities == 1


def test_runtime_probe_missing_database_is_unavailable_and_never_created(tmp_path):
    from apple_weatherkit import probe_baseline_compatibility

    cache = _probe_cache(tmp_path)
    missing = tmp_path / "missing.db"
    report = probe_baseline_compatibility(
        (get_city("sfo"),), cache=cache, baseline_db=missing, now_provider=lambda: NOW,
    )
    assert report.paired_targets == 0
    assert report.unpaired_targets == 1
    assert not missing.exists()


def test_probe_only_needs_no_credentials_or_network_and_prints_only_counts(tmp_path, monkeypatch, capsys):
    cache = _probe_cache(tmp_path)
    baseline = _probe_baseline(tmp_path)
    monkeypatch.delenv("ENABLE_APPLE_WEATHER", raising=False)
    monkeypatch.setenv("APPLE_WEATHER_RUNTIME_CACHE_PATH", str(cache.path))
    status = run_cli(
        ["--probe-only", "--cities", "sfo", "--baseline-db", str(baseline)],
        transport=lambda *_a, **_kw: pytest.fail("probe must not call WeatherKit"),
        now_provider=lambda: NOW, runtime_root=tmp_path,
    )
    output = capsys.readouterr().out
    assert status == 0
    assert "compatibility" in output
    assert "paired=1" in output
    assert "not an accuracy evaluation" in output
    for private_content in ("68.0", "69.8", "71.25", "2.5", "KSFO", str(baseline), "signed-test-token"):
        assert private_content not in output
