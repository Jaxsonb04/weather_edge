"""Auto-settle re-checks settled lots against later final truth (audit F6).

Research V6 lot 3687 -- ``KXHIGHLAX-26OCT22-T71`` ("72° or above"), NO x3 at
$0.7933 all-in -- was settled on a 72°F final for -$2.38. The NWS final is 71
and the exchange finalized "70° to 71°", which pays the NO side +$0.62.

Auto-settle verified each lot only against the value it had just settled from,
so its verification row said MATCH by construction, and no later tick looked
again: a final corrected afterwards (``forecaster/city_truth.upsert_settlement``
lets a later final replace an earlier one) was never noticed. These tests pin
the later look, its limits (it only escalates a recorded verdict, never writes
a first one, never re-books P&L), and the dry-run restatement preview.
"""

from __future__ import annotations

import io
import sqlite3
import sys
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sfo_kalshi_quant import exchange_settlement
from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.cli import main
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.research_policy import TARGET_POLICY_V6
from sfo_kalshi_quant.restatement import restate

FORECASTER = Path(__file__).resolve().parents[2] / "forecaster"
if str(FORECASTER) not in sys.path:
    sys.path.append(str(FORECASTER))

import city_truth  # noqa: E402  -- the forecaster's own settlement-truth writer

LAX = get_city("lax")
SERIES = LAX.series_ticker
TICKER = "KXHIGHLAX-26OCT22-T71"
TARGET = "2026-10-22"
RESEARCH_V6 = TARGET_POLICY_V6.account_id


def _lot_3687() -> TradeDecision:
    # NO x3 at a 0.78 ask; the fee averages to 0.0133, so 0.7933 all-in.
    return TradeDecision(
        ticker=TICKER,
        label="72° or above",
        action="BUY_NO",
        approved=True,
        probability=0.909,
        probability_lcb=0.819,
        yes_bid=0.21,
        yes_ask=0.23,
        spread=0.02,
        fee_per_contract=0.0133,
        cost_per_contract=0.7933,
        edge=0.149,
        edge_lcb=0.0255,
        kelly_fraction=0.01,
        recommended_contracts=3.0,
        expected_profit=0.45,
        reasons=[],
        side="NO",
        entry_ask=0.78,
        entry_bid=0.77,
        entry_ask_size=50,
        entry_bid_size=50,
        strike_type="greater",
        floor_strike=71.0,
        cap_strike=None,
    )


def _bin_lot(target: str) -> TradeDecision:
    """NO x2 on the "80° to 81°" bin."""

    return TradeDecision(
        ticker=f"KXHIGHLAX-26OCT{target[-2:]}-B80.5",
        label="80° to 81°",
        action="BUY_NO",
        approved=True,
        probability=0.8,
        probability_lcb=0.7,
        yes_bid=0.2,
        yes_ask=0.21,
        spread=0.01,
        fee_per_contract=0.02,
        cost_per_contract=0.8,
        edge=0.1,
        edge_lcb=0.05,
        kelly_fraction=0.01,
        recommended_contracts=2.0,
        expected_profit=0.2,
        reasons=[],
        side="NO",
        entry_ask=0.78,
        entry_bid=0.77,
        entry_ask_size=20,
        entry_bid_size=20,
        strike_type="between",
        floor_strike=80.0,
        cap_strike=81.0,
    )


def _forecaster_root(tmp: Path) -> Path:
    root = tmp / "forecaster"
    root.mkdir()
    with closing(sqlite3.connect(root / "weather.db")) as conn:
        city_truth.ensure_schema(conn)
        conn.commit()
    return root


def _store_final(root: Path, local_date: str, high: int) -> None:
    """Write a final the way the forecaster does: a later final replaces one."""

    with closing(sqlite3.connect(root / "weather.db")) as conn:
        city_truth.upsert_settlement(conn, "KLAX", local_date, high, is_final=True)
        conn.commit()


def _no_live_cli(*args, **kwargs):
    raise AssertionError("settlement must never fetch a live CLI product")


def _run(root: Path, db_path: Path, day: date, *command: str) -> tuple[int, str, str]:
    """Run one CLI command with the LAX settlement clock pinned to ``day``."""

    instant = datetime(
        day.year, day.month, day.day, 7, 10, tzinfo=LAX.fixed_standard_timezone()
    )
    out, err = io.StringIO(), io.StringIO()
    with patch(
        "sfo_kalshi_quant.cli.settlement_clock", lambda now=None, city=None: instant
    ), patch(
        "sfo_kalshi_quant.cli.settlement_today",
        lambda now=None, city=None: instant.date(),
    ), patch(
        "sfo_kalshi_quant.settlement.fetch_recent_cli_settlements", _no_live_cli
    ), redirect_stdout(out), redirect_stderr(err):
        code = main(
            [
                "--forecaster-root", str(root),
                "--db-path", str(db_path),
                "--no-color", *command,
            ]
        )
    return code, out.getvalue(), err.getvalue()


def _auto_settle(root: Path, db_path: Path, day: date) -> tuple[int, str, str]:
    # The exchange guard is independent of this re-check and has its own
    # suite; skipping it keeps these runs about the CLI truth alone.
    return _run(
        root, db_path, day,
        "paper-auto-settle", "--cities", "lax", "--skip-exchange-check",
    )


def _verification(db_path: Path, order_id: int) -> tuple | None:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(
            "SELECT booked_high_f, final_high_f, verification_status "
            "FROM paper_settlement_verifications WHERE order_id=?",
            (order_id,),
        ).fetchone()


def _table(db_path: Path, table: str) -> list[tuple]:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


def _order(db_path: Path, order_id: int) -> sqlite3.Row:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM paper_orders WHERE id=?", (order_id,)
        ).fetchone()


def _restatement(db_path: Path) -> dict[int, dict]:
    return {int(entry["order_id"]): entry for entry in restate(db_path)["orders"]}


def test_a_final_corrected_after_settlement_is_flagged_on_a_later_tick():
    with TemporaryDirectory() as tmp:
        root = _forecaster_root(Path(tmp))
        _store_final(root, TARGET, 72)
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(TARGET, _lot_3687())

        code, _, _ = _auto_settle(root, db_path, date(2026, 10, 23))

        assert code == 0
        booked = _order(db_path, order_id)
        assert booked["status"] == "PAPER_SETTLED"
        assert booked["settlement_high_f"] == 72.0
        assert booked["resolved_yes"] == 1
        assert round(booked["realized_pnl"], 2) == -2.38
        # Verified against the value it was just settled from.
        assert _verification(db_path, order_id) == (72.0, 72.0, "MATCH")
        assert _restatement(db_path)[order_id]["verification"] == "VERIFIED"

        # The forecaster's writer replaces the final with a corrected one.
        _store_final(root, TARGET, 71)
        orders_before = _table(db_path, "paper_orders")
        ledger_before = _table(db_path, "paper_account_ledger")
        outcomes_before = _table(db_path, "market_day_settlements")

        # Most ticks settle nothing; the re-check must still run on them.
        code, out, err = _auto_settle(root, db_path, date(2026, 11, 1))

        assert code == 0
        assert "auto-settle skipped" in out
        assert _verification(db_path, order_id) == (72.0, 71.0, "MISMATCH")
        assert "SETTLEMENT VERIFICATION MISMATCH" in err
        assert (
            f"order={order_id} market={TICKER} target={TARGET} booked=72F final=71F"
        ) in err
        assert "was=MATCH" in err
        assert "pnl_delta=+3.00" in err
        assert "newly_flagged=1" in out
        # Nothing is re-booked: the order, its P&L, the ledger, and the
        # booked market-day outcome stay exactly as they were.
        assert _table(db_path, "paper_orders") == orders_before
        assert _table(db_path, "paper_account_ledger") == ledger_before
        assert _table(db_path, "market_day_settlements") == outcomes_before
        # Restatement now treats the lot as unverified (stricter), which is
        # what removes it from the replay's verified evidence.
        entry = _restatement(db_path)[order_id]
        assert entry["verification"] == "UNVERIFIABLE"
        assert "SETTLEMENT_MISMATCH" in entry["findings"]

        # The flip is announced once; the standing count repeats every tick.
        code, out, err = _auto_settle(root, db_path, date(2026, 11, 1))

        assert code == 0
        assert f"order={order_id} " not in err
        assert "STANDING SETTLEMENT VERIFICATION MISMATCHES: 1 lot(s)" in err
        assert "newly_flagged=0" in out
        assert "standing_mismatches=1" in out
        assert _verification(db_path, order_id) == (72.0, 71.0, "MISMATCH")


def test_settling_a_late_final_with_a_later_date_leaves_earlier_verdicts_alone():
    """Tick 2 settles Oct 20 (a late final) and Oct 22 together.

    The verification used to cover Oct 20..Oct 22 with a truth map holding only
    those two dates, so the Oct 21 lot settled on tick 1 was rewritten from
    MATCH to MISSING_FINAL even though weather.db holds its final.
    """

    days = ("2026-10-20", "2026-10-21", "2026-10-22")
    with TemporaryDirectory() as tmp:
        root = _forecaster_root(Path(tmp))
        _store_final(root, "2026-10-21", 74)
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        ids = {day: store.record_paper_order(day, _bin_lot(day)) for day in days}

        assert _auto_settle(root, db_path, date(2026, 10, 24))[0] == 0
        assert _verification(db_path, ids["2026-10-21"]) == (74.0, 74.0, "MATCH")
        with closing(sqlite3.connect(db_path)) as conn:
            checked_at = conn.execute(
                "SELECT checked_at FROM paper_settlement_verifications "
                "WHERE order_id=?",
                (ids["2026-10-21"],),
            ).fetchone()

        _store_final(root, "2026-10-20", 82)
        _store_final(root, "2026-10-22", 71)
        code, out, err = _auto_settle(root, db_path, date(2026, 10, 24))

        assert code == 0
        assert _verification(db_path, ids["2026-10-20"]) == (82.0, 82.0, "MATCH")
        assert _verification(db_path, ids["2026-10-22"]) == (71.0, 71.0, "MATCH")
        assert _verification(db_path, ids["2026-10-21"]) == (74.0, 74.0, "MATCH")
        with closing(sqlite3.connect(db_path)) as conn:
            assert conn.execute(
                "SELECT checked_at FROM paper_settlement_verifications "
                "WHERE order_id=?",
                (ids["2026-10-21"],),
            ).fetchone() == checked_at
        assert "MISSING_FINAL" not in err
        assert (
            "settlement verification: checked=2 mismatches=0 missing_final_truth=0"
        ) in out
        assert _restatement(db_path)[ids["2026-10-21"]]["verification"] == "VERIFIED"


def test_the_timer_reports_but_never_writes_a_first_verification_row():
    """A lot settled by hand has no verification row; the timer adds none.

    A new row clears SETTLEMENT_VERIFICATION_REQUIRED in restatement, so
    writing one is the owner-approved sweep's call, not the timer's. The
    disagreement is still reported on every run.
    """

    with TemporaryDirectory() as tmp:
        root = _forecaster_root(Path(tmp))
        _store_final(root, TARGET, 71)
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(TARGET, _lot_3687())
        # `paper-settle --settlement-high 72 --city lax`: no verification row.
        assert store.settle_paper_orders(TARGET, 72.0, series_ticker=SERIES) == 1

        for _ in range(2):
            code, out, err = _auto_settle(root, db_path, date(2026, 11, 1))

            assert code == 0
            assert _verification(db_path, order_id) is None
            assert (
                f"order={order_id} market={TICKER} target={TARGET} "
                "booked=72F final=71F"
            ) in err
            assert "was=none" in err
            assert "unverified=1" in out
            assert "newly_flagged=0" in out
        assert "SETTLEMENT_VERIFICATION_REQUIRED" in (
            _restatement(db_path)[order_id]["findings"]
        )


def test_the_timer_only_escalates_a_recorded_verdict():
    """It never downgrades MATCH for a missing final and never clears a flag."""

    match_day, flagged_day, missing_day = "2026-10-20", "2026-10-21", "2026-10-22"
    with TemporaryDirectory() as tmp:
        root = _forecaster_root(Path(tmp))
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        ids = {}
        for day, booked in ((match_day, 74), (flagged_day, 75), (missing_day, 70)):
            ids[day] = store.record_paper_order(day, _bin_lot(day))
            assert store.settle_paper_orders(day, booked, series_ticker=SERIES) == 1
        store.verify_paper_settlements(
            {
                (SERIES, match_day): 74,
                (SERIES, flagged_day): 76,
            },
            intervals={SERIES: (match_day, missing_day)},
        )
        before = _table(db_path, "paper_settlement_verifications")
        assert [row[-1] for row in before] == ["MATCH", "MISMATCH", "MISSING_FINAL"]
        # weather.db now lacks the MATCH lot's final, and agrees with the
        # booked high of the flagged and the missing-final lots.
        _store_final(root, flagged_day, 75)
        _store_final(root, missing_day, 70)

        code, out, err = _auto_settle(root, db_path, date(2026, 10, 25))

        assert code == 0
        assert _table(db_path, "paper_settlement_verifications") == before
        assert "checked=2 drift=0" in out
        assert "missing_final=1" in out
        # The owner's explicit sweep is what clears a flag.
        assert "STANDING SETTLEMENT VERIFICATION MISMATCHES: 1 lot(s)" in err


def test_the_recheck_window_is_the_operator_sweeps_fourteen_days():
    from sfo_kalshi_quant._cli.paper import AUTO_SETTLE_TRUTH_RECHECK_DAYS

    today = date(2026, 11, 1)
    inside = (today - timedelta(days=AUTO_SETTLE_TRUTH_RECHECK_DAYS - 1)).isoformat()
    outside = (today - timedelta(days=AUTO_SETTLE_TRUTH_RECHECK_DAYS)).isoformat()
    with TemporaryDirectory() as tmp:
        root = _forecaster_root(Path(tmp))
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        ids = {}
        for day in (inside, outside):
            ids[day] = store.record_paper_order(day, _bin_lot(day))
            assert store.settle_paper_orders(day, 74, series_ticker=SERIES) == 1
            _store_final(root, day, 74)
        store.verify_paper_settlements(
            {(SERIES, inside): 74, (SERIES, outside): 74},
            intervals={SERIES: (outside, inside)},
        )
        _store_final(root, inside, 80)
        _store_final(root, outside, 80)

        code, out, _ = _auto_settle(root, db_path, today)

        assert code == 0
        assert AUTO_SETTLE_TRUTH_RECHECK_DAYS == 14
        assert _verification(db_path, ids[inside]) == (74.0, 80.0, "MISMATCH")
        assert _verification(db_path, ids[outside]) == (74.0, 74.0, "MATCH")
        assert "window=14d checked=1 drift=1" in out


def test_restate_previews_each_account_separately_and_writes_nothing():
    """Restating rewrites ledger history, so the explicit path is a dry run."""

    live_day = "2026-10-21"
    with TemporaryDirectory() as tmp:
        root = _forecaster_root(Path(tmp))
        _store_final(root, TARGET, 72)
        _store_final(root, live_day, 74)
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        research = store.record_paper_order(TARGET, _lot_3687())
        live = store.record_paper_order(live_day, _bin_lot(live_day))
        with closing(sqlite3.connect(db_path)) as conn:
            # Move the lot into the Research ROI V6 book with its full identity.
            conn.execute(
                "UPDATE paper_orders SET account_id=?, risk_profile='research', "
                "research_sleeve=?, research_policy_version=?, policy_fingerprint=? "
                "WHERE id=?",
                (
                    TARGET_POLICY_V6.account_id,
                    TARGET_POLICY_V6.sleeve.value,
                    TARGET_POLICY_V6.policy_version,
                    TARGET_POLICY_V6.policy_fingerprint,
                    research,
                ),
            )
            conn.commit()
        live_account = _order(db_path, live)["account_id"]
        assert live_account and live_account != RESEARCH_V6
        assert _auto_settle(root, db_path, date(2026, 10, 23))[0] == 0
        # Corrected finals: LA Oct 22 is 71 (NO now wins); Oct 21 is 80, which
        # lands in the live lot's 80-81 bin (NO now loses).
        _store_final(root, TARGET, 71)
        _store_final(root, live_day, 80)
        tables = (
            "paper_orders",
            "paper_account_ledger",
            "paper_settlement_verifications",
            "paper_settlement_exchange_checks",
            "market_day_settlements",
        )
        before = {table: _table(db_path, table) for table in tables}
        exchange_calls: list[str] = []

        def _no_exchange():
            exchange_calls.append("called")
            raise AssertionError("a dry run makes no exchange call")

        with patch.object(exchange_settlement, "default_exchange_client", _no_exchange):
            code, out, err = _run(
                root, db_path, date(2026, 11, 1),
                "paper-resettle", "--verify", "--restate", "--days", "30",
            )

        assert code == 0, err
        assert "dry run" in out
        assert (
            f"RESTATE order={research} account={RESEARCH_V6} market={TICKER} "
            f"target={TARGET} NO x3 booked=72F final=71F "
            "pnl=-2.38->+0.62 delta=+3.00 verification=MATCH"
        ) in out
        assert f"RESTATE order={live} account={live_account} " in out
        assert "booked=74F final=80F" in out
        assert "delta=-2.00" in out
        # One line per account; the two books are never added together.
        assert f"account={RESEARCH_V6} lots=1 pnl_delta=+3.00" in out
        assert f"account={live_account} lots=1 pnl_delta=-2.00" in out
        assert "pnl_delta=+1.00" not in out
        assert {table: _table(db_path, table) for table in tables} == before
        assert exchange_calls == []


def test_a_crash_in_the_recheck_cannot_fail_or_block_settlement():
    def explode(*args, **kwargs):
        raise RuntimeError("recheck bug")

    with TemporaryDirectory() as tmp:
        root = _forecaster_root(Path(tmp))
        _store_final(root, TARGET, 72)
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(TARGET, _lot_3687())

        with patch.object(PaperStore, "recheck_paper_settlements", explode):
            code, out, err = _auto_settle(root, db_path, date(2026, 10, 23))

        assert code == 0
        assert _order(db_path, order_id)["status"] == "PAPER_SETTLED"
        assert "settlement verification: checked=1 mismatches=0" in out
        assert "SETTLEMENT TRUTH RE-CHECK FAILED: RuntimeError: recheck bug" in err
