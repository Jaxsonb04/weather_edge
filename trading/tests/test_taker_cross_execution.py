"""Opportunistic taker-cross and reservation-price resting execution.

The July 2026 regime showed maker-only entries starving: approved candidates
carried positive after-fee taker-cost edge, but the bid+1 resting quote filled
under 20% of the time and the book earned near-zero. These tests pin the two
flag-gated execution upgrades:

* ``limit_taker_cross_enabled`` — cross the displayed ask immediately when the
  after-fee LOWER-BOUND edge at the taker price still clears the SAME
  ``limit_price_edge_lcb_buffer`` bar the maker path enforces. No decision or
  safety gate changes; only already-approved candidates are eligible.
* ``limit_resting_reservation_fallback`` — when bid+1 violates the LCB buffer,
  rest deeper at the highest tick that preserves the buffer instead of
  dropping the candidate entirely.
* ``research_target_taker_cross`` — the target research book may cross the
  displayed ask whenever its documented zero after-fee point/LCB floor holds,
  instead of only when the spread is one tick.

The frozen ``StrategyConfig()`` defaults keep every flag OFF so the
conservative baseline and its historical fingerprints remain reproducible.
"""

from __future__ import annotations

import math

from sfo_kalshi_quant.config import (
    StrategyConfig,
    strategy_config_for_profile,
)
from sfo_kalshi_quant.execution import (
    buy_limit_for_decision,
    target_research_quote,
)
from sfo_kalshi_quant.fees import quadratic_fee_average_per_contract
from sfo_kalshi_quant.models import TradeDecision


def _decision(**overrides) -> TradeDecision:
    values = {
        "ticker": "KXHIGHTSFO-TEST-B74.5",
        "label": "74° to 75°",
        "action": "BUY_NO",
        "approved": True,
        "probability": 0.95,
        "probability_lcb": 0.94,
        "yes_bid": 0.08,
        "yes_ask": 0.10,
        "spread": 0.02,
        "fee_per_contract": 0.01,
        "cost_per_contract": 0.91,
        "edge": 0.04,
        "edge_lcb": 0.03,
        "kelly_fraction": 0.01,
        "recommended_contracts": 12.0,
        "expected_profit": 0.4,
        "reasons": [],
        "side": "NO",
        "entry_bid": 0.88,
        "entry_ask": 0.90,
        "entry_bid_size": 10.0,
        "entry_ask_size": 10.0,
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


def test_frozen_default_config_keeps_every_new_flag_off():
    config = StrategyConfig()
    assert config.limit_taker_cross_enabled is False
    assert config.limit_resting_reservation_fallback is False
    assert config.research_target_taker_cross is False
    assert config.limit_taker_cross_min_edge_lcb == 0.02
    assert config.limit_taker_cross_min_notional == 5.0


def test_live_profile_enables_taker_cross_and_reservation_fallback():
    live = strategy_config_for_profile("live")
    assert live.limit_taker_cross_enabled is True
    assert live.limit_resting_reservation_fallback is True
    # The crossing bar is the APPROVAL gate's own after-fee lower-bound floor,
    # not the maker reservation margin. Superseded 2026-07-27: holding taker
    # fills to the maker margin refused a population that settled at a 95.1%
    # win rate and +$3.47/day (see the LIVE_PROFILE_OVERRIDES rationale).
    assert live.limit_taker_cross_min_edge_lcb == 0.0
    assert live.limit_taker_cross_min_edge_lcb < live.limit_price_edge_lcb_buffer
    assert live.limit_taker_cross_min_notional == 1.0


def test_research_profile_scopes_capture_to_the_target_book():
    # The generic-path flags stay off (legacy generic research is archived and
    # its bid+1 semantics are the tested baseline); the active target ledger
    # gets the crossing capture at its own unchanged zero floor.
    research = strategy_config_for_profile("research")
    assert research.limit_taker_cross_enabled is False
    assert research.limit_resting_reservation_fallback is False
    assert research.research_target_taker_cross is True


def _research_cross_config() -> StrategyConfig:
    return strategy_config_for_profile("research")


def _research_rest_config() -> StrategyConfig:
    base = strategy_config_for_profile("research")
    return StrategyConfig(**{**base.__dict__, "research_target_taker_cross": False})


def test_disabled_flag_preserves_resting_maker_behavior():
    decision = _decision()
    quote = buy_limit_for_decision(decision, StrategyConfig())
    assert quote is not None
    assert quote.would_cross is False
    assert quote.price == 0.89  # bid + one tick


def test_taker_cross_takes_displayed_ask_when_lcb_clears_buffer():
    config = StrategyConfig(limit_taker_cross_enabled=True)
    decision = _decision()
    quote = buy_limit_for_decision(decision, config)
    assert quote is not None
    assert quote.would_cross is True
    assert quote.price == 0.90
    # Whole contracts, capped at displayed ask depth.
    assert quote.contracts == 10.0
    expected_cost = _taker_cost(0.90, 10.0, config, decision.ticker)
    assert math.isclose(quote.cost_per_contract, expected_cost, abs_tol=1e-9)
    assert quote.edge_lcb >= config.limit_taker_cross_min_edge_lcb - 1e-12
    # Taker fees are charged, not maker fees.
    assert quote.fee_per_contract > 0.0


def test_taker_cross_respects_recommended_size_below_depth():
    config = StrategyConfig(limit_taker_cross_enabled=True)
    quote = buy_limit_for_decision(
        _decision(recommended_contracts=7.4, entry_ask_size=50.0),
        config,
    )
    assert quote is not None
    assert quote.would_cross is True
    assert quote.contracts == 7.0


def test_taker_cross_declines_when_lcb_below_buffer_and_rests_instead():
    config = StrategyConfig(limit_taker_cross_enabled=True)
    # LCB-edge at the 0.90 ask is below 2%, but at the 0.89 maker price it
    # still clears, so the candidate must fall back to the resting quote.
    decision = _decision(probability_lcb=0.915)
    quote = buy_limit_for_decision(decision, config)
    assert quote is not None
    assert quote.would_cross is False
    assert quote.price == 0.89
    assert quote.edge_lcb >= config.limit_price_edge_lcb_buffer - 1e-12


def test_taker_cross_declines_thin_ask_below_min_notional():
    config = StrategyConfig(limit_taker_cross_enabled=True)
    # 4 contracts * ~0.9 cost < $5 executable minimum -> rest, do not cross.
    decision = _decision(entry_ask_size=4.0)
    quote = buy_limit_for_decision(decision, config)
    assert quote is not None
    assert quote.would_cross is False
    assert quote.price == 0.89


def test_taker_cross_requires_at_least_one_whole_contract():
    config = StrategyConfig(limit_taker_cross_enabled=True)
    quote = buy_limit_for_decision(_decision(entry_ask_size=0.0), config)
    assert quote is not None
    assert quote.would_cross is False


def test_reservation_fallback_rests_deeper_instead_of_dropping():
    config = StrategyConfig(limit_resting_reservation_fallback=True)
    # At bid+1 (0.89) the maker LCB-edge is below the 2% buffer, so the old
    # behavior dropped the candidate. The fallback must rest at the highest
    # tick that preserves the buffer by construction.
    decision = _decision(probability_lcb=0.90)
    quote = buy_limit_for_decision(decision, config)
    assert quote is not None
    assert quote.would_cross is False
    assert quote.price < 0.89
    assert quote.edge_lcb + 1e-12 >= config.limit_price_edge_lcb_buffer
    # One tick higher must violate the buffer (highest admissible price).
    higher = quote.price + config.limit_price_tick
    fee = quadratic_fee_average_per_contract(
        higher,
        quote.contracts,
        maker=True,
        fee_multiplier=config.fee_multiplier,
        taker_rate=config.taker_fee_rate,
        maker_rate=config.maker_fee_rate,
        series_ticker=decision.ticker,
    )
    assert decision.probability_lcb - (higher + fee) < config.limit_price_edge_lcb_buffer


def test_reservation_fallback_disabled_preserves_drop_behavior():
    quote = buy_limit_for_decision(
        _decision(probability_lcb=0.90),
        StrategyConfig(),
    )
    assert quote is None


def test_reservation_fallback_gives_up_below_one_tick():
    config = StrategyConfig(limit_resting_reservation_fallback=True)
    quote = buy_limit_for_decision(
        _decision(probability=0.05, probability_lcb=0.02, entry_bid=0.01, entry_ask=0.03),
        config,
    )
    assert quote is None


def test_target_research_flag_off_rests_on_wide_spread():
    quote = target_research_quote(_decision(), _research_rest_config())
    assert quote is not None
    assert quote.would_cross is False
    assert quote.price == 0.89


def test_target_research_taker_cross_crosses_wide_spread_at_zero_floor():
    decision = _decision(recommended_contracts=8.0)
    quote = target_research_quote(decision, _research_cross_config())
    assert quote is not None
    assert quote.would_cross is True
    assert quote.price == 0.90
    assert quote.contracts == 8.0
    assert quote.edge >= -1e-12
    assert quote.edge_lcb >= -1e-12


def test_target_research_taker_cross_takes_a_bounded_partial_visible_fill():
    # The visible slice may fill immediately even when the policy request is
    # larger. Exact taker fees and both non-negative edge floors still bind.
    decision = _decision(recommended_contracts=12.0, entry_ask_size=10.0)
    quote = target_research_quote(decision, _research_cross_config())
    assert quote is not None
    assert quote.would_cross is True
    assert quote.price == 0.90
    assert quote.contracts == 10.0


def test_target_research_partial_cross_keeps_the_one_dollar_floor():
    # One 90c contract is real displayed liquidity but remains below the
    # replay-selected $1 minimum, so the larger request keeps its maker path.
    decision = _decision(recommended_contracts=12.0, entry_ask_size=1.0)
    quote = target_research_quote(decision, _research_cross_config())
    assert quote is not None
    assert quote.would_cross is False
    assert quote.price == 0.89


def test_target_research_natural_cross_keeps_the_one_dollar_floor():
    decision = _decision(
        entry_bid=0.89,
        entry_ask=0.90,
        entry_ask_size=1.0,
        recommended_contracts=12.0,
    )
    quote = target_research_quote(decision, _research_cross_config())
    assert quote is not None
    assert quote.would_cross is False
    assert quote.price == 0.89
    assert quote.contracts == 12.0


def test_target_research_taker_cross_falls_back_when_floor_fails():
    # Taker cost at the ask exceeds the LCB -> zero floor fails when crossing;
    # the maker quote at 0.89 still clears, so the book must rest as before.
    decision = _decision(probability_lcb=0.905)
    quote = target_research_quote(decision, _research_cross_config())
    assert quote is not None
    assert quote.would_cross is False
    assert quote.price == 0.89


def test_target_research_partial_cross_keeps_the_point_probability_floor():
    decision = _decision(model_probability=0.90)
    quote = target_research_quote(decision, _research_cross_config())
    assert quote is not None
    assert quote.would_cross is False
    assert quote.price == 0.89


def test_target_research_taker_cross_requires_displayed_depth():
    quote = target_research_quote(_decision(entry_ask_size=0.4), _research_cross_config())
    assert quote is not None
    assert quote.would_cross is False


def test_target_research_partial_cross_rejects_nonfinite_negative_depth():
    quote = target_research_quote(
        _decision(entry_ask_size=float("-inf")),
        _research_cross_config(),
    )
    assert quote is not None
    assert quote.would_cross is False
