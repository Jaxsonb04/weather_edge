"""Fetch the exchange's own settlement for booked paper lots and reconcile.

The verdicts, SQL and rationale live in
:mod:`sfo_kalshi_quant.store.exchange_settlement_checks`; this module is the
network half.  It is built so nothing it does can decide whether settlement
happens: it runs after the journal is written, holds no database lock while it
talks to the network, and turns every fetch failure into an ``UNCHECKED`` row.

Endpoints
    ``GET /markets/{ticker}`` serves recent markets only.  Once the exchange
    moves a market to its historical partition that path returns 404 and the
    same record is served by ``GET /historical/markets/{ticker}``.  Measured
    2026-09-13: ``KXHIGHMIA-26AUG29-T88`` is live-only (historical 404) and
    ``KXHIGHTSFO-26JUN12-T81`` is historical-only (live 404).  Settled history
    in this book starts 2026-06-10, so a live-only check would leave most of it
    permanently unchecked; the historical path is asked only after a live 404.

Rate
    Requests are spaced at least :data:`EXCHANGE_CHECK_MIN_INTERVAL_SECONDS`
    apart, under the public API's ~3 requests/second.  Each run fetches at most
    ``max_fetches`` markets (a historical fallback is a second request for the
    same market), and the first transport failure stops fetching for the rest of
    the run, so an exchange outage costs one timeout rather than one per lot.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.error import HTTPError

from ._util import _optional_float
from .kalshi import KalshiPublicClient
from .settlement_truth import integer_settlement_high_f
from .store.exchange_settlement_checks import (
    EXCHANGE_CHECK_MATCH,
    EXCHANGE_CHECK_MISMATCH,
    EXCHANGE_CHECK_PENDING,
    EXCHANGE_CHECK_UNCHECKED,
    booked_winner_for_lot,
    cached_exchange_resolutions,
    classify_exchange_settlement,
    record_exchange_resolution,
    record_exchange_settlement_checks,
    settled_lots_for_exchange_check,
    standing_exchange_mismatches,
)

EXCHANGE_CHECK_TIMEOUT_SECONDS = 10
EXCHANGE_CHECK_RETRIES = 2
EXCHANGE_CHECK_MIN_INTERVAL_SECONDS = 0.4
# The settle timer fires every thirty minutes; forty spaced markets is ~16 s of
# polite fetching in the worst case, and a normal tick fetches only the handful
# of recently settled markets that have not finalized yet.
AUTO_SETTLE_EXCHANGE_CHECK_MAX_FETCHES = 40
AUTO_SETTLE_EXCHANGE_CHECK_LOOKBACK_DAYS = 7
# The operator backfill walks a whole --days window once; finalized results are
# cached, so a second run over the same window fetches nothing.
RESETTLE_EXCHANGE_CHECK_MAX_FETCHES = 400

LIVE_MARKET_ENDPOINT = "markets"
HISTORICAL_MARKET_ENDPOINT = "historical/markets"
_TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]*$")


class ExchangeCheckUnavailable(Exception):
    """The exchange cannot be asked right now; stop fetching for this run."""


class ExchangeMarketUnreadable(Exception):
    """One market cannot be read; other markets can still be fetched."""


class ExchangeJsonClient(Protocol):
    def get_json(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


def default_exchange_client() -> ExchangeJsonClient:
    """The production public client.  ``tests/conftest.py`` replaces this."""

    return KalshiPublicClient(
        timeout=EXCHANGE_CHECK_TIMEOUT_SECONDS, retries=EXCHANGE_CHECK_RETRIES
    )


class _RequestSpacer:
    """Keep consecutive requests at least ``min_interval`` seconds apart."""

    def __init__(
        self,
        min_interval: float,
        *,
        sleep: Callable[[float], object],
        monotonic: Callable[[], float],
    ) -> None:
        self._min_interval = max(0.0, float(min_interval))
        self._sleep = sleep
        self._monotonic = monotonic
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None and self._min_interval > 0:
            remaining = self._min_interval - (self._monotonic() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._monotonic()


def parse_market_resolution(
    payload: object, ticker: str, endpoint: str
) -> dict[str, Any]:
    """The fields the reconciliation needs, or ``ExchangeMarketUnreadable``."""

    market = payload.get("market") if isinstance(payload, Mapping) else None
    if not isinstance(market, Mapping):
        raise ExchangeMarketUnreadable(
            f"{endpoint}/{ticker} returned no market object"
        )
    returned = str(market.get("ticker") or "")
    if returned != ticker:
        raise ExchangeMarketUnreadable(
            f"{endpoint}/{ticker} returned market {returned!r}"
        )
    status = str(market.get("status") or "").strip().lower()
    if not status:
        raise ExchangeMarketUnreadable(f"{endpoint}/{ticker} returned no status")
    raw_value = market.get("expiration_value")
    value = None
    if raw_value not in (None, ""):
        value = _optional_float(raw_value)
        if value is None:
            raise ExchangeMarketUnreadable(
                f"{endpoint}/{ticker} returned unparseable expiration_value "
                f"{raw_value!r}"
            )
    return {
        "market_ticker": ticker,
        "market_status": status,
        "result": str(market.get("result") or "").strip().lower(),
        "expiration_value": value,
        "source_endpoint": endpoint,
    }


def fetch_market_resolution(
    client: ExchangeJsonClient, ticker: str, *, spacer: _RequestSpacer
) -> dict[str, Any]:
    """Live endpoint first; the historical partition only after a live 404."""

    if not _TICKER_PATTERN.match(ticker):
        raise ExchangeMarketUnreadable(f"refusing to request malformed ticker {ticker!r}")
    endpoint = LIVE_MARKET_ENDPOINT
    try:
        spacer.wait()
        payload = client.get_json(f"{LIVE_MARKET_ENDPOINT}/{ticker}")
    except HTTPError as exc:
        if exc.code != 404:
            raise ExchangeCheckUnavailable(
                f"exchange returned HTTP {exc.code} for {LIVE_MARKET_ENDPOINT}/{ticker}"
            ) from exc
        endpoint = HISTORICAL_MARKET_ENDPOINT
        try:
            spacer.wait()
            payload = client.get_json(f"{HISTORICAL_MARKET_ENDPOINT}/{ticker}")
        except HTTPError as historical_exc:
            if historical_exc.code == 404:
                raise ExchangeMarketUnreadable(
                    f"{ticker} not found on the live or historical market endpoint"
                ) from historical_exc
            raise ExchangeCheckUnavailable(
                f"exchange returned HTTP {historical_exc.code} for "
                f"{HISTORICAL_MARKET_ENDPOINT}/{ticker}"
            ) from historical_exc
    return parse_market_resolution(payload, ticker, endpoint)


def _fetch_resolutions(
    tickers: list[str],
    *,
    max_fetches: int,
    client_factory: Callable[[], ExchangeJsonClient],
    spacer: _RequestSpacer,
) -> tuple[dict[str, dict[str, Any]], dict[str, str], int, str | None]:
    resolutions: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    attempted = 0
    stopped: str | None = None
    client: ExchangeJsonClient | None = None
    for ticker in tickers:
        if stopped is not None:
            errors[ticker] = f"not fetched this run: {stopped}"
            continue
        if attempted >= max_fetches:
            errors[ticker] = (
                f"not fetched this run: per-run budget of {max_fetches} market "
                "fetches spent"
            )
            continue
        if client is None:
            try:
                client = client_factory()
            except Exception as exc:  # a broken client must not block settlement
                stopped = f"exchange client unavailable ({type(exc).__name__}: {exc})"
                errors[ticker] = stopped
                continue
        attempted += 1
        try:
            resolutions[ticker] = fetch_market_resolution(client, ticker, spacer=spacer)
        except ExchangeMarketUnreadable as exc:
            errors[ticker] = str(exc)
        except ExchangeCheckUnavailable as exc:
            stopped = str(exc)
            errors[ticker] = stopped
        except Exception as exc:  # transport: timeouts, resets, exhausted retries
            stopped = f"{type(exc).__name__}: {exc}"
            errors[ticker] = stopped
    return resolutions, errors, attempted, stopped


def run_exchange_settlement_checks(
    store: Any,
    *,
    max_fetches: int,
    intervals: Mapping[str, tuple[str, str]] | None = None,
    settled_since: str | None = None,
    undecided_only: bool = False,
    client_factory: Callable[[], ExchangeJsonClient] | None = None,
    min_interval_seconds: float | None = None,
    sleep: Callable[[float], object] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Reconcile settled lots with the exchange and persist one verdict each.

    Reads the lots and the cache, closes that connection, fetches what is not
    cached, then writes every verdict in one short transaction.
    """

    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        lots = settled_lots_for_exchange_check(
            conn,
            intervals=intervals,
            settled_since=settled_since,
            undecided_only=undecided_only,
        )
        tickers = list(dict.fromkeys(str(lot["market_ticker"]) for lot in lots))
        cached = cached_exchange_resolutions(conn, tickers)

    spacer = _RequestSpacer(
        EXCHANGE_CHECK_MIN_INTERVAL_SECONDS
        if min_interval_seconds is None
        else min_interval_seconds,
        sleep=time.sleep if sleep is None else sleep,
        monotonic=time.monotonic if monotonic is None else monotonic,
    )
    resolutions, errors, attempted, stopped = _fetch_resolutions(
        [ticker for ticker in tickers if ticker not in cached],
        max_fetches=max_fetches,
        client_factory=default_exchange_client if client_factory is None else client_factory,
        spacer=spacer,
    )

    checked_at = datetime.now(UTC).isoformat()
    checks: list[dict[str, Any]] = []
    for lot in lots:
        ticker = str(lot["market_ticker"])
        booked_high = integer_settlement_high_f(lot["settlement_high_f"])
        booked_winner = booked_winner_for_lot(lot)
        verdict = classify_exchange_settlement(
            booked_high_f=booked_high,
            booked_winner=booked_winner,
            resolution=cached.get(ticker) or resolutions.get(ticker),
            error=errors.get(ticker),
        )
        checks.append(
            {
                "order_id": int(lot["id"]),
                "checked_at": checked_at,
                "market_ticker": ticker,
                "target_date": str(lot["target_date"]),
                "booked_high_f": booked_high,
                "booked_winner": booked_winner,
                **verdict,
            }
        )

    with store.connect() as conn:
        for resolution in resolutions.values():
            record_exchange_resolution(conn, resolution, fetched_at=checked_at)
        record_exchange_settlement_checks(conn, checks)
        standing = standing_exchange_mismatches(conn)

    def _count(verdict: str) -> int:
        return sum(1 for check in checks if check["verification_status"] == verdict)

    return {
        "checked": checks,
        "match": _count(EXCHANGE_CHECK_MATCH),
        "mismatches": _count(EXCHANGE_CHECK_MISMATCH),
        "kalshi_pending": _count(EXCHANGE_CHECK_PENDING),
        "unchecked": _count(EXCHANGE_CHECK_UNCHECKED),
        "fetched": attempted,
        "cached": len(cached),
        "standing_mismatches": standing,
        "fetch_stopped": stopped,
    }
