"""``paper-resettle --verify --restate --apply``: the one-lot, owner-approved write.

The dry-run preview is pinned in ``test_nov01_settle_reverify.py``. These tests
pin what applying may touch (lot 3687's own archived ledger and market-day row,
plus the realized P&L that cross-book sizing reads), its idempotence, and every
refusal.
"""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from sfo_kalshi_quant.account import LIVE_STABILITY_ACCOUNT_ID
from sfo_kalshi_quant.cli import main
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.research_m1 import M1_ACCOUNT_ID
from sfo_kalshi_quant.research_policy import TARGET_POLICY, TARGET_POLICY_V6
from sfo_kalshi_quant.restatement import restate
from sfo_kalshi_quant.store.exchange_settlement_checks import (
    record_exchange_resolution,
)
from test_nov01_settle_reverify import (
    RESEARCH_V6,
    SERIES,
    TARGET,
    TICKER,
    _bin_lot,
    _forecaster_root,
    _lot_3687,
    _order,
    _store_final,
    _table,
    city_truth,
)

TABLES = (
    "paper_orders",
    "paper_account_ledger",
    "paper_settlement_verifications",
    "market_day_settlements",
    "paper_accounts",
)


def _into_account(db_path: Path, order_id: int, policy) -> None:
    """Move a lot into ``policy``'s book with its full research identity."""

    account_id = policy.account_id
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "UPDATE paper_orders SET account_id=?, risk_profile='research', "
            "research_sleeve=?, research_policy_version=?, policy_fingerprint=? "
            "WHERE id=?",
            (
                account_id,
                policy.sleeve.value,
                policy.policy_version,
                policy.policy_fingerprint,
                order_id,
            ),
        )
        conn.execute(
            "UPDATE paper_account_ledger SET account_id=? WHERE order_id=?",
            (account_id, order_id),
        )
        conn.commit()


@pytest.fixture
def book(tmp_path):
    """Lot 3687 settled on 72F in archived V6, flagged, beside a Live lot."""

    root = _forecaster_root(tmp_path)
    db_path = tmp_path / "paper.db"
    store = PaperStore(db_path)
    lot = store.record_paper_order(TARGET, _lot_3687())
    live_day = "2026-10-20"
    live = store.record_paper_order(live_day, _bin_lot(live_day))
    _into_account(db_path, lot, TARGET_POLICY_V6)
    assert _order(db_path, live)["account_id"] == LIVE_STABILITY_ACCOUNT_ID
    assert store.settle_paper_orders(TARGET, 72.0, series_ticker=SERIES) == 1
    assert store.settle_paper_orders(live_day, 72.0, series_ticker=SERIES) == 1
    store.verify_paper_settlements({(SERIES, TARGET): 71}, intervals={SERIES: (TARGET, TARGET)})
    return root, db_path, store, lot, live


def _apply(root, db_path, order_id, *, delta="3.00", reason="audit F6"):
    command = [
        "--forecaster-root", str(root),
        "--db-path", str(db_path),
        "--no-color",
        "paper-resettle", "--verify", "--restate", "--apply",
        "--order-id", str(order_id),
        "--expected-delta", delta,
    ]
    if reason:
        command += ["--reason", reason]
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(command)
    return code, out.getvalue(), err.getvalue()


def _snapshot(db_path: Path) -> dict[str, list[tuple]]:
    return {table: _table(db_path, table) for table in TABLES}


def _cash(store: PaperStore, account_id: str) -> float:
    return sum(row["amount"] for row in store.account_ledger(account_id=account_id))


def _seed_exchange(db_path: Path, result: str, value: float | None) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        record_exchange_resolution(
            conn,
            {
                "market_ticker": TICKER,
                "market_status": "finalized",
                "result": result,
                "expiration_value": value,
                "source_endpoint": "test",
            },
            fetched_at="2026-10-24T00:00:00+00:00",
        )
        conn.commit()


def test_apply_moves_only_the_lots_own_archived_ledger_by_the_delta(book):
    root, db_path, store, lot, live = book
    _store_final(root, TARGET, 71)
    before = _snapshot(db_path)
    booked = _order(db_path, lot)
    assert round(booked["realized_pnl"], 2) == -2.38
    cash = {
        account: _cash(store, account)
        for account in (RESEARCH_V6, LIVE_STABILITY_ACCOUNT_ID, TARGET_POLICY.account_id)
    }
    assert store.account_order_ledger_reconciliation(RESEARCH_V6)["status"] == "reconciled"

    code, out, err = _apply(root, db_path, lot)

    assert code == 0, err
    assert f"applied: order={lot} account={RESEARCH_V6}" in out
    assert "high=72F->71F" in out and "delta=+3.00" in out
    restated = _order(db_path, lot)
    assert restated["status"] == "PAPER_SETTLED"
    assert restated["settled_at"] == booked["settled_at"]
    assert restated["settlement_high_f"] == 71.0
    assert restated["resolved_yes"] == 0
    assert restated["position_won"] == 1
    assert round(restated["realized_pnl"], 2) == 0.62
    outcome = json.loads(restated["outcome_diagnostics_json"])["outcome"]
    assert outcome["realized_pnl"] == 0.62 and outcome["position_won"] is True

    assert _cash(store, RESEARCH_V6) - cash[RESEARCH_V6] == pytest.approx(3.0)
    assert _cash(store, LIVE_STABILITY_ACCOUNT_ID) == cash[LIVE_STABILITY_ACCOUNT_ID]
    assert _cash(store, TARGET_POLICY.account_id) == cash[TARGET_POLICY.account_id]

    after = _snapshot(db_path)
    new_ledger = [row for row in after["paper_account_ledger"]
                  if row not in before["paper_account_ledger"]]
    assert len(new_ledger) == 1
    assert len(after["paper_account_ledger"]) == len(before["paper_account_ledger"]) + 1
    assert [row for row in after["paper_orders"] if row[0] != lot] == [
        row for row in before["paper_orders"] if row[0] != lot
    ]
    assert after["paper_accounts"] == before["paper_accounts"]

    audit = store.account_ledger(account_id=RESEARCH_V6)[-1]
    assert audit["event_type"] == "SETTLEMENT_RESTATEMENT"
    assert audit["order_id"] == lot
    details = json.loads(audit["details_json"])
    assert details["reason"] == "audit F6"
    assert details["truth_source"] == "cli_settlement"
    assert details["before"]["settlement_high_f"] == 72
    assert details["before"]["position_won"] is False
    assert details["after"]["settlement_high_f"] == 71
    assert details["after"]["position_won"] is True
    assert round(details["before"]["realized_pnl"], 2) == -2.38
    assert round(details["after"]["realized_pnl"], 2) == 0.62

    assert store.account_order_ledger_reconciliation(RESEARCH_V6)["status"] == "reconciled"
    # The flag clears and the lot is evidence again: its order, its verification
    # and its market-day outcome all say 71.
    verification = _table(db_path, "paper_settlement_verifications")
    assert (lot, TICKER, TARGET, 71.0, 71.0, "MATCH") in [
        (row[0], *row[2:]) for row in verification
    ]
    entry = {int(e["order_id"]): e for e in restate(db_path)["orders"]}[lot]
    assert "SETTLEMENT_MISMATCH" not in entry["findings"]
    assert "OUTCOME_DIAGNOSTICS_MISMATCH" not in entry["findings"]
    assert "REALIZED_PNL_MISMATCH" not in entry["findings"]
    recheck = store.recheck_paper_settlements(
        {(SERIES, TARGET): 71}, intervals={SERIES: (TARGET, TARGET)}
    )
    # The Live lot sits on another city-day and keeps its own booking.
    assert recheck["drift"] == []
    day = {
        (row["market_ticker"]): (row["settlement_high_f"], row["resolved_yes"])
        for row in store.market_day_settlements()
    }
    assert day[TICKER] == (71.0, 0)


def test_a_second_apply_changes_nothing_and_says_so(book):
    root, db_path, _, lot, _ = book
    _store_final(root, TARGET, 71)
    assert _apply(root, db_path, lot)[0] == 0
    applied = _snapshot(db_path)

    code, out, err = _apply(root, db_path, lot)

    assert code == 0, err
    assert "already applied, nothing changed" in out
    assert _snapshot(db_path) == applied


def test_a_repeat_with_a_different_delta_refuses_instead_of_saying_applied(book):
    root, db_path, _, lot, _ = book
    _store_final(root, TARGET, 71)
    assert _apply(root, db_path, lot)[0] == 0
    applied = _snapshot(db_path)

    code, out, err = _apply(root, db_path, lot, delta="99.00")

    assert code == 1
    assert "already applied at delta +3.00" in err
    assert "already applied, nothing changed" not in out
    assert _snapshot(db_path) == applied


def test_every_applied_move_keeps_its_audit_event(book):
    root, db_path, store, lot, _ = book
    moves = ((71, "3.00"), (70, "0.00"), (71, "0.00"), (70, "0.00"))
    for high, delta in moves:
        _store_final(root, TARGET, high)
        assert _apply(root, db_path, lot, delta=delta)[0] == 0

    events = [
        row
        for row in store.account_ledger(account_id=RESEARCH_V6)
        if row["event_type"] == "SETTLEMENT_RESTATEMENT"
    ]
    assert len(events) == len(moves)


def test_market_day_write_is_scoped_to_the_lots_own_market(book):
    root, db_path, store, lot, _ = book
    _store_final(root, TARGET, 71)
    rows = {row["market_ticker"]: dict(row) for row in store.market_day_settlements()}
    assert len(rows) == 2

    assert _apply(root, db_path, lot)[0] == 0

    after = {row["market_ticker"]: dict(row) for row in store.market_day_settlements()}
    assert after[TICKER]["settlement_high_f"] == 71.0
    assert {k: v for k, v in after.items() if k != TICKER} == {
        k: v for k, v in rows.items() if k != TICKER
    }


def test_a_failed_market_day_write_undoes_the_whole_apply(book, monkeypatch):
    root, db_path, _, lot, _ = book
    _store_final(root, TARGET, 71)
    before = _snapshot(db_path)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("market-day table unavailable")

    monkeypatch.setattr("sfo_kalshi_quant.db.record_market_day_settlements", boom)
    code, out, _ = _apply(root, db_path, lot)

    assert code == 1
    assert "applied" not in out
    assert _snapshot(db_path) == before


def test_the_correction_reaches_research_sizing_equity_but_no_live_input(book):
    root, db_path, store, lot, _ = book
    _store_final(root, TARGET, 71)
    research = store.paper_equity(1000.0, risk_profile="research")
    live = store.paper_equity(
        1000.0, risk_profile="live", account_id=LIVE_STABILITY_ACCOUNT_ID
    )

    assert _apply(root, db_path, lot)[0] == 0

    assert round(store.paper_equity(1000.0, risk_profile="research") - research, 2) == 3.0
    assert store.paper_equity(
        1000.0, risk_profile="live", account_id=LIVE_STABILITY_ACCOUNT_ID
    ) == live


def test_apply_touches_only_the_named_lot_of_several_flagged(book):
    root, db_path, store, lot, _ = book
    other_day = "2026-10-21"
    other = store.record_paper_order(other_day, _bin_lot(other_day))
    _into_account(db_path, other, TARGET_POLICY_V6)
    assert store.settle_paper_orders(other_day, 74.0, series_ticker=SERIES) == 1
    _store_final(root, other_day, 80)
    _store_final(root, TARGET, 71)
    flagged = _order(db_path, other)

    assert _apply(root, db_path, lot)[0] == 0

    assert dict(_order(db_path, other)) == dict(flagged)


def test_without_apply_the_restate_stays_a_dry_run(book):
    root, db_path, _, lot, _ = book
    _store_final(root, TARGET, 71)
    before = _snapshot(db_path)

    code = main(
        [
            "--forecaster-root", str(root), "--db-path", str(db_path), "--no-color",
            "paper-resettle", "--verify", "--restate", "--days", "60",
            "--order-id", str(lot), "--expected-delta", "3.00",
        ]
    )

    assert code == 0
    assert _snapshot(db_path) == before


@pytest.mark.parametrize("delta", ["2.99", "-3.00", "nan"])
def test_apply_refuses_any_delta_but_the_computed_one(book, delta):
    root, db_path, _, lot, _ = book
    _store_final(root, TARGET, 71)
    before = _snapshot(db_path)

    code, _, err = _apply(root, db_path, lot, delta=delta)

    assert code == 1
    assert "computed delta +3.00" in err
    assert _snapshot(db_path) == before


def test_apply_refuses_a_missing_final(book):
    root, db_path, _, lot, _ = book
    before = _snapshot(db_path)

    code, _, err = _apply(root, db_path, lot)

    assert code == 1
    assert "no confirmed final truth" in err
    assert _snapshot(db_path) == before


def test_apply_refuses_a_preliminary_final(book):
    root, db_path, _, lot, _ = book
    with closing(sqlite3.connect(root / "weather.db")) as conn:
        city_truth.upsert_settlement(conn, "KLAX", TARGET, 71, is_final=False)
        conn.commit()
    before = _snapshot(db_path)

    code, _, err = _apply(root, db_path, lot)

    assert code == 1
    assert "no confirmed final truth" in err
    assert _snapshot(db_path) == before


@pytest.mark.parametrize(
    ("result", "value"), [("yes", None), ("no", 69.0)], ids=["result", "value"]
)
def test_apply_refuses_when_the_exchange_contradicts_the_final(book, result, value):
    root, db_path, _, lot, _ = book
    _store_final(root, TARGET, 71)
    _seed_exchange(db_path, result, value)
    before = _snapshot(db_path)

    code, _, err = _apply(root, db_path, lot)

    assert code == 1
    assert "exchange does not confirm 71F" in err
    assert _snapshot(db_path) == before


def test_an_agreeing_exchange_result_is_kept_in_the_audit_record(book):
    root, db_path, store, lot, _ = book
    _store_final(root, TARGET, 71)
    _seed_exchange(db_path, "no", 71.0)

    code, out, err = _apply(root, db_path, lot)

    assert code == 0, err
    assert "exchange=MATCH" in out
    audit = store.account_ledger(account_id=RESEARCH_V6)[-1]
    assert json.loads(audit["details_json"])["exchange_verdict"] == "MATCH"


def test_apply_refuses_a_lot_that_is_still_open(tmp_path):
    root = _forecaster_root(tmp_path)
    _store_final(root, TARGET, 71)
    db_path = tmp_path / "paper.db"
    store = PaperStore(db_path)
    lot = store.record_paper_order(TARGET, _lot_3687())
    _into_account(db_path, lot, TARGET_POLICY_V6)
    before = _snapshot(db_path)

    code, _, err = _apply(root, db_path, lot)

    assert code == 1
    assert "not settled (status PAPER_FILLED)" in err
    assert _snapshot(db_path) == before


def test_apply_refuses_an_expired_lot(book):
    root, db_path, _, lot, _ = book
    _store_final(root, TARGET, 71)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("UPDATE paper_orders SET status='PAPER_EXPIRED' WHERE id=?", (lot,))
        conn.commit()
    before = _snapshot(db_path)

    code, _, err = _apply(root, db_path, lot)

    assert code == 1
    assert "not settled (status PAPER_EXPIRED)" in err
    assert _snapshot(db_path) == before


def test_apply_refuses_a_lot_in_the_m1_ledger(book):
    root, db_path, _, lot, _ = book
    _store_final(root, TARGET, 71)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "UPDATE paper_orders SET account_id=? WHERE id=?", (M1_ACCOUNT_ID, lot)
        )
        conn.commit()
    before = _snapshot(db_path)

    code, _, err = _apply(root, db_path, lot)

    assert code == 1
    assert f"ledger {M1_ACCOUNT_ID} is not archived" in err
    assert _snapshot(db_path) == before


def test_apply_refuses_a_lot_in_the_active_v7_ledger(book):
    root, db_path, _, lot, _ = book
    _store_final(root, TARGET, 71)
    _into_account(db_path, lot, TARGET_POLICY)
    before = _snapshot(db_path)

    code, _, err = _apply(root, db_path, lot)

    assert code == 1
    assert f"ledger {TARGET_POLICY.account_id} is not archived" in err
    assert _snapshot(db_path) == before


def test_apply_refuses_a_lot_in_the_active_live_ledger(book):
    root, db_path, _, _, live = book
    _store_final(root, TARGET, 74)
    before = _snapshot(db_path)

    code, _, err = _apply(root, db_path, live, delta="0.00")

    assert code == 1
    assert f"ledger {LIVE_STABILITY_ACCOUNT_ID} is not archived" in err
    assert _snapshot(db_path) == before


def test_apply_rolls_back_when_the_ledger_would_not_reconcile(book):
    root, db_path, store, lot, _ = book
    _store_final(root, TARGET, 71)
    other_day = "2026-10-21"
    broken = store.record_paper_order(other_day, _bin_lot(other_day))
    _into_account(db_path, broken, TARGET_POLICY_V6)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "DELETE FROM paper_account_ledger WHERE order_id=? AND event_type='ENTRY_FILL'",
            (broken,),
        )
        conn.commit()
    before = _snapshot(db_path)

    code, _, err = _apply(root, db_path, lot)

    assert code == 1
    assert "would not reconcile" in err
    assert _snapshot(db_path) == before


@pytest.mark.parametrize(
    "drop", ["--restate", "--order-id", "--expected-delta", "--reason"]
)
def test_apply_needs_every_explicit_argument(book, drop):
    root, db_path, _, lot, _ = book
    _store_final(root, TARGET, 71)
    before = _snapshot(db_path)
    arguments = {
        "--restate": [],
        "--order-id": [str(lot)],
        "--expected-delta": ["3.00"],
        "--reason": ["audit F6"],
    }
    del arguments[drop]
    command = [
        "--forecaster-root", str(root), "--db-path", str(db_path), "--no-color",
        "paper-resettle", "--verify", "--apply",
        *(part for flag, values in arguments.items() for part in (flag, *values)),
    ]
    err = io.StringIO()
    with redirect_stderr(err):
        code = main(command)

    assert code == 1
    assert "--apply needs" in err.getvalue()
    assert _snapshot(db_path) == before


def test_apply_refuses_while_a_sibling_lot_keeps_the_old_high(book):
    root, db_path, store, lot, _ = book
    _store_final(root, TARGET, 71)
    sibling = store.record_paper_order(TARGET, _bin_lot(TARGET))
    assert store.settle_paper_orders(TARGET, 72.0, series_ticker=SERIES) == 1
    before = _snapshot(db_path)

    code, _, err = _apply(root, db_path, lot)

    assert code != 0 and str(sibling) in err
    assert _snapshot(db_path) == before


def test_apply_refuses_while_an_expired_sibling_keeps_the_old_high(book):
    root, db_path, store, lot, _ = book
    _store_final(root, TARGET, 71)
    sibling = store.record_paper_order(TARGET, _bin_lot(TARGET))
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "UPDATE paper_orders SET status='PAPER_EXPIRED', "
            "settlement_high_f=72 WHERE id=?",
            (sibling,),
        )
        conn.commit()
    before = _snapshot(db_path)

    code, _, err = _apply(root, db_path, lot)

    assert code != 0 and str(sibling) in err
    assert _snapshot(db_path) == before


def test_apply_replaces_a_stale_exchange_mismatch_verdict(book):
    root, db_path, store, lot, _ = book
    _store_final(root, TARGET, 71)
    _seed_exchange(db_path, "no", 71.0)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO paper_settlement_exchange_checks "
            "(order_id, checked_at, market_ticker, target_date, "
            "verification_status) VALUES (?, 'x', ?, ?, 'MISMATCH')",
            (lot, TICKER, TARGET),
        )
        conn.commit()

    code, _, err = _apply(root, db_path, lot)
    assert code == 0, err

    with closing(sqlite3.connect(db_path)) as conn:
        status = conn.execute(
            "SELECT verification_status FROM paper_settlement_exchange_checks "
            "WHERE order_id=?",
            (lot,),
        ).fetchone()[0]
    assert status == "MATCH"
