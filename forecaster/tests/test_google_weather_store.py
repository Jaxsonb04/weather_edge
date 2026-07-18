"""Boundary tests for Google Weather runtime storage and usage policy."""

from __future__ import annotations

import importlib
from datetime import timedelta
from pathlib import Path

import pytest

import weather_cache_config


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
