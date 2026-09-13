"""Sizing clips must still fit cash and risk after integer-quantity fee rounding."""

from datetime import UTC, datetime

import pytest

from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.paper import PaperTrader, prepare_research_target_decisions, with_target_research_execution
from sfo_kalshi_quant.research_policy import TARGET_POLICY
from sfo_kalshi_quant.research_portfolio import ResearchOpportunity, allocate_research_plans
from test_target_execution_capacity import _candidate
from test_research_sleeves import _insert_research_order


@pytest.mark.parametrize("constraint", ["cash", "daily"])
def test_expanded_taker_clip_reprices_fees_before_comparing_budget(constraint):
    config = strategy_config_for_profile("research")
    source = prepare_research_target_decisions([_candidate()], config)[0]
    budget = source.cost_per_contract * 112
    kwargs = {"target_available_cash": budget} if constraint == "cash" else {"realized_today": -(150.0 - budget)}
    plan = allocate_research_plans(
        [ResearchOpportunity(source, "2026-09-12", 1)], motion_opportunities=[], **kwargs,
    )
    chosen = plan.target.legs[0].decision
    final = with_target_research_execution(chosen, config)
    assert final is not None
    assert chosen.recommended_contracts == 111.0
    assert final.cost_per_contract == chosen.cost_per_contract
    assert final.fee_per_contract == chosen.fee_per_contract
    assert final.recommended_contracts * final.cost_per_contract <= budget
    assert plan.target.legs[0].spend == pytest.approx(85.78)


def test_expanded_taker_cash_clip_cannot_change_to_a_resting_order():
    config = strategy_config_for_profile("research")
    source = prepare_research_target_decisions([_candidate()], config)[0]
    plan = allocate_research_plans(
        [ResearchOpportunity(source, "2026-09-12", 1)], motion_opportunities=[],
        target_available_cash=0.80,
    )
    # A single contract cannot clear the $1 taker minimum. Keeping the
    # pre-allocation execution mode prevents changed reservation semantics.
    assert not plan.target.legs


def test_exact_fee_clip_is_admitted_with_fractional_existing_risk(tmp_path):
    config = strategy_config_for_profile("research")
    source = prepare_research_target_decisions([_candidate()], config)[0]
    budget = source.cost_per_contract * 112
    prior_cost = 150.0 - budget
    store = PaperStore(
        tmp_path / "rounded-fee-capacity.db",
        research_clock=lambda: datetime(2026, 7, 25, 20, tzinfo=UTC),
    )
    # Historical maker fills can be fractional, leaving non-cent risk room.
    with store.connect() as conn:
        prior_id = _insert_research_order(
            conn, ticker="KXHIGHDEN-26JUL26-B80.5", account_id=TARGET_POLICY.account_id,
            sleeve=TARGET_POLICY.sleeve.value, policy_version=TARGET_POLICY.policy_version,
            policy_fingerprint=TARGET_POLICY.policy_fingerprint,
        )
        conn.execute(
            "UPDATE paper_orders SET contracts=?,cost_per_contract=0.8,fee_per_contract=0,"
            "entry_price=0.8 WHERE id=?", (prior_cost / 0.8, prior_id),
        )
        store._record_ledger_event(
            conn, account_id=TARGET_POLICY.account_id, order_id=prior_id,
            event_type="ENTRY_FILL", amount=-prior_cost,
            idempotency_key="test:fractional-prior-entry", details={},
        )
    plan = allocate_research_plans(
        [ResearchOpportunity(source, "2026-07-30", 1)], motion_opportunities=[],
        target_available_cash=budget, run_id="exact-rounded-fee",
    )
    result = PaperTrader(store, config, risk_profile="research", entry_mode="limit").execute_research_plans(
        "2026-07-30", plan, source_decisions=[source], objective_day="2026-07-25",
        lead_bucket="day-ahead", scan_run_id="exact-rounded-fee",
        observed_high_state="complete=0;high=unavailable",
    )
    assert len(result.target_order_ids) == 1
    order = store.paper_order(result.target_order_ids[0])
    assert order["contracts"] == 111.0
    assert order["status"] == "PAPER_FILLED"
    assert store.research_open_risk(account_id=TARGET_POLICY.account_id) <= 150.0
