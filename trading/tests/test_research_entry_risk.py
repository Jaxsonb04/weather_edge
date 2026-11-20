from __future__ import annotations

from dataclasses import replace
import math

import pytest

from sfo_kalshi_quant.account import strategy_fingerprint
from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.research_entry_risk import (
    RESEARCH_ENTRY_RISK_VERSION,
    TARGET_ENTRY_FULL_LOSS_CAP,
    TARGET_OPEN_RISK_CHARGE_FRACTION,
    target_entry_spend_limit,
    target_remaining_daily_risk,
)
from sfo_kalshi_quant.research_policy import TARGET_POLICY
from sfo_kalshi_quant.research_portfolio import ResearchOpportunity, allocate_research_plans
from test_target_growth_v2 import _structural_target_candidate


@pytest.mark.parametrize("cost,lcb", [(0.68, 0.7636), (0.94, 0.9435), (0.80, 0.8207)])
def test_entries_spend_a_flat_ninety_dollars_whatever_the_edge(cost, lcb):
    # v8 (2026-11-12) dropped the quarter-Kelly term: any funded leg spends up
    # to the $90 position cap, limited only by displayed depth and the caps.
    candidate = replace(
        _structural_target_candidate(bid=cost - 0.01, ask=cost + 0.01, ask_size=1000),
        cost_per_contract=cost,
        probability_lcb=lcb,
        edge_lcb=lcb - cost,
        probability=max(lcb, 0.95),
        edge=max(lcb, 0.95) - cost,
    )
    plan = allocate_research_plans([ResearchOpportunity(candidate, "2026-07-30", 1)])
    leg = plan.target.legs[0]
    assert leg.spend <= 90.0
    assert leg.spend + leg.decision.cost_per_contract > 90.0
    assert target_entry_spend_limit(cost, lcb) == 90.0


@pytest.mark.parametrize("bad", [None, True, "0.9", float("nan"), float("inf"), -0.1, 1.1])
def test_malformed_entry_evidence_fails_closed(bad):
    assert target_entry_spend_limit(bad, 0.95) == 0.0
    assert target_entry_spend_limit(0.8, bad) == 0.0


def test_spend_is_flat_once_the_lcb_covers_cost_and_zero_otherwise():
    # The decision LCB already carries the one-cent entry edge, so LCB == cost
    # is exactly p - cost == 0.01 and funds the full $90.
    assert target_entry_spend_limit(0.944, 0.944) == TARGET_ENTRY_FULL_LOSS_CAP
    assert target_entry_spend_limit(0.94, 0.9435) == TARGET_ENTRY_FULL_LOSS_CAP
    assert target_entry_spend_limit(0.945, 0.944) == 0.0
    assert target_entry_spend_limit(1.0, 1.0) == 0.0
    assert TARGET_ENTRY_FULL_LOSS_CAP == (
        TARGET_POLICY.reference_equity * TARGET_POLICY.max_position_risk_pct
    ) == 90.0


def test_daily_room_is_the_realized_pause_and_open_risk_does_not_consume_it():
    # v8 holds to settlement, so open cost is not charged against the $500
    # (25%) budget; only booked losses shrink it, and profits never grow it.
    assert TARGET_OPEN_RISK_CHARGE_FRACTION == 0.0
    assert RESEARCH_ENTRY_RISK_VERSION == "research-entry-risk-v4-market-signal-2026-11-12"
    assert target_remaining_daily_risk(0.0, 260.0) == pytest.approx(500.0)
    assert target_remaining_daily_risk(0.0, 1500.0) == pytest.approx(500.0)
    assert target_remaining_daily_risk(-100.0, 260.0) == pytest.approx(400.0)
    assert target_remaining_daily_risk(80.0, 260.0) == pytest.approx(500.0)
    assert target_remaining_daily_risk(-500.0, 0.0) == 0.0
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
    # 88e417a64d8be9b1bb933b3b / 92934c133d00d85deb078b3c; under behaviour-v3
    # before the 2026-09-13 `limit_taker_cross_max_levels` field it was
    # 93326de538852004fc08aa99.
    assert strategy_fingerprint(live, entry_mode="limit") == "9725984064a55081065c6313"
    # Research without research_entry_risk_version hashes to
    # 4d812e63727caf1b3de6b127 under behaviour-v3; with it, the research
    # identity moves. Under research-entry-risk-v2-scaled-2026-09-12 (PR #121)
    # it was 69a29d415bad6047b03264be; the v3 scaling release (2026-09-13:
    # stop-scaled daily open-risk charge, no behind-bid reservation, 30-minute
    # day-ahead rest) moves it again while the live pin above stays put.
    assert strategy_fingerprint(research, entry_mode="limit") != "4d812e63727caf1b3de6b127"
    assert strategy_fingerprint(research, entry_mode="limit") != "69a29d415bad6047b03264be"
    # Two 2026-09-13 inputs move the research identity, and this release ships
    # both in one deploy. The v3 version alone gave 40806599ad7acb21eab44c8d;
    # the `limit_taker_cross_max_levels` config field alone (research pins it
    # to 1, but the key itself is hashed: a labelling change, no research
    # behaviour change) gave 0db3fd6d76056f7f7b141ff2. Together, recomputed
    # from strategy_config_for_profile("research") at integration. The current
    # pins also include the November 7 $2,000 paper_bankroll decision:
    assert strategy_fingerprint(research, entry_mode="limit") != "40806599ad7acb21eab44c8d"
    assert strategy_fingerprint(research, entry_mode="limit") != "0db3fd6d76056f7f7b141ff2"
    assert strategy_fingerprint(research, entry_mode="limit") == "282324065b36575cb6232ebe"
    assert TARGET_POLICY.policy_fingerprint == "902bcfb7b6417501f3e1df8a"
    assert TARGET_POLICY.reference_equity == 2000.0
    assert TARGET_POLICY.target_pnl == 2000.0


def test_explicit_research_identity_versions_custom_and_city_configurations():
    from sfo_kalshi_quant.cities import CITIES
    from sfo_kalshi_quant.config import config_for_city

    research = strategy_config_for_profile("research")
    for city in CITIES:
        config = replace(config_for_city(research, city), min_edge=0.123)
        assert strategy_fingerprint(config, entry_mode="limit", risk_profile="research") != (
            strategy_fingerprint(config, entry_mode="limit", risk_profile="live")
        )
