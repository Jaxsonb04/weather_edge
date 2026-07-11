from __future__ import annotations

from urllib.error import HTTPError
from urllib.parse import urlencode

import sfo_kalshi_quant.cli as cli_module
from sfo_kalshi_quant.kalshi import KalshiPublicClient
from sfo_kalshi_quant.models import MarketBin


def _payload(ticker: str) -> dict[str, object]:
    return {
        "ticker": ticker,
        "event_ticker": "KXHIGHTSFO-TEST",
        "title": "Highest temperature in San Francisco?",
        "yes_sub_title": "68 to 69",
        "strike_type": "between",
        "floor_strike": 68,
        "cap_strike": 69,
        "yes_bid_dollars": "0.40",
        "yes_ask_dollars": "0.42",
        "no_bid_dollars": "0.58",
        "no_ask_dollars": "0.60",
        "yes_bid_size_fp": "10.00",
        "yes_ask_size_fp": "11.00",
        "status": "active",
    }


def test_get_markets_uses_documented_comma_separated_tickers_filter(monkeypatch):
    client = KalshiPublicClient()
    requested = {}

    def fake_get_json(path, params=None):
        requested.update({"path": path, "params": params})
        return {"markets": [_payload("TICKER-A"), _payload("TICKER-B")], "cursor": ""}

    monkeypatch.setattr(client, "get_json", fake_get_json)

    markets = client.get_markets(["TICKER-A", "TICKER-B", "TICKER-A"])

    assert requested == {
        "path": "markets",
        "params": {"tickers": "TICKER-A,TICKER-B", "limit": 2},
    }
    assert [market.ticker for market in markets] == ["TICKER-A", "TICKER-B"]


def test_get_markets_chunks_at_conservative_ticker_count(monkeypatch):
    client = KalshiPublicClient()
    calls = []
    tickers = [f"TICKER-{index:03d}" for index in range(121)]

    def fake_get_json(path, params=None):
        calls.append((path, params))
        return {"markets": [], "cursor": ""}

    monkeypatch.setattr(client, "get_json", fake_get_json)

    assert client.get_markets(tickers) == []

    assert [len(params["tickers"].split(",")) for _, params in calls] == [50, 50, 21]
    assert all(path == "markets" for path, _ in calls)


def test_get_markets_chunks_before_encoded_query_exceeds_safe_length(monkeypatch):
    client = KalshiPublicClient()
    params_seen = []
    tickers = [f"TICKER-{index}-" + "X" * 400 for index in range(12)]

    def fake_get_json(_path, params=None):
        params_seen.append(params)
        return {"markets": [], "cursor": ""}

    monkeypatch.setattr(client, "get_json", fake_get_json)

    client.get_markets(tickers)

    assert len(params_seen) > 1
    assert all(len(urlencode(params)) <= 1800 for params in params_seen)
    assert [ticker for params in params_seen for ticker in params["tickers"].split(",")] == tickers


def test_monitor_batch_failure_falls_back_once_per_unique_ticker():
    class Client:
        def __init__(self):
            self.batch_calls = 0
            self.single_calls = []

        def get_markets(self, tickers):
            self.batch_calls += 1
            raise ValueError("unsupported batch response schema")

        def get_market(self, ticker):
            self.single_calls.append(ticker)
            return MarketBin.from_kalshi(_payload(ticker))

    client = Client()

    results = cli_module._monitor_market_lookup(
        client, ["TICKER-A", "TICKER-A", "TICKER-B"]
    )

    assert client.batch_calls == 1
    assert client.single_calls == ["TICKER-A", "TICKER-B"]
    assert set(results) == {"TICKER-A", "TICKER-B"}
    assert all(isinstance(value, MarketBin) for value in results.values())


def test_monitor_global_batch_failure_does_not_fan_out_to_single_requests():
    class Client:
        def __init__(self):
            self.batch_calls = 0
            self.single_calls = []

        def get_markets(self, tickers):
            self.batch_calls += 1
            raise OSError("global transport outage")

        def get_market(self, ticker):
            self.single_calls.append(ticker)
            raise AssertionError("global batch outage must not create an N-request storm")

    client = Client()

    results = cli_module._monitor_market_lookup(
        client, ["TICKER-A", "TICKER-A", "TICKER-B"]
    )

    assert client.batch_calls == 1
    assert client.single_calls == []
    assert set(results) == {"TICKER-A", "TICKER-B"}
    assert all(isinstance(value, OSError) for value in results.values())


def test_monitor_rate_limit_batch_failure_does_not_fan_out():
    class Client:
        single_calls = []

        def get_markets(self, _tickers):
            raise HTTPError("https://example.test", 429, "rate limited", {}, None)

        def get_market(self, ticker):
            self.single_calls.append(ticker)
            raise AssertionError("rate limit must not fan out")

    client = Client()

    results = cli_module._monitor_market_lookup(client, ["TICKER-A", "TICKER-B"])

    assert client.single_calls == []
    assert all(isinstance(value, HTTPError) and value.code == 429 for value in results.values())


def test_monitor_falls_back_only_for_market_missing_from_batch_response():
    class Client:
        single_calls = []

        def get_markets(self, _tickers):
            return [MarketBin.from_kalshi(_payload("TICKER-A"))]

        def get_market(self, ticker):
            self.single_calls.append(ticker)
            return MarketBin.from_kalshi(_payload(ticker))

    client = Client()

    results = cli_module._monitor_market_lookup(client, ["TICKER-A", "TICKER-B"])

    assert client.single_calls == ["TICKER-B"]
    assert set(results) == {"TICKER-A", "TICKER-B"}


def test_iter_trades_follows_cursors_and_deduplicates_trade_ids(monkeypatch):
    client = KalshiPublicClient()
    calls = []
    trade_a = {"trade_id": "A"}
    trade_b = {"trade_id": "B"}
    trade_c = {"trade_id": "C"}

    def fake_get_trades(**kwargs):
        calls.append(kwargs.get("cursor"))
        if kwargs.get("cursor") is None:
            return {"trades": [trade_a, trade_b], "cursor": "next-page"}
        return {"trades": [trade_b, trade_c], "cursor": ""}

    monkeypatch.setattr(client, "get_trades", fake_get_trades)

    trades = list(
        client.iter_trades(ticker="TICKER-A", min_ts=1, max_ts=2, limit=1000)
    )

    assert calls == [None, "next-page"]
    assert [trade["trade_id"] for trade in trades] == ["A", "B", "C"]
