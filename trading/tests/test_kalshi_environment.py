from __future__ import annotations

import pytest

from sfo_kalshi_quant.kalshi import (
    DEMO_BASE_URL,
    PROD_BASE_URL,
    KalshiPublicClient,
)


@pytest.mark.parametrize(
    ("environment", "expected"),
    (("demo", DEMO_BASE_URL), ("prod", PROD_BASE_URL), ("", PROD_BASE_URL)),
)
def test_default_base_url_follows_kalshi_environment(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    expected: str,
) -> None:
    monkeypatch.setenv("KALSHI_ENV", environment)

    assert KalshiPublicClient().base_url == expected


def test_default_base_url_stays_production_compatible_when_env_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KALSHI_ENV", raising=False)

    assert KalshiPublicClient().base_url == PROD_BASE_URL


def test_unknown_kalshi_environment_is_rejected_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KALSHI_ENV", "staging")

    with pytest.raises(ValueError, match=r"KALSHI_ENV.*demo.*prod"):
        KalshiPublicClient()


def test_explicit_base_url_wins_even_when_environment_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KALSHI_ENV", "staging")

    client = KalshiPublicClient(base_url="https://fixture.example/api/")

    assert client.base_url == "https://fixture.example/api"
