from __future__ import annotations

from datetime import UTC, datetime
from dataclasses import replace
import sqlite3

from sfo_kalshi_quant.account import (
    LIVE_STABILITY_ACCOUNT_ID,
    RESEARCH_ACCOUNT_ID,
    SHARED_ACCOUNT_ID,
)
from sfo_kalshi_quant.config import SFO_TZ
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.profile_identity import row_published_profile_key
from sfo_kalshi_quant.research_policy import (
    MOTION_POLICY,
    TARGET_POLICY,
    TARGET_POLICY_V1,
    TARGET_POLICY_V2,
)
from sfo_kalshi_quant.strategy_lab.build import (
    _accounting_payload,
    _bind_accounting_to_profiles,
)
from sfo_kalshi_quant.replay import (
    _eligible_readiness_order_ids,
    _eligible_readiness_root_ids,
)


def _one_day_summary() -> dict[str, object]:
    today = datetime.now(UTC).astimezone(SFO_TZ).date().isoformat()
    return {
        "available": True,
        "window_days": 1,
        "window_start": today,
        "window_end": today,
        "days": [{"date": today, "profiles": {}}],
        "profiles": [],
        "totals": {},
    }


def _live_decision(ticker: str) -> TradeDecision:
    return TradeDecision(
        ticker=ticker,
        label="70° to 71°",
        action="BUY_NO",
        approved=True,
        probability=0.90,
        probability_lcb=0.85,
        yes_bid=0.20,
        yes_ask=0.21,
        spread=0.01,
        fee_per_contract=0.0,
        cost_per_contract=0.80,
        edge=0.10,
        edge_lcb=0.05,
        kelly_fraction=0.01,
        recommended_contracts=1.0,
        expected_profit=0.10,
        reasons=[],
        side="NO",
        entry_bid=0.79,
        entry_ask=0.80,
        entry_bid_size=100.0,
        entry_ask_size=100.0,
        trade_quality_score=80.0,
    )


def test_fresh_restart_publishes_exactly_two_active_thousand_dollar_ledgers(
    tmp_path,
) -> None:
    db_path = tmp_path / "paper.db"
    PaperStore(db_path)

    payload = _accounting_payload(
        _one_day_summary(),
        {"profiles": []},
        db_path=db_path,
    )

    assert payload["schema_version"] == 3
    assert set(payload["active_ledgers"]) == {"live_stability", "research_roi"}
    live = payload["active_ledgers"]["live_stability"]
    research = payload["active_ledgers"]["research_roi"]
    assert live["account_id"] == LIVE_STABILITY_ACCOUNT_ID
    assert research["account_id"] == TARGET_POLICY.account_id
    assert live["initial_equity"] == live["realized_equity"] == 1000.0
    assert research["initial_equity"] == research["realized_equity"] == 1000.0
    assert live["status"] == research["status"] == "ACTIVE"
    assert live["days"][-1]["closing_equity"] == 1000.0
    assert research["days"][-1]["closing_equity"] == 1000.0
    assert TARGET_POLICY.target_pnl == 50.0

    archived_ids = {
        row["account_id"] for row in payload["archived_accounts"]
    }
    assert archived_ids >= {
        SHARED_ACCOUNT_ID,
        RESEARCH_ACCOUNT_ID,
        TARGET_POLICY_V1.account_id,
        TARGET_POLICY_V2.account_id,
        MOTION_POLICY.account_id,
    }
    assert all(
        row["status"] in {"ARCHIVED", "ARCHIVED_SETTLING"}
        for row in payload["archived_accounts"]
    )
    archived_by_id = {
        row["account_id"]: row for row in payload["archived_accounts"]
    }
    assert (
        archived_by_id[SHARED_ACCOUNT_ID]["profile_key"]
        == "legacy-shared-account"
    )
    assert "shared" in archived_by_id[SHARED_ACCOUNT_ID]["label"].lower()
    assert (
        archived_by_id[RESEARCH_ACCOUNT_ID]["profile_key"]
        == "legacy-research-shadow-account"
    )


def test_restart_publication_fails_closed_for_malformed_active_ledgers(
    tmp_path,
) -> None:
    db_path = tmp_path / "paper.db"
    store = PaperStore(db_path)
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_accounts SET status='ARCHIVED', opening_cash=900 "
            "WHERE account_id=?",
            (LIVE_STABILITY_ACCOUNT_ID,),
        )

    payload = _accounting_payload(
        _one_day_summary(),
        {"profiles": []},
        db_path=db_path,
    )

    assert payload["available"] is False
    assert payload["active_ledgers"] == {}
    assert payload["accounts"] == {}
    assert "invalid" in payload["reason"]


def test_restart_publication_fails_closed_for_missing_order_ledger_event(
    tmp_path,
) -> None:
    db_path = tmp_path / "paper.db"
    store = PaperStore(db_path)
    order_id = store.record_paper_order(
        "2026-07-27",
        _live_decision("KXHIGHTSFO-26JUL27-B70.5"),
        risk_profile="live",
    )
    assert order_id is not None
    with store.connect() as conn:
        conn.execute(
            "DELETE FROM paper_account_ledger WHERE order_id=?",
            (order_id,),
        )

    reconciliation = store.account_order_ledger_reconciliation(
        LIVE_STABILITY_ACCOUNT_ID
    )
    payload = _accounting_payload(
        _one_day_summary(),
        {"profiles": []},
        db_path=db_path,
    )

    assert reconciliation["status"] == "mismatch"
    assert reconciliation["mismatched_order_ids"] == [order_id]
    assert payload["available"] is False
    assert payload["active_ledgers"] == {}


def test_account_reconciliation_accepts_valid_filled_order(tmp_path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    order_id = store.record_paper_order(
        "2026-07-27",
        replace(
            _live_decision("KXHIGHTSFO-26JUL27-B70.5"),
            recommended_contracts=4.0,
            expected_profit=0.4,
        ),
        risk_profile="live",
    )

    assert order_id is not None
    assert store.account_order_ledger_reconciliation(
        LIVE_STABILITY_ACCOUNT_ID
    )["status"] == "reconciled"


def test_account_reconciliation_accepts_valid_resting_reservation(tmp_path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    order_id = store.record_paper_order(
        "2026-07-27",
        _live_decision("KXHIGHTSFO-26JUL27-B70.5"),
        risk_profile="live",
        status="PAPER_LIMIT_RESTING",
        entry_mode="limit",
    )

    assert order_id is not None
    assert store.account_order_ledger_reconciliation(
        LIVE_STABILITY_ACCOUNT_ID
    )["status"] == "reconciled"


def test_account_reconciliation_accepts_valid_partial_close(tmp_path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    order_id = store.record_paper_order(
        "2026-07-27",
        replace(
            _live_decision("KXHIGHTSFO-26JUL27-B70.5"),
            recommended_contracts=4.0,
            expected_profit=0.4,
        ),
        risk_profile="live",
    )
    assert order_id is not None

    store.close_paper_order(order_id, 0.9, max_quantity=1.0)

    assert store.account_order_ledger_reconciliation(
        LIVE_STABILITY_ACCOUNT_ID
    )["status"] == "reconciled"


def test_account_reconciliation_accepts_valid_resolved_order(tmp_path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    order_id = store.record_paper_order(
        "2026-07-27",
        _live_decision("KXHIGHTSFO-26JUL27-B70.5"),
        risk_profile="live",
    )
    assert order_id is not None

    assert store.settle_paper_orders("2026-07-27", 75.0) == 1

    assert store.account_order_ledger_reconciliation(
        LIVE_STABILITY_ACCOUNT_ID
    )["status"] == "reconciled"


def test_archived_resting_order_is_published_as_settling_not_final(
    tmp_path,
) -> None:
    db_path = tmp_path / "paper.db"
    store = PaperStore(db_path)
    order_id = store.record_paper_order(
        "2026-07-27",
        _live_decision("KXHIGHTSFO-26JUL27-B70.5"),
        risk_profile="live",
        status="PAPER_LIMIT_RESTING",
        entry_mode="limit",
    )
    assert order_id is not None
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_orders SET account_id=?, risk_profile='research', "
            "research_sleeve=?, research_policy_version=?, "
            "policy_fingerprint=? WHERE id=?",
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

    payload = _accounting_payload(
        _one_day_summary(),
        {"profiles": []},
        db_path=db_path,
    )
    archived = next(
        row
        for row in payload["archived_accounts"]
        if row["account_id"] == TARGET_POLICY_V2.account_id
    )

    assert archived["status"] == "ARCHIVED_SETTLING"
    assert archived["open_positions"] == 0
    assert archived["pending_limit_orders"] == 1
    assert archived["pending_limit_risk"] > 0


def test_profile_daily_history_binds_true_account_balance_without_rewriting_pnl(
    tmp_path,
) -> None:
    db_path = tmp_path / "paper.db"
    PaperStore(db_path)
    accounting = _accounting_payload(
        _one_day_summary(),
        {"profiles": []},
        db_path=db_path,
    )
    day = accounting["active_ledgers"]["live_stability"]["days"][-1]["date"]
    profiles = [
        {
            "label": "Live Stability",
            "risk_profile": "live",
            "profile_type": "primary",
            "daily_summary": {
                "days": [
                    {
                        "date": day,
                        "cumulative_realized": 0.0,
                        "realized_pnl": 0.0,
                    }
                ],
            },
        },
        {
            "label": "Research ROI",
            "risk_profile": "research-target",
            "profile_type": "experimental",
            "daily_summary": {
                "days": [
                    {
                        "date": day,
                        "cumulative_realized": 0.0,
                        "realized_pnl": 0.0,
                    }
                ],
            },
        },
        {
            "label": "Research motion",
            "risk_profile": "research-motion",
            "profile_type": "experimental",
            "daily_summary": {"days": []},
        },
    ]

    bound = _bind_accounting_to_profiles(profiles, accounting)
    by_name = {row["risk_profile"]: row for row in bound}

    assert by_name["live"]["daily_summary"]["equity_basis"] == "account_equity"
    assert by_name["live"]["daily_summary"]["starting_bankroll"] == 1000.0
    assert by_name["live"]["daily_summary"]["current_equity"] == 1000.0
    assert by_name["live"]["daily_summary"]["days"][-1]["closing_equity"] == 1000.0
    assert (
        by_name["research-target"]["daily_summary"]["days"][-1]["closing_equity"]
        == 1000.0
    )
    assert by_name["research-motion"]["archived"] is True
    assert by_name["research-motion"]["daily_summary"].get("current_equity") != 1000.0


def test_invalid_accounting_removes_active_profiles_from_public_projection() -> None:
    profiles = [
        {
            "label": "Live Stability",
            "risk_profile": "live",
            "profile_type": "primary",
        },
        {
            "label": "Research ROI",
            "risk_profile": "research-target",
            "profile_type": "experimental",
        },
        {
            "label": "Legacy live",
            "risk_profile": "live-legacy",
            "profile_type": "primary",
            "archived": True,
        },
    ]

    bound = _bind_accounting_to_profiles(
        profiles,
        {
            "schema_version": 3,
            "available": False,
            "reason": "fresh active paper ledgers invalid or unavailable",
            "accounts": {},
            "active_ledgers": {},
            "archived_accounts": [],
        },
    )

    assert [row["risk_profile"] for row in bound] == ["live-legacy"]


def test_readiness_keeps_valid_live_evidence_across_legacy_and_fresh_accounts(
    tmp_path,
) -> None:
    store = PaperStore(tmp_path / "paper.db")
    fresh_id = store.record_paper_order(
        "2026-07-27",
        _live_decision("KXHIGHTSFO-26JUL27-B70.5"),
        risk_profile="live",
    )
    legacy_id = store.record_paper_order(
        "2026-07-28",
        replace(
            _live_decision("KXHIGHTSFO-26JUL28-B71.5"),
            label="71° to 72°",
        ),
        risk_profile="live",
    )
    assert fresh_id is not None and legacy_id is not None
    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE paper_orders SET account_id=? WHERE id=?",
            (SHARED_ACCOUNT_ID, legacy_id),
        )
        rows = conn.execute("SELECT * FROM paper_orders ORDER BY id").fetchall()

    assert _eligible_readiness_root_ids(
        rows,
        "2000-01-01T00:00:00+00:00",
    ) == {fresh_id, legacy_id}


def test_readiness_rejects_crossed_or_research_identity_on_live_accounts(
    tmp_path,
) -> None:
    store = PaperStore(tmp_path / "paper.db")
    crossed_id = store.record_paper_order(
        "2026-07-27",
        _live_decision("KXHIGHTSFO-26JUL27-B70.5"),
        risk_profile="live",
    )
    research_id = store.record_paper_order(
        "2026-07-28",
        replace(
            _live_decision("KXHIGHTSFO-26JUL28-B71.5"),
            label="71° to 72°",
        ),
        risk_profile="live",
    )
    assert crossed_id is not None and research_id is not None
    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE paper_orders SET research_sleeve=?, "
            "research_policy_version=?, policy_fingerprint=? WHERE id=?",
            (
                TARGET_POLICY.sleeve.value,
                TARGET_POLICY.policy_version,
                TARGET_POLICY.policy_fingerprint,
                crossed_id,
            ),
        )
        conn.execute(
            "UPDATE paper_orders SET risk_profile='research', account_id=? "
            "WHERE id=?",
            (SHARED_ACCOUNT_ID, research_id),
        )
        rows = conn.execute("SELECT * FROM paper_orders ORDER BY id").fetchall()

    assert {row_published_profile_key(row) for row in rows} == {
        "unknown",
        "research",
    }
    assert _eligible_readiness_root_ids(
        rows,
        "2000-01-01T00:00:00+00:00",
    ) == set()
    assert _eligible_readiness_order_ids(
        rows,
        "2000-01-01T00:00:00+00:00",
    ) == set()


def test_new_live_scan_evidence_is_stamped_to_live_stability_account(
    tmp_path,
) -> None:
    store = PaperStore(tmp_path / "paper.db")

    snapshot_ids = store.record_decisions(
        "2026-07-27",
        [_live_decision("KXHIGHTSFO-26JUL27-B70.5")],
        risk_profile="live",
    )

    assert len(snapshot_ids) == 1
    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        decision = conn.execute(
            "SELECT * FROM decision_snapshots WHERE id=?",
            (snapshot_ids[0],),
        ).fetchone()
        assert decision is not None
        context = conn.execute(
            "SELECT * FROM scan_context_snapshots WHERE id=?",
            (decision["scan_context_id"],),
        ).fetchone()
    assert decision["account_id"] == LIVE_STABILITY_ACCOUNT_ID
    assert context is not None
    assert context["account_id"] == LIVE_STABILITY_ACCOUNT_ID
    assert row_published_profile_key(decision) == "live"
