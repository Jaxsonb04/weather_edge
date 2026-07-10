"""P0-B: the real sizing throttle -- bigger, honest paper stake.

The 2026-06-16 cap raise was dead weight because the binding throttle had
already flipped to Kelly-on-a-thin-edge, made worse by sizing off the pure LCB
(kelly_lcb_weight=1.0, double-counting the uncertainty the approval gate already
enforces) and by int() truncation of the contract count. These tests lock the
fixes: a less-pessimistic sizing blend, round() instead of int() on the paper
path, a binding-constraint diagnostic, and an honest Kelly-zero reason.
"""

from dataclasses import replace

from sfo_kalshi_quant.config import StrategyConfig, strategy_config_for_profile
from sfo_kalshi_quant.models import BucketProbability
from sfo_kalshi_quant.risk import TradeEvaluator
from sfo_kalshi_quant.standard_bins import standard_sfo_bins


def _no_favorite():
    """An approved NO favorite: NO ask 0.78 (cost ~0.80), deep liquidity, a
    comfortably positive lower-bound edge."""
    market = next(row for row in standard_sfo_bins() if row.yes_sub_title == "66° to 67°")
    market = replace(
        market,
        status="active",
        yes_bid=0.20,
        yes_ask=0.24,
        no_bid=0.76,
        no_ask=0.78,
        yes_bid_size=200.0,
        yes_ask_size=200.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.10,
        lower_confidence=0.06,
        empirical_probability=0.10,
        normal_probability=0.10,
        effective_n=250,
        model_probability=0.10,
        market_probability=0.12,
    )
    return market, probability


def test_balanced_sizes_bigger_than_conservative_for_the_same_favorite():
    market, probability = _no_favorite()
    conservative = TradeEvaluator(StrategyConfig()).evaluate_market(
        market, probability, bankroll=1000, side="NO"
    )
    balanced = TradeEvaluator(strategy_config_for_profile("balanced")).evaluate_market(
        market, probability, bankroll=1000, side="NO"
    )
    assert conservative.approved and balanced.approved
    # Lower kelly_lcb_weight + the bigger per-position cap let balanced deploy
    # materially more than the conservative baseline on the same opportunity.
    assert balanced.recommended_contracts > conservative.recommended_contracts


def test_binding_constraint_is_reported_on_an_approved_trade():
    market, probability = _no_favorite()
    decision = TradeEvaluator(strategy_config_for_profile("balanced")).evaluate_market(
        market, probability, bankroll=1000, side="NO"
    )
    assert decision.approved
    assert decision.binding_constraint in {
        "kelly_budget",
        "position_risk_cap",
        "max_contracts_per_market",
    }


def test_thin_displayed_ask_no_longer_caps_sizing():
    """Displayed ask depth must NOT bound the recommended size any more.

    The ask_size sizing allowance was a taker-era assumption: a maker-first
    resting bid's fill is gated by FUTURE traded volume (the queue-ahead fill
    model), not by the ask displayed at entry. 558/560 approved live rows over
    72h were ask_size-bound (median displayed ask: 2 contracts), starving the
    book. The taker cases (market entry / crossing limit) are clamped at the
    execution gate instead -- see test_limit_orders.py.
    """
    market, probability = _no_favorite()
    # Only 3 contracts of NO ask depth (no_ask_size falls back to yes_bid_size).
    market = replace(market, yes_bid_size=3.0)
    decision = TradeEvaluator(strategy_config_for_profile("balanced")).evaluate_market(
        market, probability, bankroll=1000, side="NO"
    )
    assert decision.approved
    assert decision.binding_constraint != "ask_size"
    assert decision.recommended_contracts > 3.0


def test_round_contracts_does_not_truncate_paper_stake():
    """A raw 1.6 contracts rounds to 2 on the rounding (paper) path, but floors
    to 1 on the frozen conservative baseline."""
    market, probability = _no_favorite()
    # Force the per-position cap to make raw contracts ~1.6 (1.28 / 0.80).
    cfg = replace(
        StrategyConfig(),
        max_position_risk_pct=0.00128,  # 1000 * 0.00128 = $1.28 budget
        round_contracts=False,
    )
    floored = TradeEvaluator(cfg).evaluate_market(market, probability, bankroll=1000, side="NO")
    rounded = TradeEvaluator(replace(cfg, round_contracts=True)).evaluate_market(
        market, probability, bankroll=1000, side="NO"
    )
    assert rounded.recommended_contracts > floored.recommended_contracts


def test_kelly_zero_gives_an_honest_reason_not_a_generic_sizing_error():
    """When the model-probability edge gate approves a trade but the blended
    sizing probability has no positive edge over cost, the rejection reason must
    name the Kelly-zero cause, not a misleading generic 'risk sizing' error."""
    market = next(row for row in standard_sfo_bins() if row.yes_sub_title == "66° to 67°")
    market = replace(
        market,
        status="active",
        yes_bid=0.48,
        yes_ask=0.50,
        yes_bid_size=200.0,
        yes_ask_size=200.0,
    )
    # Point/LCB both just below cost (~0.51) -> Kelly zero; but the model gate
    # (edge_gate_uses_model_probability on fast-feedback) sees a strong 0.80.
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.47,
        lower_confidence=0.46,
        empirical_probability=0.47,
        normal_probability=0.47,
        effective_n=250,
        model_probability=0.80,
        market_probability=0.78,
    )
    decision = TradeEvaluator(strategy_config_for_profile("fast-feedback")).evaluate_market(
        market, probability, bankroll=1000, side="YES"
    )
    assert not decision.approved
    assert any("Kelly fraction is zero" in reason for reason in decision.reasons)
