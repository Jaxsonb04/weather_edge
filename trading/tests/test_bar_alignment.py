"""Taker-cross bar aligned to the approval gate, including natural crosses.

The live book approves on a non-negative after-fee lower-bound edge but its
execution layer refused any quote under the MAKER reservation margin (0.02).
In the current regime every approved live candidate has a one-tick spread and
an after-fee lower-bound edge of 0.002-0.007, so it was approved and then never
placed: 23 approvals on 2026-07-26/27 produced zero orders.

These tests pin the two halves of the fix:

* a NATURAL cross (bid+1 already at the ask) routes through the taker path
  instead of being judged against the maker reservation margin, and
* the live crossing bar is the approval gate's own floor (non-negative
  after-fee lower-bound edge), not the maker margin.

The frozen ``StrategyConfig()`` baseline keeps the feature off entirely, and the
research collector's generic path is unchanged.
"""

from __future__ import annotations

import math

from sfo_kalshi_quant.config import StrategyConfig, strategy_config_for_profile
from sfo_kalshi_quant.execution import buy_limit_for_decision
from sfo_kalshi_quant.fees import quadratic_fee_average_per_contract
from sfo_kalshi_quant.models import TradeDecision


def _decision(**overrides) -> TradeDecision:
    """A production-shaped candidate: NO favorite, one-tick spread, thin depth.

    Modelled on KXHIGHAUS-26JUL27-T96 as recorded on 2026-07-26: bid 0.94,
    ask 0.95, ask depth 4-11, probability 0.983, lower bound 0.960.
    """

    values = {
        "ticker": "KXHIGHAUS-26JUL27-T96",
        "label": "95 or below",
        "action": "BUY_NO",
        "approved": True,
        "probability": 0.983,
        "probability_lcb": 0.960,
        "yes_bid": 0.05,
        "yes_ask": 0.06,
        "spread": 0.01,
        "fee_per_contract": 0.003,
        "cost_per_contract": 0.953,
        "edge": 0.0295,
        "edge_lcb": 0.0066,
        "kelly_fraction": 0.02,
        "recommended_contracts": 87.0,
        "expected_profit": 2.5,
        "reasons": [],
        "side": "NO",
        "entry_bid": 0.94,
        "entry_ask": 0.95,
        "entry_bid_size": 8.0,
        "entry_ask_size": 11.0,
    }
    values.update(overrides)
    return TradeDecision(**values)


def _taker_cost(price: float, contracts: float, config: StrategyConfig, ticker: str) -> float:
    fee = quadratic_fee_average_per_contract(
        price,
        contracts,
        maker=False,
        fee_multiplier=config.fee_multiplier,
        taker_rate=config.taker_fee_rate,
        maker_rate=config.maker_fee_rate,
        series_ticker=ticker,
    )
    return price + fee


def test_live_crossing_bar_is_the_approval_gate_floor():
    live = strategy_config_for_profile("live")
    assert live.limit_taker_cross_enabled is True
    assert live.limit_taker_cross_min_edge_lcb == 0.0


def test_frozen_baseline_still_disables_every_capture_flag():
    config = StrategyConfig()
    assert config.limit_taker_cross_enabled is False
    assert config.limit_resting_reservation_fallback is False
    assert config.research_target_taker_cross is False
    # The frozen default bar is unchanged; only the live profile moves.
    assert config.limit_taker_cross_min_edge_lcb == 0.02


def test_production_shaped_candidate_now_places_instead_of_being_discarded():
    """The exact case that produced 23 approvals and zero orders."""

    live = strategy_config_for_profile("live")
    decision = _decision()
    quote = buy_limit_for_decision(decision, live)

    assert quote is not None, "approved candidate must not be silently discarded"
    assert quote.would_cross is True
    assert quote.price == 0.95
    # Whole contracts, capped at displayed ask depth.
    assert quote.contracts == 11.0
    expected_cost = _taker_cost(0.95, 11.0, live, decision.ticker)
    assert math.isclose(quote.cost_per_contract, expected_cost, abs_tol=1e-9)
    # Positive but sub-maker-margin lower-bound edge: exactly the population
    # the old bar refused.
    assert 0.0 <= quote.edge_lcb < 0.02


def test_natural_cross_is_judged_on_the_taker_bar_not_the_maker_margin():
    """A one-tick spread is a taker fill either way; the maker margin must not veto it."""

    config = StrategyConfig(limit_taker_cross_enabled=True, limit_taker_cross_min_edge_lcb=0.0)
    decision = _decision()
    inside = decision.bid + config.limit_price_tick
    assert inside >= decision.ask - 1e-12, "fixture must be a natural cross"

    quote = buy_limit_for_decision(decision, config)
    assert quote is not None
    assert quote.would_cross is True
    assert quote.edge_lcb >= 0.0


def test_natural_cross_still_refused_when_the_floor_fails():
    """Alignment is not removal: a negative after-fee lower-bound edge is declined."""

    live = strategy_config_for_profile("live")
    # Lower bound below the all-in taker cost -> no execution.
    quote = buy_limit_for_decision(_decision(probability_lcb=0.94), live)
    assert quote is None


def test_high_bar_config_still_refuses_the_thin_candidate():
    """The bar is a real parameter: restoring 0.02 restores the old refusal."""

    config = StrategyConfig(limit_taker_cross_enabled=True, limit_taker_cross_min_edge_lcb=0.02)
    assert buy_limit_for_decision(_decision(), config) is None


def test_executable_minimum_still_blocks_a_too_thin_book():
    """One contract at ~0.95 remains below the live profile's $1 floor."""

    live = strategy_config_for_profile("live")
    assert buy_limit_for_decision(_decision(entry_ask_size=1.0), live) is None


def test_wide_spread_still_prefers_the_improving_maker_quote_when_it_qualifies():
    """Alignment must not turn every entry into a taker fill."""

    live = strategy_config_for_profile("live")
    # Three-tick spread with a lower bound that clears the maker buffer at
    # bid+1 (0.91) but not the after-fee floor at the ask (0.93): the book
    # should improve the bid and rest, not cross.
    decision = _decision(
        entry_bid=0.90,
        entry_ask=0.93,
        spread=0.03,
        probability_lcb=0.932,
        probability=0.96,
    )
    quote = buy_limit_for_decision(decision, live)
    assert quote is not None
    assert quote.would_cross is False
    assert quote.price == 0.91
    assert quote.edge_lcb >= live.limit_price_edge_lcb_buffer


def test_reservation_fallback_rests_deeper_rather_than_discarding():
    """When bid+1 misses the maker margin, rest at the deepest qualifying tick."""

    live = strategy_config_for_profile("live")
    decision = _decision(
        entry_bid=0.90,
        entry_ask=0.93,
        spread=0.03,
        probability_lcb=0.925,
        probability=0.95,
    )
    quote = buy_limit_for_decision(decision, live)
    assert quote is not None
    assert quote.would_cross is False
    # 0.91 would leave only 0.015 of lower-bound edge, under the 0.02 margin.
    assert quote.price == 0.90
    assert quote.edge_lcb >= live.limit_price_edge_lcb_buffer


def test_research_generic_path_is_unchanged():
    research = strategy_config_for_profile("research")
    assert research.limit_taker_cross_enabled is False
    assert research.limit_resting_reservation_fallback is False
    # The target book keeps its own crossing rule at its unchanged zero floor.
    assert research.research_target_taker_cross is True
