"""Bounded target reservation quotes and pre-allocation taker capacity."""

from dataclasses import replace

import pytest

from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.execution import target_research_quote
from sfo_kalshi_quant.fees import quadratic_fee_average_per_contract
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.paper import (
    _prepare_research_disposition,
    prepare_research_target_decisions,
    with_target_research_execution,
)
from sfo_kalshi_quant.research_entry_risk import target_entry_spend_limit
from sfo_kalshi_quant.research_policy import TARGET_POLICY
from sfo_kalshi_quant.research_portfolio import ResearchOpportunity, allocate_research_plans


def _candidate(**changes):
    decision = TradeDecision(
        ticker="KXHIGHTSFO-26SEP12-B82.5",
        label="82° to 83°",
        action="BUY_NO",
        approved=True,
        probability=0.98,
        probability_lcb=0.95,
        model_probability=0.98,
        yes_bid=0.24,
        yes_ask=0.26,
        spread=0.02,
        fee_per_contract=0.0,
        cost_per_contract=0.76,
        edge=0.22,
        edge_lcb=0.19,
        kelly_fraction=0.0,
        recommended_contracts=25.0,
        expected_profit=5.5,
        reasons=[],
        side="NO",
        entry_bid=0.74,
        entry_ask=0.76,
        entry_bid_size=50.0,
        entry_ask_size=200.0,
        strike_type="between",
        floor_strike=82.0,
        cap_strike=83.0,
        binding_constraint="research_policy_allocator",
    )
    return replace(decision, **changes)


@pytest.mark.parametrize("lcb,expected_price", [(0.885, 0.88), (0.875, 0.87)])
def test_target_rests_at_bounded_reservation_when_inside_quote_fails(lcb, expected_price):
    decision = _candidate(entry_bid=0.88, entry_ask=0.90, probability_lcb=lcb)
    config = strategy_config_for_profile("research")
    quote = target_research_quote(decision, config)
    assert quote is not None
    assert not quote.would_cross
    assert quote.price == expected_price
    assert quote.contracts == 25.0
    assert quote.edge >= 0.0
    assert quote.edge_lcb >= 0.0


def test_target_reservation_handles_failed_natural_cross_with_maker_fee():
    decision = _candidate(entry_bid=0.89, entry_ask=0.90, probability_lcb=0.895)
    config = replace(strategy_config_for_profile("research"), maker_fee_rate=0.0175)
    quote = target_research_quote(decision, config)
    assert quote is not None
    assert not quote.would_cross
    assert quote.price == 0.89
    assert quote.fee_per_contract == quadratic_fee_average_per_contract(
        quote.price,
        quote.contracts,
        maker=True,
        fee_multiplier=config.fee_multiplier,
        maker_rate=config.maker_fee_rate,
        taker_rate=config.taker_fee_rate,
        series_ticker=decision.ticker,
    )


def test_target_reservation_skips_zero_kelly_bid_for_executable_lower_tick():
    decision = _candidate(
        entry_bid=0.96,
        entry_ask=0.98,
        probability_lcb=0.96,
        model_probability=0.99,
        probability=0.99,
    )
    prepared = prepare_research_target_decisions(
        [decision], strategy_config_for_profile("research")
    )[0]
    assert prepared.approved
    assert prepared.limit_price == 0.95
    plans = allocate_research_plans(
        [ResearchOpportunity(prepared, "2026-09-12", 1)],
        motion_opportunities=[],
    )
    assert len(plans.target.legs) == 1
    assert plans.target.legs[0].spend > 0.0
    assert plans.target.legs[0].decision.edge_lcb > 0.0


@pytest.mark.parametrize("lcb", [0.96, 0.960001])
def test_unfunded_initial_quote_tries_bounded_fallback_and_repeats_canonically(lcb):
    decision = _candidate(entry_bid=0.95, entry_ask=0.98, probability_lcb=lcb)
    config = strategy_config_for_profile("research")
    prepared = prepare_research_target_decisions([decision], config)[0]
    assert prepared.approved
    assert prepared.limit_price == 0.95
    assert prepared.limit_edge_lcb > 0.0
    final = with_target_research_execution(
        replace(prepared, recommended_contracts=7.0), config
    )
    assert final is not None
    assert final.limit_price == 0.95
    assert final.recommended_contracts == 7.0


@pytest.mark.parametrize("probability_field", ["probability_lcb", "model_probability"])
def test_target_reservation_cannot_reach_more_than_one_tick_below_bid(probability_field):
    decision = _candidate(
        entry_bid=0.88, entry_ask=0.90, **{probability_field: 0.865}
    )
    assert target_research_quote(decision, strategy_config_for_profile("research")) is None


def test_target_reservation_does_not_change_disabled_or_rejected_paths():
    config = strategy_config_for_profile("research")
    decision = _candidate(entry_bid=0.88, entry_ask=0.90, probability_lcb=0.885)
    assert target_research_quote(replace(decision, approved=False), config) is None
    assert target_research_quote(
        decision, replace(config, research_target_taker_cross=False)
    ) is None


def test_preallocation_target_taker_uses_depth_and_risk_capacity_above_structural_cap():
    config = strategy_config_for_profile("research")
    prepared = prepare_research_target_decisions([_candidate()], config)[0]
    assert prepared.approved
    assert prepared.recommended_contracts > 25.0
    assert prepared.recommended_contracts <= prepared.ask_size
    assert prepared.binding_constraint == "research_visible_ask_depth"
    quote = target_research_quote(prepared, config)
    assert quote is not None and quote.would_cross
    assert quote.contracts == prepared.recommended_contracts
    assert quote.cost_per_contract == prepared.cost_per_contract
    assert prepared.recommended_contracts * prepared.cost_per_contract <= (
        target_entry_spend_limit(prepared.cost_per_contract, prepared.probability_lcb)
        + 1e-9
    )
    # The next contract violates the independently recomputed risk budget.
    next_quote = target_research_quote(
        replace(prepared, recommended_contracts=prepared.recommended_contracts + 1.0),
        config,
    )
    assert next_quote is not None
    assert next_quote.contracts * next_quote.cost_per_contract > target_entry_spend_limit(
        next_quote.cost_per_contract, prepared.probability_lcb
    )


def test_expanded_target_never_grows_again_after_allocation():
    config = strategy_config_for_profile("research")
    prepared = prepare_research_target_decisions([_candidate()], config)[0]
    selected = replace(prepared, recommended_contracts=7.0)
    final = _prepare_research_disposition(
        selected, policy=TARGET_POLICY, selected=True, reason=None, config=config
    )
    assert final.approved
    assert final.recommended_contracts == 7.0
    assert final.binding_constraint == "research_visible_ask_depth"
    assert with_target_research_execution(selected, config).recommended_contracts == 7.0


def test_source_expansion_keeps_actual_whole_displayed_depth():
    config = strategy_config_for_profile("research")
    prepared = prepare_research_target_decisions(
        [_candidate(entry_ask_size=33.7)], config
    )[0]
    assert prepared.recommended_contracts == 33.0


@pytest.mark.parametrize(
    "decision",
    [
        _candidate(binding_constraint="max_contracts_per_market"),
        _candidate(limit_price=0.76, recommended_contracts=7.0),
        _candidate(approved=False),
    ],
)
def test_source_expansion_requires_fresh_approved_structural_origin(decision):
    prepared = prepare_research_target_decisions(
        [decision], strategy_config_for_profile("research")
    )[0]
    assert prepared.recommended_contracts <= decision.recommended_contracts


def test_resting_structural_source_retains_its_existing_allocator_path():
    prepared = prepare_research_target_decisions(
        [_candidate(entry_ask_size=1.0)], strategy_config_for_profile("research")
    )[0]
    assert prepared.approved
    assert prepared.recommended_contracts == 25.0
    assert prepared.limit_price == 0.75
    assert prepared.binding_constraint == "research_policy_allocator"
