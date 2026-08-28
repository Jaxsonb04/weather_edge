from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
import sqlite3

import pytest

from sfo_kalshi_quant.account import account_for_research_sleeve
from sfo_kalshi_quant.research_policy import (
    ALL_RESEARCH_POLICIES,
    MOTION_POLICY,
    TARGET_POLICY,
    TARGET_POLICY_V1,
    TARGET_POLICY_V2,
    TARGET_POLICY_V3,
    TARGET_POLICY_V4,
    TARGET_POLICY_V5,
    ResearchSleeve,
)
from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.paper import with_target_research_execution
from sfo_kalshi_quant.profile_identity import (
    execution_profile_key,
    published_profile_key,
)
from sfo_kalshi_quant.research_portfolio import (
    ResearchOpportunity,
    allocate_research_plans,
)


def test_target_roi_v3_becomes_active_without_rewriting_prior_identities() -> None:
    assert TARGET_POLICY_V1.account_id == "paper-research-target-v1"
    assert TARGET_POLICY_V1.policy_version == "research-target-v1"
    assert TARGET_POLICY_V1.policy_fingerprint == "dea759010dc85ca5f4f610e2"

    assert TARGET_POLICY_V2.account_id == "paper-research-target-v2"
    assert TARGET_POLICY_V2.policy_version == "research-target-growth-v2"
    assert TARGET_POLICY_V2.reference_equity == 1000.0
    assert TARGET_POLICY_V2.target_return == 0.016
    assert TARGET_POLICY_V2.target_pnl == 16.0
    assert TARGET_POLICY_V2.max_position_risk_pct == 0.06
    assert TARGET_POLICY_V2.max_city_target_risk_pct == 0.06
    assert TARGET_POLICY_V2.max_region_day_risk_pct == 0.12
    assert TARGET_POLICY_V2.max_aggregate_risk_pct == 0.25
    assert TARGET_POLICY_V2.daily_loss_pause_pct == 0.10
    assert TARGET_POLICY_V2.allocator_version == "policy-sized-v2"

    # 2026-07-31: TARGET_POLICY is v5 (v4 breadth geometry scaled 1.5x).
    assert TARGET_POLICY.account_id == "paper-research-roi-v6"
    assert TARGET_POLICY.policy_version == "research-target-roi-v6"
    assert TARGET_POLICY.reference_equity == 1000.0
    assert TARGET_POLICY.target_return == 0.05
    assert TARGET_POLICY.target_pnl == 50.0
    assert TARGET_POLICY.max_position_risk_pct == 0.09
    assert TARGET_POLICY.max_city_target_risk_pct == 0.18
    assert TARGET_POLICY.max_region_day_risk_pct == 0.36
    assert TARGET_POLICY.max_aggregate_risk_pct == 0.75
    assert TARGET_POLICY.daily_loss_pause_pct == 0.15
    assert TARGET_POLICY.allocator_version == "policy-sized-v3"

    assert ALL_RESEARCH_POLICIES == (
        TARGET_POLICY_V1,
        TARGET_POLICY_V2,
        TARGET_POLICY_V3,
        TARGET_POLICY_V4,
        TARGET_POLICY_V5,
        TARGET_POLICY,
        MOTION_POLICY,
    )


def _structural_target_candidate(*, bid: float, ask: float, ask_size: float) -> TradeDecision:
    return TradeDecision(
        ticker="KXHIGHTOKC-26JUL30-B96.5",
        label="96° to 97°",
        action="BUY_NO",
        approved=True,
        probability=0.95,
        probability_lcb=0.90,
        model_probability=0.95,
        yes_bid=0.24,
        yes_ask=0.25,
        spread=ask - bid,
        fee_per_contract=0.0,
        cost_per_contract=ask,
        edge=0.95 - ask,
        edge_lcb=0.90 - ask,
        kelly_fraction=0.0,
        recommended_contracts=25.0,
        expected_profit=(0.95 - ask) * 25.0,
        reasons=[],
        side="NO",
        entry_bid=bid,
        entry_ask=ask,
        entry_bid_size=100.0,
        entry_ask_size=ask_size,
        strike_type="between",
        floor_strike=96.0,
        cap_strike=97.0,
        trade_quality_score=75.0,
        binding_constraint="research_policy_allocator",
    )


def test_resting_structural_target_uses_the_active_policy_position_budget() -> None:
    prepared = with_target_research_execution(
        _structural_target_candidate(bid=0.74, ask=0.76, ask_size=1.0),
        strategy_config_for_profile("research"),
    )
    assert prepared is not None

    plans = allocate_research_plans(
        [ResearchOpportunity(prepared, "2026-07-30", 1)],
        motion_opportunities=[],
    )

    assert len(plans.target.legs) == 1
    assert plans.target.legs[0].decision.recommended_contracts == 120.0
    assert plans.target.legs[0].spend == 90.0


def test_resting_growth_target_survives_atomic_admission(tmp_path) -> None:
    from sfo_kalshi_quant.db import PaperStore
    from sfo_kalshi_quant.paper import PaperTrader

    config = strategy_config_for_profile("research")
    prepared = with_target_research_execution(
        _structural_target_candidate(bid=0.74, ask=0.76, ask_size=1.0),
        config,
    )
    assert prepared is not None
    plans = allocate_research_plans(
        [ResearchOpportunity(prepared, "2026-07-30", 1)],
        motion_opportunities=[],
        run_id="growth-v2-atomic",
    )
    store = PaperStore(
        tmp_path / "growth-v2-atomic.db",
        research_clock=lambda: datetime(2026, 7, 25, 20, tzinfo=UTC),
    )

    result = PaperTrader(
        store,
        config,
        risk_profile="research",
        entry_mode="limit",
    ).execute_research_plans(
        "2026-07-30",
        plans,
        source_decisions=[prepared],
        objective_day="2026-07-25",
        lead_bucket="day-ahead",
        scan_run_id="growth-v2-atomic",
        observed_high_state="complete=0;high=unavailable",
    )

    assert len(result.target_order_ids) == 1
    order = store.paper_order(result.target_order_ids[0])
    assert order is not None
    assert order["account_id"] == TARGET_POLICY.account_id
    assert order["entry_mode"] == "limit"
    assert order["status"] == "PAPER_LIMIT_RESTING"
    assert order["contracts"] == 120.0
    assert order["reserved_cost"] == 90.0


def test_crossing_structural_target_stays_clamped_to_visible_depth() -> None:
    prepared = with_target_research_execution(
        _structural_target_candidate(bid=0.75, ask=0.76, ask_size=5.9),
        strategy_config_for_profile("research"),
    )
    assert prepared is not None

    plans = allocate_research_plans(
        [ResearchOpportunity(prepared, "2026-07-30", 1)],
        motion_opportunities=[],
    )

    assert len(plans.target.legs) == 1
    assert plans.target.legs[0].decision.recommended_contracts == 5.0


def test_structural_target_does_not_expand_a_fee_bearing_source_quote() -> None:
    source = _structural_target_candidate(bid=0.74, ask=0.76, ask_size=100.0)
    fee_bearing = replace(
        source,
        fee_per_contract=0.01,
        cost_per_contract=0.76,
        edge=0.19,
        edge_lcb=0.14,
        expected_profit=4.75,
    )

    plans = allocate_research_plans(
        [ResearchOpportunity(fee_bearing, "2026-07-30", 1)],
        motion_opportunities=[],
    )

    assert len(plans.target.legs) == 1
    assert plans.target.legs[0].decision.recommended_contracts == 25.0


def test_all_target_eras_keep_distinct_published_identities() -> None:
    def published(policy) -> str:
        return published_profile_key(
            "research",
            research_sleeve=policy.sleeve.value,
            account_id=policy.account_id,
            research_policy_version=policy.policy_version,
            policy_fingerprint=policy.policy_fingerprint,
        )

    assert published(TARGET_POLICY_V1) == "research-target-v1"
    assert published(TARGET_POLICY_V2) == "research-target-v2"
    assert published(TARGET_POLICY) == "research-target"
    assert execution_profile_key("research-target-v1") == "research"
    assert execution_profile_key("research-target-v2") == "research"
    assert execution_profile_key("research-target") == "research"


def test_active_routing_uses_v3_while_prior_accounts_remain_known(
    tmp_path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    assert account_for_research_sleeve(ResearchSleeve.TARGET) == TARGET_POLICY.account_id

    store = PaperStore(tmp_path / "target-v3-routing.db")
    archived = store.research_account_state(account_id=TARGET_POLICY_V1.account_id)
    assert archived is not None
    assert archived["status"] == "ARCHIVED"
    assert store.account_policy_capacity(
        target_date="2026-07-30",
        market_ticker="KXHIGHTSFO-26JUL30-B70",
        risk_profile="research",
        requested_spend=1.0,
        account_id=TARGET_POLICY_V1.account_id,
    ) == {
        "allowed_spend": 0.0,
        "reason": "research account policy is archived",
    }
    assert (
        store.research_realized_pnl_for_day(
            account_id=TARGET_POLICY_V1.account_id,
            objective_day=date(2026, 7, 25),
        )
        == 0.0
    )


def test_archived_v1_policy_cannot_admit_new_research_orders(tmp_path) -> None:
    from sfo_kalshi_quant.db import PaperStore, ResearchAdmission

    store = PaperStore(tmp_path / "target-v1-admission.db")
    admission = ResearchAdmission(
        account_id=TARGET_POLICY_V1.account_id,
        sleeve=TARGET_POLICY_V1.sleeve,
        policy_version=TARGET_POLICY_V1.policy_version,
        policy_fingerprint=TARGET_POLICY_V1.policy_fingerprint,
        objective_day="2026-07-25",
        scan_run_id="archived-v1",
        reentry_fingerprint="archived-v1-entry",
        lead_bucket="day-ahead",
        entry_decision_id=1,
    )

    with pytest.raises(ValueError, match="active research policy"):
        store._policy_for_research_admission(admission)


def test_legacy_v1_outcomes_remain_readable_after_v3_activation(tmp_path) -> None:
    from sfo_kalshi_quant.db import PaperStore

    objective_day = date(2026, 7, 25)
    store = PaperStore(tmp_path / "target-v1-history.db")
    order_id = store.record_paper_order(
        "2026-07-26",
        _structural_target_candidate(bid=0.74, ask=0.76, ask_size=5.0),
        risk_profile="live",
    )
    assert order_id is not None
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_orders SET account_id=?, risk_profile='research', "
            "research_sleeve=?, "
            "research_policy_version=?, policy_fingerprint=?, objective_day=?, "
            "lead_bucket='day-ahead', scan_run_id='legacy-v1', "
            "reentry_fingerprint='legacy-v1-entry', status='PAPER_CLOSED', "
            "realized_pnl=5.0, closed_at=? WHERE id=?",
            (
                TARGET_POLICY_V1.account_id,
                TARGET_POLICY_V1.sleeve.value,
                TARGET_POLICY_V1.policy_version,
                TARGET_POLICY_V1.policy_fingerprint,
                objective_day.isoformat(),
                datetime(2026, 7, 25, 20, tzinfo=UTC).isoformat(),
                order_id,
            ),
        )

    assert store.research_realized_pnl_for_day(
        account_id=TARGET_POLICY_V1.account_id,
        objective_day=objective_day,
    ) == 5.0


def test_fresh_store_bootstraps_every_research_policy_account(tmp_path) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(tmp_path / "target-v2-bootstrap.db")

    states = {
        policy.account_id: store.research_account_state(account_id=policy.account_id)
        for policy in ALL_RESEARCH_POLICIES
    }
    assert set(states) == {
        "paper-research-target-v1",
        "paper-research-target-v2",
        "paper-research-roi-v3",
        "paper-research-roi-v4",
        "paper-research-roi-v5",
        "paper-research-roi-v6",
        "paper-research-motion-v1",
    }
    assert all(state is not None for state in states.values())
    assert all(state["realized_equity"] == 1000.0 for state in states.values())
    assert all(state["available_cash"] == 1000.0 for state in states.values())


def test_upgrade_preserves_v1_goal_and_creates_a_separate_v3_goal(tmp_path) -> None:
    from sfo_kalshi_quant.db import PaperStore

    db_path = tmp_path / "target-v1-upgrade.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE research_daily_goals ("
            "objective_day TEXT NOT NULL, account_id TEXT NOT NULL, "
            "policy_version TEXT NOT NULL, created_at TEXT NOT NULL, "
            "reference_equity REAL NOT NULL, target_return REAL NOT NULL, "
            "target_pnl REAL NOT NULL, "
            "PRIMARY KEY(objective_day, account_id, policy_version))"
        )
        conn.execute(
            "INSERT INTO research_daily_goals VALUES (?, ?, ?, ?, 1000, 0.05, 50)",
            (
                "2026-07-25",
                TARGET_POLICY_V1.account_id,
                TARGET_POLICY_V1.policy_version,
                datetime.now(UTC).isoformat(),
            ),
        )

    store = PaperStore(db_path)
    active = store.research_daily_goal_state(objective_day=date(2026, 7, 25))
    report = store.research_daily_goal_report(
        through_day=date(2026, 7, 25),
        window_days=1,
    )

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT account_id, policy_version, policy_fingerprint, target_pnl "
            "FROM research_daily_goals ORDER BY account_id"
        ).fetchall()

    assert active.target_pnl == 50.0
    assert report["account_id"] == TARGET_POLICY.account_id
    assert report["policy_version"] == TARGET_POLICY.policy_version
    assert rows == [
        (
            TARGET_POLICY.account_id,
            TARGET_POLICY.policy_version,
            TARGET_POLICY.policy_fingerprint,
            50.0,
        ),
        (
            TARGET_POLICY_V1.account_id,
            TARGET_POLICY_V1.policy_version,
            TARGET_POLICY_V1.policy_fingerprint,
            50.0,
        ),
    ]
