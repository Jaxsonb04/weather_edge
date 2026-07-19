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
import sqlite3
import traceback
from datetime import datetime, timedelta, timezone
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
