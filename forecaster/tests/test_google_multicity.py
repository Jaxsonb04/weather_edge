"""Network-free tests for city-aware Google Weather fetching (Task 4).

Covers: every configured city is fetchable with its own coordinates/station;
strict hourly pagination (1/2/3 pages, the hard 3-page ceiling, and the
zero-page no-call case); underfilled pages; independent per-endpoint
reservation/dispatch/completion lifecycle and failure classification with no
cross-contamination between endpoints; fixed-standard settlement-day
bucketing across a DST boundary; secret-safe exceptions; and the two binding
runtime-store contracts:

  A. A failed or partial fetch must never call a store replace/generation API
     with an empty (or partial) mapping -- that would wipe or falsely extend
     an already-active generation. Parse/transport failure => write nothing.
  B. A retry must always perform a fresh fetch. Nothing in this module caches
     a parsed response across calls, so two calls always dispatch two fresh
     transport requests and persist the second (freshest) response, never a
     replay of the first.
"""

from __future__ import annotations

import json
import math
import sqlite3
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

import google_api
from cities import CITIES, get_city
from google_weather_store import (
    GoogleRuntimeStore,
    GoogleUsageLedger,
    GoogleWeatherBudgetExceeded,
)


TEST_NOW = datetime(2026, 7, 18, 19, 0, tzinfo=timezone.utc)
TEST_KEY = "test-secret-key"


def _usage_ledger(tmp_path, **limits):
    return GoogleUsageLedger(tmp_path / "weather.db", **limits)


def _runtime_store(tmp_path):
    return GoogleRuntimeStore(tmp_path / "google_runtime.db", production=False)


def _ledger_rows(usage, *, city_slug, endpoint):
    with sqlite3.connect(usage.db_path) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM google_weather_usage_events
                WHERE city_slug = ? AND endpoint = ?
                ORDER BY page_number, reserved_at
                """,
                (city_slug, endpoint),
            ).fetchall()
        ]


def _rendered_exception(exc):
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        if isinstance(self._payload, (bytes, bytearray)):
            return self._payload
        return json.dumps(self._payload).encode("utf-8")


def _hour(valid_at, temp_f):
    return {
        "interval": {
            "startTime": valid_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        },
        "temperature": {"degrees": temp_f, "unit": "FAHRENHEIT"},
    }


def _hourly_payload(hours, *, next_token=None):
    payload = {"forecastHours": hours, "timeZone": {"id": "America/Los_Angeles"}}
    if next_token:
        payload["nextPageToken"] = next_token
    return payload


def _daily_payload(rows):
    days = []
    for target_iso, high_f in rows:
        year, month, day = target_iso.split("-")
        days.append(
            {
                "displayDate": {
                    "year": int(year),
                    "month": int(month),
                    "day": int(day),
                },
                "maxTemperature": {"degrees": high_f, "unit": "FAHRENHEIT"},
            }
        )
    return {"forecastDays": days}


def _current_payload(temp_f, *, current_time=None):
    payload = {"temperature": {"degrees": temp_f, "unit": "FAHRENHEIT"}}
    if current_time:
        payload["currentTime"] = current_time
    return payload


# ---------------------------------------------------------------------------
# All 15 cities are fetchable with their own coordinates/station.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("city", CITIES, ids=lambda city: city.slug)
def test_every_configured_city_is_fetchable_with_its_own_coordinates_and_station(
    tmp_path, city
):
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    calls = []
    valid_at = TEST_NOW + timedelta(hours=1)

    def transport(url, timeout=20):
        calls.append(url)
        return _Response(_hourly_payload([_hour(valid_at, 68.0)]))

    rows = google_api.fetch_city_hourly(
        city,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        max_pages=1,
        transport=transport,
        now=TEST_NOW,
    )

    assert len(calls) == 1
    assert f"location.latitude={city.latitude:.4f}" in calls[0]
    assert f"location.longitude={city.longitude:.4f}" in calls[0]
    assert len(rows) == 1
    assert rows[0].temperature_f == 68.0

    active = runtime.active_hourly(
        city_slug=city.slug, station_id=city.nws_station_id, now=TEST_NOW
    )
    assert len(active) == 1
    assert active[0].city_slug == city.slug
    assert active[0].station_id == city.nws_station_id


# ---------------------------------------------------------------------------
# Strict hourly pagination.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("page_count", [1, 2, 3])
def test_hourly_pagination_stops_exactly_when_pages_run_out(tmp_path, page_count):
    city = get_city("sfo")
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    calls = []

    def transport(url, timeout=20):
        calls.append(url)
        page_number = len(calls)
        next_token = f"page-{page_number + 1}" if page_number < page_count else None
        return _Response(
            _hourly_payload(
                [_hour(TEST_NOW + timedelta(hours=page_number), 60.0 + page_number)],
                next_token=next_token,
            )
        )

    rows = google_api.fetch_city_hourly(
        city,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        max_pages=3,
        transport=transport,
        now=TEST_NOW,
    )

    assert len(calls) == page_count
    assert len(rows) == page_count
    ledger_rows = _ledger_rows(usage, city_slug="sfo", endpoint="hourly")
    assert len(ledger_rows) == page_count
    assert all(row["status"] == "success" for row in ledger_rows)


def test_hourly_pagination_hard_ceiling_stops_at_three_pages(tmp_path):
    city = get_city("sfo")
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    calls = []

    def transport(url, timeout=20):
        calls.append(url)
        page_number = len(calls)
        return _Response(
            _hourly_payload(
                [_hour(TEST_NOW + timedelta(hours=page_number), 60.0)],
                next_token=f"page-{page_number + 1}",
            )
        )

    rows = google_api.fetch_city_hourly(
        city,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        max_pages=10,
        transport=transport,
        now=TEST_NOW,
    )

    assert len(calls) == 3
    assert len(rows) == 3


def test_zero_configured_hourly_pages_makes_no_hourly_calls_at_all(
    tmp_path, monkeypatch
):
    city = get_city("sfo")
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    calls = []
    monkeypatch.setattr(google_api, "GOOGLE_HOURLY_MAX_PAGES", 0)

    def transport(url, timeout=20):
        calls.append(url)
        return _Response(_hourly_payload([]))

    rows = google_api.fetch_city_hourly(
        city,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        max_pages=3,
        transport=transport,
        now=TEST_NOW,
    )

    assert calls == []
    assert rows == ()
    assert _ledger_rows(usage, city_slug="sfo", endpoint="hourly") == []
    assert (
        runtime.active_hourly(city_slug="sfo", station_id="KSFO", now=TEST_NOW) == ()
    )


def test_underfilled_hourly_pages_still_complete_successfully(tmp_path):
    city = get_city("sfo")
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    calls = []

    def transport(url, timeout=20):
        calls.append(url)
        page_number = len(calls)
        # Each page returns far fewer than a full 24-hour page, but still
        # advertises a next-page token for two pages, then stops.
        hours = [
            _hour(TEST_NOW + timedelta(hours=page_number, minutes=m), 55.0 + m)
            for m in range(3)
        ]
        next_token = "next" if page_number < 2 else None
        return _Response(_hourly_payload(hours, next_token=next_token))

    rows = google_api.fetch_city_hourly(
        city,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        max_pages=3,
        transport=transport,
        now=TEST_NOW,
    )

    assert len(calls) == 2
    assert len(rows) == 6
    active = runtime.active_hourly(city_slug="sfo", station_id="KSFO", now=TEST_NOW)
    assert len(active) == 6
    ledger_rows = _ledger_rows(usage, city_slug="sfo", endpoint="hourly")
    assert all(row["status"] == "success" for row in ledger_rows)


# ---------------------------------------------------------------------------
# Reservation / dispatch / completion lifecycle and failure classification.
# ---------------------------------------------------------------------------


def test_successful_page_completes_as_success_with_one_billable_event(tmp_path):
    city = get_city("sfo")
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    valid_at = TEST_NOW + timedelta(hours=1)

    def transport(url, timeout=20):
        return _Response(_hourly_payload([_hour(valid_at, 65.0)]))

    google_api.fetch_city_hourly(
        city,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        max_pages=1,
        transport=transport,
        now=TEST_NOW,
    )

    rows = _ledger_rows(usage, city_slug="sfo", endpoint="hourly")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "success"
    assert row["billable_events"] == 1
    assert row["dispatched_at"] is not None
    assert row["completed_at"] is not None
    assert row["error_kind"] is None
    assert usage.usage(now=TEST_NOW).daily_events == 1


def test_budget_exhaustion_raises_before_any_reservation_or_dispatch(tmp_path):
    city = get_city("sfo")
    usage = _usage_ledger(
        tmp_path, daily_budget=0, monthly_budget=0, soft_monthly_ceiling=0
    )
    runtime = _runtime_store(tmp_path)
    calls = []

    def transport(url, timeout=20):
        calls.append(url)
        return _Response(_hourly_payload([]))

    with pytest.raises(GoogleWeatherBudgetExceeded):
        google_api.fetch_city_hourly(
            city,
            key=TEST_KEY,
            usage=usage,
            runtime=runtime,
            max_pages=1,
            transport=transport,
            now=TEST_NOW,
        )

    assert calls == []
    assert _ledger_rows(usage, city_slug="sfo", endpoint="hourly") == []


@pytest.mark.parametrize(
    ("build_transport", "expected_kind"),
    [
        (
            lambda: (lambda url, timeout=20: (_ for _ in ()).throw(TimeoutError("t"))),
            "timeout",
        ),
        (
            lambda: (
                lambda url, timeout=20: (_ for _ in ()).throw(
                    HTTPError(url, 503, "server error", {}, None)
                )
            ),
            "http",
        ),
        (
            lambda: (
                lambda url, timeout=20: (_ for _ in ()).throw(
                    URLError("connection refused")
                )
            ),
            "transport",
        ),
        (lambda: (lambda url, timeout=20: _Response(b"not-json")), "parse"),
        (lambda: (lambda url, timeout=20: _Response([1, 2, 3])), "parse"),
    ],
    ids=["timeout", "http", "transport", "bad-json", "wrong-shape"],
)
def test_failure_classification_matches_error_kind(
    tmp_path, build_transport, expected_kind
):
    city = get_city("sfo")
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    transport = build_transport()

    with pytest.raises(google_api.GoogleCityFetchError):
        google_api.fetch_city_hourly(
            city,
            key=TEST_KEY,
            usage=usage,
            runtime=runtime,
            max_pages=1,
            transport=transport,
            now=TEST_NOW,
        )

    rows = _ledger_rows(usage, city_slug="sfo", endpoint="hourly")
    assert len(rows) == 1
    assert rows[0]["status"] == "consumed"
    assert rows[0]["error_kind"] == expected_kind
    assert rows[0]["billable_events"] == 1
    assert (
        runtime.active_hourly(city_slug="sfo", station_id="KSFO", now=TEST_NOW) == ()
    )


# ---------------------------------------------------------------------------
# Independent endpoint failures: no cross-contamination.
# ---------------------------------------------------------------------------


def test_hourly_failure_does_not_contaminate_daily_budget_or_store(tmp_path):
    city = get_city("nyc")
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)

    def failing_transport(url, timeout=20):
        raise TimeoutError("synthetic failure")

    with pytest.raises(google_api.GoogleCityFetchError):
        google_api.fetch_city_hourly(
            city,
            key=TEST_KEY,
            usage=usage,
            runtime=runtime,
            max_pages=1,
            transport=failing_transport,
            now=TEST_NOW,
        )

    def working_transport(url, timeout=20):
        return _Response(_daily_payload([("2026-07-19", 88.0)]))

    daily_rows = google_api.fetch_city_daily(
        city,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        transport=working_transport,
        now=TEST_NOW,
    )

    assert daily_rows == (
        google_api.GoogleDailyRow(target_date="2026-07-19", high_f=88.0),
    )
    assert (
        runtime.active_daily(city_slug="nyc", station_id="KNYC", now=TEST_NOW) != ()
    )
    assert (
        runtime.active_hourly(city_slug="nyc", station_id="KNYC", now=TEST_NOW) == ()
    )

    counts = usage.usage(now=TEST_NOW)
    assert counts.daily_events == 2

    hourly_db = _ledger_rows(usage, city_slug="nyc", endpoint="hourly")
    daily_db = _ledger_rows(usage, city_slug="nyc", endpoint="daily")
    assert hourly_db[0]["status"] == "consumed"
    assert hourly_db[0]["error_kind"] == "timeout"
    assert daily_db[0]["status"] == "success"


def test_daily_failure_does_not_contaminate_hourly_budget_or_store(tmp_path):
    city = get_city("nyc")
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)

    def failing_transport(url, timeout=20):
        raise TimeoutError("synthetic failure")

    with pytest.raises(google_api.GoogleCityFetchError):
        google_api.fetch_city_daily(
            city,
            key=TEST_KEY,
            usage=usage,
            runtime=runtime,
            transport=failing_transport,
            now=TEST_NOW,
        )

    valid_at = TEST_NOW + timedelta(hours=1)

    def working_transport(url, timeout=20):
        return _Response(_hourly_payload([_hour(valid_at, 70.0)]))

    hourly_rows = google_api.fetch_city_hourly(
        city,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        max_pages=1,
        transport=working_transport,
        now=TEST_NOW,
    )

    assert len(hourly_rows) == 1
    assert (
        runtime.active_hourly(city_slug="nyc", station_id="KNYC", now=TEST_NOW) != ()
    )
    assert (
        runtime.active_daily(city_slug="nyc", station_id="KNYC", now=TEST_NOW) == ()
    )

    hourly_db = _ledger_rows(usage, city_slug="nyc", endpoint="hourly")
    daily_db = _ledger_rows(usage, city_slug="nyc", endpoint="daily")
    assert hourly_db[0]["status"] == "success"
    assert daily_db[0]["status"] == "consumed"
    assert daily_db[0]["error_kind"] == "timeout"


def test_current_conditions_fetch_and_failure_are_independent_of_hourly_and_daily(
    tmp_path,
):
    city = get_city("sfo")
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)

    def failing_transport(url, timeout=20):
        raise HTTPError(url, 500, "boom", {}, None)

    with pytest.raises(google_api.GoogleCityFetchError):
        google_api.fetch_city_current(
            city,
            key=TEST_KEY,
            usage=usage,
            runtime=runtime,
            transport=failing_transport,
            now=TEST_NOW,
        )

    valid_at = TEST_NOW + timedelta(hours=1)

    def working_transport(url, timeout=20):
        return _Response(_hourly_payload([_hour(valid_at, 71.0)]))

    hourly_rows = google_api.fetch_city_hourly(
        city,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        max_pages=1,
        transport=working_transport,
        now=TEST_NOW,
    )

    assert len(hourly_rows) == 1
    assert (
        runtime.active_current(city_slug="sfo", station_id="KSFO", now=TEST_NOW)
        is None
    )
    current_db = _ledger_rows(usage, city_slug="sfo", endpoint="current")
    assert current_db[0]["status"] == "consumed"
    assert current_db[0]["error_kind"] == "http"
    assert current_db[0]["response_status_class"] == 5


# ---------------------------------------------------------------------------
# Contract A: never write an empty/partial generation on failure.
# ---------------------------------------------------------------------------


def test_contract_a_a_page_failure_after_partial_success_writes_nothing(tmp_path):
    city = get_city("sfo")
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    calls = []

    def transport(url, timeout=20):
        calls.append(url)
        if len(calls) == 1:
            return _Response(
                _hourly_payload(
                    [
                        _hour(TEST_NOW + timedelta(hours=h), 60.0 + h)
                        for h in range(1, 25)
                    ],
                    next_token="page-2",
                )
            )
        raise TimeoutError("synthetic failure on page 2")

    with pytest.raises(google_api.GoogleCityFetchError):
        google_api.fetch_city_hourly(
            city,
            key=TEST_KEY,
            usage=usage,
            runtime=runtime,
            max_pages=3,
            transport=transport,
            now=TEST_NOW,
        )

    assert len(calls) == 2
    assert (
        runtime.active_hourly(city_slug="sfo", station_id="KSFO", now=TEST_NOW) == ()
    )
    # The first page's dispatch still billed even though nothing was written.
    ledger_rows = _ledger_rows(usage, city_slug="sfo", endpoint="hourly")
    assert [row["status"] for row in ledger_rows] == ["success", "consumed"]


def test_contract_a_never_wipes_an_existing_generation_on_full_fetch_failure(
    tmp_path,
):
    city = get_city("sfo")
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    runtime.write_hourly(
        city_slug="sfo",
        station_id="KSFO",
        issued_at=TEST_NOW - timedelta(hours=1),
        valid_at=TEST_NOW + timedelta(hours=1),
        temperature_f=59.0,
        stored_at=TEST_NOW - timedelta(minutes=30),
    )

    def failing_transport(url, timeout=20):
        raise TimeoutError("synthetic failure")

    with pytest.raises(google_api.GoogleCityFetchError):
        google_api.fetch_city_hourly(
            city,
            key=TEST_KEY,
            usage=usage,
            runtime=runtime,
            max_pages=1,
            transport=failing_transport,
            now=TEST_NOW,
        )

    active = runtime.active_hourly(
        city_slug="sfo", station_id="KSFO", now=TEST_NOW
    )
    assert len(active) == 1
    assert active[0].temperature_f == 59.0


def test_contract_a_a_response_with_zero_usable_hours_writes_nothing(tmp_path):
    city = get_city("sfo")
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    runtime.write_hourly(
        city_slug="sfo",
        station_id="KSFO",
        issued_at=TEST_NOW - timedelta(hours=1),
        valid_at=TEST_NOW + timedelta(hours=1),
        temperature_f=59.0,
        stored_at=TEST_NOW - timedelta(minutes=30),
    )

    def transport(url, timeout=20):
        # A syntactically valid, successfully-dispatched response with no
        # forecast hours at all -- must not be treated as "delete everything".
        return _Response(_hourly_payload([]))

    rows = google_api.fetch_city_hourly(
        city,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        max_pages=1,
        transport=transport,
        now=TEST_NOW,
    )

    assert rows == ()
    active = runtime.active_hourly(
        city_slug="sfo", station_id="KSFO", now=TEST_NOW
    )
    assert len(active) == 1
    assert active[0].temperature_f == 59.0


# ---------------------------------------------------------------------------
# Contract B: a retry always performs a fresh fetch, never a cached replay.
# ---------------------------------------------------------------------------


def test_contract_b_retry_always_performs_a_fresh_fetch(tmp_path):
    city = get_city("sfo")
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    valid_at = TEST_NOW + timedelta(hours=1)
    responses = iter(
        [
            _hourly_payload([_hour(valid_at, 60.0)]),
            _hourly_payload([_hour(valid_at, 75.0)]),
        ]
    )
    calls = []

    def transport(url, timeout=20):
        calls.append(url)
        return _Response(next(responses))

    first = google_api.fetch_city_hourly(
        city,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        max_pages=1,
        transport=transport,
        now=TEST_NOW,
    )
    second = google_api.fetch_city_hourly(
        city,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        max_pages=1,
        transport=transport,
        now=TEST_NOW + timedelta(seconds=1),
    )

    assert len(calls) == 2
    assert first[0].temperature_f == 60.0
    assert second[0].temperature_f == 75.0

    active = runtime.active_hourly(
        city_slug="sfo", station_id="KSFO", now=TEST_NOW
    )
    assert len(active) == 1
    assert active[0].temperature_f == 75.0

    ledger_rows = _ledger_rows(usage, city_slug="sfo", endpoint="hourly")
    assert len(ledger_rows) == 2
    assert ledger_rows[0]["reservation_id"] != ledger_rows[1]["reservation_id"]


# ---------------------------------------------------------------------------
# Fixed-standard settlement-day bucketing (integration, DST boundary).
# ---------------------------------------------------------------------------


def test_hourly_rows_bucket_station_date_by_fixed_standard_time_across_dst(
    tmp_path,
):
    city = get_city("nyc")
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    # 2026-03-08T07:00:00Z is when America/New_York civil time jumps from
    # 2am EST straight to 3am EDT.
    transition = datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)
    instants = [transition + timedelta(hours=offset) for offset in (-2, -1, 0, 1, 2)]

    def transport(url, timeout=20):
        return _Response(
            _hourly_payload(
                [_hour(instant, 40.0 + i) for i, instant in enumerate(instants)]
            )
        )

    rows = google_api.fetch_city_hourly(
        city,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        max_pages=1,
        transport=transport,
        now=transition,
    )

    assert [row.station_date.isoformat() for row in rows] == ["2026-03-08"] * 5
    fixed_tz = city.fixed_standard_timezone()
    assert [row.valid_at.astimezone(fixed_tz).hour for row in rows] == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Secret safety.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fetch_kind",
    ["hourly", "daily", "current"],
)
def test_secret_key_and_url_never_appear_in_a_raised_exception(tmp_path, fetch_kind):
    city = get_city("nyc")
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    key = "AIzaSySecretMaterial123456789"

    def leaking_transport(url, timeout=20):
        assert key in url  # sanity: the secret really is embedded in the URL
        raise TimeoutError(f"timed out requesting {url}")

    if fetch_kind == "hourly":
        call = lambda: google_api.fetch_city_hourly(
            city,
            key=key,
            usage=usage,
            runtime=runtime,
            max_pages=1,
            transport=leaking_transport,
            now=TEST_NOW,
        )
    elif fetch_kind == "daily":
        call = lambda: google_api.fetch_city_daily(
            city,
            key=key,
            usage=usage,
            runtime=runtime,
            transport=leaking_transport,
            now=TEST_NOW,
        )
    else:
        call = lambda: google_api.fetch_city_current(
            city,
            key=key,
            usage=usage,
            runtime=runtime,
            transport=leaking_transport,
            now=TEST_NOW,
        )

    with pytest.raises(google_api.GoogleCityFetchError) as raised:
        call()

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    rendered = _rendered_exception(raised.value)
    assert key not in rendered
    assert key not in str(raised.value)


def test_http_error_with_key_bearing_message_is_sanitized(tmp_path):
    city = get_city("sfo")
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    key = "AIzaSySecretMaterial123456789"

    def transport(url, timeout=20):
        raise HTTPError(url, 500, f"server rejected key {key}", {}, None)

    with pytest.raises(google_api.GoogleCityFetchError) as raised:
        google_api.fetch_city_hourly(
            city,
            key=key,
            usage=usage,
            runtime=runtime,
            max_pages=1,
            transport=transport,
            now=TEST_NOW,
        )

    rendered = _rendered_exception(raised.value)
    assert key not in rendered
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


# ---------------------------------------------------------------------------
# Task 5: complete station-day highs and the fixed research challenger.
# ---------------------------------------------------------------------------


import google_runtime_blend
from google_runtime_blend import (
    challenger_from_runtime_high,
    derive_station_day_high,
    google_challenger,
)
from google_weather_store import GoogleHourlyRuntime, GoogleRuntimeHigh


def _write_full_station_day(runtime, *, city, issued_at, target_date, base_temp=60.0):
    """Write 24 consecutive fixed-standard hourly rows covering target_date."""

    tz = city.fixed_standard_timezone()
    start_local = datetime.combine(
        date.fromisoformat(target_date), datetime.min.time(), tzinfo=tz
    )
    temperatures_by_valid_at = {
        (start_local + timedelta(hours=hour)).astimezone(timezone.utc): base_temp + hour
        for hour in range(24)
    }
    runtime.replace_hourly_generation(
        city_slug=city.slug,
        station_id=city.nws_station_id,
        issued_at=issued_at,
        temperatures_by_valid_at=temperatures_by_valid_at,
        stored_at=issued_at,
    )
    return temperatures_by_valid_at


def test_complete_high_requires_all_24_fixed_standard_hours(tmp_path):
    city = get_city("nyc")
    runtime = _runtime_store(tmp_path)
    issued_at = TEST_NOW
    target_date = "2026-07-19"
    temperatures = _write_full_station_day(
        runtime, city=city, issued_at=issued_at, target_date=target_date
    )

    result = derive_station_day_high(
        runtime, city=city, target_date=target_date, now=issued_at
    )

    assert result is not None
    assert result.complete is True
    assert result.covered_hours == 24
    assert result.high_f == max(temperatures.values())
    assert result.target_date == target_date

    stored = runtime.active_runtime_high(
        city_slug=city.slug,
        station_id=city.nws_station_id,
        target_date=target_date,
        now=issued_at,
    )
    assert stored == result


def test_partial_same_day_is_remaining_heat_not_complete_high(tmp_path):
    city = get_city("nyc")
    runtime = _runtime_store(tmp_path)
    issued_at = TEST_NOW
    target_date = "2026-07-19"
    tz = city.fixed_standard_timezone()
    start_local = datetime.combine(
        date.fromisoformat(target_date), datetime.min.time(), tzinfo=tz
    )
    temperatures_by_valid_at = {
        (start_local + timedelta(hours=hour)).astimezone(timezone.utc): 60.0 + hour
        for hour in range(23)  # hour 23 is missing
    }
    runtime.replace_hourly_generation(
        city_slug=city.slug,
        station_id=city.nws_station_id,
        issued_at=issued_at,
        temperatures_by_valid_at=temperatures_by_valid_at,
        stored_at=issued_at,
    )

    result = derive_station_day_high(
        runtime, city=city, target_date=target_date, now=issued_at
    )

    assert result is not None
    assert result.covered_hours == 23
    assert result.complete is False
    # Partial coverage is "remaining heat," never a usable final high: the
    # challenger must fail closed rather than adjust off an incomplete day.
    assert (
        challenger_from_runtime_high(result, baseline_mu=80.0, baseline_sigma=3.0)
        is None
    )


def test_derive_station_day_high_returns_none_and_writes_nothing_without_hourly_data(
    tmp_path,
):
    city = get_city("nyc")
    runtime = _runtime_store(tmp_path)
    target_date = "2026-07-19"

    result = derive_station_day_high(
        runtime, city=city, target_date=target_date, now=TEST_NOW
    )

    assert result is None
    assert (
        runtime.active_runtime_high(
            city_slug=city.slug,
            station_id=city.nws_station_id,
            target_date=target_date,
            now=TEST_NOW,
        )
        is None
    )


def test_hourly_rows_bucket_a_complete_station_day_across_a_dst_boundary(tmp_path):
    # 2026-03-08 is when America/New_York civil time jumps from 2am EST
    # straight to 3am EDT -- the fixed-standard station day must still be
    # exactly 24 hours.
    city = get_city("nyc")
    runtime = _runtime_store(tmp_path)
    issued_at = datetime(2026, 3, 8, 0, 0, tzinfo=timezone.utc)
    target_date = "2026-03-08"

    temperatures = _write_full_station_day(
        runtime, city=city, issued_at=issued_at, target_date=target_date
    )

    result = derive_station_day_high(
        runtime, city=city, target_date=target_date, now=issued_at
    )

    assert result is not None
    assert result.complete is True
    assert result.covered_hours == 24
    assert result.high_f == max(temperatures.values())


def test_group_station_day_hours_fails_closed_on_a_colliding_hour():
    # The public store API cannot organically produce this (a unique
    # valid_at primary key plus generation-scoped MAX(issued_at) reads
    # structurally prevent it) -- this directly exercises the
    # integrity-layer guard contract A calls for, against a hand-built
    # anomaly the store itself does not defend against.
    city = get_city("nyc")
    tz = city.fixed_standard_timezone()
    target = date.fromisoformat("2026-07-19")
    issued_at = TEST_NOW
    start_local = datetime.combine(target, datetime.min.time(), tzinfo=tz)
    rows = [
        GoogleHourlyRuntime(
            city_slug=city.slug,
            station_id=city.nws_station_id,
            issued_at=issued_at,
            valid_at=(start_local + timedelta(hours=hour)).astimezone(timezone.utc),
            temperature_f=60.0 + hour,
            expires_at=issued_at + timedelta(hours=1),
        )
        for hour in range(24)
    ]
    # A 25th row colliding on hour 5.
    colliding = GoogleHourlyRuntime(
        city_slug=city.slug,
        station_id=city.nws_station_id,
        issued_at=issued_at,
        valid_at=rows[5].valid_at,
        temperature_f=999.0,
        expires_at=issued_at + timedelta(hours=1),
    )

    result = google_runtime_blend._group_station_day_hours(
        rows + [colliding], target=target, standard_tz=tz
    )

    assert result is None


def test_group_station_day_hours_fails_closed_on_mixed_issue_generations():
    city = get_city("nyc")
    tz = city.fixed_standard_timezone()
    target = date.fromisoformat("2026-07-19")
    issued_at = TEST_NOW
    other_issued_at = TEST_NOW + timedelta(hours=1)
    start_local = datetime.combine(target, datetime.min.time(), tzinfo=tz)
    rows = [
        GoogleHourlyRuntime(
            city_slug=city.slug,
            station_id=city.nws_station_id,
            issued_at=issued_at if hour < 23 else other_issued_at,
            valid_at=(start_local + timedelta(hours=hour)).astimezone(timezone.utc),
            temperature_f=60.0 + hour,
            expires_at=issued_at + timedelta(hours=1),
        )
        for hour in range(24)
    ]

    result = google_runtime_blend._group_station_day_hours(
        rows, target=target, standard_tz=tz
    )

    assert result is None


def test_challenger_uses_fifteen_percent_share_capped_at_one_point_five():
    """Mirrors the plan's Task 5 Step 1 example, corrected for the 7F block.

    The plan's own gap=15 example (``google_challenger(80, 3, 95).mu ==
    pytest.approx(81.5)``) contradicts both the spec's explicit "abs(gap) >=
    7F blocks" rule (design doc section 7.3) and the plan's OWN Step 3
    sample implementation, which blocks before the cap can ever bind. The
    spec is authoritative here (reinforced by this task's binding contract
    on fail-closed corroboration blocks), so this test keeps the plan's
    first (unblocked) assertion and replaces the second with an in-band
    value that actually exercises the 15% share/cap math without silently
    contradicting the block rule. True cap saturation is verified directly
    in test_capped_google_adjustment_saturates_at_one_point_five_both_directions,
    and the block itself in test_seven_degree_gap_emits_block_not_probability.
    """

    unblocked = google_challenger(80.0, 3.0, 84.0)
    assert unblocked.mu == pytest.approx(80.6)
    assert unblocked.sigma == 3.0
    assert unblocked.action == "forecast"

    near_boundary = google_challenger(80.0, 3.0, 86.99)  # gap=6.99, still unblocked
    assert near_boundary.mu == pytest.approx(80.0 + 0.15 * 6.99)
    assert near_boundary.action == "forecast"


def test_challenger_applies_the_fixed_share_in_both_directions():
    warmer = google_challenger(80.0, 3.0, 84.0)
    cooler = google_challenger(80.0, 3.0, 76.0)

    assert warmer.mu == pytest.approx(80.6)
    assert cooler.mu == pytest.approx(79.4)
    assert warmer.sigma == cooler.sigma == 3.0
    assert warmer.action == cooler.action == "forecast"


def test_capped_google_adjustment_saturates_at_one_point_five_both_directions():
    # Unreachable through google_challenger itself under the frozen 7F block
    # gate (see google_runtime_blend._capped_google_adjustment) -- tested
    # directly as an independent safety bound.
    assert google_runtime_blend._capped_google_adjustment(10.0) == pytest.approx(1.5)
    assert google_runtime_blend._capped_google_adjustment(100.0) == pytest.approx(1.5)
    assert google_runtime_blend._capped_google_adjustment(-10.0) == pytest.approx(-1.5)
    assert google_runtime_blend._capped_google_adjustment(-100.0) == pytest.approx(-1.5)


def test_seven_degree_gap_emits_block_not_probability():
    blocked_high = google_challenger(80.0, 3.0, 87.0)  # gap=+7 exactly
    blocked_low = google_challenger(80.0, 3.0, 73.0)  # gap=-7 exactly
    far_blocked = google_challenger(80.0, 3.0, 95.0)  # gap=+15
    just_under = google_challenger(80.0, 3.0, 86.99)  # gap=6.99

    for blocked in (blocked_high, blocked_low, far_blocked):
        assert blocked.mu is None
        assert blocked.action == "external_runtime_corroboration_block"
        assert blocked.sigma == 3.0

    assert just_under.mu is not None
    assert just_under.action == "forecast"


def test_google_challenger_is_pure_and_deterministic():
    first = google_challenger(80.3, 2.7, 83.1)
    second = google_challenger(80.3, 2.7, 83.1)

    assert first == second


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_google_challenger_rejects_non_finite_baseline_mu(bad):
    with pytest.raises(ValueError, match="finite"):
        google_challenger(bad, 3.0, 84.0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_google_challenger_rejects_non_finite_baseline_sigma(bad):
    with pytest.raises(ValueError, match="finite"):
        google_challenger(80.0, bad, 84.0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_google_challenger_rejects_non_finite_google_high(bad):
    with pytest.raises(ValueError, match="finite"):
        google_challenger(80.0, 3.0, bad)


def test_google_challenger_rejects_non_finite_inputs_before_computing_gap():
    """A NaN high must never bypass the 7F block or fabricate an adjustment.

    Before validation, ``nan >= 7.0`` is False (NaN block comparisons are
    always False), so a NaN google_high fell through to the forecast branch
    and ``min(1.5, nan)``-style clamps silently produced a confident-looking
    +1.5F adjustment from garbage input. Validation must reject it outright.
    """

    with pytest.raises(ValueError, match="finite"):
        google_challenger(80.0, 3.0, float("nan"))


def test_challenger_from_runtime_high_returns_none_without_google_evidence():
    assert (
        challenger_from_runtime_high(None, baseline_mu=80.0, baseline_sigma=3.0)
        is None
    )


def test_challenger_from_runtime_high_fails_closed_on_incomplete_coverage():
    incomplete = GoogleRuntimeHigh(
        city_slug="nyc",
        station_id="KNYC",
        issued_at=TEST_NOW,
        target_date="2026-07-19",
        high_f=84.0,
        covered_hours=23,
        complete=False,
        expires_at=TEST_NOW + timedelta(hours=1),
    )

    assert (
        challenger_from_runtime_high(incomplete, baseline_mu=80.0, baseline_sigma=3.0)
        is None
    )


def test_challenger_from_runtime_high_forwards_the_fixed_formula():
    complete = GoogleRuntimeHigh(
        city_slug="nyc",
        station_id="KNYC",
        issued_at=TEST_NOW,
        target_date="2026-07-19",
        high_f=84.0,
        covered_hours=24,
        complete=True,
        expires_at=TEST_NOW + timedelta(hours=1),
    )

    result = challenger_from_runtime_high(
        complete, baseline_mu=80.0, baseline_sigma=3.0
    )

    assert result == google_challenger(80.0, 3.0, 84.0)


# ---------------------------------------------------------------------------
# Live-path isolation (requirement 4: research-only evidence).
#
# The challenger is research-only: nothing it computes may reach the live
# SFO forecast path, LSTM/EMOS training, adaptive weights, MOS, residual
# de-bias, or historical baseline scorecards. Today (Task 5) nothing wires
# google_runtime_blend into any of those modules yet -- that wiring, if it
# ever happens, is scoped to Task 7/8 under the plan's own explicit
# research-shadow-only policy. This guard exists so that wiring cannot
# happen silently/accidentally in some other change.
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIVE_AND_TRAINING_MODULES = tuple(
    _REPO_ROOT / relative
    for relative in (
        "forecaster/blend_sources.py",
        "forecaster/blend_archive.py",
        "forecaster/blend_learners.py",
        "forecaster/emos_forecast.py",
        "forecaster/emos_recalibration.py",
        "forecaster/postproc_recalibration.py",
        "forecaster/postproc_models.py",
        "forecaster/recalibration_replay.py",
        "forecaster/forecast_backtest.py",
        "forecaster/forecast_postproc_backtest.py",
        "forecaster/forecast_scoring.py",
        "forecaster/scores.py",
        "trading/sfo_kalshi_quant/forecast.py",
        "trading/sfo_kalshi_quant/db.py",
        "trading/sfo_kalshi_quant/models.py",
        "trading/sfo_kalshi_quant/prediction_features.py",
        "trading/sfo_kalshi_quant/report.py",
        "trading/sfo_kalshi_quant/publication.py",
        "trading/sfo_kalshi_quant/store/diagnostics.py",
        "trading/sfo_kalshi_quant/store/schema.py",
    )
)


def test_google_runtime_blend_never_imports_the_live_or_training_path():
    source = Path(google_runtime_blend.__file__).read_text()
    for forbidden in (
        "blend_sources",
        "blend_archive",
        "blend_learners",
        "emos_forecast",
        "emos_recalibration",
        "postproc_recalibration",
        "postproc_models",
        "recalibration_replay",
        "forecast_backtest",
        "forecast_postproc_backtest",
        "forecast_scoring",
        "sfo_kalshi_quant",
    ):
        assert forbidden not in source


def test_no_live_or_training_module_imports_the_google_challenger():
    for path in _LIVE_AND_TRAINING_MODULES:
        assert path.is_file(), path
        assert "google_runtime_blend" not in path.read_text()


# ---------------------------------------------------------------------------
# Task 6: budget-safe 15-city refresh orchestration.
# ---------------------------------------------------------------------------

import ast

import google_multicity_refresh as gmr
from weather_cache_config import (
    CURRENT_API_URL,
    DAILY_API_URL,
    GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET,
    GOOGLE_WEATHER_SOFT_MONTHLY_CEILING,
    HOURLY_API_URL,
)


def _noop_baseline():
    return None


def _hourly_page_response(url, *, temp_f=65.0):
    """Deterministically serve exactly 3 hourly pages per city."""

    if "pageToken=p2" in url:
        return _Response(
            _hourly_payload([_hour(TEST_NOW + timedelta(hours=2), temp_f)], next_token="p3")
        )
    if "pageToken=p3" in url:
        return _Response(_hourly_payload([_hour(TEST_NOW + timedelta(hours=3), temp_f)]))
    return _Response(
        _hourly_payload([_hour(TEST_NOW + timedelta(hours=1), temp_f)], next_token="p2")
    )


def _full_bundle_transport(*, fail=None):
    """A transport fake that serves hourly/daily/current for every city.

    ``fail`` is an optional ``(city_slug, endpoint)`` pair whose request
    raises a transport error -- every other city/endpoint still succeeds,
    exercising per-city and per-endpoint isolation.
    """

    calls = []

    def transport(url, timeout=20):
        calls.append(url)
        if fail is not None:
            city_slug, endpoint = fail
            failing_city = get_city(city_slug)
            endpoint_url = {
                "hourly": HOURLY_API_URL,
                "daily": DAILY_API_URL,
                "current": CURRENT_API_URL,
            }[endpoint]
            if url.startswith(endpoint_url) and (
                f"location.latitude={failing_city.latitude:.4f}" in url
            ):
                raise URLError("simulated transport failure")
        if url.startswith(HOURLY_API_URL):
            return _hourly_page_response(url)
        if url.startswith(DAILY_API_URL):
            return _Response(_daily_payload([("2026-07-19", 70.0)]))
        if url.startswith(CURRENT_API_URL):
            return _Response(_current_payload(65.0))
        raise AssertionError(f"unexpected Google Weather URL: {url}")

    transport.calls = calls
    return transport


# ---------------------------------------------------------------------------
# Plan Step 1: budget arithmetic.
# ---------------------------------------------------------------------------


def test_sfo_bundle_cost_matches_documented_190_per_day():
    assert gmr.SFO_BUNDLE_MAX_EVENTS == 5
    assert 38 * gmr.SFO_BUNDLE_MAX_EVENTS == 190
    assert 190 * 31 == 5890


def test_non_sfo_bundle_cost_matches_documented_56_per_day():
    assert gmr.NON_SFO_BUNDLE_MAX_EVENTS == 4
    assert 14 * gmr.NON_SFO_BUNDLE_MAX_EVENTS == 56
    assert 56 * 31 == 1736


def test_total_daily_and_monthly_budget_matches_documented_schedule():
    assert 190 + 56 == 246
    assert 5890 + 1736 == 7626


def test_soft_ceiling_reserves_two_hundred_events_under_the_hard_monthly_cap():
    assert GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET == 8000
    assert GOOGLE_WEATHER_SOFT_MONTHLY_CEILING == 7800
    assert GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET - GOOGLE_WEATHER_SOFT_MONTHLY_CEILING == 200


# ---------------------------------------------------------------------------
# SFO priority ordering.
# ---------------------------------------------------------------------------


def test_sfo_is_always_first_regardless_of_input_order():
    ordered = gmr._priority_order(tuple(reversed(CITIES)), priority_hints=None)
    assert ordered[0].slug == "sfo"
    assert {city.slug for city in ordered} == {city.slug for city in CITIES}


def test_default_priority_order_falls_back_to_configured_market_volume_order():
    ordered = gmr._priority_order(CITIES, priority_hints=None)
    non_sfo_order = [city.slug for city in ordered if city.slug != "sfo"]
    expected_order = [city.slug for city in CITIES if city.slug != "sfo"]
    assert non_sfo_order == expected_order


def test_active_exposure_hint_overrides_market_volume_order():
    # Denver is last by configured market volume; a strong exposure hint
    # must still promote it ahead of every other non-SFO city.
    hints = {"den": gmr.CityPriorityHint(active_exposure=1000.0)}
    ordered = gmr._priority_order(CITIES, priority_hints=hints)
    non_sfo_order = [city.slug for city in ordered if city.slug != "sfo"]
    assert non_sfo_order[0] == "den"


def test_soonest_close_breaks_ties_before_market_volume_order():
    hints = {
        "den": gmr.CityPriorityHint(soonest_close_at=TEST_NOW + timedelta(hours=1)),
        "mia": gmr.CityPriorityHint(soonest_close_at=TEST_NOW + timedelta(hours=5)),
    }
    ordered = gmr._priority_order(CITIES, priority_hints=hints)
    non_sfo_order = [city.slug for city in ordered if city.slug != "sfo"]
    assert non_sfo_order.index("den") < non_sfo_order.index("mia")


def test_oldest_corroboration_breaks_ties_before_market_volume_order():
    hints = {
        "den": gmr.CityPriorityHint(corroboration_age_seconds=10_000.0),
        "mia": gmr.CityPriorityHint(corroboration_age_seconds=10.0),
    }
    ordered = gmr._priority_order(CITIES, priority_hints=hints)
    non_sfo_order = [city.slug for city in ordered if city.slug != "sfo"]
    assert non_sfo_order.index("den") < non_sfo_order.index("mia")


# ---------------------------------------------------------------------------
# Per-city and per-endpoint failure isolation.
# ---------------------------------------------------------------------------


def test_one_city_failure_leaves_the_other_fourteen_intact(tmp_path):
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    transport = _full_bundle_transport(fail=("lax", "hourly"))

    report = gmr.refresh_all_cities(
        cities=CITIES,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        transport=transport,
        now=TEST_NOW,
        archive_baseline=_noop_baseline,
    )

    assert len(report.cities) == len(CITIES)
    by_slug = {status.city_slug: status for status in report.cities}

    failing = by_slug["lax"]
    assert failing.attempted is True
    assert failing.endpoints["hourly"] == "failed"
    assert failing.error_kind == "hourly"
    # lax's daily fetch is independently isolated from its hourly failure.
    assert failing.endpoints["daily"] == "success"

    for city in CITIES:
        if city.slug == "lax":
            continue
        status = by_slug[city.slug]
        assert status.attempted is True, city.slug
        assert status.skipped_reason is None, city.slug
        assert status.available is True, city.slug
        assert all(outcome == "success" for outcome in status.endpoints.values()), city.slug


def test_google_weather_budget_exceeded_during_a_bundle_is_recorded_not_raised(tmp_path):
    usage = _usage_ledger(tmp_path, daily_budget=1000, monthly_budget=1000, soft_monthly_ceiling=3)
    runtime = _runtime_store(tmp_path)
    transport = _full_bundle_transport()

    report = gmr.refresh_all_cities(
        cities=(get_city("sfo"),),
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        transport=transport,
        now=TEST_NOW,
        archive_baseline=_noop_baseline,
    )

    status = report.cities[0]
    assert status.endpoints["hourly"] == "success"
    # SFO's own bundle exceeds a 2-event soft ceiling mid-flight; the ledger
    # itself blocks the remaining reservations and the orchestrator records
    # (never raises) the outcome.
    assert status.endpoints["daily"] == "budget_exceeded"
    assert status.error_kind == "budget"


# ---------------------------------------------------------------------------
# Hard budget stop: honored, with clean partial completion.
# ---------------------------------------------------------------------------


def test_hard_daily_budget_stop_leaves_partial_cities_completed_cleanly(tmp_path):
    usage = _usage_ledger(tmp_path, daily_budget=13, monthly_budget=1000, soft_monthly_ceiling=900)
    runtime = _runtime_store(tmp_path)
    transport = _full_bundle_transport()
    # Market-volume order among these three (excluding sfo) is mia, lax, chi.
    cities = (get_city("sfo"), get_city("chi"), get_city("lax"), get_city("mia"))

    report = gmr.refresh_all_cities(
        cities=cities,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        transport=transport,
        now=TEST_NOW,
        archive_baseline=_noop_baseline,
    )

    by_slug = {status.city_slug: status for status in report.cities}
    # sfo (5) + mia (4) + lax (4) = 13 == the daily budget: both fit.
    for slug in ("sfo", "mia", "lax"):
        assert by_slug[slug].attempted is True, slug
        assert by_slug[slug].available is True, slug
        assert by_slug[slug].skipped_reason is None, slug
    # chi: 13 + 4 = 17 > 13 -> the hard daily stop is honored; chi is never
    # attempted (no partial reservation, no wasted budget).
    assert by_slug["chi"].attempted is False
    assert by_slug["chi"].skipped_reason == "daily_budget"
    assert by_slug["chi"].endpoints == {}
    assert report.daily_events == 13


def test_hard_monthly_budget_stop_is_honored(tmp_path):
    usage = _usage_ledger(tmp_path, daily_budget=1000, monthly_budget=8, soft_monthly_ceiling=8)
    runtime = _runtime_store(tmp_path)
    transport = _full_bundle_transport()
    cities = (get_city("sfo"), get_city("mia"))

    report = gmr.refresh_all_cities(
        cities=cities,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        transport=transport,
        now=TEST_NOW,
        archive_baseline=_noop_baseline,
    )

    by_slug = {status.city_slug: status for status in report.cities}
    assert by_slug["sfo"].attempted is True
    # sfo (5) + mia (4) = 9 > monthly_budget (8): mia never attempted.
    assert by_slug["mia"].attempted is False
    assert by_slug["mia"].skipped_reason == "monthly_budget"


# ---------------------------------------------------------------------------
# Soft-ceiling priority behavior: SFO keeps priority, non-SFO stops.
# ---------------------------------------------------------------------------


def test_soft_ceiling_stops_non_sfo_but_sfo_keeps_priority(tmp_path):
    usage = _usage_ledger(tmp_path, daily_budget=1000, monthly_budget=1000, soft_monthly_ceiling=3)
    runtime = _runtime_store(tmp_path)
    transport = _full_bundle_transport()
    cities = (get_city("sfo"), get_city("mia"))

    report = gmr.refresh_all_cities(
        cities=cities,
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        transport=transport,
        now=TEST_NOW,
        archive_baseline=_noop_baseline,
    )

    by_slug = {status.city_slug: status for status in report.cities}
    # SFO is exempt from the orchestrator's soft-ceiling pre-check and is
    # always attempted; the shared ledger still enforces the true ceiling
    # once SFO's own bundle reaches it (a secondary safety net).
    assert by_slug["sfo"].attempted is True
    assert by_slug["sfo"].endpoints["hourly"] == "success"
    assert by_slug["sfo"].endpoints["daily"] == "budget_exceeded"
    # Non-SFO is subject to the soft ceiling and is skipped before it ever
    # attempts a reservation -- the "non-essential refreshes stop" behavior.
    assert by_slug["mia"].attempted is False
    assert by_slug["mia"].skipped_reason == "soft_ceiling"


# ---------------------------------------------------------------------------
# Baseline-first ordering and bidirectional failure isolation.
# ---------------------------------------------------------------------------


def test_baseline_archiver_runs_before_any_google_fetch_is_attempted(tmp_path):
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    events = []

    def baseline():
        events.append("baseline")

    inner_transport = _full_bundle_transport()

    def transport(url, timeout=20):
        events.append("google")
        return inner_transport(url, timeout=timeout)

    gmr.refresh_all_cities(
        cities=(get_city("sfo"),),
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        transport=transport,
        now=TEST_NOW,
        archive_baseline=baseline,
    )

    assert events[0] == "baseline"
    assert "google" in events
    assert events.index("baseline") < events.index("google")


def test_baseline_failure_does_not_block_google_fetching(tmp_path):
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    transport = _full_bundle_transport()

    def failing_baseline():
        raise RuntimeError("simulated EMOS outage")

    report = gmr.refresh_all_cities(
        cities=(get_city("sfo"),),
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        transport=transport,
        now=TEST_NOW,
        archive_baseline=failing_baseline,
    )

    assert report.baseline.attempted is True
    assert report.baseline.succeeded is False
    assert report.baseline.error_kind == "RuntimeError"
    assert report.cities[0].attempted is True
    assert report.cities[0].available is True


def test_google_fetch_failure_cannot_retroactively_affect_the_already_archived_baseline(tmp_path):
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    transport = _full_bundle_transport(fail=("sfo", "hourly"))
    baseline_calls = []

    def baseline():
        baseline_calls.append(1)

    report = gmr.refresh_all_cities(
        cities=(get_city("sfo"),),
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        transport=transport,
        now=TEST_NOW,
        archive_baseline=baseline,
    )

    assert baseline_calls == [1]
    assert report.baseline.succeeded is True
    assert report.cities[0].endpoints["hourly"] == "failed"


# ---------------------------------------------------------------------------
# Runtime purge each cycle.
# ---------------------------------------------------------------------------


def test_purge_expired_runs_once_per_cycle(tmp_path):
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    runtime.replace_hourly_generation(
        city_slug="sfo",
        station_id=get_city("sfo").nws_station_id,
        issued_at=TEST_NOW - timedelta(hours=5),
        temperatures_by_valid_at={TEST_NOW - timedelta(hours=4): 60.0},
        stored_at=TEST_NOW - timedelta(hours=5),
    )
    assert runtime.next_expiry(now=TEST_NOW - timedelta(hours=6)) is not None

    report = gmr.refresh_all_cities(
        cities=(get_city("sfo"),),
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        transport=_full_bundle_transport(),
        now=TEST_NOW,
        archive_baseline=_noop_baseline,
    )

    assert report.purged_rows >= 1
    # The seeded expired row is gone; only the fresh row this cycle wrote survives.
    active = runtime.active_hourly(
        city_slug="sfo", station_id=get_city("sfo").nws_station_id, now=TEST_NOW
    )
    assert all(row.temperature_f != 60.0 for row in active)


# ---------------------------------------------------------------------------
# Missing API key: fails neutral, still archives baseline and purges.
# ---------------------------------------------------------------------------


def test_missing_api_key_skips_every_city_but_still_archives_baseline_and_purges(tmp_path):
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    baseline_calls = []

    report = gmr.refresh_all_cities(
        cities=(get_city("sfo"), get_city("mia")),
        key=None,
        usage=usage,
        runtime=runtime,
        transport=_full_bundle_transport(),
        now=TEST_NOW,
        archive_baseline=lambda: baseline_calls.append(1),
    )

    assert baseline_calls == [1]
    assert report.baseline.succeeded is True
    for status in report.cities:
        assert status.attempted is False
        assert status.skipped_reason == "missing_api_key"
    assert report.daily_events == 0
    assert report.monthly_events == 0


# ---------------------------------------------------------------------------
# Raw-free compatibility status (plan Task 6 Step 4).
# ---------------------------------------------------------------------------


def test_compatibility_status_never_contains_raw_google_content(tmp_path):
    usage = _usage_ledger(tmp_path)
    runtime = _runtime_store(tmp_path)
    report = gmr.refresh_all_cities(
        cities=(get_city("sfo"), get_city("mia")),
        key=TEST_KEY,
        usage=usage,
        runtime=runtime,
        transport=_full_bundle_transport(),
        now=TEST_NOW,
        archive_baseline=_noop_baseline,
    )

    payload = gmr.build_compatibility_status(report)
    serialized = json.dumps(payload)

    for forbidden in (
        "temperature",
        "high_f",
        "highF",
        "condition",
        "degrees",
        "maxTemperature",
        "forecastHours",
        "forecastDays",
        TEST_KEY,
    ):
        assert forbidden not in serialized, forbidden

    assert payload["cities"]["sfo"]["endpoints"]["hourly"] == "success"
    assert set(payload.keys()) == {
        "source",
        "mode",
        "generated_at",
        "budget",
        "purged_runtime_rows",
        "baseline",
        "cities",
    }
    assert set(payload["budget"].keys()) == {
        "daily_events",
        "daily_event_budget",
        "monthly_events",
        "monthly_event_budget",
        "soft_monthly_ceiling",
    }


# ---------------------------------------------------------------------------
# Research-only / structural isolation.
# ---------------------------------------------------------------------------


def _imported_module_names(module):
    """Actual import statements only -- not docstring prose mentioning a name."""

    tree = ast.parse(Path(module.__file__).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _imported_names_from(module, *, source_module):
    """Names imported via ``from source_module import ...`` in ``module``."""

    tree = ast.parse(Path(module.__file__).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == source_module:
            names.update(alias.name for alias in node.names)
    return names


def test_google_multicity_refresh_never_imports_the_google_challenger():
    assert "google_runtime_blend" not in _imported_module_names(gmr)


def test_multicity_refresh_never_uses_fetch_city_weather():
    """Contract A: budget decisions come from the ledger, never
    ``fetch_city_weather``'s global monthly-delta ``dispatched_events``
    field. This module composes the per-endpoint fetchers directly instead.
    """

    assert "fetch_city_weather" not in _imported_names_from(gmr, source_module="google_api")
