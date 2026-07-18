"""Boundary tests for Google Weather runtime storage and usage policy."""

from __future__ import annotations

import importlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import weather_cache_config


TEST_NOW = datetime(2026, 7, 18, 19, 0, tzinfo=timezone.utc)


def _usage_ledger(tmp_path, **limits):
    from google_weather_store import GoogleUsageLedger

    return GoogleUsageLedger(tmp_path / "weather.db", **limits)


def _seed_billable_events(ledger, count: int, *, billing_date: str) -> None:
    """Arrange valid near-limit history without thousands of tiny transactions."""

    billing_month = billing_date[:7]
    reserved_at = f"{billing_date}T12:00:00.000000+00:00"
    with sqlite3.connect(ledger.db_path) as connection:
        connection.executemany(
            """
            INSERT INTO google_weather_usage_events (
                reservation_id, billing_month, billing_date_pacific,
                city_slug, station_id, endpoint, page_number, reserved_at,
                status, billable_events
            ) VALUES (?, ?, ?, 'test-city', 'TEST0001', 'hourly', 1, ?,
                      'reserved', 1)
            """,
            (
                (
                    f"seed-{billing_date}-{index}",
                    billing_month,
                    billing_date,
                    reserved_at,
                )
                for index in range(count)
            ),
        )


def _race_two_reservations(ledger, *, now: datetime) -> tuple[int, list[str]]:
    from google_weather_store import GoogleWeatherBudgetExceeded

    start = threading.Barrier(2)

    def attempt(index: int):
        start.wait()
        try:
            ledger.reserve_event(
                city_slug="phx",
                station_id="KPHX",
                endpoint="daily",
                page_number=index,
                now=now,
            )
            return True, ""
        except GoogleWeatherBudgetExceeded as exc:
            return False, exc.scope

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    return sum(admitted for admitted, _scope in results), [
        scope for admitted, scope in results if not admitted
    ]


def _reload_config_with(monkeypatch: pytest.MonkeyPatch, **environment: object):
    """Reload config with a temporary environment, then restore it after use."""

    for name, value in environment.items():
        monkeypatch.setenv(name, str(value))
    return importlib.reload(weather_cache_config)


def test_google_runtime_ttls_cannot_exceed_official_maxima(monkeypatch):
    with monkeypatch.context() as environment:
        config = _reload_config_with(
            environment,
            GOOGLE_HOURLY_TTL_SECONDS=7_200,
            GOOGLE_CURRENT_TTL_SECONDS=7_200,
            GOOGLE_TODAY_DAILY_TTL_SECONDS=31 * 24 * 60 * 60,
            GOOGLE_FUTURE_DAILY_TTL_SECONDS=48 * 60 * 60,
        )

        assert config.GOOGLE_HOURLY_TTL == timedelta(hours=1)
        assert config.GOOGLE_CURRENT_TTL == timedelta(hours=1)
        assert config.GOOGLE_TODAY_DAILY_TTL == timedelta(days=30)
        assert config.GOOGLE_FUTURE_DAILY_TTL == timedelta(hours=24)

    importlib.reload(weather_cache_config)


def test_google_runtime_ttls_may_be_shortened(monkeypatch):
    with monkeypatch.context() as environment:
        config = _reload_config_with(
            environment,
            GOOGLE_HOURLY_TTL_SECONDS=1_800,
            GOOGLE_CURRENT_TTL_SECONDS=900,
            GOOGLE_TODAY_DAILY_TTL_SECONDS=14 * 24 * 60 * 60,
            GOOGLE_FUTURE_DAILY_TTL_SECONDS=12 * 60 * 60,
        )

        assert config.GOOGLE_HOURLY_TTL == timedelta(minutes=30)
        assert config.GOOGLE_CURRENT_TTL == timedelta(minutes=15)
        assert config.GOOGLE_TODAY_DAILY_TTL == timedelta(days=14)
        assert config.GOOGLE_FUTURE_DAILY_TTL == timedelta(hours=12)

    importlib.reload(weather_cache_config)


def test_hourly_page_limit_cannot_be_configured_above_three(monkeypatch):
    with monkeypatch.context() as environment:
        config = _reload_config_with(environment, GOOGLE_HOURLY_MAX_PAGES=4)

        assert config.GOOGLE_HOURLY_MAX_PAGES == 3

    importlib.reload(weather_cache_config)


def test_google_runtime_budget_defaults_are_the_internal_limits(monkeypatch):
    with monkeypatch.context() as environment:
        environment.delenv("GOOGLE_WEATHER_DAILY_EVENT_BUDGET", raising=False)
        environment.delenv("GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET", raising=False)
        environment.delenv("GOOGLE_WEATHER_SOFT_MONTHLY_CEILING", raising=False)
        config = importlib.reload(weather_cache_config)

        assert config.GOOGLE_WEATHER_DAILY_EVENT_BUDGET == 260
        assert config.GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET == 8_000
        assert config.GOOGLE_WEATHER_SOFT_MONTHLY_CEILING == 7_800

    importlib.reload(weather_cache_config)


def test_google_runtime_budgets_can_shrink_but_cannot_exceed_limits(monkeypatch):
    with monkeypatch.context() as environment:
        config = _reload_config_with(
            environment,
            GOOGLE_WEATHER_DAILY_EVENT_BUDGET=9_999,
            GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET=99_999,
            GOOGLE_WEATHER_SOFT_MONTHLY_CEILING=99_999,
        )

        assert config.GOOGLE_WEATHER_DAILY_EVENT_BUDGET == 260
        assert config.GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET == 8_000
        assert config.GOOGLE_WEATHER_SOFT_MONTHLY_CEILING == 7_800

    with monkeypatch.context() as environment:
        config = _reload_config_with(
            environment,
            GOOGLE_WEATHER_DAILY_EVENT_BUDGET=200,
            GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET=6_000,
            GOOGLE_WEATHER_SOFT_MONTHLY_CEILING=5_900,
        )

        assert config.GOOGLE_WEATHER_DAILY_EVENT_BUDGET == 200
        assert config.GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET == 6_000
        assert config.GOOGLE_WEATHER_SOFT_MONTHLY_CEILING == 5_900

    importlib.reload(weather_cache_config)


def test_production_runtime_path_must_stay_under_run_weatheredge(
    tmp_path, monkeypatch
):
    injected_path = tmp_path / "google_runtime.db"

    with monkeypatch.context() as environment:
        environment.setenv("GOOGLE_RUNTIME_DB_PATH", str(injected_path))
        environment.setenv("KALSHI_ENV", "prod")
        with pytest.raises(
            RuntimeError,
            match="Google runtime content must live under /run/weatheredge",
        ):
            importlib.reload(weather_cache_config)

    importlib.reload(weather_cache_config)


def test_unit_tests_may_inject_a_temporary_runtime_path(tmp_path, monkeypatch):
    injected_path = tmp_path / "google_runtime.db"

    with monkeypatch.context() as environment:
        environment.setenv("GOOGLE_RUNTIME_DB_PATH", str(injected_path))
        environment.delenv("KALSHI_ENV", raising=False)
        config = importlib.reload(weather_cache_config)

        assert config.GOOGLE_RUNTIME_DB_PATH == injected_path

    importlib.reload(weather_cache_config)


def test_google_runtime_database_path_defaults_to_production_tmpfs(monkeypatch):
    with monkeypatch.context() as environment:
        environment.delenv("GOOGLE_RUNTIME_DB_PATH", raising=False)
        config = importlib.reload(weather_cache_config)

        assert config.GOOGLE_RUNTIME_DB_PATH == Path(
            "/run/weatheredge/google_runtime.db"
        )

    importlib.reload(weather_cache_config)


def test_cancelled_reservation_releases_budget_before_dispatch(tmp_path):
    ledger = _usage_ledger(
        tmp_path,
        daily_budget=1,
        monthly_budget=1,
        soft_monthly_ceiling=1,
    )
    first = ledger.reserve_event(
        city_slug="sfo",
        station_id="KSFO",
        endpoint="hourly",
        page_number=1,
        now=TEST_NOW,
    )

    assert ledger.cancel_before_dispatch(first, now=TEST_NOW + timedelta(seconds=1))
    second = ledger.reserve_event(
        city_slug="sfo",
        station_id="KSFO",
        endpoint="hourly",
        page_number=1,
        now=TEST_NOW + timedelta(seconds=2),
    )

    assert ledger.event(second).status == "reserved"
    assert ledger.usage(now=TEST_NOW).daily_events == 1


def test_dispatched_reservation_is_consumed_and_cannot_be_cancelled(tmp_path):
    ledger = _usage_ledger(tmp_path)
    event = ledger.reserve_event(
        city_slug="den",
        station_id="KDEN",
        endpoint="current",
        page_number=0,
        now=TEST_NOW,
    )

    ledger.mark_dispatched(event, now=TEST_NOW + timedelta(seconds=1))

    assert not ledger.cancel_before_dispatch(
        event, now=TEST_NOW + timedelta(seconds=2)
    )
    state = ledger.event(event)
    assert state.status == "consumed"
    assert state.billable_events == 1
    assert state.dispatched_at is not None


@pytest.mark.parametrize(
    ("success", "response_class", "error_kind", "expected_status"),
    [
        (True, 2, None, "success"),
        (False, None, "timeout", "consumed"),
        (False, None, "transport", "consumed"),
        (False, 5, "http", "consumed"),
        (False, 2, "parse", "consumed"),
    ],
)
def test_completed_dispatch_always_remains_billable(
    tmp_path, success, response_class, error_kind, expected_status
):
    ledger = _usage_ledger(tmp_path)
    event = ledger.reserve_event(
        city_slug="sea",
        station_id="KSEA",
        endpoint="daily",
        page_number=0,
        now=TEST_NOW,
    )
    ledger.mark_dispatched(event, now=TEST_NOW + timedelta(seconds=1))

    ledger.complete_event(
        event,
        success=success,
        response_status_class=response_class,
        error_kind=error_kind,
        now=TEST_NOW + timedelta(seconds=2),
    )

    state = ledger.event(event)
    assert state.status == expected_status
    assert state.billable_events == 1
    assert state.completed_at is not None
    assert state.response_status_class == response_class
    assert state.error_kind == error_kind
    assert ledger.usage(now=TEST_NOW).daily_events == 1


def test_stale_undispatched_reservations_are_cancelled_in_bulk(tmp_path):
    ledger = _usage_ledger(tmp_path)
    stale = ledger.reserve_event(
        city_slug="mia",
        station_id="KMIA",
        endpoint="hourly",
        page_number=1,
        now=TEST_NOW,
    )
    dispatched = ledger.reserve_event(
        city_slug="mia",
        station_id="KMIA",
        endpoint="hourly",
        page_number=2,
        now=TEST_NOW,
    )
    ledger.mark_dispatched(dispatched, now=TEST_NOW + timedelta(seconds=1))

    cancelled = ledger.cancel_stale_undispatched(
        before=TEST_NOW + timedelta(seconds=5),
        now=TEST_NOW + timedelta(seconds=6),
    )

    assert cancelled == 1
    assert ledger.event(stale).status == "cancelled"
    assert ledger.event(dispatched).status == "consumed"
    assert ledger.usage(now=TEST_NOW).daily_events == 1


def test_concurrent_reservations_cannot_exceed_260_daily_events(tmp_path):
    ledger = _usage_ledger(tmp_path)
    _seed_billable_events(ledger, 259, billing_date="2026-07-18")

    admitted, rejected_scopes = _race_two_reservations(ledger, now=TEST_NOW)

    assert admitted == 1
    assert rejected_scopes == ["daily"]
    assert ledger.usage(now=TEST_NOW).daily_events == 260


def test_concurrent_reservations_cannot_exceed_8000_monthly_events(tmp_path):
    ledger = _usage_ledger(
        tmp_path,
        daily_budget=8_001,
        monthly_budget=8_000,
        soft_monthly_ceiling=8_000,
    )
    _seed_billable_events(ledger, 7_999, billing_date="2026-07-18")

    admitted, rejected_scopes = _race_two_reservations(ledger, now=TEST_NOW)

    assert admitted == 1
    assert rejected_scopes == ["monthly"]
    assert ledger.usage(now=TEST_NOW).monthly_events == 8_000


def test_concurrent_reservations_stop_at_7800_soft_monthly_ceiling(tmp_path):
    ledger = _usage_ledger(
        tmp_path,
        daily_budget=8_001,
        monthly_budget=8_000,
        soft_monthly_ceiling=7_800,
    )
    _seed_billable_events(ledger, 7_799, billing_date="2026-07-18")

    admitted, rejected_scopes = _race_two_reservations(ledger, now=TEST_NOW)

    assert admitted == 1
    assert rejected_scopes == ["soft monthly"]
    assert ledger.usage(now=TEST_NOW).monthly_events == 7_800


def test_usage_billing_day_uses_pacific_civil_time(tmp_path):
    ledger = _usage_ledger(
        tmp_path,
        daily_budget=1,
        monthly_budget=2,
        soft_monthly_ceiling=2,
    )
    before_midnight = datetime(2026, 7, 18, 6, 59, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 7, 18, 7, 0, tzinfo=timezone.utc)

    ledger.reserve_event(
        city_slug="bos",
        station_id="KBOS",
        endpoint="current",
        page_number=0,
        now=before_midnight,
    )
    ledger.reserve_event(
        city_slug="bos",
        station_id="KBOS",
        endpoint="current",
        page_number=0,
        now=after_midnight,
    )

    assert ledger.usage(now=before_midnight).daily_events == 1
    assert ledger.usage(now=after_midnight).daily_events == 1
    assert ledger.usage(now=after_midnight).monthly_events == 2


def test_usage_ledger_rejects_request_urls_and_never_persists_keys(tmp_path):
    ledger = _usage_ledger(tmp_path)
    sentinel_key = "top-secret-google-key"

    with pytest.raises(
        ValueError,
        match="endpoint must be a supported Google Weather endpoint",
    ):
        ledger.reserve_event(
            city_slug="nyc",
            station_id="KNYC",
            endpoint=f"https://weather.googleapis.com/hourly?key={sentinel_key}",
            page_number=1,
            now=TEST_NOW,
        )

    with sqlite3.connect(ledger.db_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(google_weather_usage_events)"
            )
        }
    assert "url" not in columns
    assert "api_key" not in columns
    assert "page_token" not in columns
    assert "response_body" not in columns
    assert sentinel_key.encode() not in ledger.db_path.read_bytes()


def test_dispatch_transition_can_only_be_owned_once(tmp_path):
    from google_weather_store import GoogleUsageLifecycleError

    ledger = _usage_ledger(tmp_path)
    event = ledger.reserve_event(
        city_slug="sfo",
        station_id="KSFO",
        endpoint="hourly",
        page_number=1,
        now=TEST_NOW,
    )

    ledger.mark_dispatched(event, now=TEST_NOW + timedelta(seconds=1))

    with pytest.raises(
        GoogleUsageLifecycleError,
        match="Google Weather event is not dispatchable",
    ):
        ledger.mark_dispatched(event, now=TEST_NOW + timedelta(seconds=2))


def test_concurrent_dispatch_transition_has_exactly_one_owner(tmp_path):
    from google_weather_store import GoogleUsageLifecycleError

    ledger = _usage_ledger(tmp_path)
    event = ledger.reserve_event(
        city_slug="sfo",
        station_id="KSFO",
        endpoint="hourly",
        page_number=1,
        now=TEST_NOW,
    )
    start = threading.Barrier(2)

    def claim_dispatch(_index: int) -> str:
        start.wait()
        try:
            ledger.mark_dispatched(event, now=TEST_NOW + timedelta(seconds=1))
            return "owner"
        except GoogleUsageLifecycleError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim_dispatch, range(2)))

    assert sorted(outcomes) == ["owner", "rejected"]


@pytest.mark.parametrize("existing_state", ["success", "error", "cancelled"])
def test_terminal_or_cancelled_event_cannot_reclaim_dispatch(
    tmp_path, existing_state
):
    from google_weather_store import GoogleUsageLifecycleError

    ledger = _usage_ledger(tmp_path)
    event = ledger.reserve_event(
        city_slug="sfo",
        station_id="KSFO",
        endpoint="hourly",
        page_number=1,
        now=TEST_NOW,
    )
    if existing_state == "cancelled":
        ledger.cancel_before_dispatch(event, now=TEST_NOW + timedelta(seconds=1))
    else:
        ledger.mark_dispatched(event, now=TEST_NOW + timedelta(seconds=1))
        ledger.complete_event(
            event,
            success=existing_state == "success",
            response_status_class=2,
            error_kind=None if existing_state == "success" else "parse",
            now=TEST_NOW + timedelta(seconds=2),
        )

    with pytest.raises(
        GoogleUsageLifecycleError,
        match="Google Weather event is not dispatchable",
    ):
        ledger.mark_dispatched(event, now=TEST_NOW + timedelta(seconds=3))


def test_unknown_event_cannot_claim_dispatch(tmp_path):
    from google_weather_store import GoogleUsageEvent

    ledger = _usage_ledger(tmp_path)
    unknown = GoogleUsageEvent(
        reservation_id="123e4567e89b42d3a456426614174000",
        endpoint="hourly",
        page_number=1,
    )

    with pytest.raises(KeyError, match="unknown Google Weather usage event"):
        ledger.mark_dispatched(unknown, now=TEST_NOW)


def test_every_configured_city_station_pair_is_accepted(tmp_path):
    from cities import CITIES

    ledger = _usage_ledger(tmp_path)

    for page_number, city in enumerate(CITIES):
        event = ledger.reserve_event(
            city_slug=city.slug,
            station_id=city.nws_station_id,
            endpoint="daily",
            page_number=page_number,
            now=TEST_NOW,
        )
        assert ledger.event(event).status == "reserved"


@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        ("city_slug", {"city_slug": "AIzaSySecretMaterial123456789"}),
        ("station_id", {"station_id": "AIzaSySecretMaterial123456789"}),
        ("endpoint", {"endpoint": "AIzaSySecretMaterial123456789"}),
        (
            "reservation_id",
            {"reservation_id": "AIzaSySecretMaterial123456789"},
        ),
    ],
)
def test_reservation_identity_fields_reject_key_shaped_values_without_echoing(
    tmp_path, field, overrides
):
    ledger = _usage_ledger(tmp_path)
    sentinel = "AIzaSySecretMaterial123456789"
    values = {
        "city_slug": "sfo",
        "station_id": "KSFO",
        "endpoint": "hourly",
        "page_number": 1,
        "now": TEST_NOW,
        **overrides,
    }

    with pytest.raises(ValueError) as rejected:
        ledger.reserve_event(**values)

    assert sentinel not in str(rejected.value)
    assert sentinel.encode() not in ledger.db_path.read_bytes()
    assert field in {"city_slug", "station_id", "endpoint", "reservation_id"}


def test_city_and_station_must_be_the_same_canonical_pair(tmp_path):
    ledger = _usage_ledger(tmp_path)

    with pytest.raises(ValueError, match="city and station must match"):
        ledger.reserve_event(
            city_slug="sfo",
            station_id="KMIA",
            endpoint="daily",
            page_number=0,
            now=TEST_NOW,
        )
    assert b"KMIA" not in ledger.db_path.read_bytes()


def test_error_kind_is_closed_and_cannot_persist_key_material(tmp_path):
    ledger = _usage_ledger(tmp_path)
    sentinel = "AIzaSySecretMaterial123456789"
    event = ledger.reserve_event(
        city_slug="sfo",
        station_id="KSFO",
        endpoint="current",
        page_number=0,
        now=TEST_NOW,
    )
    ledger.mark_dispatched(event, now=TEST_NOW + timedelta(seconds=1))

    with pytest.raises(ValueError) as rejected:
        ledger.complete_event(
            event,
            success=False,
            error_kind=sentinel,
            now=TEST_NOW + timedelta(seconds=2),
        )

    assert sentinel not in str(rejected.value)
    assert sentinel.encode() not in ledger.db_path.read_bytes()
    assert ledger.event(event).status == "consumed"


def test_exact_reservation_retry_is_idempotent_before_budget_check(tmp_path):
    ledger = _usage_ledger(
        tmp_path,
        daily_budget=1,
        monthly_budget=1,
        soft_monthly_ceiling=1,
    )
    reservation_id = "123e4567e89b42d3a456426614174000"
    first = ledger.reserve_event(
        city_slug="sfo",
        station_id="KSFO",
        endpoint="daily",
        page_number=0,
        reservation_id=reservation_id,
        now=TEST_NOW,
    )

    retried = ledger.reserve_event(
        city_slug="sfo",
        station_id="KSFO",
        endpoint="daily",
        page_number=0,
        reservation_id=reservation_id,
        now=TEST_NOW + timedelta(seconds=1),
    )

    assert retried == first
    assert ledger.usage(now=TEST_NOW).daily_events == 1


def test_reservation_retry_with_conflicting_metadata_fails_deterministically(tmp_path):
    from google_weather_store import GoogleUsageLifecycleError

    ledger = _usage_ledger(
        tmp_path,
        daily_budget=1,
        monthly_budget=1,
        soft_monthly_ceiling=1,
    )
    reservation_id = "123e4567e89b42d3a456426614174000"
    ledger.reserve_event(
        city_slug="sfo",
        station_id="KSFO",
        endpoint="daily",
        page_number=0,
        reservation_id=reservation_id,
        now=TEST_NOW,
    )

    with pytest.raises(
        GoogleUsageLifecycleError,
        match="reservation metadata conflicts",
    ):
        ledger.reserve_event(
            city_slug="mia",
            station_id="KMIA",
            endpoint="daily",
            page_number=0,
            reservation_id=reservation_id,
            now=TEST_NOW,
        )


def test_usage_daily_and_monthly_counts_share_one_database_snapshot(tmp_path):
    ledger = _usage_ledger(tmp_path)
    original_count = ledger._count
    calls = 0

    def insert_between_legacy_reads(connection, column, value):
        nonlocal calls
        result = original_count(connection, column, value)
        calls += 1
        if calls == 1:
            other = _usage_ledger(tmp_path)
            other.reserve_event(
                city_slug="sfo",
                station_id="KSFO",
                endpoint="current",
                page_number=0,
                now=TEST_NOW,
            )
        return result

    ledger._count = insert_between_legacy_reads

    counts = ledger.usage(now=TEST_NOW)

    assert counts.daily_events == counts.monthly_events
