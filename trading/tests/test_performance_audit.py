"""Profit reporting must preserve account boundaries and losing partial exits."""
import importlib.util
from pathlib import Path

import pytest

from test_logical_positions import _paper_order

_spec = importlib.util.spec_from_file_location(
    "performance_audit", Path(__file__).resolve().parents[2] / "scripts/audit_paper_performance.py"
)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


def _export():
    root = _paper_order(1, contracts=6, status="PAPER_FILLED", realized_pnl=None,
                        exit_price=None, closed_at=None)
    root.update(requested_contracts=10, filled_contracts=8, fee_per_contract=.01)
    loss = _paper_order(2, parent_order_id=1, contracts=2, realized_pnl=-.16,
                        closed_at="2026-07-16T21:00:00+00:00")
    loss["fee_per_contract"] = .01
    win = _paper_order(3, realized_pnl=1, closed_at="2026-07-15T21:00:00+00:00")
    win.update(account_id="other", requested_contracts=2, filled_contracts=2)
    partial = _paper_order(4, realized_pnl=.4, closed_at="2026-07-19T09:00:00+00:00")
    partial.update(requested_contracts=2, filled_contracts=2)
    return {"captured_at": "2026-07-19T12:00:00+00:00", "paper": {
        "paper_accounts": {"rows": [{"account_id": name, "initial_capital": 1000,
            "status": "ACTIVE", "created_at": "2026-07-15T20:00:00+00:00"}
            for name in ("paper-shared", "other")]},
        "paper_orders": {"rows": [root, loss, win, partial]}}}


def test_account_separation_partial_exits_and_calendar_denominator():
    report = audit.build_report(_export())
    first, other = report["accounts"]
    assert first["invalid_roots_excluded"] == 0
    assert first["net_realized_pnl"] == pytest.approx(.24)
    assert first["resolved_logical_positions"] == 1  # partial root still open
    assert first["resolved_entry_capital"] == pytest.approx(4 * .93)
    assert first["filled_contracts"] == 10  # child is not another entry
    assert first["requested_contracts"] == 12
    assert first["entry_fees_on_resolved_lots"] == pytest.approx(.02)
    assert first["exit_fees_on_resolved_lots"] == pytest.approx(.04)
    assert [d["realized_pnl"] for d in first["daily_history"]] == [-.16, 0, 0]
    assert first["mean_complete_calendar_day_pnl"] == pytest.approx(-.16 / 3)
    assert first["current_partial_day_pnl"] == .4
    assert other["net_realized_pnl"] == 1
    assert other["activation_day_pnl"] == 1
    assert other["mean_complete_calendar_day_pnl"] == 0
    assert "closing_equity" not in first["daily_history"][0]


def test_future_booking_fails_instead_of_contaminating_snapshot():
    export = _export()
    export["paper"]["paper_orders"]["rows"][-1]["closed_at"] = "2026-07-20T00:00:00+00:00"
    with pytest.raises(ValueError, match="after the evidence snapshot"):
        audit.build_report(export)


@pytest.mark.parametrize("target", [0, -1, float("nan"), float("inf")])
def test_invalid_target_is_rejected(target):
    with pytest.raises(ValueError, match="daily target"):
        audit.build_report(_export(), daily_target=target)


def test_unassigned_legacy_losses_and_partial_exits_stay_visible_without_account_reassignment():
    export = _export()
    baseline_accounts = audit.build_report(export)["accounts"]
    loss = _paper_order(5, realized_pnl=-2.0)
    open_root = _paper_order(6, contracts=6, status="PAPER_FILLED", realized_pnl=None,
                             exit_price=None, closed_at=None)
    partial_loss = _paper_order(7, parent_order_id=6, contracts=2, realized_pnl=-.16,
                               closed_at="2026-07-17T21:00:00+00:00")
    invalid = _paper_order(8, contracts=-1, realized_pnl=-99.0)
    for row in (loss, open_root, partial_loss, invalid):
        row.update(account_id=None, risk_profile="research")
    export["paper"]["paper_orders"]["rows"].extend([loss, open_root, partial_loss, invalid])

    report = audit.build_report(export)
    assert report["accounts"] == baseline_accounts
    history, = report["unassigned_histories"]
    assert history["owner_kind"] == "null" and history["raw_owner"] is None
    assert history["valid_roots"] == 2 and history["invalid_roots_excluded"] == 1
    assert history["resolved_logical_positions"] == 1
    assert history["resolved_lots"] == 2
    assert history["open_roots_with_resolved_lots"] == 1
    assert history["net_realized_pnl"] == pytest.approx(-2.16)
    assert history["role_profile_breakdown"]["research"]["net_realized_pnl"] == pytest.approx(-2.16)
    assert [(day["date"], day["realized_pnl"]) for day in history["daily_history"]] == [
        ("2026-07-15", -2.0), ("2026-07-17", -.16),
    ]
    assert history["daily_history"][-1]["cumulative_attributed_pnl"] == pytest.approx(-2.16)
    for key in ("initial_capital", "realized_equity", "closing_equity", "roi", "mean_complete_calendar_day_pnl"):
        assert key not in history


def test_unassigned_null_empty_missing_and_other_owners_remain_distinct():
    export = _export()
    rows = [_paper_order(order_id) for order_id in range(5, 9)]
    rows[0]["account_id"] = None
    rows[1]["account_id"] = ""
    rows[2].pop("account_id")
    rows[3]["account_id"] = "legacy-owner-without-ledger"
    export["paper"]["paper_orders"]["rows"].extend(rows)
    histories = audit.build_report(export)["unassigned_histories"]
    assert {row["owner_kind"] for row in histories} == {"null", "empty", "missing", "unmatched"}
    assert all(row["valid_roots"] == 1 and row["net_realized_pnl"] == -.16 for row in histories)


@pytest.mark.parametrize("owner_kind", ["null", "empty", "missing", "unmatched"])
def test_future_unassigned_bookings_fail_instead_of_contaminating_snapshot(owner_kind):
    export = _export()
    row = _paper_order(5, closed_at="2026-07-20T00:00:00+00:00")
    if owner_kind == "missing":
        row.pop("account_id")
    else:
        row["account_id"] = {"null": None, "empty": "", "unmatched": "unmatched-owner"}[owner_kind]
    export["paper"]["paper_orders"]["rows"].append(row)
    with pytest.raises(ValueError, match="after the evidence snapshot"):
        audit.build_report(export)
