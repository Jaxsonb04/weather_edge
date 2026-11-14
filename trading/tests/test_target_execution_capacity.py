"""Taker-only target quotes and pre-allocation taker capacity."""

from dataclasses import replace

import pytest

from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.execution import target_research_quote
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


@pytest.mark.parametrize("lcb", [0.885, 0.875])
def test_research_profile_drops_thin_edge_instead_of_resting(lcb):
    # The taker cost at the 0.90 ask fails the zero LCB floor. The v8 book
    # (2026-11-12) never rests a maker quote, so the candidate is dropped.
    config = strategy_config_for_profile("research")
    assert config.research_target_taker_cross is True
    decision = _candidate(entry_bid=0.88, entry_ask=0.90, probability_lcb=lcb)
    assert target_research_quote(decision, config) is None
    assert with_target_research_execution(decision, config) is None
    prepared = prepare_research_target_decisions([decision], config)[0]
    assert not prepared.approved


@pytest.mark.parametrize(
    "lcb,ask_size,expected_price",
    [
        # LCB clears the taker cost at the displayed ask: cross.
        (0.93, 200.0, 0.90),
        # No displayed depth to cross: nothing, never a resting bid.
        (0.93, 0.0, None),
        # LCB clears bid+1 but not the taker cost: nothing.
        (0.90, 200.0, None),
    ],
)
def test_research_profile_quotes_only_at_the_displayed_ask(lcb, ask_size, expected_price):
    config = strategy_config_for_profile("research")
    decision = _candidate(
        entry_bid=0.88, entry_ask=0.90, probability_lcb=lcb, entry_ask_size=ask_size
    )
    quote = target_research_quote(decision, config)
    if expected_price is None:
        assert quote is None
        return
    assert quote is not None
    assert quote.would_cross is True
    assert quote.price == expected_price
    assert quote.edge_lcb >= 0.0


def test_target_quote_does_not_change_disabled_or_rejected_paths():
    config = strategy_config_for_profile("research")
    decision = _candidate(entry_bid=0.88, entry_ask=0.90, probability_lcb=0.95)
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


def test_thin_structural_source_is_dropped_not_rested():
    # One 76c contract is below the $1 taker minimum and there is no maker path.
    prepared = prepare_research_target_decisions(
        [_candidate(entry_ask_size=1.0)], strategy_config_for_profile("research")
    )[0]
    assert not prepared.approved
    assert prepared.limit_price is None
