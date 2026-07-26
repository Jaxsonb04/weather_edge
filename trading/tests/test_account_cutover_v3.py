from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

from sfo_kalshi_quant.account import (
    LIVE_STABILITY_ACCOUNT_ID,
    RESEARCH_ACCOUNT_ID,
    SHARED_ACCOUNT_ID,
    account_for_profile,
    account_for_research_sleeve,
)
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.research_policy import (
    ALL_RESEARCH_POLICIES,
    MOTION_POLICY,
    TARGET_POLICY,
    TARGET_POLICY_V1,
    TARGET_POLICY_V2,
    ResearchSleeve,
)
from sfo_kalshi_quant.profile_identity import (
    execution_profile_key,
    published_profile_key,
)
from sfo_kalshi_quant.models import TradeDecision


def _decision() -> TradeDecision:
    return TradeDecision(
        ticker="KXHIGHTSFO-26JUL30-B70",
        label="70° to 71°",
        action="BUY_YES",
        approved=True,
        probability=0.75,
        probability_lcb=0.65,
        yes_bid=0.29,
        yes_ask=0.40,
        spread=0.11,
        fee_per_contract=0.01,
        cost_per_contract=0.41,
        edge=0.34,
        edge_lcb=0.24,
        kelly_fraction=0.03,
        recommended_contracts=20.0,
        expected_profit=6.8,
        reasons=[],
        side="YES",
        entry_bid=0.29,
        entry_ask=0.40,
        entry_bid_size=10.0,
        entry_ask_size=100.0,
        strike_type="between",
        floor_strike=70.0,
        cap_strike=71.0,
    )


def test_fresh_store_activates_exact_live_bankroll_and_archives_legacy_accounts(
    tmp_path,
) -> None:
    db_path = tmp_path / "account-cutover-v3.db"
    store = PaperStore(db_path)

    assert account_for_profile("live") == LIVE_STABILITY_ACCOUNT_ID
    assert store.live_account_state() == {
        "account_id": LIVE_STABILITY_ACCOUNT_ID,
        "initial_capital": 1000.0,
        "opening_cash": 1000.0,
        "cash_balance": 1000.0,
        "open_cost_basis": 0.0,
        "reservations": 0.0,
        "available_cash": 1000.0,
        "realized_equity": 1000.0,
        "high_water_equity": 1000.0,
        "drawdown": 0.0,
        "status": "ACTIVE",
    }
    assert store.shared_account_state()["account_id"] == SHARED_ACCOUNT_ID
    assert store.shared_account_state()["status"] == "ARCHIVED"
    assert store.research_account_state()["account_id"] == RESEARCH_ACCOUNT_ID
    assert store.research_account_state()["status"] == "ARCHIVED"
    order_id = store.record_paper_order("2026-07-30", _decision(), risk_profile="live")
    assert order_id is not None
    assert store.paper_order(order_id)["account_id"] == LIVE_STABILITY_ACCOUNT_ID

    PaperStore(db_path)
    opening_events = [
        row
        for row in store.account_ledger(account_id=LIVE_STABILITY_ACCOUNT_ID)
        if row["event_type"] == "OPENING_CASH"
    ]
    assert len(opening_events) == 1
    assert opening_events[0]["amount"] == 1000.0


def test_target_v2_is_frozen_and_target_v3_is_the_only_active_policy() -> None:
    assert TARGET_POLICY_V2.account_id == "paper-research-target-v2"
    assert TARGET_POLICY_V2.policy_version == "research-target-growth-v2"
    assert TARGET_POLICY_V2.target_return == 0.016
    assert TARGET_POLICY_V2.allocator_version == "policy-sized-v2"

    assert TARGET_POLICY.account_id == "paper-research-roi-v3"
    assert TARGET_POLICY.policy_version == "research-target-roi-v3"
    assert TARGET_POLICY.reference_equity == 1000.0
    assert TARGET_POLICY.target_return == 0.05
    assert TARGET_POLICY.target_pnl == 50.0
    assert TARGET_POLICY.max_position_risk_pct == 0.08
    assert TARGET_POLICY.max_city_target_risk_pct == 0.10
    assert TARGET_POLICY.max_region_day_risk_pct == 0.20
    assert TARGET_POLICY.max_aggregate_risk_pct == 0.40
    assert TARGET_POLICY.daily_loss_pause_pct == 0.12
    assert TARGET_POLICY.min_lead_days == 1
    assert TARGET_POLICY.one_contract is False
    assert TARGET_POLICY.allocator_version == "policy-sized-v3"
    assert account_for_research_sleeve(ResearchSleeve.TARGET) == TARGET_POLICY.account_id
    assert ALL_RESEARCH_POLICIES == (
        TARGET_POLICY_V1,
        TARGET_POLICY_V2,
        TARGET_POLICY,
        MOTION_POLICY,
    )


def test_published_identities_separate_fresh_live_and_every_research_era() -> None:
    assert (
        published_profile_key("live", account_id=LIVE_STABILITY_ACCOUNT_ID)
        == "live"
    )
    assert published_profile_key("live", account_id=SHARED_ACCOUNT_ID) == "live-legacy"
    assert published_profile_key("live", account_id=None) == "live-legacy"
    assert published_profile_key("unknown", account_id=None) == "live-legacy"
    assert execution_profile_key("live-legacy") == "live"

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
    assert published(MOTION_POLICY) == "research-motion"


def test_fresh_store_archives_every_research_policy_except_v3(tmp_path) -> None:
    store = PaperStore(tmp_path / "research-policy-cutover-v3.db")

    statuses = {
        policy.account_id: store.research_account_state(account_id=policy.account_id)[
            "status"
        ]
        for policy in ALL_RESEARCH_POLICIES
    }
    assert statuses == {
        TARGET_POLICY_V1.account_id: "ARCHIVED",
        TARGET_POLICY_V2.account_id: "ARCHIVED",
        TARGET_POLICY.account_id: "ACTIVE",
        MOTION_POLICY.account_id: "ARCHIVED",
    }


def test_reinitialization_archives_every_unknown_active_account(tmp_path) -> None:
    db_path = tmp_path / "unknown-active-account.db"
    store = PaperStore(db_path)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO paper_accounts "
            "(account_id, created_at, initial_capital, opening_cash, "
            "high_water_equity, status, cutover_note) "
            "VALUES ('paper-custom-legacy', ?, 1000, 1000, 1000, 'ACTIVE', "
            "'pre-cutover custom experiment')",
            (datetime.now(UTC).isoformat(),),
        )

    PaperStore(db_path)

    with store.connect() as conn:
        active_ids = {
            row[0]
            for row in conn.execute(
                "SELECT account_id FROM paper_accounts WHERE status='ACTIVE'"
            )
        }
        custom_status = conn.execute(
            "SELECT status FROM paper_accounts WHERE account_id='paper-custom-legacy'"
        ).fetchone()[0]
    assert active_ids == {
        LIVE_STABILITY_ACCOUNT_ID,
        TARGET_POLICY.account_id,
    }
    assert custom_status == "ARCHIVED"


def test_generic_research_shared_capital_opt_in_fails_closed(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PAPER_RESEARCH_SHARED_CAPITAL_ENABLED", "true")
    store = PaperStore(tmp_path / "generic-research-cutover-v3.db")

    assert account_for_profile("research") == RESEARCH_ACCOUNT_ID
    assert store.account_policy_capacity(
        target_date="2026-07-30",
        market_ticker="KXHIGHTSFO-26JUL30-B70",
        risk_profile="research",
        requested_spend=20.0,
    ) == {
        "allowed_spend": 0.0,
        "reason": "generic research account is archived",
    }
    assert (
        store.record_paper_order(
            "2026-07-30",
            _decision(),
            risk_profile="research",
        )
        is None
    )
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0


def test_live_capacity_reads_the_active_live_ledger_not_legacy_shared(tmp_path) -> None:
    store = PaperStore(tmp_path / "live-capacity-cutover-v3.db")
    with store.connect() as conn:
        store._record_ledger_event(
            conn,
            account_id=LIVE_STABILITY_ACCOUNT_ID,
            order_id=None,
            event_type="TEST_LOSS",
            amount=-996.0,
            idempotency_key="test:live-loss",
        )

    assert store.shared_account_state()["available_cash"] == 1000.0
    assert store.live_account_state()["available_cash"] == 4.0
    assert store.account_policy_capacity(
        target_date="2026-07-30",
        market_ticker="KXHIGHTSFO-26JUL30-B70",
        risk_profile="live",
        requested_spend=20.0,
    ) == {
        "allowed_spend": 0.0,
        "reason": "15% account drawdown pause",
    }


def test_live_capacity_still_counts_unassigned_legacy_open_risk(tmp_path) -> None:
    store = PaperStore(tmp_path / "legacy-open-risk-cutover-v3.db")
    order_id = store.record_paper_order(
        "2026-07-30",
        replace(_decision(), recommended_contracts=600.0),
        risk_profile="live",
    )
    assert order_id is not None
    with store.connect() as conn:
        conn.execute("UPDATE paper_orders SET account_id=NULL WHERE id=?", (order_id,))
        conn.execute(
            "UPDATE paper_account_ledger SET account_id=? WHERE order_id=?",
            (SHARED_ACCOUNT_ID, order_id),
        )

    capacity = store.account_policy_capacity(
        target_date="2026-07-31",
        market_ticker="KXHIGHNY-26JUL31-B80",
        risk_profile="live",
        requested_spend=20.0,
    )
    assert capacity["allowed_spend"] == 0.0
    assert capacity["reason"] == "account risk room below $5 executable minimum"


def test_live_admission_fails_closed_if_fixed_capital_identity_is_tampered(
    tmp_path,
) -> None:
    store = PaperStore(tmp_path / "tampered-live-capital.db")
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_accounts SET opening_cash=999 WHERE account_id=?",
            (LIVE_STABILITY_ACCOUNT_ID,),
        )

    assert store.account_policy_capacity(
        target_date="2026-07-30",
        market_ticker="KXHIGHTSFO-26JUL30-B70",
        risk_profile="live",
        requested_spend=20.0,
    ) == {
        "allowed_spend": 0.0,
        "reason": "live stability account capital does not match policy",
    }
    assert (
        store.record_paper_order(
            "2026-07-30",
            _decision(),
            risk_profile="live",
        )
        is None
    )


def test_archived_v2_outcomes_remain_readable_after_v3_activation(tmp_path) -> None:
    store = PaperStore(tmp_path / "archived-v2-history.db")
    order_id = store.record_paper_order("2026-07-30", _decision(), risk_profile="live")
    assert order_id is not None
    objective_day = date(2026, 7, 25)
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_orders SET account_id=?, risk_profile='research', "
            "research_sleeve=?, research_policy_version=?, policy_fingerprint=?, "
            "objective_day=?, lead_bucket='day-ahead', scan_run_id='legacy-v2', "
            "reentry_fingerprint='legacy-v2-entry', status='PAPER_CLOSED', "
            "realized_pnl=5.0, closed_at=? WHERE id=?",
            (
                TARGET_POLICY_V2.account_id,
                TARGET_POLICY_V2.sleeve.value,
                TARGET_POLICY_V2.policy_version,
                TARGET_POLICY_V2.policy_fingerprint,
                objective_day.isoformat(),
                datetime(2026, 7, 25, 20, tzinfo=UTC).isoformat(),
                order_id,
            ),
        )

    assert (
        store.research_realized_pnl_for_day(
            account_id=TARGET_POLICY_V2.account_id,
            objective_day=objective_day,
        )
        == 5.0
    )


def test_archived_v2_resting_order_still_fills_and_settles(tmp_path) -> None:
    store = PaperStore(tmp_path / "archived-v2-lifecycle.db")
    order_id = store.record_paper_order(
        "2026-07-30",
        _decision(),
        risk_profile="live",
        status="PAPER_LIMIT_RESTING",
        entry_mode="limit",
    )
    assert order_id is not None
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_orders SET account_id=?, risk_profile='research', "
            "research_sleeve=?, research_policy_version=?, policy_fingerprint=? "
            "WHERE id=?",
            (
                TARGET_POLICY_V2.account_id,
                TARGET_POLICY_V2.sleeve.value,
                TARGET_POLICY_V2.policy_version,
                TARGET_POLICY_V2.policy_fingerprint,
                order_id,
            ),
        )
        conn.execute(
            "UPDATE paper_account_ledger SET account_id=? WHERE order_id=?",
            (TARGET_POLICY_V2.account_id, order_id),
        )

    filled = store.fill_resting_limit_order(
        order_id,
        evidence={"trade_id": "archived-v2-fill"},
    )
    assert filled["status"] == "PAPER_FILLED"
    assert store.settle_paper_orders("2026-07-30", 70.0) == 1
    assert store.paper_order(order_id)["status"] == "PAPER_SETTLED"
    assert (
        store.research_account_state(account_id=TARGET_POLICY_V2.account_id)[
            "realized_equity"
        ]
        > 1000.0
    )


def test_only_v3_research_capacity_accepts_new_risk(tmp_path) -> None:
    store = PaperStore(tmp_path / "research-capacity-v3.db")

    for policy in (TARGET_POLICY_V1, TARGET_POLICY_V2, MOTION_POLICY):
        assert store.account_policy_capacity(
            target_date="2026-07-30",
            market_ticker="KXHIGHTSFO-26JUL30-B70",
            risk_profile="research",
            requested_spend=20.0,
            account_id=policy.account_id,
        ) == {
            "allowed_spend": 0.0,
            "reason": "research account policy is archived",
        }

    assert store.account_policy_capacity(
        target_date="2026-07-30",
        market_ticker="KXHIGHTSFO-26JUL30-B70",
        risk_profile="research",
        requested_spend=20.0,
        account_id=TARGET_POLICY.account_id,
    ) == {
        "allowed_spend": 20.0,
        "reason": None,
    }
