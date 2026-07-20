import os
from dataclasses import replace

from sfo_kalshi_quant.config import StrategyConfig, strategy_config_for_profile
from sfo_kalshi_quant.models import BucketProbability
from sfo_kalshi_quant.risk import TradeEvaluator
from sfo_kalshi_quant.standard_bins import standard_sfo_bins


def _bin(label: str, **overrides):
    market = next(row for row in standard_sfo_bins() if row.yes_sub_title == label)
    return replace(market, status="active", **overrides)


def test_no_bid_support_blocks_penny_tail_trade():
    market = _bin(
        "66° to 67°",
        yes_bid=0.0,
        yes_ask=0.01,
        yes_bid_size=0.0,
        yes_ask_size=100.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.18,
        lower_confidence=0.12,
        empirical_probability=0.18,
        normal_probability=0.18,
        effective_n=250,
        model_probability=0.30,
        market_probability=0.005,
    )
    decision = TradeEvaluator(StrategyConfig()).evaluate_market(market, probability, bankroll=350)
    assert not decision.approved
    assert any("no exit support" in reason for reason in decision.reasons)
    assert decision.trade_quality_score >= 0.0


def test_two_cent_tail_trade_requires_exceptional_support():
    market = _bin(
        "66° to 67°",
        yes_bid=0.01,
        yes_ask=0.02,
        yes_bid_size=5.0,
        yes_ask_size=100.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.18,
        lower_confidence=0.12,
        empirical_probability=0.18,
        normal_probability=0.18,
        effective_n=250,
        residual_probability=0.18,
        ensemble_probability=0.10,
        model_probability=0.10,
        market_probability=0.04,
    )
    decision = TradeEvaluator(StrategyConfig()).evaluate_market(market, probability, bankroll=350)
    assert not decision.approved
    assert any("1c/2c tail requires exceptional support" in reason for reason in decision.reasons)


def test_cheap_tail_trade_can_pass_with_exceptional_support_and_tight_book():
    market = _bin(
        "66° to 67°",
        yes_bid=0.04,
        yes_ask=0.05,
        yes_bid_size=80.0,
        yes_ask_size=100.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.18,
        lower_confidence=0.13,
        empirical_probability=0.18,
        normal_probability=0.18,
        effective_n=250,
        residual_probability=0.14,
        ensemble_probability=0.12,
        model_probability=0.10,
        market_probability=0.05,
    )
    decision = TradeEvaluator(StrategyConfig()).evaluate_market(market, probability, bankroll=350)
    assert decision.approved
    assert decision.trade_quality_score > 0.0


def test_two_cent_tail_is_structurally_blocked_by_relative_spread():
    """A 1c spread on a 2c contract means exit starts at -50% ROI; never enter."""

    market = _bin(
        "66° to 67°",
        yes_bid=0.01,
        yes_ask=0.02,
        yes_bid_size=80.0,
        yes_ask_size=100.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.18,
        lower_confidence=0.13,
        empirical_probability=0.18,
        normal_probability=0.18,
        effective_n=250,
        residual_probability=0.14,
        ensemble_probability=0.12,
        model_probability=0.10,
        market_probability=0.04,
    )
    decision = TradeEvaluator(StrategyConfig()).evaluate_market(market, probability, bankroll=350)
    assert not decision.approved
    assert any("stop band" in reason for reason in decision.reasons)


def test_live_admits_mid_ladder_cheap_tail_that_strict_baseline_rejects():
    # FAVORITE-BAND REORIENTATION (2026-07-06): live now rejects cheap tails
    # outright (documented FLB: longshots lose post-fee), so the cheap-tail
    # bid-size loosening is asserted with the band disabled -- the machinery
    # must stay correct for the research collector that still runs it.
    market = _bin(
        "66° to 67°",
        yes_bid=0.04,
        yes_ask=0.05,
        yes_bid_size=12.0,
        yes_ask_size=100.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.20,
        lower_confidence=0.14,
        empirical_probability=0.20,
        normal_probability=0.20,
        effective_n=250,
        residual_probability=0.16,
        ensemble_probability=0.12,
        model_probability=0.10,
        market_probability=0.05,
    )
    strict = TradeEvaluator(StrategyConfig())
    live_config = strategy_config_for_profile("live")
    live = TradeEvaluator(live_config)
    banded = live.evaluate_market(market, probability, bankroll=350)
    assert not banded.approved
    assert any("favorite band" in reason for reason in banded.reasons)
    unbanded = TradeEvaluator(replace(live_config, favorite_band_enabled=False))
    assert not strict.evaluate_market(market, probability, bankroll=350).approved
    assert unbanded.evaluate_market(market, probability, bankroll=350).approved


def test_edge_gate_can_measure_point_edge_against_model_probability():
    # On a liquid market the blended posterior is dragged toward the book, which
    # erases the model's disagreement before the point-edge gate sees it. With
    # edge_gate_uses_model_probability the point edge is measured against the
    # model probability instead, so the disagreement survives; the lower-bound
    # edge stays on the conservative blended band either way.
    market = _bin(
        "66° to 67°",
        yes_bid=0.14,
        yes_ask=0.15,
        yes_bid_size=50.0,
        yes_ask_size=50.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.18,
        lower_confidence=0.12,
        empirical_probability=0.18,
        normal_probability=0.18,
        effective_n=250,
        model_probability=0.30,
        market_probability=0.10,
    )
    blended = TradeEvaluator(StrategyConfig(edge_gate_uses_model_probability=False))
    on_model = TradeEvaluator(StrategyConfig(edge_gate_uses_model_probability=True))
    d_blended = blended.evaluate_market(market, probability, bankroll=350)
    d_model = on_model.evaluate_market(market, probability, bankroll=350)
    assert d_model.edge > d_blended.edge
    # Lower-bound edge (the safety floor) is unchanged by the flag.
    assert abs(d_model.edge_lcb - d_blended.edge_lcb) < 1e-9


def test_live_still_rejects_negative_lower_bound_edge():
    # The loosening must not reopen the proven failure mode: a trade whose
    # lower-confidence edge is negative is still refused under balanced.
    market = _bin(
        "66° to 67°",
        yes_bid=0.39,
        yes_ask=0.40,
        yes_bid_size=50.0,
        yes_ask_size=50.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.44,
        lower_confidence=0.34,
        empirical_probability=0.44,
        normal_probability=0.44,
        effective_n=250,
        model_probability=0.45,
        market_probability=0.40,
    )
    balanced = TradeEvaluator(strategy_config_for_profile("live"))
    decision = balanced.evaluate_market(market, probability, bankroll=350)
    assert not decision.approved
    assert any("lower-bound edge" in reason for reason in decision.reasons)


def test_model_market_gap_blocks_disagreement_trade():
    market = _bin(
        "66° to 67°",
        yes_bid=0.05,
        yes_ask=0.06,
        yes_bid_size=20.0,
        yes_ask_size=20.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.25,
        lower_confidence=0.20,
        empirical_probability=0.25,
        normal_probability=0.25,
        effective_n=250,
        model_probability=0.40,
        market_probability=0.05,
    )
    decision = TradeEvaluator(StrategyConfig()).evaluate_market(market, probability, bankroll=350)
    assert not decision.approved
    assert any("model/market gap" in reason for reason in decision.reasons)


def test_spread_at_configured_max_is_not_rejected_by_float_noise():
    market = _bin(
        "66° to 67°",
        yes_bid=0.86,
        yes_ask=0.92,
        yes_bid_size=20.0,
        yes_ask_size=20.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.96,
        lower_confidence=0.94,
        empirical_probability=0.96,
        normal_probability=0.96,
        effective_n=250,
        model_probability=0.96,
        market_probability=0.94,
    )
    decision = TradeEvaluator(StrategyConfig()).evaluate_market(market, probability, bankroll=350)
    assert not any("spread" in reason for reason in decision.reasons)


def test_buy_no_uses_complement_probability_and_no_book():
    market = _bin(
        "68° to 69°",
        yes_bid=0.29,
        yes_ask=0.31,
        no_bid=0.69,
        no_ask=0.71,
        yes_bid_size=80.0,
        yes_ask_size=100.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.15,
        lower_confidence=0.10,
        empirical_probability=0.15,
        normal_probability=0.15,
        effective_n=250,
        residual_probability=0.14,
        ensemble_probability=0.17,
        model_probability=0.16,
        market_probability=0.15,
    )
    decision = TradeEvaluator(StrategyConfig()).evaluate_market(market, probability, bankroll=350, side="NO")
    assert decision.approved
    assert decision.action == "BUY_NO"
    assert decision.side == "NO"
    assert decision.probability == 0.85
    assert round(decision.probability_lcb, 2) == 0.80
    assert decision.bid == 0.69
    assert decision.ask == 0.71
    assert decision.bid_size == 100.0
    assert decision.ask_size == 80.0


def test_live_profile_rejects_negative_lcb_penny_tail():
    """Jun 2026 live record: 103 sub-5c entries with avg modeled p of 8.7%
    won 1.9% of the time. Balanced must reject headline-edge-only tails."""

    market = _bin(
        "66° to 67°",
        yes_bid=0.03,
        yes_ask=0.04,
        yes_bid_size=20.0,
        yes_ask_size=20.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.09,
        lower_confidence=0.001,
        empirical_probability=0.09,
        normal_probability=0.09,
        effective_n=250,
        residual_probability=0.12,
        ensemble_probability=0.07,
        model_probability=0.09,
        market_probability=0.04,
    )
    decision = TradeEvaluator(strategy_config_for_profile("live")).evaluate_market(
        market,
        probability,
        bankroll=1000,
    )
    assert not decision.approved
    assert any("lower-bound edge" in reason for reason in decision.reasons)


def test_live_profile_approves_structurally_sound_value_trade():
    # In-band favorite (>=70c): the shape of trade the reoriented live book
    # exists to take.
    market = _bin(
        "68° to 69°",
        yes_bid=0.72,
        yes_ask=0.74,
        yes_bid_size=40.0,
        yes_ask_size=40.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.85,
        lower_confidence=0.80,
        empirical_probability=0.85,
        normal_probability=0.85,
        effective_n=400,
        residual_probability=0.84,
        ensemble_probability=0.86,
        model_probability=0.86,
        market_probability=0.73,
    )
    decision = TradeEvaluator(strategy_config_for_profile("live")).evaluate_market(
        market,
        probability,
        bankroll=1000,
    )
    assert decision.approved
    assert decision.recommended_contracts >= 1.0
    assert decision.recommended_contracts == int(decision.recommended_contracts)
    assert decision.trade_quality_score > 0.0


def test_default_profile_is_balanced_for_paper_research():
    previous = os.environ.pop("PAPER_RISK_PROFILE", None)
    try:
        default = strategy_config_for_profile()
        balanced = strategy_config_for_profile("live")
    finally:
        if previous is not None:
            os.environ["PAPER_RISK_PROFILE"] = previous

    assert default.min_edge == balanced.min_edge
    assert default.kelly_lcb_weight == balanced.kelly_lcb_weight
    assert default.cheap_tail_min_edge_lcb == balanced.cheap_tail_min_edge_lcb


def test_strict_and_live_reject_wide_relative_spread_penny_tail():
    market = _bin(
        "66° to 67°",
        yes_bid=0.01,
        yes_ask=0.02,
        yes_bid_size=12.0,
        yes_ask_size=20.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.12,
        lower_confidence=0.08,
        empirical_probability=0.12,
        normal_probability=0.12,
        effective_n=250,
        residual_probability=0.10,
        ensemble_probability=0.06,
        model_probability=0.09,
        market_probability=0.02,
    )

    # The strict baseline and live both reject a 1-2c tail whose 50%-of-cost
    # spread leaves no exit support. The research collector intentionally runs a
    # looser spread-fraction gate (0.50) to gather these, so it is excluded here.
    strict = TradeEvaluator(StrategyConfig()).evaluate_market(market, probability, bankroll=1000)
    live = TradeEvaluator(strategy_config_for_profile("live")).evaluate_market(
        market, probability, bankroll=1000
    )
    assert not strict.approved
    assert not live.approved


def test_research_profile_is_more_active_but_smaller_sized_than_live():
    # The single merged collector: loosest gates so it approves the widest set,
    # smallest size so a bad idea stays tiny. All the legacy collector aliases
    # resolve to this one config.
    live = strategy_config_for_profile("live")
    research = strategy_config_for_profile("research")

    # Looser gates than live.
    assert research.min_edge < live.min_edge
    assert research.min_edge_lcb < live.min_edge_lcb
    assert research.max_source_spread_f == 10.0
    assert research.comfort_edge_enabled is False  # collects center bins too
    # Smaller size than live.
    assert research.max_contracts_per_market < live.max_contracts_per_market
    assert research.max_position_risk_pct < live.max_position_risk_pct
    assert research.kelly_lcb_weight == 0.5

    # Every legacy collector alias maps onto the same research config.
    for alias in ("exploratory", "fast-feedback", "fast"):
        assert strategy_config_for_profile(alias).min_edge == research.min_edge
        assert (
            strategy_config_for_profile(alias).max_contracts_per_market
            == research.max_contracts_per_market
        )


def test_research_can_collect_raw_edge_trade_rejected_by_live_lcb():
    market = _bin(
        "68° to 69°",
        yes_bid=0.29,
        yes_ask=0.31,
        yes_bid_size=40.0,
        yes_ask_size=40.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.40,
        lower_confidence=0.305,
        empirical_probability=0.40,
        normal_probability=0.40,
        effective_n=220,
        residual_probability=0.40,
        ensemble_probability=0.39,
        model_probability=0.40,
        market_probability=0.30,
    )

    balanced = TradeEvaluator(strategy_config_for_profile("live")).evaluate_market(
        market,
        probability,
        bankroll=1000,
    )
    fast = TradeEvaluator(strategy_config_for_profile("research")).evaluate_market(
        market,
        probability,
        bankroll=1000,
    )

    assert not balanced.approved
    assert any("lower-bound edge" in reason for reason in balanced.reasons)
    assert fast.approved
    # Meaningful-stake retune (2026-06-17): fast-feedback is no longer pocket
    # change. It still sizes below balanced (cap 25 contracts) but takes a real
    # position rather than ~1-3 contracts.
    assert 1.0 <= fast.recommended_contracts <= 25.0


def test_research_collects_tiny_trade_on_moderate_source_disagreement():
    market = _bin(
        "74° or above",
        yes_bid=0.47,
        yes_ask=0.48,
        no_bid=0.52,
        no_ask=0.53,
        yes_bid_size=40.0,
        yes_ask_size=40.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.38,
        lower_confidence=0.35,
        empirical_probability=0.38,
        normal_probability=0.38,
        effective_n=180,
        residual_probability=0.38,
        ensemble_probability=0.39,
        model_probability=0.38,
        market_probability=0.4460,
    )

    decision = TradeEvaluator(strategy_config_for_profile("research")).evaluate_market(
        market,
        probability,
        bankroll=1000,
        side="NO",
        source_spread_f=9.6,
    )

    assert decision.approved, decision.reasons
    # Bounded by fast-feedback's per-position cap (25 contracts), not the old
    # ~3-contract pocket-change size (2026-06-17 meaningful-stake retune).
    assert decision.recommended_contracts <= 25.0


def test_research_blocks_deep_negative_lcb_research_trade():
    market = _bin(
        "74° or above",
        yes_bid=0.47,
        yes_ask=0.48,
        no_bid=0.52,
        no_ask=0.53,
        yes_bid_size=40.0,
        yes_ask_size=40.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.3216,
        lower_confidence=0.0402,
        empirical_probability=0.3216,
        normal_probability=0.3216,
        effective_n=180,
        residual_probability=0.1295,
        ensemble_probability=0.0,
        model_probability=0.0907,
        market_probability=0.4460,
    )

    decision = TradeEvaluator(strategy_config_for_profile("research")).evaluate_market(
        market,
        probability,
        bankroll=1000,
        side="NO",
        source_spread_f=9.6,
    )

    assert not decision.approved
    assert any("lower-bound edge" in reason for reason in decision.reasons)


def test_research_still_blocks_extreme_source_disagreement():
    market = _bin(
        "74° or above",
        yes_bid=0.47,
        yes_ask=0.48,
        no_bid=0.52,
        no_ask=0.53,
        yes_bid_size=40.0,
        yes_ask_size=40.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.3216,
        lower_confidence=0.0402,
        empirical_probability=0.3216,
        normal_probability=0.3216,
        effective_n=180,
        residual_probability=0.1295,
        ensemble_probability=0.0,
        model_probability=0.0907,
        market_probability=0.4460,
    )

    decision = TradeEvaluator(strategy_config_for_profile("research")).evaluate_market(
        market,
        probability,
        bankroll=1000,
        side="NO",
        source_spread_f=10.1,
    )

    assert not decision.approved
    assert any("source spread" in reason for reason in decision.reasons)


def test_research_cheap_tail_requires_positive_lcb_after_tightening():
    market = _bin(
        "66° to 67°",
        yes_bid=0.03,
        yes_ask=0.04,
        yes_bid_size=10.0,
        yes_ask_size=50.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.09,
        lower_confidence=0.04,
        empirical_probability=0.09,
        normal_probability=0.09,
        effective_n=250,
        residual_probability=0.09,
        ensemble_probability=0.06,
        model_probability=0.09,
        market_probability=0.04,
    )

    decision = TradeEvaluator(strategy_config_for_profile("research")).evaluate_market(
        market,
        probability,
        bankroll=1000,
    )

    assert not decision.approved
    assert any("1c/2c tail requires exceptional support" in reason for reason in decision.reasons)


def test_event_risk_cap_scales_total_approved_exposure():
    markets = [
        _bin(
            "66° to 67°",
            yes_bid=0.09,
            yes_ask=0.10,
            yes_bid_size=5000.0,
            yes_ask_size=5000.0,
        ),
        _bin(
            "68° to 69°",
            yes_bid=0.09,
            yes_ask=0.10,
            yes_bid_size=5000.0,
            yes_ask_size=5000.0,
        ),
    ]
    probabilities = {
        market.ticker: BucketProbability(
            ticker=market.ticker,
            label=market.yes_sub_title,
            probability=0.80,
            lower_confidence=0.75,
            empirical_probability=0.80,
            normal_probability=0.80,
            effective_n=250,
            model_probability=0.80,
            market_probability=0.10,
        )
        for market in markets
    }
    config = StrategyConfig(
        min_edge=0.0,
        min_edge_lcb=0.0,
        max_model_market_gap=1.0,
        max_position_risk_pct=0.50,
        max_event_risk_pct=0.01,
        max_contracts_per_market=5000.0,
    )

    decisions = TradeEvaluator(config).rank(markets, probabilities, bankroll=1000.0)

    approved = [decision for decision in decisions if decision.approved]
    spend = sum(decision.recommended_contracts * decision.cost_per_contract for decision in approved)
    assert len(approved) == 2
    assert spend <= 10.0 + 1e-9
    assert spend > 9.99


def test_live_blocks_trade_when_forecast_sources_disagree():
    market = _bin(
        "72° to 73°",
        yes_bid=0.79,
        yes_ask=0.80,
        yes_bid_size=40.0,
        yes_ask_size=40.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.90,
        lower_confidence=0.86,
        empirical_probability=0.90,
        normal_probability=0.90,
        effective_n=250,
        model_probability=0.91,
        market_probability=0.80,
    )
    config = strategy_config_for_profile("live")
    evaluator = TradeEvaluator(config)

    calm = evaluator.evaluate_market(market, probability, bankroll=1000, source_spread_f=5.0)
    assert calm.approved, calm.reasons

    # Frequency push (2026-07-03): live now trades higher-disagreement days too
    # (max_source_spread_f 7->10, matching research), sized down by posterior-mean
    # Kelly + the source-spread sigma inflation. 2026-06-10 caveat still stands --
    # losing entries carried 9.6-11F spread -- so only spreads ABOVE the 10F bar
    # are blocked now; validate the win-rate impact on a walk-forward.
    moderate = evaluator.evaluate_market(market, probability, bankroll=1000, source_spread_f=9.6)
    assert moderate.approved, moderate.reasons
    stormy = evaluator.evaluate_market(market, probability, bankroll=1000, source_spread_f=10.5)
    assert not stormy.approved
    assert any("source spread" in reason for reason in stormy.reasons)

    fast = TradeEvaluator(strategy_config_for_profile("research"))
    research = fast.evaluate_market(market, probability, bankroll=1000, source_spread_f=9.6)
    assert research.approved, research.reasons
    extreme = fast.evaluate_market(market, probability, bankroll=1000, source_spread_f=10.1)
    assert not extreme.approved


def test_research_allows_reentry_while_live_is_one_and_done():
    # The research collector allows re-entry after a close (lifetime cap 3) to
    # raise paper-trade turnover; the real-money-intent live profile stays
    # one-and-done so a closed position is never re-bought.
    assert strategy_config_for_profile("live").max_entries_per_market_side == 1
    assert strategy_config_for_profile("research").max_entries_per_market_side == 3


def test_research_candidate_mode_skips_only_edge_and_sizing_gates() -> None:
    market = _bin(
        "72° to 73°",
        yes_bid=0.79,
        yes_ask=0.80,
        yes_bid_size=40.0,
        yes_ask_size=40.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.81,
        lower_confidence=0.70,
        empirical_probability=0.81,
        normal_probability=0.81,
        effective_n=250,
        model_probability=0.813,
        market_probability=0.80,
    )
    evaluator = TradeEvaluator(strategy_config_for_profile("research"))

    legacy = evaluator.evaluate_market(market, probability, bankroll=1000.0)
    candidate = evaluator.evaluate_market(
        market,
        probability,
        bankroll=1000.0,
        candidate_only=True,
    )
    closed = evaluator.evaluate_market(
        replace(market, status="closed"),
        probability,
        bankroll=1000.0,
        candidate_only=True,
    )

    assert legacy.approved is False
    assert candidate.approved is True
    assert candidate.recommended_contracts == 25.0
    assert closed.approved is False
    assert any("market status" in reason for reason in closed.reasons)


def test_default_false_candidate_mode_is_live_byte_for_byte_noop() -> None:
    market = _bin(
        "72° to 73°",
        yes_bid=0.79,
        yes_ask=0.80,
        yes_bid_size=40.0,
        yes_ask_size=40.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.90,
        lower_confidence=0.86,
        empirical_probability=0.90,
        normal_probability=0.90,
        effective_n=250,
        model_probability=0.91,
        market_probability=0.80,
    )
    evaluator = TradeEvaluator(strategy_config_for_profile("live"))

    assert evaluator.evaluate_market(
        market, probability, bankroll=1000.0
    ) == evaluator.evaluate_market(
        market, probability, bankroll=1000.0, candidate_only=False
    )


def test_live_regime_gate_and_strict_floors_unchanged_by_frequency_retune():
    # The frequency work must not touch the real-money-intent profile.
    balanced = strategy_config_for_profile("live")
    assert balanced.blocked_forecast_cohorts  # warm/hot still blocked
    assert balanced.min_edge_lcb == 0.0  # proven LCB floor intact
    assert balanced.max_spread == 0.07


def test_live_deploys_meaningful_stake_not_pocket_change():
    # Meaningful-stake retune (2026-06-17): on a $1000 paper book the engine must
    # deploy a real Kelly-sized fraction per favorite (~5%/position), not the
    # ~$2-20 pocket change that left the equity inert. Locks the retune against a
    # regression back to cents.
    market = _bin(
        "66° to 67°",
        yes_bid=0.12,
        yes_ask=0.14,
        no_bid=0.86,
        no_ask=0.88,
        yes_bid_size=200.0,
        yes_ask_size=200.0,
    )
    probability = BucketProbability(
        ticker=market.ticker,
        label=market.yes_sub_title,
        probability=0.06,
        lower_confidence=0.045,
        empirical_probability=0.06,
        normal_probability=0.06,
        effective_n=180,
        residual_probability=0.06,
        ensemble_probability=0.06,
        model_probability=0.06,
        market_probability=0.12,
    )
    cfg = strategy_config_for_profile("live")
    decision = TradeEvaluator(cfg).evaluate_market(
        market, probability, bankroll=1000.0, side="NO"
    )
    assert decision.approved, decision.reasons
    spend = decision.recommended_contracts * decision.cost_per_contract
    # A meaningful stake, not pocket change.
    assert spend >= 30.0
    # ...but still bounded by the per-position risk budget (~$50 = 5% of $1000).
    assert spend <= 1000 * cfg.max_position_risk_pct + decision.cost_per_contract
