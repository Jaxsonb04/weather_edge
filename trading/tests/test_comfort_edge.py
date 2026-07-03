"""Comfortable far-tail NO entry: block near-forecast coin-flips, size up far
tails, never override the positive after-fee edge_lcb floor."""

from dataclasses import replace

from sfo_kalshi_quant.config import StrategyConfig, strategy_config_for_profile
from sfo_kalshi_quant.models import BucketProbability
from sfo_kalshi_quant.portfolio import allocate_portfolio, portfolio_limits_for_profile
from sfo_kalshi_quant.risk import TradeEvaluator, _comfort_edge_assessment
from sfo_kalshi_quant.standard_bins import standard_sfo_bins


def _no_favorite(*, yes_prob: float = 0.05, yes_lower: float = 0.0, no_ask: float = 0.80):
    """A NO-favorite on the 66-67 bin (continuous interval 65.5..67.5)."""

    market = next(row for row in standard_sfo_bins() if row.yes_sub_title == "66° to 67°")
    market = replace(
        market,
        status="active",
        yes_bid=round(1.0 - no_ask - 0.02, 4),
        yes_ask=round(1.0 - no_ask + 0.02, 4),
        no_bid=round(no_ask - 0.02, 4),
        no_ask=no_ask,
        yes_bid_size=200.0,
        yes_ask_size=200.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=yes_prob,
        lower_confidence=yes_lower,
        empirical_probability=yes_prob,
        normal_probability=yes_prob,
        effective_n=250,
        model_probability=yes_prob,
        market_probability=yes_prob,
    )
    return market, probability


# --- unit: the distance assessment -----------------------------------------

def test_comfort_assessment_is_inert_when_disabled_or_inapplicable():
    market, _ = _no_favorite()
    strict = StrategyConfig()  # comfort_edge_enabled is False
    assert _comfort_edge_assessment(
        side="NO", market=market, forecast_high_f=75.0, forecast_sigma_f=None, config=strict
    ) == (None, 1.0)

    live = strategy_config_for_profile("live")
    # YES is never SIZE-boosted, and a far-from-forecast YES is not shaped at all
    # (forecast 75 sits 7.5F from the 65.5..67.5 bin -> well outside the band).
    assert _comfort_edge_assessment(
        side="YES", market=market, forecast_high_f=75.0, forecast_sigma_f=None, config=live
    ) == (None, 1.0)
    # No forecast -> inert.
    assert _comfort_edge_assessment(
        side="NO", market=market, forecast_high_f=None, forecast_sigma_f=None, config=live
    ) == (None, 1.0)


def test_comfort_blocks_near_forecast_yes_coin_flip():
    # A YES bet on the bin at the forecast is the diffuse-favorite coin-flip and
    # is blocked (mirrors the NO block); it is never size-boosted.
    market, _ = _no_favorite()  # bin interval 65.5..67.5
    live = strategy_config_for_profile("live")
    reason, mult = _comfort_edge_assessment(
        side="YES", market=market, forecast_high_f=66.0, forecast_sigma_f=None, config=live
    )
    assert reason is not None and "coin-flip" in reason and "YES" in reason
    assert mult == 1.0  # YES is blocked or inert, never boosted


def test_comfort_blocks_near_forecast_no_and_boosts_far_tails():
    market, _ = _no_favorite()  # bin interval 65.5..67.5
    live = strategy_config_for_profile("live")
    # Default floor sigma 3.0 -> block 1.25*3.0=3.75F, full 2.5*3.0=7.5F.

    # Forecast sits inside the bin -> distance 0 -> blocked.
    reason, mult = _comfort_edge_assessment(
        side="NO", market=market, forecast_high_f=66.0, forecast_sigma_f=None, config=live
    )
    assert reason is not None and "coin-flip" in reason and mult == 1.0

    # Comfortably far tail (forecast 75 -> 7.5F out) -> full size boost, no block.
    reason, mult = _comfort_edge_assessment(
        side="NO", market=market, forecast_high_f=75.0, forecast_sigma_f=None, config=live
    )
    assert reason is None
    assert mult == live.comfort_edge_max_size_boost

    # In the taper band (forecast 72 -> 4.5F out) -> partial boost between 1 and max.
    reason, mult = _comfort_edge_assessment(
        side="NO", market=market, forecast_high_f=72.0, forecast_sigma_f=None, config=live
    )
    assert reason is None
    assert 1.0 < mult < live.comfort_edge_max_size_boost


def test_comfort_distance_scales_with_forecast_sigma():
    market, _ = _no_favorite()
    live = strategy_config_for_profile("live")
    # After the frequency push the block band is 0.4*sigma (was 1.25). Sigma
    # scaling is preserved, just narrower: a wide-sigma day (sigma 10F -> band 4F)
    # still pulls a near-forecast NO bin into the coin-flip band, while a
    # floor-sigma day (band ~1.2F) would trade the same bin.
    reason, _mult = _comfort_edge_assessment(
        side="NO", market=market, forecast_high_f=68.0, forecast_sigma_f=10.0, config=live
    )
    assert reason is not None


# --- integration: evaluate_market ------------------------------------------

def test_live_blocks_near_forecast_no_bet():
    market, probability = _no_favorite()
    live = TradeEvaluator(strategy_config_for_profile("live"))
    decision = live.evaluate_market(
        market, probability, bankroll=1000, side="NO", forecast_high_f=66.0
    )
    assert not decision.approved
    assert any("comfort-edge" in r for r in decision.reasons)


def test_research_collector_does_not_block_near_forecast_no_bet():
    # The collector keeps comfort off so it records the center bins the readiness
    # rescore needs; the same near-forecast NO is admitted at tiny size.
    market, probability = _no_favorite()
    research = TradeEvaluator(strategy_config_for_profile("research"))
    decision = research.evaluate_market(
        market, probability, bankroll=1000, side="NO", forecast_high_f=66.0
    )
    assert decision.approved
    assert not any("comfort-edge" in r for r in decision.reasons)


def test_june_17_19_near_forecast_no_failure_mode_is_live_blocked_research_capped():
    # The post-June-16 loss pattern was NO bets too close to the forecasted high.
    # Live must sit those out; research may keep collecting examples, but the
    # shared allocator still caps total paper loss by profile.
    market, probability = _no_favorite()
    live = TradeEvaluator(strategy_config_for_profile("live"))
    research = TradeEvaluator(strategy_config_for_profile("research"))

    research_decisions = []
    for target in ("2026-06-17", "2026-06-18", "2026-06-19"):
        live_decision = live.evaluate_market(
            market, probability, bankroll=1000, side="NO", forecast_high_f=66.0
        )
        assert not live_decision.approved, target
        assert any("comfort-edge" in reason for reason in live_decision.reasons)

        research_decision = research.evaluate_market(
            market, probability, bankroll=1000, side="NO", forecast_high_f=66.0
        )
        assert research_decision.approved, target
        research_decisions.append(research_decision)

    plan = allocate_portfolio(research_decisions, bankroll=1000, risk_profile="research")
    assert plan.worst_case_loss <= portfolio_limits_for_profile("research", 1000).max_daily_loss


def test_far_tail_no_is_sized_up_versus_no_boost():
    # Forecast 59F (cold cohort, NOT regime-blocked) sits 6.5F below the 66-67
    # bin -> a comfortably far tail at full boost.
    market, probability = _no_favorite()
    live_cfg = strategy_config_for_profile("live")
    boosted = TradeEvaluator(live_cfg).evaluate_market(
        market, probability, bankroll=200, side="NO", forecast_high_f=59.0
    )
    no_boost = TradeEvaluator(replace(live_cfg, comfort_edge_max_size_boost=1.0)).evaluate_market(
        market, probability, bankroll=200, side="NO", forecast_high_f=59.0
    )
    assert boosted.approved and no_boost.approved
    assert boosted.recommended_contracts > no_boost.recommended_contracts


def test_comfort_never_overrides_the_positive_edge_lcb_floor():
    # A genuine far tail (forecast 59F, 6.5F out), but priced so the after-fee
    # lower-bound edge is negative: live must still reject it -- comfort is
    # gate+sizing, never a bypass of the EV floor.
    market, probability = _no_favorite(yes_prob=0.05, yes_lower=0.0, no_ask=0.95)
    live = TradeEvaluator(strategy_config_for_profile("live"))
    decision = live.evaluate_market(
        market, probability, bankroll=1000, side="NO", forecast_high_f=59.0
    )
    assert not decision.approved
    assert any("lower-bound edge" in r for r in decision.reasons)
