from __future__ import annotations

import inspect

from sfo_kalshi_quant import db
from sfo_kalshi_quant.store import diagnostics, scoring
from sfo_kalshi_quant.store import schema
from sfo_kalshi_quant.account import policy_capacity


def test_db_facade_reexports_store_schema_constants_and_helpers() -> None:
    assert db.SCHEMA is schema.SCHEMA
    assert db.INDEXES is schema.INDEXES
    assert db.OPEN_POSITION_GUARD_INDEX is schema.OPEN_POSITION_GUARD_INDEX
    assert db._decision_diagnostics_payload is diagnostics._decision_diagnostics_payload
    assert db._sample_decision_rows is scoring._sample_decision_rows


def test_paper_store_scoring_methods_come_from_store_package() -> None:
    assert db.PaperStore.init is schema.init_store
    assert db.PaperStore._ensure_open_position_guard_index is schema.ensure_open_position_guard_index
    assert db.PaperStore.sampled_decision_rows is scoring.sampled_decision_rows
    assert db.PaperStore.signal_backtest_summary is scoring.signal_backtest_summary
    assert db.PaperStore.market_backtest_summary is scoring.market_backtest_summary
    assert isinstance(inspect.getattr_static(db.PaperStore, "_record_ledger_event"), staticmethod)


def test_policy_capacity_is_pure_and_applies_account_risk_rooms() -> None:
    result = policy_capacity(
        state={
            "realized_equity": 1000.0,
            "drawdown": 0.0,
            "available_cash": 1000.0,
        },
        active_rows=[],
        daily_pnl=0.0,
        target_date="2026-07-11",
        market_ticker="KXHIGHTSFO-TEST-B70",
        risk_profile="live",
        requested_spend=100.0,
    )

    assert result == {"allowed_spend": 30.0, "reason": None}
