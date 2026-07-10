"""P1: an expired (never-filled) maker quote must not consume the entry cap.

With ``max_entries_per_market_side`` = 1 on the live profile, counting
PAPER_EXPIRED rows in ``entries_for_market_side`` meant one unfilled 15-minute
resting quote permanently blocked its market-side for the whole target date --
no requote, ever. Historical evidence: expired order #109 blocked 32 later
approved snapshots, #169 blocked 19. An expired quote deployed zero capital
and holds no position, so it must be invisible to the cap, while resting,
filled, closed, and settled entries must all still count.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.paper import PaperTrader

TARGET = "2026-07-10"


def _decision(**overrides) -> TradeDecision:
    values = {
        "ticker": "KXHIGHCHI-TEST-T81",
        "label": "81° or above",
        "action": "BUY_NO",
        "approved": True,
        "probability": 0.82,
        "probability_lcb": 0.78,
        "yes_bid": 0.22,
        "yes_ask": 0.24,
        "spread": 0.03,
        "fee_per_contract": 0.02,
        "cost_per_contract": 0.77,
        "edge": 0.05,
        "edge_lcb": 0.01,
        "kelly_fraction": 0.01,
        "recommended_contracts": 8.0,
        "expected_profit": 0.4,
        "reasons": [],
        "side": "NO",
        "entry_bid": 0.73,
        "entry_ask": 0.75,
        "entry_bid_size": 10.0,
        "entry_ask_size": 10.0,
    }
    values.update(overrides)
    return TradeDecision(**values)


def _limit_trader(store: PaperStore) -> PaperTrader:
    return PaperTrader(
        store,
        StrategyConfig(limit_price_edge_lcb_buffer=0.02),
        entry_mode="limit",
    )


def _resting_decision() -> TradeDecision:
    # bid 0.73 / ask 0.75: bid+tick = 0.74 < ask -> the quote rests.
    return _decision(probability_lcb=0.81)


def _crossing_decision() -> TradeDecision:
    # bid 0.74 / ask 0.75: bid+tick crosses the ask -> instant taker fill.
    return _decision(probability_lcb=0.90, entry_bid=0.74, entry_ask=0.75)


def test_expired_quote_does_not_block_a_requote_on_the_same_market_side():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = _limit_trader(store)

        first = trader.place_approved(TARGET, [_resting_decision()])
        assert len(first) == 1
        assert store.paper_order(first[0])["status"] == "PAPER_LIMIT_RESTING"
        assert store.entries_for_market_side(TARGET, "KXHIGHCHI-TEST-T81", "NO") == 1

        expired = store.cancel_resting_limit_order(
            first[0], reason="15-minute maker TTL expired"
        )
        assert expired["status"] == "PAPER_EXPIRED"
        # The expired quote never filled: it must not count toward the cap.
        assert store.entries_for_market_side(TARGET, "KXHIGHCHI-TEST-T81", "NO") == 0

        second = trader.place_approved(TARGET, [_resting_decision()])
        assert len(second) == 1
        assert store.paper_order(second[0])["status"] == "PAPER_LIMIT_RESTING"


def test_resting_quote_still_blocks_a_duplicate_entry():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = _limit_trader(store)

        first = trader.place_approved(TARGET, [_resting_decision()])
        assert len(first) == 1
        assert store.entries_for_market_side(TARGET, "KXHIGHCHI-TEST-T81", "NO") == 1

        second = trader.place_approved(TARGET, [_resting_decision()])
        assert second == []


def test_filled_and_closed_entries_still_block_reentry():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = _limit_trader(store)

        first = trader.place_approved(TARGET, [_crossing_decision()])
        assert len(first) == 1
        assert store.paper_order(first[0])["status"] == "PAPER_FILLED"

        # Open position blocks (side-agnostic active-entry guard + entry cap).
        assert trader.place_approved(TARGET, [_crossing_decision()]) == []

        # A closed entry consumed the lifetime cap: still blocked.
        store.close_paper_order(first[0], 0.80)
        assert store.entries_for_market_side(TARGET, "KXHIGHCHI-TEST-T81", "NO") == 1
        assert trader.place_approved(TARGET, [_crossing_decision()]) == []
