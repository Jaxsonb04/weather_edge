from __future__ import annotations

import pytest

from sfo_kalshi_quant.kalshi import KalshiPublicClient
from sfo_kalshi_quant.models import EventSnapshot, MarketBin


def _market(ticker: str, expiration_value: object, *, result: str = "no") -> dict[str, object]:
    return {
        "ticker": ticker,
        "event_ticker": "KXHIGHLAX-26OCT22",
        "title": "Will the maximum temperature be 70-71° on Oct 22, 2026?",
        "yes_sub_title": "70° to 71°",
        "strike_type": "between",
        "floor_strike": 70,
        "cap_strike": 71,
        "yes_bid_dollars": "0.0000",
        "yes_ask_dollars": "1.0000",
        "no_bid_dollars": "0.0000",
        "no_ask_dollars": "1.0000",
        "yes_bid_size_fp": "0.00",
        "yes_ask_size_fp": "0.00",
        "status": "finalized",
        "result": result,
        "expiration_value": expiration_value,
    }


@pytest.mark.parametrize("label", ["70° to 71°", "56° or below", "79° or above", "nan", "inf"])
def test_bracket_label_settlement_value_is_unknown_not_a_crash(label):
    market = MarketBin.from_kalshi(_market("KXHIGHLAX-26OCT22-B70.5", label, result="yes"))

    assert market.expiration_value is None
    assert market.result == "yes"
    assert market.floor_strike == 70.0


@pytest.mark.parametrize(("raw", "expected"), [("75.00", 75.0), (58, 58.0), ("", None), (None, None)])
def test_numeric_settlement_value_is_unchanged(raw, expected):
    assert MarketBin.from_kalshi(_market("KXHIGHLAX-26OCT27-B70.5", raw)).expiration_value == expected


def test_rolling_event_listing_survives_a_label_settled_event(monkeypatch):
    # Kalshi finalized LA Oct 22 with "70° to 71°"; every paper scan crashed
    # from Oct 27 because that event stays in the 20-event rolling window.
    client = KalshiPublicClient()
    labelled = {
        "event_ticker": "KXHIGHLAX-26OCT22",
        "series_ticker": "KXHIGHLAX",
        "title": "Highest temperature in Los Angeles on Oct 22, 2026?",
        "markets": [_market("KXHIGHLAX-26OCT22-B70.5", "70° to 71°", result="yes")],
    }
    numeric = {
        "event_ticker": "KXHIGHLAX-26OCT27",
        "series_ticker": "KXHIGHLAX",
        "title": "Highest temperature in Los Angeles on Oct 27, 2026?",
        "markets": [_market("KXHIGHLAX-26OCT27-B70.5", "75.00")],
    }
    monkeypatch.setattr(client, "get_json", lambda path, params=None: {"events": [labelled, numeric]})

    events = client.list_event_snapshots(series_ticker="KXHIGHLAX", limit=20)

    assert [event.event_ticker for event in events] == ["KXHIGHLAX-26OCT22", "KXHIGHLAX-26OCT27"]
    assert all(isinstance(event, EventSnapshot) for event in events)
    assert events[0].markets[0].expiration_value is None
    assert events[1].markets[0].expiration_value == 75.0
