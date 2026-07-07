"""P1-D: optimal smaller case-by-case YES sizing (balanced/exploit profile)."""

from dataclasses import replace

from sfo_kalshi_quant.config import StrategyConfig, strategy_config_for_profile
from sfo_kalshi_quant.models import BucketProbability
from sfo_kalshi_quant.risk import TradeEvaluator, _yes_sizing_factor
from sfo_kalshi_quant.standard_bins import standard_sfo_bins


def _yes(label="68° to 69°", **overrides):
    market = next(row for row in standard_sfo_bins() if row.yes_sub_title == label)
    return replace(market, status="active", **overrides)


def _balanced():
    return TradeEvaluator(strategy_config_for_profile("balanced"))


# --- the Baker-McHale shrink x payout scale ---

def test_yes_sizing_factor_shrinks_hard_on_wide_uncertainty():
    cost = 0.40
    tight = _yes_sizing_factor(0.55, 0.53, cost, StrategyConfig())
    wide = _yes_sizing_factor(0.55, 0.30, cost, StrategyConfig())
    # A wide confidence interval (more estimation error) sizes strictly smaller.
    assert wide < tight
    # With almost no uncertainty the factor approaches the payout scale (cost).
    near_certain = _yes_sizing_factor(0.55, 0.5499, cost, StrategyConfig())
    assert abs(near_certain - cost) < 0.02


def test_yes_payout_scale_sizes_cheap_longshots_smaller():
    # Same relative edge and CI shape, but a 5c longshot is sized far below a
    # near-even-money YES (payout scale ~ cost).
    cheap = _yes_sizing_factor(0.10, 0.085, 0.05, StrategyConfig())
    rich = _yes_sizing_factor(0.80, 0.68, 0.40, StrategyConfig())
    assert cheap < rich


# --- the YES gates (balanced only) ---

def test_balanced_rejects_yes_without_a_positive_lower_bound_edge():
    market = _yes(yes_bid=0.30, yes_ask=0.33, yes_bid_size=50.0, yes_ask_size=50.0)
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.40,
        lower_confidence=0.30,  # edge_lcb ~ 0.30 - 0.34 < 0
        empirical_probability=0.40,
        normal_probability=0.40,
        effective_n=220,
        model_probability=0.40,
        market_probability=0.36,
    )
    decision = _balanced().evaluate_market(market, probability, bankroll=1000, side="YES")
    assert not decision.approved
    assert any("YES lower-bound edge" in r for r in decision.reasons)


def test_balanced_rejects_cheap_yes_without_an_ev_cushion():
    market = _yes("66° to 67°", yes_bid=0.08, yes_ask=0.09, yes_bid_size=80.0, yes_ask_size=80.0)
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.18,  # < 2x cost (~0.20); fee-dominated, no cushion
        lower_confidence=0.13,
        empirical_probability=0.18,
        normal_probability=0.18,
        effective_n=220,
        residual_probability=0.18,
        ensemble_probability=0.17,
        model_probability=0.18,
        market_probability=0.16,
    )
    decision = _balanced().evaluate_market(market, probability, bankroll=1000, side="YES")
    assert not decision.approved
    assert any("cheap YES" in r for r in decision.reasons)


# --- approved YES is sized small and capped ---

def test_approved_balanced_yes_is_sized_small_and_capped():
    market = _yes(yes_bid=0.36, yes_ask=0.38, yes_bid_size=200.0, yes_ask_size=200.0)
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.55,
        lower_confidence=0.45,  # edge_lcb ~ 0.45 - 0.40 = 0.05 > 0
        empirical_probability=0.55,
        normal_probability=0.55,
        effective_n=220,
        residual_probability=0.55,
        ensemble_probability=0.54,
        model_probability=0.55,
        market_probability=0.50,
    )
    # The live favorite band would reject this sub-band YES outright; disable
    # it here to isolate the estimation-shrink SIZING machinery, which the
    # research collector still exercises on the full price curve.
    cfg = replace(strategy_config_for_profile("balanced"), favorite_band_enabled=False)
    decision = TradeEvaluator(cfg).evaluate_market(market, probability, bankroll=1000, side="YES")
    assert decision.approved
    assert decision.binding_constraint == "yes_estimation_shrink"
    spend = decision.recommended_contracts * decision.cost_per_contract
    # Capped at the tighter YES per-position cap (plus one-contract rounding slack).
    assert spend <= 1000 * cfg.yes_max_position_risk_pct + decision.cost_per_contract
