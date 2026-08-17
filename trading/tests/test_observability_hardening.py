"""Regression tests for the five review findings raised against PR #101.

PR #101 (`market_day_settlements` + the `resolved_yes` / `position_won` split)
passed review for merge with five findings to fix before deployment.  Each test
below fails on the merge commit `1b7ff95ba` and passes with the fix.

F1  the settled-row migration decoded `resolved_yes` with arithmetic
    (`1 - resolved_yes`) instead of the boolean rule every Python reader uses,
    so an out-of-domain stored value flips a win into a loss.
F2  the record-only auto-settle pass silently backfilled all history on an
    unattended thirty-minute timer.
F3  `_existing_outcomes` was an unbounded N+1 point-SELECT loop.
F4  the observability write sat inside the settlement transaction with no
    isolation, so any exception from it rolled back real settlement.
F5  `series_ticker=None` stamped one city's high onto every city's market-days
    at the table's highest truth rank.
"""

from __future__ import annotations

import io
import logging
import sqlite3
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.store.market_day_settlements import (
    TRUTH_SOURCE_SETTLEMENT_PATH,
    record_market_day_settlements as record_market_day_settlements_sql,
    unrecorded_traded_target_dates as unrecorded_traded_target_dates_sql,
)


def _decision(
    ticker: str, *, floor: float, cap: float, yes_ask: float = 0.30
) -> TradeDecision:
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
# F1 -- the settled-row migration must decode like the readers, not like SQL
# ---------------------------------------------------------------------------


def _reader_position_won(side: object, resolved_yes: object) -> bool:
    """The decode every Python reader performs.

    ``db.settle_paper_orders``, ``backtest_rescore`` and ``research_shadow`` all
    evaluate ``resolved_yes if side == "YES" else not resolved_yes`` after
    coercing the stored value with ``bool`` and defaulting the side with
    ``str(side or "YES")``.
    """

    resolves_yes = bool(resolved_yes)
    if str(side or "YES").upper() == "YES":
        return resolves_yes
    return not resolves_yes


@pytest.mark.parametrize(
    "resolved_yes",
    [
        0,
        1,
        # Out-of-domain values this project already treats as real: restatement
        # raises RESOLVED_YES_INVALID for them and
        # test_restatement_exec_v4_replay.py parameterizes exactly these two.
        2,
        "bad",
    ],
)
@pytest.mark.parametrize("side", ["YES", "NO", "no", ""])
def test_settled_row_migration_decodes_exactly_like_the_python_readers(
    side: str, resolved_yes: object
) -> None:
    """`1 - resolved_yes` agrees with `not bool(resolved_yes)` only on {0, 1}.

    A NO-side row holding 2 migrated to -1, which is truthy where the reader
    said False.  Text 'bad' migrated to 1, again truthy where the reader said
    False.  And an empty-string side migrated down the NO branch while every
    reader treats it as YES.  Each of those flips a stored win into a loss.
    """

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )

        # Rewind to the pre-migration on-disk shape for a settled row.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE paper_orders SET status='PAPER_SETTLED', "
                "settled_at='2026-06-13T00:00:00+00:00', settlement_high_f=67.0, "
                "realized_pnl=0.7, side=?, resolved_yes=?, position_won=NULL "
                "WHERE id=?",
                (side, resolved_yes, order_id),
            )
            conn.execute(
                "DELETE FROM schema_migrations "
                "WHERE migration_key='closed_row_position_won_v1'"
            )

        migrated = PaperStore(db_path).paper_order(order_id)

        expected = 1 if _reader_position_won(side, resolved_yes) else 0
        assert migrated["position_won"] == expected
        # The migration writes a boolean, never raw arithmetic or raw text.
        assert migrated["position_won"] in (0, 1)
        assert isinstance(migrated["position_won"], int)
        # resolved_yes keeps the market fact it was given, untouched.
        assert migrated["resolved_yes"] == resolved_yes


def test_settled_migration_agrees_with_the_live_settlement_path() -> None:
    """The migration and `settle_paper_orders` must produce the same flag."""

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        no_side = store.record_paper_order(
            "2026-06-12",
            replace(
                _decision("KXHIGHTSFO-TEST-B70.5", floor=70.0, cap=71.0),
                action="BUY_NO",
                side="NO",
            ),
        )
        assert store.settle_paper_orders("2026-06-12", 67.0) == 1
        live = store.paper_order(no_side)["position_won"]

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE paper_orders SET position_won=NULL WHERE id=?", (no_side,)
            )
            conn.execute(
                "DELETE FROM schema_migrations "
                "WHERE migration_key='closed_row_position_won_v1'"
            )

        assert PaperStore(db_path).paper_order(no_side)["position_won"] == live


# ---------------------------------------------------------------------------
# F2 -- the record-only pass is a residual sweep, not a history backfill
# ---------------------------------------------------------------------------


def test_record_only_pass_is_bounded_to_a_recent_window() -> None:
    """The unattended timer records the recent residual and nothing older.

    Unbounded, the first run after deploy would have walked 319 (series, date)
    pairs on production -- one write-lock cycle each, inside the thirty-minute
    settle service, with no dry-run.  Deep history belongs to
    `paper-backfill-market-day-settlements`.
    """

    from sfo_kalshi_quant.cli import main as cli_main

    recent = (date.today() - timedelta(days=2)).isoformat()
    ancient = "2015-06-12"

    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        root.mkdir()
        with sqlite3.connect(root / "weather.db") as conn:
            conn.execute(
                "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
                "max_temperature_f INTEGER, is_final INTEGER NOT NULL DEFAULT 1)"
            )
            conn.executemany(
                "INSERT INTO cli_settlements VALUES ('KSFO', ?, 71, 1)",
                [(recent,), (ancient,)],
            )

        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        for target, ticker in (
            (recent, "KXHIGHTSFO-TEST-B70.5"),
            (ancient, "KXHIGHTSFO-TEST-B71.5"),
        ):
            order_id = store.record_paper_order(
                target, _decision(ticker, floor=70.0, cap=71.0)
            )
            store.close_paper_order(order_id, 0.50)

        assert store.open_paper_target_dates() == []
        assert store.unrecorded_traded_target_dates() == sorted([ancient, recent])

        out = io.StringIO()
        with patch(
            "sfo_kalshi_quant.settlement.fetch_recent_cli_settlements",
            lambda site, issuedby, timeout=20: {},
        ), redirect_stdout(out):
            assert (
                cli_main(
                    [
                        "--forecaster-root", str(root),
                        "--db-path", str(db_path),
                        "--no-color", "paper-auto-settle", "--cities", "sfo",
                    ]
                )
                == 0
            )

        recorded = _rows(store)
        # The recent residual is still swept by the timer...
        assert "KXHIGHTSFO-TEST-B70.5" in recorded
        # ...and eleven years of history is left to the operator command.
        assert "KXHIGHTSFO-TEST-B71.5" not in recorded
        assert store.unrecorded_traded_target_dates() == [ancient]

        # The explicit operator command still owns and can reach deep history.
        store.backfill_market_day_settlements()


def test_the_operator_backfill_still_reaches_history_the_timer_skips() -> None:
    from sfo_kalshi_quant._cli.paper import (
        RECORD_ONLY_RESIDUAL_LOOKBACK_DAYS,
        _recent_target_dates,
    )

    inside = (date.today() - timedelta(days=1)).isoformat()
    edge = (
        date.today() - timedelta(days=RECORD_ONLY_RESIDUAL_LOOKBACK_DAYS)
    ).isoformat()
    outside = (
        date.today() - timedelta(days=RECORD_ONLY_RESIDUAL_LOOKBACK_DAYS + 1)
    ).isoformat()

    assert _recent_target_dates([inside, edge, outside]) == [inside, edge]


# ---------------------------------------------------------------------------
# F3 -- the residual scan is set-based, not one point SELECT per market-day
# ---------------------------------------------------------------------------


def _market_day_reads(statements: list[str]) -> list[str]:
    return [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and "FROM market_day_settlements" in statement
    ]


def _traded_market_day_book(db_path: Path, count: int) -> PaperStore:
    store = PaperStore(db_path)
    for index in range(count):
        order_id = store.record_paper_order(
            "2026-06-12",
            _decision(
                f"KXHIGHTSFO-TEST-B{60 + index}.5",
                floor=60.0 + index,
                cap=61.0 + index,
            ),
        )
        store.close_paper_order(order_id, 0.50)
    return store


def test_residual_scan_does_not_issue_one_query_per_market_day() -> None:
    """The N+1 the auto-settle timer ran over the whole book every 30 minutes."""

    market_days = 12
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        _traded_market_day_book(db_path, market_days)

        statements: list[str] = []
        with sqlite3.connect(db_path) as conn:
            conn.set_trace_callback(statements.append)
            found = unrecorded_traded_target_dates_sql(
                conn, series_ticker="KXHIGHTSFO"
            )

        assert found == ["2026-06-12"]
        # One set-based anti-join. The point-SELECT loop issued one statement
        # per traded market-day on top of the aggregate scan.
        assert len(statements) == 1, statements


def test_recording_reads_existing_outcomes_once_not_once_per_market_day() -> None:
    market_days = 12
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        _traded_market_day_book(db_path, market_days)

        statements: list[str] = []
        with sqlite3.connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.set_trace_callback(statements.append)
            summary = record_market_day_settlements_sql(
                conn,
                target_date="2026-06-12",
                settlement_high_f=67.0,
                recorded_at="2026-06-13T00:00:00+00:00",
                series_ticker="KXHIGHTSFO",
                truth_source=TRUTH_SOURCE_SETTLEMENT_PATH,
            )

        assert summary["market_days_recorded"] == market_days
        assert len(_market_day_reads(statements)) == 1, _market_day_reads(statements)


# ---------------------------------------------------------------------------
# F4 -- an observability failure must never roll back real settlement
# ---------------------------------------------------------------------------


def test_observability_failure_cannot_roll_back_a_settlement(caplog) -> None:
    """The recorder runs inside the settlement transaction, after the ledger
    writes and before the commit. An exception from a measurement-only write
    used to discard real settlement -- the exact regression class this release
    exists to avoid."""

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )

        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("database or disk is full")

        with caplog.at_level(logging.ERROR, logger="sfo_kalshi_quant.db"), patch(
            "sfo_kalshi_quant.db.record_market_day_settlements", boom
        ):
            assert store.settle_paper_orders("2026-06-12", 67.0) == 1

        row = store.paper_order(order_id)
        assert row["status"] == "PAPER_SETTLED"
        assert row["settlement_high_f"] == 67.0
        assert row["resolved_yes"] == 1
        assert row["position_won"] == 1
        assert row["realized_pnl"] is not None
        # Only the observability rows were discarded.
        assert store.market_day_settlements() == []
        # And the failure is loud, not silent.
        assert any(
            "market-day settlement recording failed" in record.getMessage()
            for record in caplog.records
        )


def test_a_genuine_settlement_error_is_still_raised() -> None:
    """The swallow is scoped to the observability write and nothing else."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )
        with patch(
            "sfo_kalshi_quant.db._row_resolves_yes",
            side_effect=sqlite3.OperationalError("settlement itself failed"),
        ), pytest.raises(sqlite3.OperationalError, match="settlement itself failed"):
            store.settle_paper_orders("2026-06-12", 67.0)


# ---------------------------------------------------------------------------
# F5 -- one station's high must never be recorded against another city
# ---------------------------------------------------------------------------


def test_settlement_never_records_another_citys_market_day() -> None:
    """`series_ticker=None` recorded EVERY city's market-days for the date with
    one city's high, at truth_rank 3 -- the highest authority in the table, so
    the upsert overwrote the correct value and nothing weaker could restore it.
    """

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )
        other_city = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHNY-TEST-B90.5", floor=90.0, cap=91.0)
        )
        store.close_paper_order(other_city, 0.50)

        # No series scope: the settlement pass covers San Francisco only.
        assert store.settle_paper_orders("2026-06-12", 67.0) == 1

        recorded = _rows(store)
        assert "KXHIGHTSFO-TEST-B66.5" in recorded
        assert recorded["KXHIGHTSFO-TEST-B66.5"]["series_ticker"] == "KXHIGHTSFO"
        # New York's market-day is NOT stamped with San Francisco's 67°F.
        assert "KXHIGHNY-TEST-B90.5" not in recorded
        assert "2026-06-12" in store.unrecorded_traded_target_dates(
            series_ticker="KXHIGHNY"
        )


def test_recorder_refuses_an_unscoped_settlement_high() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        order_id = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )
        store.close_paper_order(order_id, 0.50)
        with pytest.raises(ValueError, match="requires a series_ticker"):
            store.record_market_day_settlements(
                "2026-06-12", 67.0, series_ticker=None
            )


def test_an_ambiguous_settlement_pass_records_nothing() -> None:
    """Two cities settling on one unscoped call cannot both be right."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )
        store.record_paper_order(
            "2026-06-12", _decision("KXHIGHNY-TEST-B66.5", floor=66.0, cap=67.0)
        )

        # Both settle (the pre-existing settle-loop hazard, unchanged here), but
        # the observability table records neither rather than attributing one
        # station's high to two cities.
        assert store.settle_paper_orders("2026-06-12", 67.0) == 2
        assert store.market_day_settlements() == []
