from __future__ import annotations

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


def test_monitor_batch_failure_falls_back_once_per_unique_ticker():
    class Client:
        def __init__(self):
            self.batch_calls = 0
            self.single_calls = []

        def get_markets(self, tickers):
            self.batch_calls += 1
            raise OSError("batch unavailable")

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
