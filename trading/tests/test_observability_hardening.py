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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.settlement_day import settlement_today
from sfo_kalshi_quant.store.market_day_settlements import (
    TRUTH_SOURCE_CLI_SETTLEMENT,
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


def _wholly_exited_book(tmp: Path, targets: tuple[tuple[str, str], ...]) -> tuple[
    Path, Path, PaperStore
]:
    """A forecaster root plus a paper book whose every lot exited early.

    Each ``(target_date, ticker)`` gets one order that is closed, never settled
    -- the wholly-exited shape that has no settled sibling by construction and
    is the whole reason this table exists.  ``weather.db`` carries a final CLI
    maximum for every one of those dates, which is the truth the operator
    backfill now reaches deep history with.
    """

    root = tmp / "forecaster"
    root.mkdir()
    with sqlite3.connect(root / "weather.db") as conn:
        conn.execute(
            "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
            "max_temperature_f INTEGER, is_final INTEGER NOT NULL DEFAULT 1)"
        )
        conn.executemany(
            "INSERT INTO cli_settlements VALUES ('KSFO', ?, 71, 1)",
            [(target,) for target, _ in targets],
        )

    db_path = tmp / "paper.db"
    store = PaperStore(db_path)
    for target, ticker in targets:
        order_id = store.record_paper_order(
            target, _decision(ticker, floor=70.0, cap=71.0)
        )
        store.close_paper_order(order_id, 0.50)
    return root, db_path, store


def _run_cli(root: Path, db_path: Path, *command: str) -> str:
    from sfo_kalshi_quant.cli import main as cli_main

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
                    "--no-color", *command,
                ]
            )
            == 0
        )
    return out.getvalue()


def test_record_only_pass_is_bounded_to_a_recent_window() -> None:
    """The unattended timer records the recent residual and nothing older.

    Unbounded, the first run after deploy would have walked 308 completed
    (series, date) pairs on production -- one write-lock cycle each, inside the
    thirty-minute settle service, with no dry-run.  Deep history belongs to
    `paper-backfill-market-day-settlements`.
    """

    # Settlement-clock dates, not host-local ones: `_recent_target_dates` and
    # `_completed_open_target_dates` both measure against the station's fixed
    # standard-time clock, so a UTC host would otherwise disagree with the
    # window by a day for seven hours out of every twenty-four.
    recent = (settlement_today() - timedelta(days=2)).isoformat()
    ancient = "2015-06-12"

    with TemporaryDirectory() as tmp:
        root, db_path, store = _wholly_exited_book(
            Path(tmp),
            (
                (recent, "KXHIGHTSFO-TEST-B70.5"),
                (ancient, "KXHIGHTSFO-TEST-B71.5"),
            ),
        )

        assert store.open_paper_target_dates() == []
        assert store.unrecorded_traded_target_dates() == sorted([ancient, recent])

        _run_cli(root, db_path, "paper-auto-settle", "--cities", "sfo")

        recorded = _rows(store)
        # The recent residual is still swept by the timer...
        assert "KXHIGHTSFO-TEST-B70.5" in recorded
        # ...and eleven years of history is left to the operator command.
        assert "KXHIGHTSFO-TEST-B71.5" not in recorded
        assert store.unrecorded_traded_target_dates() == [ancient]


def test_the_operator_backfill_still_reaches_history_the_timer_skips() -> None:
    """The claim the record-only window rests on, executed rather than asserted.

    The window is only defensible because the operator command can reach what
    it skips.  Order-derived truth alone cannot: a wholly-exited series-day has
    no settled sibling *by construction*, so before the final CLI maximum
    became a truth source this backfill recorded nothing here and reported the
    day unrecoverable -- the release's own blind spot, left open by the release.
    """

    recent = (settlement_today() - timedelta(days=2)).isoformat()
    ancient = "2015-06-12"

    with TemporaryDirectory() as tmp:
        root, db_path, store = _wholly_exited_book(
            Path(tmp),
            (
                (recent, "KXHIGHTSFO-TEST-B70.5"),
                (ancient, "KXHIGHTSFO-TEST-B71.5"),
            ),
        )
        _run_cli(root, db_path, "paper-auto-settle", "--cities", "sfo")
        assert store.unrecorded_traded_target_dates() == [ancient]

        # Order-derived sources only: the day the timer skipped stays dark.
        blind = store.backfill_market_day_settlements(dry_run=True)
        assert blind["recorded_from_settled_sibling"] == 0
        assert blind["recorded_from_cli_settlements"] == 0
        assert blind["recorded_from_dataset_markets"] == 0
        assert [entry["market_ticker"] for entry in blind["unrecoverable"]] == [
            "KXHIGHTSFO-TEST-B71.5"
        ]
        # The day the timer did reach is already recorded, not unrecoverable.
        assert blind["already_recorded"] == 1

        # The shipped command loads weather.db itself and reaches it.
        report = _run_cli(root, db_path, "paper-backfill-market-day-settlements")
        assert "1 from final CLI maxima" in report
        assert "0 unrecoverable" in report

        history = _rows(store)["KXHIGHTSFO-TEST-B71.5"]
        assert history["target_date"] == ancient
        assert history["truth_source"] == TRUTH_SOURCE_CLI_SETTLEMENT
        assert history["settlement_high_f"] == 71
        assert store.unrecorded_traded_target_dates() == []


def test_the_record_only_window_is_measured_on_the_settlement_clock() -> None:
    """The boundary itself, pinned to an explicit instant.

    Passing ``now`` removes the host clock from the test entirely.  The
    original form built its fixtures from ``date.today()`` while
    ``_recent_target_dates`` measures on the station's fixed-PST settlement
    clock; on a UTC host the two disagree between 00:00 and 07:00 UTC, so CI
    failed deterministically for seven hours a day while local runs passed.
    """

    from sfo_kalshi_quant._cli.paper import (
        RECORD_ONLY_RESIDUAL_LOOKBACK_DAYS,
        _recent_target_dates,
    )

    # 00:30 UTC on 2026-08-17 is still 2026-08-16 on the settlement clock.
    now = datetime(2026, 8, 17, 0, 30, tzinfo=UTC)
    assert settlement_today(now) == date(2026, 8, 16)

    anchor = settlement_today(now)
    inside = (anchor - timedelta(days=1)).isoformat()
    edge = (anchor - timedelta(days=RECORD_ONLY_RESIDUAL_LOOKBACK_DAYS)).isoformat()
    outside = (
        anchor - timedelta(days=RECORD_ONLY_RESIDUAL_LOOKBACK_DAYS + 1)
    ).isoformat()

    assert _recent_target_dates([inside, edge, outside], now=now) == [inside, edge]
    # And the host clock cannot move that answer.
    assert _recent_target_dates(
        [inside, edge, outside], now=datetime(2026, 8, 17, 0, 30, tzinfo=UTC)
    ) == [inside, edge]


# ---------------------------------------------------------------------------
# F2b -- the CLI archive is what makes the operator command reach deep history
# ---------------------------------------------------------------------------


def test_a_late_cli_backfill_cannot_downgrade_a_booked_settlement() -> None:
    """Rank order, exercised where it actually bites.

    The ledger paid out against the high `settle_paper_orders` was handed. A
    CLI value that finalized differently afterwards must not rewrite the
    observability table to disagree with the journal --
    `verify_paper_settlements` exists to raise that divergence as an incident,
    and an overwrite here would hide it.
    """

    target = "2026-06-12"
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        root.mkdir()
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        store.record_paper_order(
            target, _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )
        assert store.settle_paper_orders(target, 67.0, series_ticker="KXHIGHTSFO") == 1
        assert _rows(store)["KXHIGHTSFO-TEST-B66.5"]["settlement_high_f"] == 67

        # A corrected CLI high arrives later and is offered to the backfill.
        summary = store.backfill_market_day_settlements(
            cli_settlement_highs={("KXHIGHTSFO", target): 90.0}
        )
        assert summary["recorded_from_cli_settlements"] == 0
        assert summary["already_recorded"] == 1

        booked = _rows(store)["KXHIGHTSFO-TEST-B66.5"]
        assert booked["settlement_high_f"] == 67
        assert booked["truth_source"] == TRUTH_SOURCE_SETTLEMENT_PATH


def test_the_cli_high_outranks_a_bare_finalized_exchange_result() -> None:
    """The CLI carries a temperature; the exchange dataset carries only YES/NO.

    Recording the dataset's bare outcome would leave ``settlement_high_f`` NULL
    on a day the archive can state exactly, so the CLI wins the tie.
    """

    target = "2026-06-12"
    ticker = "KXHIGHTSFO-TEST-B70.5"
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            target, _decision(ticker, floor=70.0, cap=71.0)
        )
        store.close_paper_order(order_id, 0.50)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE dataset_kalshi_markets "
                "(ticker TEXT, market_status TEXT, result TEXT)"
            )
            conn.execute(
                "INSERT INTO dataset_kalshi_markets VALUES (?, 'finalized', 'no')",
                (ticker,),
            )

        summary = store.backfill_market_day_settlements(
            cli_settlement_highs={("KXHIGHTSFO", target): 71.0}
        )
        assert summary["recorded_from_cli_settlements"] == 1
        assert summary["recorded_from_dataset_markets"] == 0

        row = _rows(store)[ticker]
        assert row["truth_source"] == TRUTH_SOURCE_CLI_SETTLEMENT
        assert row["settlement_high_f"] == 71


def test_stored_truth_ranks_are_re_derived_from_their_source_name() -> None:
    """A database written before ``cli_settlement`` joined the ladder.

    Its ``settlement_path`` rows carry the old rank 3, which now ties with a
    fresh ``settled_sibling`` write and would lose to it. The rank is a cached
    projection of the source name, so init re-derives it.
    """

    target = "2026-06-12"
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        store.record_paper_order(
            target, _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )
        assert store.settle_paper_orders(target, 67.0, series_ticker="KXHIGHTSFO") == 1

        # Rewind to the pre-ladder on-disk shape.
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE market_day_settlements SET truth_rank = 3")
            conn.execute(
                "DELETE FROM schema_migrations "
                "WHERE migration_key='market_day_settlement_truth_rank_v2'"
            )

        migrated = _rows(PaperStore(db_path))["KXHIGHTSFO-TEST-B66.5"]
        assert migrated["truth_rank"] == 4
        assert migrated["truth_source"] == TRUTH_SOURCE_SETTLEMENT_PATH


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


def test_a_real_sqlite_statement_error_inside_the_savepoint_is_contained() -> None:
    """A pure-Python `raise` never touches the savepoint; a failed statement does.

    The isolation only matters for errors SQLite itself raises mid-write, which
    leave the savepoint holding partial state.  This drives a genuine
    statement-level failure (a NOT NULL violation on the observability table)
    rather than a stand-in exception.
    """

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )

        def violate_not_null(conn, **kwargs):
            conn.execute(
                "INSERT INTO market_day_settlements (market_ticker, target_date, "
                "series_ticker, recorded_at, resolved_yes, truth_source, truth_rank) "
                "VALUES ('KXHIGHTSFO-TEST-B66.5', '2026-06-12', 'KXHIGHTSFO', "
                "'2026-06-13T00:00:00+00:00', NULL, 'settlement_path', 4)"
            )

        # Sanity: the statement really does fail at the SQLite layer, so this
        # test cannot pass by simply never erroring.
        with sqlite3.connect(db_path) as probe, pytest.raises(sqlite3.IntegrityError):
            violate_not_null(probe)

        with patch(
            "sfo_kalshi_quant.db.record_market_day_settlements", violate_not_null
        ):
            assert store.settle_paper_orders("2026-06-12", 67.0) == 1

        row = store.paper_order(order_id)
        assert row["status"] == "PAPER_SETTLED"
        assert row["realized_pnl"] is not None
        assert store.market_day_settlements() == []


def test_a_partial_observability_write_is_rolled_back_whole() -> None:
    """One good row lands, then the write fails: neither may survive.

    Without the savepoint the good row would ride along on the settlement
    commit, leaving a half-recorded market-day that reads as complete.
    """

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            "2026-06-12", _decision("KXHIGHTSFO-TEST-B66.5", floor=66.0, cap=67.0)
        )

        def partial_then_fail(conn, **kwargs):
            conn.execute(
                "INSERT INTO market_day_settlements (market_ticker, target_date, "
                "series_ticker, recorded_at, settlement_high_f, resolved_yes, "
                "truth_source, truth_rank) VALUES "
                "('KXHIGHTSFO-TEST-B66.5', '2026-06-12', 'KXHIGHTSFO', "
                "'2026-06-13T00:00:00+00:00', 67.0, 1, 'settlement_path', 4)"
            )
            # A real second write that SQLite refuses: same primary key.
            conn.execute(
                "INSERT INTO market_day_settlements (market_ticker, target_date, "
                "series_ticker, recorded_at, resolved_yes, truth_source, truth_rank) "
                "VALUES ('KXHIGHTSFO-TEST-B66.5', '2026-06-12', 'KXHIGHTSFO', "
                "'2026-06-13T00:00:00+00:00', 1, 'settlement_path', 4)"
            )

        with patch(
            "sfo_kalshi_quant.db.record_market_day_settlements", partial_then_fail
        ):
            assert store.settle_paper_orders("2026-06-12", 67.0) == 1

        assert store.paper_order(order_id)["status"] == "PAPER_SETTLED"
        # The row that did land is gone with the one that failed.
        assert store.market_day_settlements() == []


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
