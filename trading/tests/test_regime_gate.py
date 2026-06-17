"""P1-F: warm/hot regime gate -- balanced blocks the anti-calibrated cohorts."""

from dataclasses import replace

from sfo_kalshi_quant.config import (
    COLD_COHORT,
    HOT_COHORT,
    NORMAL_COHORT,
    WARM_COHORT,
    strategy_config_for_profile,
    temperature_cohort,
)
from sfo_kalshi_quant.models import BucketProbability
from sfo_kalshi_quant.risk import TradeEvaluator
from sfo_kalshi_quant.standard_bins import standard_sfo_bins


def _no_favorite():
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


def test_temperature_cohort_boundaries():
    assert temperature_cohort(59.9) == COLD_COHORT
    assert temperature_cohort(60.0) == NORMAL_COHORT
    assert temperature_cohort(69.9) == NORMAL_COHORT
    assert temperature_cohort(70.0) == WARM_COHORT
    assert temperature_cohort(79.9) == WARM_COHORT
    assert temperature_cohort(80.0) == HOT_COHORT


def test_balanced_blocks_warm_and_hot_forecast_cohorts():
    market, probability = _no_favorite()
    evaluator = TradeEvaluator(strategy_config_for_profile("balanced"))
    warm = evaluator.evaluate_market(market, probability, bankroll=1000, side="NO", forecast_high_f=75.0)
    hot = evaluator.evaluate_market(market, probability, bankroll=1000, side="NO", forecast_high_f=85.0)
    assert not warm.approved
    assert any("regime" in r for r in warm.reasons)
    assert not hot.approved


def test_balanced_trades_cold_and_normal_forecast_cohorts():
    market, probability = _no_favorite()
    evaluator = TradeEvaluator(strategy_config_for_profile("balanced"))
    normal = evaluator.evaluate_market(market, probability, bankroll=1000, side="NO", forecast_high_f=65.0)
    cold = evaluator.evaluate_market(market, probability, bankroll=1000, side="NO", forecast_high_f=55.0)
    assert normal.approved
    assert cold.approved


def test_fast_feedback_still_explores_warm_days_to_collect_calibration_data():
    market, probability = _no_favorite()
    evaluator = TradeEvaluator(strategy_config_for_profile("fast-feedback"))
    warm = evaluator.evaluate_market(market, probability, bankroll=1000, side="NO", forecast_high_f=75.0)
    assert warm.approved


def test_regime_gate_is_inert_without_a_forecast_high():
    market, probability = _no_favorite()
    evaluator = TradeEvaluator(strategy_config_for_profile("balanced"))
    decision = evaluator.evaluate_market(market, probability, bankroll=1000, side="NO")
    assert decision.approved
