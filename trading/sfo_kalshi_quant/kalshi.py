from __future__ import annotations

import json
import time
from datetime import date
from http.client import IncompleteRead
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import SERIES_TICKER
from .models import EventSnapshot, MarketBin, format_event_date_token, target_date_from_event_ticker


PROD_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"


class KalshiUnavailable(OSError):
    """Kalshi could not be reached after retries.

    Subclasses OSError so existing ``except (URLError, OSError)`` handlers in
    the scanner treat an exhausted retry like any other transient lookup
    failure and fall back to the probability-only ladder.
    """


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(seconds, 30.0))


class KalshiPublicClient:
    """Tiny public Kalshi API client.

    The live-order API is intentionally absent from this class. Paper trading
    should remain unable to place real orders by construction.
    """

    def __init__(
        self,
        base_url: str = PROD_BASE_URL,
        timeout: int = 20,
        *,
        retries: int = 3,
        backoff: float = 0.5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(1, retries)
        self.backoff = max(0.0, backoff)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = f"?{urlencode(params)}" if params else ""
        url = f"{self.base_url}/{path.lstrip('/')}{query}"
        request = Request(url, headers={"accept": "application/json"})
        backoff = self.backoff
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                # 429/5xx are transient; honor Retry-After when present. Other
                # 4xx (e.g. 404 no such event) are permanent, so re-raise now.
                if exc.code == 429 or 500 <= exc.code < 600:
                    last_exc = exc
                    if attempt < self.retries - 1:
                        wait = _parse_retry_after(exc.headers.get("Retry-After"))
                        time.sleep(wait if wait is not None else backoff)
                        backoff *= 2
                        continue
                raise
            except (URLError, OSError, IncompleteRead, json.JSONDecodeError) as exc:
                # Read-phase timeouts/resets are OSError, NOT URLError, so they
                # must be caught here or they abort the whole multi-target scan.
                last_exc = exc
                if attempt < self.retries - 1:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise KalshiUnavailable(f"Kalshi request failed after {self.retries} attempts: {exc}") from exc
        raise KalshiUnavailable(  # pragma: no cover - loop always returns or raises
            f"Kalshi request failed after {self.retries} attempts: {last_exc}"
        )

    def get_series(self, series_ticker: str = SERIES_TICKER) -> dict[str, Any]:
        return self.get_json(f"series/{series_ticker}")

    def get_event(self, event_ticker: str, *, with_nested_markets: bool = True) -> EventSnapshot:
        payload = self.get_json(
            f"events/{event_ticker}",
            {"with_nested_markets": str(with_nested_markets).lower()},
        )
        return EventSnapshot.from_kalshi(payload["event"])

    def get_market(self, market_ticker: str) -> MarketBin:
        payload = self.get_json(f"markets/{market_ticker}")
        return MarketBin.from_kalshi(payload["market"])

    def get_markets(self, market_tickers: list[str]) -> list[MarketBin]:
        """Fetch public market metadata via the documented ``tickers`` filter.

        The endpoint documents a maximum page size of 1,000 but no separate
        ticker-filter cap, so requests are kept within that published page
        bound. Callers retain per-ticker fallback for transport/API failures.
        """

        unique = list(dict.fromkeys(str(ticker) for ticker in market_tickers if ticker))
        markets: list[MarketBin] = []
        for start in range(0, len(unique), 1000):
            chunk = unique[start : start + 1000]
            payload = self.get_json(
                "markets",
                {"tickers": ",".join(chunk), "limit": len(chunk)},
            )
            markets.extend(
                MarketBin.from_kalshi(row) for row in payload.get("markets", [])
            )
        return markets

    def list_events(
        self,
        *,
        series_ticker: str = SERIES_TICKER,
        limit: int = 50,
        cursor: str | None = None,
        with_nested_markets: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "series_ticker": series_ticker,
            "limit": limit,
            "with_nested_markets": str(with_nested_markets).lower(),
        }
        if cursor:
            params["cursor"] = cursor
        return self.get_json("events", params)

    def list_event_snapshots(
        self,
        *,
        series_ticker: str = SERIES_TICKER,
        limit: int = 50,
        cursor: str | None = None,
        with_nested_markets: bool = True,
    ) -> list[EventSnapshot]:
        payload = self.list_events(
            series_ticker=series_ticker,
            limit=limit,
            cursor=cursor,
            with_nested_markets=with_nested_markets,
        )
        return [EventSnapshot.from_kalshi(row) for row in payload.get("events", [])]

    def find_event_by_date(
        self,
        target_date: date,
        *,
        series_ticker: str = SERIES_TICKER,
        max_pages: int = 4,
    ) -> EventSnapshot | None:
        expected = f"{series_ticker}-{format_event_date_token(target_date)}"
        cursor = None
        for _ in range(max_pages):
            payload = self.list_events(series_ticker=series_ticker, cursor=cursor)
            for event_payload in payload.get("events", []):
                if event_payload.get("event_ticker") == expected:
                    return EventSnapshot.from_kalshi(event_payload)
            cursor = payload.get("cursor")
            if not cursor:
                break
        return None

    def get_orderbook(self, market_ticker: str, depth: int = 10) -> dict[str, Any]:
        return self.get_json(f"markets/{market_ticker}/orderbook", {"depth": depth})

    def get_trades(
        self,
        *,
        ticker: str,
        min_ts: int,
        max_ts: int,
        limit: int = 1000,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "ticker": ticker,
            "min_ts": min_ts,
            "max_ts": max_ts,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        return self.get_json("markets/trades", params)

    def list_historical_markets(
        self,
        *,
        series_ticker: str = SERIES_TICKER,
        limit: int = 1000,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "series_ticker": series_ticker,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        return self.get_json("historical/markets", params)

    def get_historical_market_candlesticks(
        self,
        market_ticker: str,
        *,
        start_ts: int,
        end_ts: int,
        period_interval: int = 60,
    ) -> dict[str, Any]:
        return self.get_json(
            f"historical/markets/{market_ticker}/candlesticks",
            {
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": period_interval,
            },
        )

    def get_historical_trades(
        self,
        *,
        ticker: str,
        min_ts: int,
        max_ts: int,
        limit: int = 1000,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "ticker": ticker,
            "min_ts": min_ts,
            "max_ts": max_ts,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        return self.get_json("historical/trades", params)


def load_event_snapshots(path: Path, target_date: date | None = None) -> list[EventSnapshot]:
    """Load Kalshi event JSON saved from either event or events endpoints."""

    payload = json.loads(path.read_text())
    if "event" in payload:
        events = [EventSnapshot.from_kalshi(payload["event"])]
    elif "events" in payload:
        events = [EventSnapshot.from_kalshi(row) for row in payload.get("events", [])]
    elif "markets" in payload:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for market in payload.get("markets", []):
            grouped.setdefault(market["event_ticker"], []).append(market)
        events = [
            EventSnapshot.from_kalshi(
                {
                    "event_ticker": event_ticker,
                    "title": f"Kalshi market snapshot {event_ticker}",
                    "markets": markets,
                }
            )
            for event_ticker, markets in grouped.items()
        ]
    elif "event_ticker" in payload:
        events = [EventSnapshot.from_kalshi(payload)]
    else:
        raise ValueError(f"Unrecognized Kalshi event payload: {path}")
    if target_date is not None:
        events = [event for event in events if event.target_date == target_date]
    return events
