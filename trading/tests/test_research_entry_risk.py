from __future__ import annotations

from dataclasses import replace
import math

import pytest

from sfo_kalshi_quant.account import strategy_fingerprint
from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.research_entry_risk import (
    target_entry_spend_limit,
    target_remaining_daily_risk,
)
from sfo_kalshi_quant.research_policy import TARGET_POLICY
from sfo_kalshi_quant.research_portfolio import ResearchOpportunity, allocate_research_plans
from test_target_growth_v2 import _structural_target_candidate


@pytest.mark.parametrize(
    "cost,lcb,contracts",
    [(0.68, 0.7636, 96), (0.94, 0.9435, 15), (0.80, 0.8207, 32)],
)
def test_incident_inputs_have_conservative_entry_ceilings(cost, lcb, contracts):
    # Public September 12 incident inputs. This proves sizing, not P&L replay.
    candidate = replace(
        _structural_target_candidate(bid=cost - 0.01, ask=cost + 0.01, ask_size=100),
        cost_per_contract=cost,
        probability_lcb=lcb,
        edge_lcb=lcb - cost,
        probability=max(lcb, 0.95),
        edge=max(lcb, 0.95) - cost,
    )
    plan = allocate_research_plans([ResearchOpportunity(candidate, "2026-07-30", 1)])
    assert plan.target.legs[0].decision.recommended_contracts == contracts
    assert plan.target.legs[0].spend <= 90.0
    assert (contracts + 1) * cost > target_entry_spend_limit(cost, lcb)


@pytest.mark.parametrize("bad", [None, True, "0.9", float("nan"), float("inf"), -0.1, 1.1])
def test_malformed_entry_evidence_fails_closed(bad):
    assert target_entry_spend_limit(bad, 0.95) == 0.0
    assert target_entry_spend_limit(0.8, bad) == 0.0


def test_fee_cost_reduces_conservative_size_and_no_edge_cannot_fund_risk():
    assert target_entry_spend_limit(0.94, 0.944) > target_entry_spend_limit(0.942, 0.944)
    assert target_entry_spend_limit(0.944, 0.944) == 0.0
    assert target_entry_spend_limit(0.945, 0.944) == 0.0
    assert target_entry_spend_limit(1.0, 1.0) == 0.0


def test_strong_conservative_edge_can_scale_up_to_existing_production_ceiling():
    assert target_entry_spend_limit(0.75, 0.90) == 90.0
    assert target_entry_spend_limit(0.94, 0.9435) == pytest.approx(14.5833333333333)
    assert target_entry_spend_limit(0.87, 0.8984) == pytest.approx(54.6153846153846)


def test_daily_budget_charges_other_target_dates_and_cannot_recycle_losses():
    assert target_remaining_daily_risk(0.0, 130.0) == 20.0
    assert target_remaining_daily_risk(-50.0, 130.0) == 0.0
    assert target_remaining_daily_risk(-50.0, 90.0) == 10.0
    assert target_remaining_daily_risk(40.0, 130.0) == 20.0
    assert target_remaining_daily_risk(0.0, math.inf) == 0.0
    assert target_remaining_daily_risk(float("nan"), 0.0) == 0.0
    assert target_remaining_daily_risk(0.0, -1.0) == 0.0


def test_only_research_execution_fingerprint_changes_and_fixed_goal_is_preserved():
    live = strategy_config_for_profile("live")
    research = strategy_config_for_profile("research")
    # The live fingerprint must match the behaviour-v3 pin in
    # test_research_sleeves.py: the research entry-risk identity added
    # here never reaches the live book. Before the 2026-09-07 rotation of
    # STRATEGY_BEHAVIOR_VERSION the same two values were
    # 88e417a64d8be9b1bb933b3b / 92934c133d00d85deb078b3c.
    assert strategy_fingerprint(live, entry_mode="limit") == "93326de538852004fc08aa99"
    # Research without research_entry_risk_version hashes to
    # 4d812e63727caf1b3de6b127 under behaviour-v3; with it, the research
    # identity moves. Under research-entry-risk-v2-scaled-2026-09-12 it was
    # 69a29d415bad6047b03264be; the v3 scaling release (2026-09-13: 30-minute
    # day-ahead rest) moves it again while the live pin above stays put.
    assert strategy_fingerprint(research, entry_mode="limit") != "4d812e63727caf1b3de6b127"
    assert strategy_fingerprint(research, entry_mode="limit") != "69a29d415bad6047b03264be"
    assert strategy_fingerprint(research, entry_mode="limit") == "40806599ad7acb21eab44c8d"
    assert TARGET_POLICY.policy_fingerprint == "0fd9cc8ebf877a653806fe1a"
    assert TARGET_POLICY.reference_equity == 1000.0
    assert TARGET_POLICY.target_pnl == 50.0


def test_explicit_research_identity_versions_custom_and_city_configurations():
    from sfo_kalshi_quant.cities import CITIES
    from sfo_kalshi_quant.config import config_for_city

    research = strategy_config_for_profile("research")
    for city in CITIES:
        config = replace(config_for_city(research, city), min_edge=0.123)
        assert strategy_fingerprint(config, entry_mode="limit", risk_profile="research") != (
            strategy_fingerprint(config, entry_mode="limit", risk_profile="live")
        )
