"""Exit causes require persisted execution evidence, independent of P&L."""

import pytest

from sfo_kalshi_quant.exit_audit import audited_exit_reason
from sfo_kalshi_quant.summary import _exit_reason_breakdown


@pytest.mark.parametrize("pnl", [-3.0, 0.0, 3.0, None])
def test_legacy_close_does_not_invent_exit_cause_from_pnl(pnl):
    assert audited_exit_reason({"status": "PAPER_CLOSED", "realized_pnl": pnl}) == "unclassified"


@pytest.mark.parametrize("action", ["HOLD_MODEL_VETO", "HOLD_STOP_LOSS", "HOLD_TAKE_PROFIT"])
def test_hold_action_is_not_evidence_of_a_close(action):
    row = {"status": "PAPER_CLOSED", "realized_pnl": -3.0,
           "outcome_diagnostics_json": {"action": action}}
    assert audited_exit_reason(row) == "unclassified"


@pytest.mark.parametrize("action,expected", [
    ("CLOSE_STOP_LOSS", "stop_loss"),
    ("CLOSE_TAKE_PROFIT", "take_profit"),
    ("BREAK_EVEN", "break_even"),
])
def test_explicit_execution_action_determines_cause_even_when_pnl_differs(action, expected):
    row = {"status": "PAPER_CLOSED", "realized_pnl": -3.0,
           "outcome_diagnostics_json": {"exit_execution": {"monitor_action": action}}}
    assert audited_exit_reason(row) == expected


def test_conflicting_explicit_close_actions_are_unclassified():
    row = {"status": "PAPER_CLOSED", "realized_pnl": 3.0,
           "outcome_diagnostics_json": {"exit_reason": "STOP_LOSS",
             "exit_execution": {"monitor_action": "CLOSE_TAKE_PROFIT"}}}
    assert audited_exit_reason(row) == "unclassified"


def test_open_order_does_not_get_an_exit_category_from_stale_action():
    row = {"status": "PAPER_FILLED", "outcome_diagnostics_json": {"action": "STOP_LOSS"}}
    assert audited_exit_reason(row) == "unclassified"


def test_partial_expiry_is_still_an_open_filled_position():
    row = {"status": "PAPER_PARTIAL_EXPIRED", "contracts": 2.0, "filled_contracts": 2.0}
    assert audited_exit_reason(row) == "unclassified"


def test_summary_counts_unknown_closed_causes_without_counting_open_orders():
    counts = _exit_reason_breakdown([
        {"status": "PAPER_CLOSED", "realized_pnl": -3.0},
        {"status": "PAPER_CLOSED", "realized_pnl": 3.0},
        {"status": "PAPER_FILLED"},
    ])
    assert counts["closed_unclassified"] == 2
    assert counts["closed_stop_loss"] == counts["closed_take_profit"] == 0
