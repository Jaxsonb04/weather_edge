"""Wiring test: posterior-mean Kelly haircut inside TradeEvaluator (Phase 2b)."""

from dataclasses import replace

from pytest import approx

from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.models import BucketProbability
from sfo_kalshi_quant.posterior_kelly import CohortRecord, PosteriorKellyModel
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
        yes_bid_size=1_000_000.0,
        yes_ask_size=1_000_000.0,
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


def _losing_model() -> PosteriorKellyModel:
    # Realized win-rate below breakeven everywhere -> trust 0 -> multiplier == floor.
    losing = CohortRecord(n=200, wins=90.0, mean_claimed_prob=0.70, mean_cost=0.55)
    return PosteriorKellyModel(
        cohort_records={}, overall=losing, prior_strength=20.0, floor=0.2, min_cohort_n=8
    )


def _config():
    # Balanced profile with the position-risk and per-market contract caps
    # unbound, so Kelly is the binding lever and the haircut is visible in the
    # contract count rather than being clipped by a cap both arms hit.
    return replace(
        strategy_config_for_profile("balanced"),
        posterior_mean_kelly_enabled=True,
        max_position_risk_pct=0.5,
        max_contracts_per_market=1_000_000,
        round_contracts=False,
    )


def test_haircut_shrinks_size_versus_no_model():
    market, probability = _no_favorite()
    cfg = _config()
    baseline = TradeEvaluator(cfg).evaluate_market(
        market, probability, bankroll=10000, side="NO", forecast_high_f=61.0
    )
    haircut = TradeEvaluator(cfg, sizing_model=_losing_model()).evaluate_market(
        market, probability, bankroll=10000, side="NO", forecast_high_f=61.0
    )
    assert baseline.approved and haircut.approved
    # floor 0.2 -> the haircut Kelly stake is ~1/5 of the un-haircut size.
    assert haircut.recommended_contracts == approx(0.2 * baseline.recommended_contracts, rel=0.02)


def test_disabled_flag_is_a_no_op_even_with_a_model():
    market, probability = _no_favorite()
    cfg = replace(_config(), posterior_mean_kelly_enabled=False)
    without = TradeEvaluator(cfg).evaluate_market(
        market, probability, bankroll=10000, side="NO", forecast_high_f=61.0
    )
    with_model = TradeEvaluator(cfg, sizing_model=_losing_model()).evaluate_market(
        market, probability, bankroll=10000, side="NO", forecast_high_f=61.0
    )
    assert without.recommended_contracts == with_model.recommended_contracts


def test_calibrated_cohort_barely_haircuts():
    market, probability = _no_favorite()
    cfg = _config()
    # A well-calibrated, winning record -> trust ~1 -> multiplier ~1 -> size close
    # to the un-haircut baseline.
    calibrated = PosteriorKellyModel(
        cohort_records={},
        overall=CohortRecord(n=400, wins=300.0, mean_claimed_prob=0.75, mean_cost=0.55),
        prior_strength=20.0,
        floor=0.2,
        min_cohort_n=8,
    )
    baseline = TradeEvaluator(cfg).evaluate_market(
        market, probability, bankroll=10000, side="NO", forecast_high_f=61.0
    )
    good = TradeEvaluator(cfg, sizing_model=calibrated).evaluate_market(
        market, probability, bankroll=10000, side="NO", forecast_high_f=61.0
    )
    losing = TradeEvaluator(cfg, sizing_model=_losing_model()).evaluate_market(
        market, probability, bankroll=10000, side="NO", forecast_high_f=61.0
    )
    # The calibrated cohort keeps far more size than the losing one.
    assert good.recommended_contracts > losing.recommended_contracts
