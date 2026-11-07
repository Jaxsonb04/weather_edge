"""Every active paper account runs on $2,000; Live keeps its history via one deposit."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from sfo_kalshi_quant.account import (
    INITIAL_CAPITAL,
    LIVE_STABILITY_ACCOUNT_ID,
    WEEKLY_GOAL_TZ,
)
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.monitor import RESEARCH_VETO_DOLLAR_CAPS
from sfo_kalshi_quant.research_m1 import M1_POLICY
from sfo_kalshi_quant.research_policy import (
    ALL_RESEARCH_POLICIES,
    TARGET_POLICY,
    TARGET_POLICY_V6,
)
from sfo_kalshi_quant.strategy_lab.build import (
    _account_daily_balance_rows,
    _capital_deposits,
    _weekly_goal_payload,
)
from sfo_kalshi_quant.strategy_lab.status_alerts import _account_drawdown_alerts


def _as_live_ran_before_the_deposit(path, *, high_water=1070.26):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE paper_accounts SET initial_capital=1000, opening_cash=1000, "
            "high_water_equity=? WHERE account_id=?",
            (high_water, LIVE_STABILITY_ACCOUNT_ID),
        )
        conn.execute(
            "UPDATE paper_account_ledger SET amount=1000 "
            "WHERE account_id=? AND event_type='OPENING_CASH'",
            (LIVE_STABILITY_ACCOUNT_ID,),
        )


def _deposits(store):
    return [
        row
        for row in store.account_ledger(account_id=LIVE_STABILITY_ACCOUNT_ID)
        if row["event_type"] == "CAPITAL_DEPOSIT"
    ]


def test_active_accounts_run_on_two_thousand_and_v6_keeps_its_thousand():
    assert INITIAL_CAPITAL == TARGET_POLICY.reference_equity == M1_POLICY.reference_equity == 2000.0
    assert TARGET_POLICY_V6.reference_equity == 1000.0
    assert TARGET_POLICY.target_pnl == pytest.approx(100.0)


def test_v7_position_limit_stays_ninety_dollars():
    assert TARGET_POLICY.reference_equity * TARGET_POLICY.max_position_risk_pct == pytest.approx(90.0)


def test_v7_veto_stop_stays_at_35_and_archived_policies_keep_their_own():
    assert RESEARCH_VETO_DOLLAR_CAPS[TARGET_POLICY.account_id] == 35.0
    for policy in ALL_RESEARCH_POLICIES:
        if policy.account_id in RESEARCH_VETO_DOLLAR_CAPS and policy is not TARGET_POLICY:
            assert RESEARCH_VETO_DOLLAR_CAPS[policy.account_id] == pytest.approx(
                policy.target_pnl * 0.70
            )


def test_fresh_live_ledger_opens_at_two_thousand_without_a_deposit(tmp_path):
    store = PaperStore(tmp_path / "paper.db")
    state = store.live_account_state()
    assert state["initial_capital"] == state["opening_cash"] == 2000.0
    assert _deposits(store) == []


def test_existing_live_ledger_gets_one_audited_deposit_and_still_reconciles(tmp_path):
    path = tmp_path / "paper.db"
    PaperStore(path)
    _as_live_ran_before_the_deposit(path)

    store = PaperStore(path)
    PaperStore(path)  # a second start must not deposit again

    state = store.live_account_state()
    assert state["initial_capital"] == state["opening_cash"] == 2000.0
    assert state["realized_equity"] == pytest.approx(2000.0)
    assert state["high_water_equity"] == pytest.approx(2070.26)
    assert [float(row["amount"]) for row in _deposits(store)] == [1000.0]
    assert store.account_order_ledger_reconciliation(LIVE_STABILITY_ACCOUNT_ID)["status"] == "reconciled"


def test_an_archived_live_ledger_gets_no_deposit(tmp_path):
    path = tmp_path / "paper.db"
    PaperStore(path)
    _as_live_ran_before_the_deposit(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE paper_accounts SET status='ARCHIVED' WHERE account_id=?",
            (LIVE_STABILITY_ACCOUNT_ID,),
        )

    store = PaperStore(path)

    assert store.live_account_state()["initial_capital"] == 1000.0
    assert _deposits(store) == []


def _deposited_store(path, deposited_at):
    PaperStore(path)
    _as_live_ran_before_the_deposit(path)
    store = PaperStore(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE paper_account_ledger SET created_at=? WHERE event_type='CAPITAL_DEPOSIT'",
            (deposited_at.isoformat(),),
        )
    return store


def test_balances_before_the_deposit_stay_on_the_capital_live_really_had(tmp_path):
    deposited_at = datetime(2026, 11, 7, 10, tzinfo=WEEKLY_GOAL_TZ)
    store = _deposited_store(tmp_path / "paper.db", deposited_at)

    rows = _account_daily_balance_rows(
        store,
        LIVE_STABILITY_ACCOUNT_ID,
        initial_equity=2000.0,
        account_created_at="2026-07-26T10:21:52+00:00",
        day_rows=[{"date": "2026-11-06"}, {"date": "2026-11-07"}, {"date": "2026-11-08"}],
    )

    assert [row["opening_equity"] for row in rows] == [1000.0, 1000.0, 2000.0]
    assert [row["closing_equity"] for row in rows] == [1000.0, 2000.0, 2000.0]
    assert [row["cumulative_realized"] for row in rows] == [0.0, 0.0, 0.0]
    assert [row["capital_deposit"] for row in rows] == [0.0, 1000.0, 0.0]


def test_a_malformed_deposit_time_does_not_stop_the_build(tmp_path):
    path = tmp_path / "paper.db"
    store = _deposited_store(path, datetime(2026, 11, 7, 10, tzinfo=WEEKLY_GOAL_TZ))
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE paper_account_ledger SET created_at='bad' WHERE event_type='CAPITAL_DEPOSIT'"
        )

    assert _capital_deposits(store, LIVE_STABILITY_ACCOUNT_ID) == []


def test_drawdown_warning_uses_the_high_water_that_carries_the_deposit():
    account = {
        "realized_equity": 1745.90,
        "high_water_equity": 2000.0,
        "days": [{"opening_equity": 1000.0, "closing_equity": 745.90}],
    }

    assert len(_account_drawdown_alerts(account, key="live", label="Live")) == 1


def test_the_deposit_week_keeps_its_real_opening_and_the_next_week_starts_on_the_new_capital(tmp_path):
    now = datetime.now(WEEKLY_GOAL_TZ)
    live = {"account_id": LIVE_STABILITY_ACCOUNT_ID, "realized_equity": 2000.0}

    this_week = _weekly_goal_payload(_deposited_store(tmp_path / "a.db", now), live)
    last_week = _weekly_goal_payload(
        _deposited_store(tmp_path / "b.db", now - timedelta(days=7)), live
    )

    assert this_week["starting_realized_equity"] == 1000.0
    assert last_week["starting_realized_equity"] == 2000.0
