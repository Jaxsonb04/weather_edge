from __future__ import annotations

import inspect
import pickle
from pathlib import Path

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


def test_paper_store_declares_facade_methods_and_reexports_store_implementations() -> None:
    assert db.init_store is schema.init_store
    assert db.ensure_open_position_guard_index is schema.ensure_open_position_guard_index
    assert db.sampled_decision_rows is scoring.sampled_decision_rows
    assert db.signal_backtest_summary is scoring.signal_backtest_summary
    assert db.market_backtest_summary is scoring.market_backtest_summary
    assert isinstance(inspect.getattr_static(db.PaperStore, "_record_ledger_event"), staticmethod)


def test_declared_store_methods_have_stable_descriptor_names_and_pickle_round_trip() -> None:
    store = db.PaperStore(Path("unused.db"), init=False)

    for name in (
        "init",
        "_ensure_open_position_guard_index",
        "market_backtest_summary",
        "sampled_decision_rows",
        "signal_backtest_summary",
    ):
        method = getattr(store, name)
        assert method.__name__ == name
        restored = pickle.loads(pickle.dumps(method))
        assert restored.__name__ == name
        assert restored.__self__.db_path == store.db_path


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
