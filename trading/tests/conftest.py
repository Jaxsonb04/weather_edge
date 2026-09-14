"""Suite-wide guards for the trading tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _offline_exchange_settlement_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may reach the real exchange through the settlement guard.

    ``paper-auto-settle`` and ``paper-resettle --verify`` reconcile settled lots
    against the exchange's public market endpoint by default, and many tests
    drive those commands end to end.  Replacing the production client factory
    here keeps every one of them offline -- they exercise the guard's
    exchange-unreachable path, which must leave settlement untouched -- while
    the guard's own tests inject a fake client explicitly.
    """

    from sfo_kalshi_quant import exchange_settlement

    def _offline_client() -> exchange_settlement.ExchangeJsonClient:
        raise exchange_settlement.ExchangeCheckUnavailable(
            "offline test suite: the real exchange API is never called"
        )

    monkeypatch.setattr(exchange_settlement, "default_exchange_client", _offline_client)
    # An operator shell with the production off switch exported must not turn
    # the guard's own tests into skips.
    monkeypatch.delenv(exchange_settlement.EXCHANGE_CHECK_ENV_VAR, raising=False)
