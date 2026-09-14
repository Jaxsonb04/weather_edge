"""Exchange-side reconciliation of booked paper settlements.

``verify_paper_settlements`` compares a booked high with the CLI archive the lot
was settled from.  These tests pin the independent check against the exchange's
own finalized result.  Every exchange call goes through a fake client, and
``conftest.py`` replaces the production client factory for the whole suite, so
no test can reach the real API.
"""

from __future__ import annotations

import io
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from sfo_kalshi_quant import exchange_settlement
from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.cli import main
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.kalshi import KalshiPublicClient, KalshiUnavailable
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.settlement_day import settlement_today
from sfo_kalshi_quant.store.exchange_settlement_checks import (
    EXCHANGE_CHECK_MATCH,
    EXCHANGE_CHECK_MISMATCH,
    EXCHANGE_CHECK_PENDING,
    EXCHANGE_CHECK_UNCHECKED,
    classify_exchange_settlement,
)

SFO = "KXHIGHTSFO"


def _decision(
    ticker: str,
    *,
    strike_type: str = "between",
    floor: float | None = None,
    cap: float | None = None,
) -> TradeDecision:
    return TradeDecision(
        ticker=ticker,
        label="bin",
        action="BUY_YES",
        approved=True,
        probability=0.30,
        probability_lcb=0.20,
        yes_bid=0.02,
        yes_ask=0.03,
        spread=0.01,
        fee_per_contract=0.01,
        cost_per_contract=0.04,
        edge=0.26,
        edge_lcb=0.16,
        kelly_fraction=0.01,
        recommended_contracts=1.0,
        expected_profit=0.26,
        reasons=[],
        strike_type=strike_type,
        floor_strike=floor,
        cap_strike=cap,
    )


def _market(
    ticker: str, *, status: str = "finalized", result: str = "", value: object = None
) -> dict:
    market: dict = {"ticker": ticker, "status": status, "result": result}
    if value is not None:
        market["expiration_value"] = value
    return {"market": market}


def _http_error(code: int) -> HTTPError:
    return HTTPError("https://exchange.test/markets", code, "exchange error", Message(), None)


class _FakeExchange:
    """``get_json`` over a path map; an unmapped path is an exchange 404."""

    def __init__(self, responses: dict) -> None:
        self.responses = dict(responses)
        self.calls: list[str] = []

    def get_json(self, path: str, params: dict | None = None) -> dict:
        self.calls.append(path)
        response = self.responses.get(path)
        if response is None:
            raise _http_error(404)
        if isinstance(response, BaseException):
            raise response
        return response


def _run_check(store: PaperStore, fake: _FakeExchange, **kwargs) -> dict:
    kwargs.setdefault("max_fetches", 50)
    kwargs.setdefault("min_interval_seconds", 0.0)
    return exchange_settlement.run_exchange_settlement_checks(
        store, client_factory=lambda: fake, **kwargs
    )


def _check_rows(db_path: Path) -> dict[int, dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM paper_settlement_exchange_checks").fetchall()
    return {
        int(row["order_id"]): {key: row[key] for key in row.keys() if key != "checked_at"}
        for row in rows
    }


def _cached_tickers(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT market_ticker FROM kalshi_market_resolutions ORDER BY market_ticker"
            )
        ]


def _orders(db_path: Path) -> list:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT * FROM paper_orders ORDER BY id").fetchall()


def _book(
    db_path: Path,
    target: str,
    high: float,
    lots: list[tuple[str, str, float | None, float | None]],
    *,
    series: str = SFO,
) -> tuple[PaperStore, dict[str, int]]:
    """Record ``(ticker, strike_type, floor, cap)`` lots and settle them at ``high``."""

    store = PaperStore(db_path)
    ids = {
        ticker: store.record_paper_order(
            target, _decision(ticker, strike_type=strike_type, floor=floor, cap=cap)
        )
        for ticker, strike_type, floor, cap in lots
    }
    assert store.settle_paper_orders(target, high, series_ticker=series) == len(lots)
    return store, ids


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def test_match_records_exchange_result_value_and_booked_winner():
    target = "2026-09-10"
    ticker = "KXHIGHTSFO-26SEP10-B70.5"
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store, ids = _book(db_path, target, 71.0, [(ticker, "between", 70.0, 71.0)])
        orders_before = _orders(db_path)
        fake = _FakeExchange(
            {f"markets/{ticker}": _market(ticker, result="yes", value="71.00")}
        )

        summary = _run_check(store, fake, intervals={SFO: (target, target)})

        assert (
            summary["match"],
            summary["mismatches"],
            summary["kalshi_pending"],
            summary["unchecked"],
        ) == (1, 0, 0, 0)
        assert _check_rows(db_path)[ids[ticker]] == {
            "order_id": ids[ticker],
            "market_ticker": ticker,
            "target_date": target,
            "booked_high_f": 71.0,
            "booked_winner": "YES",
            "kalshi_status": "finalized",
            "kalshi_result": "yes",
            "kalshi_expiration_value": 71.0,
            "kalshi_source_endpoint": "markets",
            "verification_status": EXCHANGE_CHECK_MATCH,
            "mismatch_reason": None,
            "check_error": None,
        }
        assert fake.calls == [f"markets/{ticker}"]
        assert _cached_tickers(db_path) == [ticker]
        assert _orders(db_path) == orders_before


def test_mismatch_when_the_exchange_resolved_the_other_side():
    target = "2026-09-10"
    ticker = "KXHIGHTSFO-26SEP10-B70.5"
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store, ids = _book(db_path, target, 71.0, [(ticker, "between", 70.0, 71.0)])
        orders_before = _orders(db_path)
        fake = _FakeExchange(
            {f"markets/{ticker}": _market(ticker, result="no", value="74.00")}
        )

        summary = _run_check(store, fake, intervals={SFO: (target, target)})

        row = _check_rows(db_path)[ids[ticker]]
        assert row["verification_status"] == EXCHANGE_CHECK_MISMATCH
        assert row["mismatch_reason"] == "result,expiration_value"
        assert (row["booked_winner"], row["kalshi_result"], row["kalshi_expiration_value"]) == (
            "YES",
            "no",
            74.0,
        )
        assert summary["mismatches"] == 1
        assert summary["standing_mismatches"] == 1
        # Booked P&L is never rewritten by the check.
        assert _orders(db_path) == orders_before


def test_a_settlement_value_disagreement_is_a_mismatch_even_when_the_payout_agrees():
    """Both numbers below the cap: the lot paid out right, the truth did not."""

    target = "2026-09-10"
    ticker = "KXHIGHTSFO-26SEP10-T74"
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store, ids = _book(db_path, target, 71.0, [(ticker, "less", None, 74.0)])
        fake = _FakeExchange(
            {f"markets/{ticker}": _market(ticker, result="yes", value="72.00")}
        )

        _run_check(store, fake, intervals={SFO: (target, target)})

        row = _check_rows(db_path)[ids[ticker]]
        assert row["booked_winner"] == "YES"
        assert row["verification_status"] == EXCHANGE_CHECK_MISMATCH
        assert row["mismatch_reason"] == "expiration_value"


@pytest.mark.parametrize(
    ("resolution", "expected"),
    [
        (
            {"market_status": "finalized", "result": "yes", "expiration_value": None},
            (EXCHANGE_CHECK_MATCH, None),
        ),
        (
            {"market_status": "finalized", "result": "", "expiration_value": 71.0},
            (EXCHANGE_CHECK_MISMATCH, "result_not_yes_no"),
        ),
        (
            {"market_status": "finalized", "result": "void", "expiration_value": 90.0},
            (EXCHANGE_CHECK_MISMATCH, "result_not_yes_no,expiration_value"),
        ),
        (
            {"market_status": "determined", "result": "no", "expiration_value": 90.0},
            (EXCHANGE_CHECK_PENDING, None),
        ),
        (
            {"market_status": "closed", "result": "", "expiration_value": None},
            (EXCHANGE_CHECK_PENDING, None),
        ),
        (None, (EXCHANGE_CHECK_UNCHECKED, None)),
    ],
    ids=[
        "finalized-without-value-matches-on-result",
        "finalized-without-result",
        "finalized-non-binary-result",
        "determined-is-not-final",
        "closed-is-not-final",
        "not-fetched",
    ],
)
def test_classification(resolution, expected):
    verdict = classify_exchange_settlement(
        booked_high_f=71.0,
        booked_winner="YES",
        resolution=resolution,
        error="network down" if resolution is None else None,
    )
    assert (verdict["verification_status"], verdict["mismatch_reason"]) == expected
    if resolution is None:
        assert verdict["check_error"] == "network down"


def test_pending_markets_are_refetched_until_finalized_and_finalized_ones_never_again():
    target = "2026-09-10"
    ticker = "KXHIGHTSFO-26SEP10-B70.5"
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store, ids = _book(db_path, target, 71.0, [(ticker, "between", 70.0, 71.0)])
        window = {SFO: (target, target)}
        fake = _FakeExchange(
            {f"markets/{ticker}": _market(ticker, status="determined", result="yes")}
        )

        first = _run_check(store, fake, intervals=window, undecided_only=True)
        assert first["kalshi_pending"] == 1
        assert _check_rows(db_path)[ids[ticker]]["verification_status"] == EXCHANGE_CHECK_PENDING
        assert _cached_tickers(db_path) == []

        fake.responses[f"markets/{ticker}"] = _market(ticker, result="yes", value="71.00")
        second = _run_check(store, fake, intervals=window, undecided_only=True)
        assert second["match"] == 1
        assert _cached_tickers(db_path) == [ticker]
        assert len(fake.calls) == 2

        # The timer's undecided-only pass no longer selects the decided lot...
        third = _run_check(store, fake, intervals=window, undecided_only=True)
        assert third["checked"] == []
        # ...and a full re-walk answers from the cache without a request.
        fourth = _run_check(store, fake, intervals=window)
        assert (fourth["match"], fourth["fetched"], fourth["cached"]) == (1, 0, 1)
        assert len(fake.calls) == 2


# ---------------------------------------------------------------------------
# Never blocks: failures are recorded, never read as agreement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        KalshiUnavailable("Kalshi request failed after 2 attempts: timed out"),
        _http_error(503),
        TimeoutError("read timed out"),
    ],
    ids=["retries-exhausted", "http-503", "read-timeout"],
)
def test_a_transport_failure_is_recorded_unchecked_and_stops_fetching(failure):
    target = "2026-09-10"
    first, second = "KXHIGHTSFO-26SEP10-B70.5", "KXHIGHTSFO-26SEP10-B72.5"
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store, ids = _book(
            db_path,
            target,
            71.0,
            [(first, "between", 70.0, 71.0), (second, "between", 72.0, 73.0)],
        )
        orders_before = _orders(db_path)
        fake = _FakeExchange(
            {
                f"markets/{first}": failure,
                f"markets/{second}": _market(second, result="no", value="71.00"),
            }
        )

        summary = _run_check(store, fake, intervals={SFO: (target, target)})

        rows = _check_rows(db_path)
        assert [rows[ids[t]]["verification_status"] for t in (first, second)] == [
            EXCHANGE_CHECK_UNCHECKED,
            EXCHANGE_CHECK_UNCHECKED,
        ]
        assert rows[ids[first]]["check_error"]
        assert rows[ids[second]]["check_error"].startswith("not fetched this run")
        # One outage costs one request, not one per market.
        assert fake.calls == [f"markets/{first}"]
        assert summary["unchecked"] == 2 and summary["fetch_stopped"]
        assert _cached_tickers(db_path) == []
        assert _orders(db_path) == orders_before


def test_the_historical_endpoint_is_asked_only_after_a_live_404():
    target = "2026-06-12"
    historical, missing, live = (
        "KXHIGHTSFO-26JUN12-B70.5",
        "KXHIGHTSFO-26JUN12-B72.5",
        "KXHIGHTSFO-26JUN12-B68.5",
    )
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store, ids = _book(
            db_path,
            target,
            71.0,
            [
                (historical, "between", 70.0, 71.0),
                (missing, "between", 72.0, 73.0),
                (live, "between", 68.0, 69.0),
            ],
        )
        fake = _FakeExchange(
            {
                f"historical/markets/{historical}": _market(
                    historical, result="yes", value="71.00"
                ),
                f"markets/{live}": _market(live, result="no", value="71.00"),
            }
        )

        summary = _run_check(store, fake, intervals={SFO: (target, target)})

        rows = _check_rows(db_path)
        assert rows[ids[historical]]["verification_status"] == EXCHANGE_CHECK_MATCH
        assert rows[ids[historical]]["kalshi_source_endpoint"] == "historical/markets"
        assert rows[ids[missing]]["verification_status"] == EXCHANGE_CHECK_UNCHECKED
        assert "not found" in rows[ids[missing]]["check_error"]
        assert rows[ids[live]]["verification_status"] == EXCHANGE_CHECK_MATCH
        assert rows[ids[live]]["kalshi_source_endpoint"] == "markets"
        # A market absent from both partitions does not stop the run.
        assert fake.calls == [
            f"markets/{historical}",
            f"historical/markets/{historical}",
            f"markets/{missing}",
            f"historical/markets/{missing}",
            f"markets/{live}",
        ]
        assert summary["fetch_stopped"] is None


def test_an_unreadable_market_is_unchecked_never_agreement():
    target = "2026-09-10"
    wrong_ticker, bad_value, no_market, good = (
        "KXHIGHTSFO-26SEP10-B70.5",
        "KXHIGHTSFO-26SEP10-B72.5",
        "KXHIGHTSFO-26SEP10-B68.5",
        "KXHIGHTSFO-26SEP10-B66.5",
    )
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store, ids = _book(
            db_path,
            target,
            71.0,
            [
                (wrong_ticker, "between", 70.0, 71.0),
                (bad_value, "between", 72.0, 73.0),
                (no_market, "between", 68.0, 69.0),
                (good, "between", 66.0, 67.0),
            ],
        )
        fake = _FakeExchange(
            {
                f"markets/{wrong_ticker}": _market(good, result="yes", value="71.00"),
                f"markets/{bad_value}": _market(bad_value, result="no", value="n/a"),
                f"markets/{no_market}": {"error": "unexpected"},
                f"markets/{good}": _market(good, result="no", value="71.00"),
            }
        )

        summary = _run_check(store, fake, intervals={SFO: (target, target)})

        rows = _check_rows(db_path)
        for ticker in (wrong_ticker, bad_value, no_market):
            assert rows[ids[ticker]]["verification_status"] == EXCHANGE_CHECK_UNCHECKED
            assert rows[ids[ticker]]["check_error"]
        assert rows[ids[good]]["verification_status"] == EXCHANGE_CHECK_MATCH
        assert summary["fetch_stopped"] is None
        assert _cached_tickers(db_path) == [good]


def test_a_decided_verdict_is_never_downgraded_by_a_later_failure():
    target = "2026-09-10"
    ticker = "KXHIGHTSFO-26SEP10-B70.5"
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store, ids = _book(db_path, target, 71.0, [(ticker, "between", 70.0, 71.0)])
        window = {SFO: (target, target)}
        _run_check(
            store,
            _FakeExchange({f"markets/{ticker}": _market(ticker, result="yes", value="71")}),
            intervals=window,
        )
        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM kalshi_market_resolutions")

        failing = _FakeExchange({f"markets/{ticker}": KalshiUnavailable("down")})
        summary = _run_check(store, failing, intervals=window)

        assert summary["unchecked"] == 1
        row = _check_rows(db_path)[ids[ticker]]
        assert row["verification_status"] == EXCHANGE_CHECK_MATCH
        assert row["kalshi_result"] == "yes"


def test_fetches_are_spaced_and_bounded_per_run():
    target = "2026-09-10"
    tickers = [
        "KXHIGHTSFO-26SEP10-B70.5",
        "KXHIGHTSFO-26SEP10-B72.5",
        "KXHIGHTSFO-26SEP10-B68.5",
    ]
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store, ids = _book(
            db_path,
            target,
            71.0,
            [
                (tickers[0], "between", 70.0, 71.0),
                (tickers[1], "between", 72.0, 73.0),
                (tickers[2], "between", 68.0, 69.0),
            ],
        )
        fake = _FakeExchange(
            {
                f"markets/{ticker}": _market(ticker, result="no", value="71.00")
                for ticker in tickers
            }
        )
        sleeps: list[float] = []

        summary = _run_check(
            store,
            fake,
            intervals={SFO: (target, target)},
            max_fetches=2,
            min_interval_seconds=0.4,
            sleep=sleeps.append,
            monotonic=lambda: 100.0,
        )

        assert fake.calls == [f"markets/{tickers[0]}", f"markets/{tickers[1]}"]
        assert sleeps == [pytest.approx(0.4)]
        assert summary["fetched"] == 2
        third = _check_rows(db_path)[ids[tickers[2]]]
        assert third["verification_status"] == EXCHANGE_CHECK_UNCHECKED
        assert "budget" in third["check_error"]


# ---------------------------------------------------------------------------
# Regression: Miami 2026-08-29, two conflicting CLI versions
# ---------------------------------------------------------------------------


def test_miami_2026_08_29_two_cli_versions_is_a_distinct_mismatch():
    """The NWS issued MIA CLI max 90 at 04:24 EDT, then max 85 at 05:10 EDT.

    The exchange settled every KXHIGHMIA-26AUG29 bin on 90.  A book settled from
    the newest CLI version booked 85.  The booked-vs-CLI verification agrees
    with itself and says nothing; the exchange check must say MISMATCH on every
    lot, including the one whose payout happens to be right.
    """

    target = "2026-08-29"
    series = "KXHIGHMIA"
    below_88 = "KXHIGHMIA-26AUG29-T88"
    bin_90_91 = "KXHIGHMIA-26AUG29-B90.5"
    bin_94_95 = "KXHIGHMIA-26AUG29-B94.5"
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store, ids = _book(
            db_path,
            target,
            85.0,
            [
                (below_88, "less", None, 88.0),
                (bin_90_91, "between", 90.0, 91.0),
                (bin_94_95, "between", 94.0, 95.0),
            ],
            series=series,
        )
        # The exchange's published settlement for the event (read 2026-09-13).
        fake = _FakeExchange(
            {
                f"markets/{below_88}": _market(below_88, result="no", value="90.00"),
                f"markets/{bin_90_91}": _market(bin_90_91, result="yes", value="90.00"),
                f"markets/{bin_94_95}": _market(bin_94_95, result="no", value="90.00"),
            }
        )

        cli_verification = store.verify_paper_settlements(
            {(series, target): 85.0}, intervals={series: (target, target)}
        )
        summary = _run_check(store, fake, intervals={series: (target, target)})

        # The existing check is blind to this shape: booked 85 == CLI 85.
        assert cli_verification["mismatches"] == 0
        rows = _check_rows(db_path)
        assert {
            ticker: (
                rows[ids[ticker]]["booked_high_f"],
                rows[ids[ticker]]["booked_winner"],
                rows[ids[ticker]]["kalshi_result"],
                rows[ids[ticker]]["kalshi_expiration_value"],
                rows[ids[ticker]]["verification_status"],
                rows[ids[ticker]]["mismatch_reason"],
            )
            for ticker in (below_88, bin_90_91, bin_94_95)
        } == {
            below_88: (85.0, "YES", "no", 90.0, "MISMATCH", "result,expiration_value"),
            bin_90_91: (85.0, "NO", "yes", 90.0, "MISMATCH", "result,expiration_value"),
            bin_94_95: (85.0, "NO", "no", 90.0, "MISMATCH", "expiration_value"),
        }
        assert summary["match"] == 0
        assert summary["mismatches"] == 3


# ---------------------------------------------------------------------------
# Command wiring: surfaced where verification already surfaces, never blocking
# ---------------------------------------------------------------------------


def _forecaster_root(tmp: Path, *, target: str | None = None, high: int = 71) -> Path:
    root = tmp / "forecaster"
    root.mkdir()
    if target is not None:
        with sqlite3.connect(root / "weather.db") as conn:
            conn.execute(
                "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
                "max_temperature_f INTEGER, fetched_at TEXT, source TEXT, "
                "is_final INTEGER NOT NULL DEFAULT 1)"
            )
            conn.execute(
                "INSERT INTO cli_settlements VALUES ('KSFO', ?, ?, 'final', 'nws_cli', 1)",
                (target, high),
            )
    return root


def _cli(root: Path, db_path: Path, *command: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with patch(
        "sfo_kalshi_quant.settlement.fetch_recent_cli_settlements",
        lambda site, issuedby, timeout=20: {},
    ), redirect_stdout(out), redirect_stderr(err):
        code = main(
            ["--forecaster-root", str(root), "--db-path", str(db_path), "--no-color", *command]
        )
    return code, out.getvalue(), err.getvalue()


def _recent_sfo_target() -> str:
    return (settlement_today(city=get_city("sfo")) - timedelta(days=3)).isoformat()


def test_auto_settle_books_the_lot_and_reports_an_exchange_mismatch_on_stderr():
    target = _recent_sfo_target()
    ticker = "KXHIGHTSFO-TEST-B70.5"
    with TemporaryDirectory() as tmp:
        root = _forecaster_root(Path(tmp), target=target, high=71)
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        store.record_paper_order(target, _decision(ticker, floor=70.0, cap=71.0))
        fake = _FakeExchange({f"markets/{ticker}": _market(ticker, result="no", value="74.00")})

        with patch.object(exchange_settlement, "default_exchange_client", lambda: fake):
            code, out, err = _cli(root, db_path, "paper-auto-settle", "--cities", "sfo")

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["status"] == "PAPER_SETTLED"
        assert row["settlement_high_f"] == 71.0
        # The CLI archive agrees with itself...
        assert "settlement verification: checked=1 mismatches=0" in out
        # ...and the exchange does not.
        assert "EXCHANGE SETTLEMENT MISMATCH" in err
        assert f"market={ticker}" in err
        assert "reason=result,expiration_value" in err
        assert "exchange settlement check: checked=1 match=0 mismatches=1" in out
        assert "standing_mismatches=1" in out


def test_auto_settle_settles_even_when_the_exchange_is_unreachable():
    target = _recent_sfo_target()
    ticker = "KXHIGHTSFO-TEST-B70.5"
    with TemporaryDirectory() as tmp:
        root = _forecaster_root(Path(tmp), target=target, high=71)
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(target, _decision(ticker, floor=70.0, cap=71.0))

        # conftest's offline client factory: the exchange cannot be reached.
        code, out, err = _cli(root, db_path, "paper-auto-settle", "--cities", "sfo")

        assert code == 0
        assert store.paper_orders(1)[0]["status"] == "PAPER_SETTLED"
        assert "settlement verification: checked=1 mismatches=0" in out
        row = _check_rows(db_path)[order_id]
        assert row["verification_status"] == EXCHANGE_CHECK_UNCHECKED
        assert "offline test suite" in row["check_error"]
        assert (
            "exchange settlement check: checked=1 match=0 mismatches=0 "
            "kalshi_pending=0 unchecked=1"
        ) in out
        assert "stopped fetching" in err


def test_a_crash_inside_the_exchange_check_cannot_fail_or_block_settlement():
    target = _recent_sfo_target()
    ticker = "KXHIGHTSFO-TEST-B70.5"
    with TemporaryDirectory() as tmp:
        root = _forecaster_root(Path(tmp), target=target, high=71)
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        store.record_paper_order(target, _decision(ticker, floor=70.0, cap=71.0))

        def explode(*args, **kwargs):
            raise RuntimeError("exchange check bug")

        with patch.object(exchange_settlement, "run_exchange_settlement_checks", explode):
            code, out, err = _cli(root, db_path, "paper-auto-settle", "--cities", "sfo")

        assert code == 0
        assert store.paper_orders(1)[0]["status"] == "PAPER_SETTLED"
        assert "settlement verification: checked=1 mismatches=0" in out
        assert "EXCHANGE SETTLEMENT CHECK FAILED: RuntimeError: exchange check bug" in err


def test_auto_settle_decides_a_pending_lot_on_a_tick_with_nothing_left_to_settle():
    """Most timer ticks settle nothing; a pending verdict must still get decided."""

    target = _recent_sfo_target()
    ticker = "KXHIGHTSFO-TEST-B70.5"
    with TemporaryDirectory() as tmp:
        root = _forecaster_root(Path(tmp), target=target, high=71)
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(target, _decision(ticker, floor=70.0, cap=71.0))
        fake = _FakeExchange({f"markets/{ticker}": _market(ticker, status="determined")})

        with patch.object(exchange_settlement, "default_exchange_client", lambda: fake):
            assert _cli(root, db_path, "paper-auto-settle", "--cities", "sfo")[0] == 0
            assert _check_rows(db_path)[order_id]["verification_status"] == EXCHANGE_CHECK_PENDING

            fake.responses[f"markets/{ticker}"] = _market(ticker, result="yes", value="71.00")
            code, out, _ = _cli(root, db_path, "paper-auto-settle", "--cities", "sfo")

        assert code == 0
        assert "auto-settle skipped" in out
        assert "exchange settlement check: checked=1 match=1" in out
        assert _check_rows(db_path)[order_id]["verification_status"] == EXCHANGE_CHECK_MATCH


def test_paper_resettle_verify_reconciles_with_the_exchange_and_can_run_offline():
    target = _recent_sfo_target()
    ticker = "KXHIGHTSFO-TEST-B70.5"
    with TemporaryDirectory() as tmp:
        root = _forecaster_root(Path(tmp))
        db_path = Path(tmp) / "paper.db"
        _book(db_path, target, 71.0, [(ticker, "between", 70.0, 71.0)])
        fake = _FakeExchange({f"markets/{ticker}": _market(ticker, result="yes", value="71.00")})
        verify = ("paper-resettle", "--verify", "--days", "30")

        with patch.object(exchange_settlement, "default_exchange_client", lambda: fake):
            code, out, err = _cli(root, db_path, *verify)
        assert code == 0
        assert "exchange settlement check: checked=1 match=1 mismatches=0" in out
        assert "EXCHANGE SETTLEMENT MISMATCH" not in err
        assert fake.calls == [f"markets/{ticker}"]

        offline = _FakeExchange({})
        with patch.object(exchange_settlement, "default_exchange_client", lambda: offline):
            code, out, _ = _cli(root, db_path, *verify, "--skip-exchange-check")
        assert code == 0
        assert "exchange settlement check skipped" in out
        assert offline.calls == []

        code, _, _ = _cli(root, db_path, *verify, "--exchange-max-fetches", "0")
        assert code == 1


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


def test_nothing_in_the_trading_path_reads_the_exchange_check_tables():
    """These tables measure settlements after the fact; they must never decide one."""

    package = Path(__file__).resolve().parents[1] / "sfo_kalshi_quant"
    allowed = {
        "store/exchange_settlement_checks.py",  # schema, verdicts, SQL
        "store/schema.py",  # table creation
        "exchange_settlement.py",  # the network half of the check
    }
    offenders = sorted(
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if (
            "paper_settlement_exchange_checks" in path.read_text()
            or "kalshi_market_resolutions" in path.read_text()
        )
        and str(path.relative_to(package)) not in allowed
    )
    assert offenders == []


def test_the_suite_cannot_reach_the_real_exchange():
    target = "2026-09-10"
    ticker = "KXHIGHTSFO-26SEP10-B70.5"
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store, ids = _book(db_path, target, 71.0, [(ticker, "between", 70.0, 71.0)])

        summary = exchange_settlement.run_exchange_settlement_checks(
            store, intervals={SFO: (target, target)}, max_fetches=5
        )

        assert summary["unchecked"] == 1
        assert "offline test suite" in _check_rows(db_path)[ids[ticker]]["check_error"]


def test_the_production_client_is_the_bounded_public_client(monkeypatch):
    """Constructing the client performs no request; only its limits are pinned."""

    monkeypatch.undo()
    monkeypatch.delenv("KALSHI_ENV", raising=False)

    client = exchange_settlement.default_exchange_client()

    assert isinstance(client, KalshiPublicClient)
    assert client.timeout == exchange_settlement.EXCHANGE_CHECK_TIMEOUT_SECONDS
    assert client.retries == exchange_settlement.EXCHANGE_CHECK_RETRIES
