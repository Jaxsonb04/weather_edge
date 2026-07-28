from __future__ import annotations

import inspect
import pickle
import resource
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

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


# ---------------------------------------------------------------------------
# 2026-07-28: PaperStore.connect leaked one descriptor per call, because
# sqlite3.Connection.__exit__ commits but does not close. That is what made the
# suite look non-deterministic on a 256-descriptor host.
# ---------------------------------------------------------------------------


def test_connect_releases_the_handle_when_the_with_block_exits() -> None:
    with TemporaryDirectory() as tmp:
        store = db.PaperStore(Path(tmp) / "p.db")
        with store.connect() as conn:
            conn.execute("SELECT 1").fetchone()
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError as exc:
            assert "closed" in str(exc).lower()
        else:
            raise AssertionError(
                "connect() handle survived its with block; it is leaking"
            )


def test_connect_still_commits_on_clean_exit_and_rolls_back_on_error() -> None:
    """Closing must not change the transaction contract the call sites rely on."""

    with TemporaryDirectory() as tmp:
        store = db.PaperStore(Path(tmp) / "p.db")

        with store.connect() as conn:
            conn.execute(
                "INSERT INTO paper_accounts "
                "(account_id, created_at, initial_capital, opening_cash, "
                " high_water_equity) VALUES ('t-commit', 'x', 1.0, 1.0, 1.0)"
            )

        try:
            with store.connect() as conn:
                conn.execute(
                    "INSERT INTO paper_accounts "
                    "(account_id, created_at, initial_capital, opening_cash, "
                    " high_water_equity) VALUES ('t-rollback', 'x', 1.0, 1.0, 1.0)"
                )
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        with store.connect() as conn:
            ids = {
                row[0]
                for row in conn.execute(
                    "SELECT account_id FROM paper_accounts "
                    "WHERE account_id LIKE 't-%'"
                )
            }
        assert "t-commit" in ids, "clean exit must still commit"
        assert "t-rollback" not in ids, "exception must still roll back"


def test_many_sequential_connects_do_not_exhaust_descriptors() -> None:
    """The regression itself, under a deliberately low descriptor cap."""

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    capped = min(96, hard) if hard != resource.RLIM_INFINITY else 96
    with TemporaryDirectory() as tmp:
        store = db.PaperStore(Path(tmp) / "p.db")
        resource.setrlimit(resource.RLIMIT_NOFILE, (capped, hard))
        try:
            for _ in range(400):
                with store.connect() as conn:
                    conn.execute("SELECT 1").fetchone()
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
