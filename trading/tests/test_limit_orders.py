import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sfo_kalshi_quant.cli import build_parser
from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.execution import buy_limit_for_decision, with_buy_limit
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.paper import PaperTrader


def _decision(**overrides) -> TradeDecision:
    values = {
        "ticker": "KXHIGHTSFO-TEST-B74.5",
        "label": "74° to 75°",
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
        "recommended_contracts": 2.0,
        "expected_profit": 0.1,
        "reasons": [],
        "side": "NO",
        "entry_bid": 0.73,
        "entry_ask": 0.75,
        "entry_bid_size": 10.0,
        "entry_ask_size": 10.0,
    }
    values.update(overrides)
    return TradeDecision(**values)


def test_buy_limit_uses_probability_lcb_reservation_price_before_ask():
    decision = _decision(probability_lcb=0.81, entry_bid=0.73, entry_ask=0.75)
    quote = buy_limit_for_decision(
        decision,
        StrategyConfig(limit_price_edge_lcb_buffer=0.02),
    )

    assert quote is not None
    assert quote.price == 0.74
    assert quote.would_cross is False
    assert quote.edge_lcb >= 0.02


def test_buy_limit_rejects_trade_when_no_price_preserves_lcb_edge():
    decision = _decision(probability_lcb=0.70, entry_bid=0.68, entry_ask=0.69)

    assert buy_limit_for_decision(
        decision,
        StrategyConfig(limit_price_edge_lcb_buffer=0.02),
    ) is None


def test_paper_limit_mode_records_resting_order_but_not_open_position():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(
            store,
            StrategyConfig(limit_price_edge_lcb_buffer=0.02),
            entry_mode="limit",
        )
        decision = _decision(probability_lcb=0.81, entry_bid=0.73, entry_ask=0.75)

        order_ids = trader.place_approved("2026-06-15", [decision])

        assert len(order_ids) == 1
        row = store.paper_order(order_ids[0])
        assert row is not None
        assert row["status"] == "PAPER_LIMIT_RESTING"
        assert row["limit_price"] == 0.74
        assert row["entry_price"] == 0.74
        assert row["edge_lcb"] == row["limit_edge_lcb"]
        assert row["expected_profit"] == row["limit_edge"] * row["contracts"]
        assert store.open_paper_orders(10) == []
        assert store.settle_paper_orders("2026-06-15", 73.0) == 0


def test_limit_mode_paper_stake_sizes_from_eventual_maker_quote():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        config = StrategyConfig(
            limit_price_edge_lcb_buffer=0.02,
            max_contracts_per_market=100.0,
        )
        trader = PaperTrader(store, config, entry_mode="limit")
        decision = _decision(
            ticker="KXCPI-TEST-B74.5",
            probability_lcb=0.81,
            entry_bid=0.29,
            entry_ask=0.40,
            entry_ask_size=100.0,
        )

        order_ids = trader.place_approved("2026-06-15", [decision], stake_dollars=10.0)

        assert len(order_ids) == 1
        row = store.paper_order(order_ids[0])
        assert row is not None
        assert row["status"] == "PAPER_LIMIT_RESTING"
        assert row["limit_price"] == 0.30
        assert row["contracts"] == 32.0
        assert row["contracts"] * row["limit_cost_per_contract"] <= 10.0 + 1e-9


def test_paper_limit_mode_fills_when_limit_crosses_visible_ask():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(
            store,
            StrategyConfig(limit_price_edge_lcb_buffer=0.02),
            entry_mode="limit",
        )
        decision = _decision(probability_lcb=0.90, entry_bid=0.74, entry_ask=0.75)

        order_ids = trader.place_approved("2026-06-15", [decision])

        assert len(order_ids) == 1
        row = store.paper_order(order_ids[0])
        assert row is not None
        assert row["status"] == "PAPER_FILLED"
        assert row["limit_price"] == 0.75
        assert row["entry_price"] == 0.75
        assert len(store.open_paper_orders(10)) == 1


def test_paper_limit_mode_does_not_fill_from_a_later_visible_quote_alone():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(
            store,
            StrategyConfig(limit_price_edge_lcb_buffer=0.02),
            entry_mode="limit",
        )
        first_scan = _decision(probability_lcb=0.81, entry_bid=0.73, entry_ask=0.75)
        order_ids = trader.place_approved("2026-06-15", [first_scan])
        assert len(order_ids) == 1
        order_id = order_ids[0]
        assert store.paper_order(order_id)["status"] == "PAPER_LIMIT_RESTING"

        later_scan = _decision(
            approved=False,
            probability_lcb=0.65,
            entry_bid=0.72,
            entry_ask=0.74,
            reasons=["edge below min"],
        )
        filled_ids = trader.place_approved("2026-06-15", [later_scan])

        assert filled_ids == []
        row = store.paper_order(order_id)
        assert row is not None
        assert row["status"] == "PAPER_LIMIT_RESTING"
        assert row["limit_price"] == 0.74
        assert len(store.open_paper_orders(10)) == 0


def test_with_buy_limit_exposes_limit_math_on_decision_for_reporting():
    limited = with_buy_limit(
        _decision(probability_lcb=0.81, entry_bid=0.73, entry_ask=0.75),
        StrategyConfig(limit_price_edge_lcb_buffer=0.02),
    )

    assert limited.limit_price == 0.74
    assert limited.limit_cost_per_contract < limited.cost_per_contract
    assert limited.limit_edge_lcb >= 0.02


def test_analyze_entry_mode_defaults_from_environment():
    # Fixture-free so it runs under both pytest and the no-arg run_tests.py
    # runner used by verify_project.sh / CI.
    with patch.dict(os.environ, {"PAPER_ENTRY_MODE": "limit"}):
        args = build_parser().parse_args(["analyze"])

    assert args.paper_entry_mode == "limit"


def test_resting_quote_size_is_not_capped_by_displayed_ask():
    """A resting maker bid's fill depends on FUTURE traded volume (queue-ahead
    fill model), not the ask displayed at entry, so its size must not be
    clamped to the displayed ask depth."""
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(
            store,
            StrategyConfig(limit_price_edge_lcb_buffer=0.02),
            entry_mode="limit",
        )
        decision = _decision(
            probability_lcb=0.81,
            entry_bid=0.73,
            entry_ask=0.75,
            recommended_contracts=25.0,
            entry_ask_size=3.0,
        )

        order_ids = trader.place_approved("2026-06-15", [decision])

        assert len(order_ids) == 1
        row = store.paper_order(order_ids[0])
        assert row["status"] == "PAPER_LIMIT_RESTING"
        assert row["contracts"] == 25.0


def test_crossing_limit_is_clamped_to_displayed_ask():
    """A crossing limit takes instantly against the visible ask: it cannot
    take more contracts than the book displays."""
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(
            store,
            StrategyConfig(limit_price_edge_lcb_buffer=0.02),
            entry_mode="limit",
        )
        decision = _decision(
            probability_lcb=0.90,
            entry_bid=0.74,
            entry_ask=0.75,
            recommended_contracts=25.0,
            entry_ask_size=3.0,
        )

        order_ids = trader.place_approved("2026-06-15", [decision])

        assert len(order_ids) == 1
        row = store.paper_order(order_ids[0])
        assert row["status"] == "PAPER_FILLED"
        assert row["contracts"] == 3.0


def test_market_entry_is_clamped_to_displayed_ask():
    """Market entry takes immediately at the displayed ask, so the taker cap
    now applied at the execution gate (not in sizing) must still bind."""
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store, StrategyConfig())
        decision = _decision(
            recommended_contracts=25.0,
            entry_ask_size=3.0,
        )

        order_ids = trader.place_approved("2026-06-15", [decision])

        assert len(order_ids) == 1
        row = store.paper_order(order_ids[0])
        assert row["status"] == "PAPER_FILLED"
        assert row["contracts"] == 3.0
