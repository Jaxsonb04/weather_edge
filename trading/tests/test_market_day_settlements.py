"""The settlement blind spot, and the `resolved_yes` semantics defect.

Part 1 -- the blind spot
    A settlement high was only ever recorded onto orders that were *still open*
    when the day settled. A market-day whose every lot exited early left no
    record of what the market did, so the population where the losses actually
    live was structurally invisible and no exit rule could be judged on it.
    ``market_day_settlements`` records the outcome of every traded market-day.

Part 2 -- the semantics defect
    ``close_paper_order`` wrote ``sign(realized_pnl)`` into ``resolved_yes``, a
    column named after the market's outcome. On production that value is a
    perfect function of the P&L sign (764/764 rows) and disagrees with the true
    market outcome on ~40% of the rows where truth is obtainable. The position
    fact moves to ``position_won``; ``resolved_yes`` keeps its real meaning.
"""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.restatement import _closed_accounting_findings
from sfo_kalshi_quant.store.market_day_settlements import (
    TRUTH_SOURCE_DATASET_MARKET,
    TRUTH_SOURCE_SETTLED_SIBLING,
    TRUTH_SOURCE_SETTLEMENT_PATH,
)


def _decision(ticker: str, *, floor: float, cap: float, yes_ask: float = 0.30) -> TradeDecision:
    return TradeDecision(
        ticker=ticker,
        label=f"{floor:.0f}° to {cap:.0f}°",
        action="BUY_YES",
        approved=True,
        probability=0.30,
        probability_lcb=0.20,
        yes_bid=max(0.01, yes_ask - 0.01),
        yes_ask=yes_ask,
        spread=0.01,
        fee_per_contract=0.006,
        cost_per_contract=yes_ask + 0.006,
        edge=0.06,
        edge_lcb=0.02,
        kelly_fraction=0.01,
        recommended_contracts=1.0,
        expected_profit=0.3,
        reasons=[],
        side="YES",
        strike_type="between",
        floor_strike=floor,
        cap_strike=cap,
    )


def _rows(store: PaperStore) -> dict[str, sqlite3.Row]:
    return {str(row["market_ticker"]): row for row in store.market_day_settlements()}


# ---------------------------------------------------------------------------
# Part 1: the blind spot
# ---------------------------------------------------------------------------


def test_fully_exited_market_day_is_recorded_when_a_sibling_settles():
    """The core blind spot: a market-day with no surviving lot used to vanish."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        held = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )
        exited = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B70.5", floor=70.0, cap=71.0)
        )
        store.close_paper_order(exited, 0.50)  # exits early, before settlement

        assert store.settle_paper_orders("2026-06-12", 67.0) == 1

        recorded = _rows(store)
        # Both market-days are present, not just the one that held to the end.
        assert set(recorded) == {"KXHIGHTSFO-TEST-B66.5", "KXHIGHTSFO-TEST-B70.5"}

        held_row = recorded["KXHIGHTSFO-TEST-B66.5"]
        assert held_row["resolved_yes"] == 1  # 67 lands inside 66-67
        assert held_row["settled_lots"] == 1 and held_row["closed_lots"] == 0

        # This is the row that did not exist before: the market's own outcome
        # for a day the book traded and then fully exited.
        exited_row = recorded["KXHIGHTSFO-TEST-B70.5"]
        assert exited_row["resolved_yes"] == 0  # 67 is outside 70-71
        assert exited_row["settled_lots"] == 0 and exited_row["closed_lots"] == 1
        assert exited_row["settlement_high_f"] == 67.0
        assert exited_row["truth_source"] == TRUTH_SOURCE_SETTLEMENT_PATH
        assert exited_row["series_ticker"] == "KXHIGHTSFO"
        assert exited_row["realized_pnl"] == pytest.approx(
            float(store.paper_order(exited)["realized_pnl"])
        )
        assert store.paper_order(held) is not None


def test_recording_is_idempotent_and_never_downgrades_authority():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )
        store.settle_paper_orders("2026-06-12", 67.0)
        first = _rows(store)["KXHIGHTSFO-TEST-B66.5"]

        # Re-running the same settlement changes nothing.
        store.settle_paper_orders("2026-06-12", 67.0)
        again = _rows(store)["KXHIGHTSFO-TEST-B66.5"]
        assert again["resolved_yes"] == first["resolved_yes"]
        assert again["truth_source"] == first["truth_source"]

        # A weaker backfill source cannot overwrite the settlement path's value.
        store.record_market_day_settlements(
            "2026-06-12", 90.0, truth_source=TRUTH_SOURCE_DATASET_MARKET
        )
        preserved = _rows(store)["KXHIGHTSFO-TEST-B66.5"]
        assert preserved["settlement_high_f"] == 67.0
        assert preserved["resolved_yes"] == 1
        assert preserved["truth_source"] == TRUTH_SOURCE_SETTLEMENT_PATH


def test_unrecorded_dates_surface_the_day_nothing_survived_to_settlement():
    """The residual the settle path cannot reach: a wholly-exited target date."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        order_id = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )
        store.close_paper_order(order_id, 0.50)

        # Nothing is open, so auto-settle would never call settle_paper_orders
        # for this date at all -- which is exactly why it was invisible.
        assert store.open_paper_target_dates() == []
        assert store.unrecorded_traded_target_dates() == ["2026-06-12"]

        summary = store.record_market_day_settlements(
            "2026-06-12", 67.0, series_ticker="KXHIGHTSFO"
        )
        assert summary["market_days_recorded"] == 1
        assert store.unrecorded_traded_target_dates() == []
        assert _rows(store)["KXHIGHTSFO-TEST-B66.5"]["resolved_yes"] == 1


def test_backfill_uses_settled_siblings_and_finalized_exchange_results():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        # Day 1: one lot settles, a sibling ticker on the same day fully exits.
        store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )
        sibling = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B70.5", floor=70.0, cap=71.0)
        )
        store.close_paper_order(sibling, 0.50)
        # Day 2: fully exited, and only the exchange remembers what happened.
        dataset_only = store.record_paper_order(
            "2026-06-13", _decision("KXHIGHTSFO-TEST-B80.5", floor=80.0, cap=81.0)
        )
        store.close_paper_order(dataset_only, 0.50)
        # Day 3: fully exited and no surviving truth at all.
        orphan = store.record_paper_order(
            "2026-06-14", _decision("KXHIGHTSFO-TEST-B90.5", floor=90.0, cap=91.0)
        )
        store.close_paper_order(orphan, 0.50)

        with sqlite3.connect(db_path) as conn:
            # Settle day 1 directly so paper_orders carries the sibling high,
            # then drop the observability rows to simulate historical data.
            conn.execute(
                "UPDATE paper_orders SET status='PAPER_SETTLED', settled_at='2026-06-13T00:00:00+00:00', "
                "settlement_high_f=67.0, resolved_yes=1, position_won=1, realized_pnl=0.7 "
                "WHERE market_ticker='KXHIGHTSFO-TEST-B66.5'"
            )
            conn.execute("DELETE FROM market_day_settlements")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dataset_kalshi_markets (
                    ticker TEXT PRIMARY KEY, event_ticker TEXT NOT NULL,
                    target_date TEXT, market_status TEXT, result TEXT,
                    raw_json TEXT NOT NULL, fetched_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO dataset_kalshi_markets "
                "(ticker, event_ticker, target_date, market_status, result, raw_json, fetched_at) "
                "VALUES ('KXHIGHTSFO-TEST-B80.5','KXHIGHTSFO-TEST','2026-06-13','finalized','yes','{}','x')"
            )

        preview = store.backfill_market_day_settlements(dry_run=True)
        assert preview["dry_run"] is True
        assert store.market_day_settlements() == []

        summary = store.backfill_market_day_settlements()
        assert summary["traded_market_days"] == 4
        # Both day-1 tickers resolve off the settled sibling's high.
        assert summary["recorded_from_settled_sibling"] == 2
        assert summary["recorded_from_dataset_markets"] == 1
        unrecoverable = summary["unrecoverable"]
        assert [entry["market_ticker"] for entry in unrecoverable] == [
            "KXHIGHTSFO-TEST-B90.5"
        ]
        assert "no settled sibling" in unrecoverable[0]["reason"]

        recorded = _rows(store)
        assert recorded["KXHIGHTSFO-TEST-B70.5"]["truth_source"] == TRUTH_SOURCE_SETTLED_SIBLING
        assert recorded["KXHIGHTSFO-TEST-B70.5"]["resolved_yes"] == 0
        assert recorded["KXHIGHTSFO-TEST-B80.5"]["truth_source"] == TRUTH_SOURCE_DATASET_MARKET
        assert recorded["KXHIGHTSFO-TEST-B80.5"]["resolved_yes"] == 1
        # The exchange result carries no temperature, so none is invented.
        assert recorded["KXHIGHTSFO-TEST-B80.5"]["settlement_high_f"] is None
        assert "KXHIGHTSFO-TEST-B90.5" not in recorded

        # Idempotent: a second pass records nothing new.
        again = store.backfill_market_day_settlements()
        assert again["recorded_from_settled_sibling"] == 0
        assert again["recorded_from_dataset_markets"] == 0
        assert again["already_recorded"] == 3


def test_recording_does_not_alter_orders_ledger_or_pnl():
    """Evidence-cost-zero: the recorder writes one new table and nothing else."""

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )
        store.close_paper_order(order_id, 0.50)

        def snapshot() -> tuple:
            with sqlite3.connect(db_path) as conn:
                orders = conn.execute(
                    "SELECT * FROM paper_orders ORDER BY id"
                ).fetchall()
                ledger = conn.execute(
                    "SELECT * FROM paper_account_ledger ORDER BY id"
                ).fetchall()
                accounts = conn.execute(
                    "SELECT * FROM paper_accounts ORDER BY account_id"
                ).fetchall()
            return orders, ledger, accounts

        before = snapshot()
        store.record_market_day_settlements("2026-06-12", 67.0)
        store.backfill_market_day_settlements()
        assert snapshot() == before
        assert store.market_day_settlements()


def test_nothing_in_the_trading_path_reads_the_observability_table():
    """This table measures decisions; it must never make one."""

    package = Path(__file__).resolve().parents[1] / "sfo_kalshi_quant"
    allowed = {
        "store/market_day_settlements.py",  # the recorder itself
        "store/schema.py",                  # table creation
        "db.py",                            # recording + read-back accessor
        "_cli/paper.py",                    # settle/backfill operator commands
        "_cli/parser.py",                   # operator command registration
        "cli.py",                           # operator command dispatch
    }
    offenders = sorted(
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if "market_day_settlements" in path.read_text()
        and str(path.relative_to(package)) not in allowed
    )
    assert offenders == []


# ---------------------------------------------------------------------------
# Part 2: the resolved_yes semantics defect
# ---------------------------------------------------------------------------


def test_closed_row_never_claims_a_market_outcome():
    """The regression guard. A close happens before the market resolves."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        order_id = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )
        row = store.close_paper_order(order_id, 0.50)

        assert row["resolved_yes"] is None
        assert row["position_won"] == 1
        assert row["settlement_high_f"] is None

        outcome = json.loads(row["outcome_diagnostics_json"])["outcome"]
        assert "resolved_yes" not in outcome
        assert outcome["position_won"] is True
        assert "closed for positive PnL" in outcome["win_loss_reason"]


def test_win_loss_accounting_is_unchanged_by_the_split():
    """Deliberately unchanged: the readers inverted the old encoding, so
    win/loss and hit-rate were always right. Repointing them at ``position_won``
    must keep every number exactly where it was."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        winner = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )
        loser = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B70.5", floor=70.0, cap=71.0)
        )
        no_side = store.record_paper_order(
            "2026-06-12",
            replace(
                _decision("KXHIGHTSFO-TEST-B75.5", floor=75.0, cap=76.0),
                action="BUY_NO",
                side="NO",
            ),
        )
        store.close_paper_order(winner, 0.50)   # YES exits up
        store.close_paper_order(loser, 0.10)    # YES exits down
        store.close_paper_order(no_side, 0.50)  # NO exits up

        summary = store.market_backtest_summary()
        assert summary["orders"] == 3
        assert summary["wins"] == 2.0
        assert summary["losses"] == 1.0
        assert summary["hit_rate"] == pytest.approx(2 / 3)


def test_settled_row_keeps_the_real_market_outcome_on_both_fields():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        no_side = store.record_paper_order(
            "2026-06-12",
            replace(
                _decision("KXHIGHTSFO-TEST-B70.5", floor=70.0, cap=71.0),
                action="BUY_NO",
                side="NO",
            ),
        )
        assert store.settle_paper_orders("2026-06-12", 67.0) == 1
        row = store.paper_order(no_side)
        # The market resolved NO (67 is outside 70-71) and the NO position won:
        # two different facts, now in two different columns.
        assert row["resolved_yes"] == 0
        assert row["position_won"] == 1
        assert row["realized_pnl"] > 0


def test_legacy_closed_rows_migrate_losslessly():
    """The historical encoding carried no information the P&L did not already
    hold, so moving it to ``position_won`` cannot change any accounting."""

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        yes_id = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )
        no_id = store.record_paper_order(
            "2026-06-12",
            replace(
                _decision("KXHIGHTSFO-TEST-B70.5", floor=70.0, cap=71.0),
                action="BUY_NO",
                side="NO",
            ),
        )
        store.close_paper_order(yes_id, 0.50)
        store.close_paper_order(no_id, 0.10)
        before = store.market_backtest_summary()

        # Rewind to the defective on-disk shape and re-run the migration.
        with sqlite3.connect(db_path) as conn:
            for order_id in (yes_id, no_id):
                row = conn.execute(
                    "SELECT side, position_won, outcome_diagnostics_json FROM paper_orders WHERE id=?",
                    (order_id,),
                ).fetchone()
                won = bool(row[1])
                legacy = won if str(row[0]).upper() == "YES" else not won
                payload = json.loads(row[2])
                payload["outcome"]["resolved_yes"] = legacy
                conn.execute(
                    "UPDATE paper_orders SET resolved_yes=?, position_won=NULL, "
                    "outcome_diagnostics_json=? WHERE id=?",
                    (1 if legacy else 0, json.dumps(payload, sort_keys=True), order_id),
                )
            conn.execute(
                "DELETE FROM schema_migrations WHERE migration_key='closed_row_position_won_v1'"
            )

        migrated = PaperStore(db_path)
        assert migrated.market_backtest_summary() == before
        for order_id, expected_won in ((yes_id, 1), (no_id, 0)):
            row = migrated.paper_order(order_id)
            assert row["resolved_yes"] is None
            assert row["position_won"] == expected_won
            assert "resolved_yes" not in json.loads(row["outcome_diagnostics_json"])["outcome"]


def test_public_payload_drops_the_market_outcome_on_closed_rows():
    from sfo_kalshi_quant.strategy_lab.paper_card import _paper_row

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        closed_id = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )
        settled_id = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B70.5", floor=70.0, cap=71.0)
        )
        store.close_paper_order(closed_id, 0.50)
        store.settle_paper_orders("2026-06-12", 67.0)

        closed_payload = _paper_row(store.paper_order(closed_id), {})
        settled_payload = _paper_row(store.paper_order(settled_id), {})

        # The public artifact no longer publishes a market outcome for a row
        # that never saw one.
        assert closed_payload["resolved_yes"] is None
        assert closed_payload["position_won"] == 1
        # A genuinely settled row still reports the market's real outcome.
        assert settled_payload["resolved_yes"] == 0
        assert settled_payload["position_won"] == 0


def test_restatement_now_sees_a_closed_row_that_claims_a_market_outcome():
    """The meta-finding: the strictest harness structurally could not see this.

    ``_settled_accounting_findings`` was the only place ``resolved_yes`` was
    reconciled against real market truth, and it is reached only for
    ``PAPER_SETTLED`` rows -- then returns early unless ``settled_at`` parses.
    A closed row has no ``settled_at``, so the defect was invisible to it.
    """

    clean = {
        "status": "PAPER_CLOSED",
        "contracts": 1.0,
        "cost_per_contract": 0.306,
        "exit_price": 0.50,
        "exit_fee_per_contract": 0.0,
        "realized_pnl": 0.194,
        "resolved_yes": None,
        "position_won": 1,
        "side": "YES",
        "closed_at": "2026-06-12T18:00:00+00:00",
    }
    outcome = {
        "outcome": {
            "event": "close",
            "resolved_at": "2026-06-12T18:00:00+00:00",
            "position_won": True,
        }
    }
    assert _closed_accounting_findings(clean, outcome) == []

    defective = {**clean, "resolved_yes": 1}
    assert "CLOSED_ROW_CLAIMS_MARKET_OUTCOME" in _closed_accounting_findings(
        defective, outcome
    )
    stale_blob = {"outcome": {**outcome["outcome"], "resolved_yes": True}}
    assert "CLOSED_OUTCOME_CLAIMS_MARKET_OUTCOME" in _closed_accounting_findings(
        clean, stale_blob
    )
    flipped = {**clean, "position_won": 0}
    assert "CLOSED_POSITION_WON_MISMATCH" in _closed_accounting_findings(
        flipped, outcome
    )


def test_auto_settle_records_a_target_date_where_every_lot_exited_early():
    """The residual blind spot, end to end through the operator command.

    ``cmd_paper_auto_settle`` iterates ``open_paper_target_dates()``. A target
    date on which every lot was closed before settlement has no open positions,
    so it never reached ``settle_paper_orders`` at all and the market's own
    outcome for it was never recorded anywhere. The record-only pass covers it
    without touching a single order.
    """

    from sfo_kalshi_quant.cli import main as cli_main

    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        root.mkdir()
        with sqlite3.connect(root / "weather.db") as conn:
            conn.execute(
                "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
                "max_temperature_f INTEGER, is_final INTEGER NOT NULL DEFAULT 1)"
            )
            conn.execute(
                "INSERT INTO cli_settlements VALUES ('KSFO', '2026-01-10', 71, 1)"
            )
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            "2026-01-10", _decision("KXHIGHTSFO-TEST-B70.5", floor=70.0, cap=71.0)
        )
        store.close_paper_order(order_id, 0.50)

        orders_before = _order_snapshot(db_path)
        assert store.open_paper_target_dates() == []
        assert store.unrecorded_traded_target_dates() == ["2026-01-10"]

        out = io.StringIO()
        with patch(
            "sfo_kalshi_quant.settlement.fetch_recent_cli_settlements",
            lambda site, issuedby, timeout=20: {},
        ), redirect_stdout(out):
            assert cli_main(
                [
                    "--forecaster-root", str(root),
                    "--db-path", str(db_path),
                    "--no-color", "paper-auto-settle", "--cities", "sfo",
                ]
            ) == 0

        assert "fully-exited market-day settlement outcome" in out.getvalue()
        recorded = _rows(store)
        assert set(recorded) == {"KXHIGHTSFO-TEST-B70.5"}
        assert recorded["KXHIGHTSFO-TEST-B70.5"]["resolved_yes"] == 1  # 71 in 70-71
        assert recorded["KXHIGHTSFO-TEST-B70.5"]["settlement_high_f"] == 71.0
        assert recorded["KXHIGHTSFO-TEST-B70.5"]["closed_lots"] == 1
        # The order journal is byte-for-byte untouched.
        assert _order_snapshot(db_path) == orders_before
        assert store.unrecorded_traded_target_dates() == []


def _order_snapshot(db_path: Path) -> list:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT * FROM paper_orders ORDER BY id").fetchall()
