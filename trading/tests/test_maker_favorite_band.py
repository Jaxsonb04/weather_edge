"""Maker-side reorientation: resting quotes pay maker fees, live concentrates
on the favorite band, and the monitor fills crossed resting limits."""

import io
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sfo_kalshi_quant.cli import main
from sfo_kalshi_quant.config import StrategyConfig, strategy_config_for_profile
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.execution import buy_limit_for_decision
from sfo_kalshi_quant.fees import (
    quadratic_fee_average_per_contract,
    quadratic_fee_per_contract,
)
from sfo_kalshi_quant.models import MarketBin, TradeDecision
from sfo_kalshi_quant.probability import BucketProbability
from sfo_kalshi_quant.risk import TradeEvaluator


def _decision(**overrides) -> TradeDecision:
    base = dict(
        ticker="KXHIGHNY-26JUL08-B84.5",
        label="84° to 85°",
        action="BUY_YES",
        approved=True,
        probability=0.85,
        probability_lcb=0.80,
        yes_bid=0.72,
        yes_ask=0.76,
        spread=0.04,
        fee_per_contract=0.02,
        cost_per_contract=0.78,
        edge=0.07,
        edge_lcb=0.02,
        kelly_fraction=0.05,
        recommended_contracts=10.0,
        expected_profit=0.7,
        reasons=[],
        side="YES",
        entry_bid=0.72,
        entry_ask=0.76,
    )
    base.update(overrides)
    return TradeDecision(**base)


def test_resting_limit_quote_charges_maker_fee():
    config = StrategyConfig(limit_price_edge_lcb_buffer=0.0)
    quote = buy_limit_for_decision(_decision(), config)
    assert quote is not None
    assert not quote.would_cross  # spread > 1 tick -> quote rests below the ask
    maker_fee = quadratic_fee_average_per_contract(
        quote.price, 10.0, maker=True, series_ticker="KXHIGHNY"
    )
    taker_fee = quadratic_fee_average_per_contract(
        quote.price, 10.0, maker=False, series_ticker="KXHIGHNY"
    )
    assert quote.fee_per_contract == maker_fee
    assert maker_fee == 0.0  # unlisted weather series have maker multiplier M=0
    assert maker_fee < taker_fee


def test_crossing_limit_quote_still_charges_taker_fee():
    config = StrategyConfig(limit_price_edge_lcb_buffer=0.0)
    # One-tick spread: the quote goes straight to the ask and crosses.
    quote = buy_limit_for_decision(
        _decision(yes_bid=0.75, entry_bid=0.75, spread=0.01), config
    )
    assert quote is not None
    assert quote.would_cross
    assert quote.fee_per_contract == quadratic_fee_average_per_contract(
        quote.price, 10.0, maker=False, series_ticker="KXHIGHNY"
    )


def _bucket(market: MarketBin, p: float, lcb: float, market_p: float) -> BucketProbability:
    return BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=p,
        lower_confidence=lcb,
        empirical_probability=p,
        normal_probability=p,
        effective_n=300,
        residual_probability=p,
        ensemble_probability=p,
        model_probability=p,
        market_probability=market_p,
    )


def _market(bid: float, ask: float) -> MarketBin:
    return MarketBin.from_kalshi(
        {
            "ticker": "KXHIGHNY-26JUL08-B84.5",
            "event_ticker": "KXHIGHNY-26JUL08",
            "title": "84-85",
            "yes_sub_title": "84° to 85°",
            "strike_type": "between",
            "floor_strike": 84,
            "cap_strike": 85,
            "yes_bid_dollars": f"{bid:.4f}",
            "yes_ask_dollars": f"{ask:.4f}",
            "no_bid_dollars": f"{1 - ask:.4f}",
            "no_ask_dollars": f"{1 - bid:.4f}",
            "yes_bid_size_fp": "50",
            "yes_ask_size_fp": "50",
            "status": "active",
        }
    )


def test_live_favorite_band_rejects_coinflips_and_accepts_favorites():
    live = TradeEvaluator(strategy_config_for_profile("live"))

    coinflip = live.evaluate_market(
        _market(0.53, 0.55), _bucket(_market(0.53, 0.55), 0.68, 0.63, 0.55), bankroll=1000
    )
    assert not coinflip.approved
    assert any("favorite band" in r for r in coinflip.reasons)

    favorite_market = _market(0.72, 0.74)
    favorite = live.evaluate_market(
        favorite_market, _bucket(favorite_market, 0.85, 0.80, 0.73), bankroll=1000
    )
    assert favorite.approved, favorite.reasons

    # The research collector keeps measuring the whole curve.
    research = TradeEvaluator(strategy_config_for_profile("research"))
    assert not research.config.favorite_band_enabled


def test_monitor_fills_resting_limit_when_later_trade_clears_queue():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = _decision(limit_price=0.73)
        order_id = store.record_paper_order(
            "2026-07-08", decision, status="PAPER_LIMIT_RESTING", entry_mode="limit"
        )
        assert order_id is not None
        crossed = _market(0.71, 0.72)

        with patch(
            "sfo_kalshi_quant.cli.KalshiPublicClient"
        ) as client_cls, redirect_stdout(io.StringIO()) as out:
            client_cls.return_value.get_trades.return_value = {
                "trades": [{
                    "trade_id": "trade-1",
                    "count_fp": "10.00",
                    "yes_price_dollars": "0.72",
                    # Ask-side (NO) takers fill resting YES bids.
                    "taker_book_side": "ask",
                    "created_time": "2099-07-08T12:00:00Z",
                }]
            }
            client_cls.return_value.get_market.return_value = crossed
            code = main(
                [
                    "--db-path",
                    str(Path(tmp) / "paper.db"),
                    "--no-color",
                    "paper-monitor",
                    "--dry-run",
                ]
            )
        assert code == 0
        assert "filled resting order" in out.getvalue()
        row = store.paper_orders(1)[0]
        assert row["status"] == "PAPER_FILLED"
