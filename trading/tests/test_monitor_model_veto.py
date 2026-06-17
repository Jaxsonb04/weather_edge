"""The paper monitor's stop-loss veto must only trust fresh model snapshots."""

import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sfo_kalshi_quant.cli import main
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.kalshi import KalshiUnavailable
from sfo_kalshi_quant.models import BucketProbability, MarketBin, TradeDecision


def _probability(ticker: str, probability: float) -> BucketProbability:
    return BucketProbability(
        ticker=ticker,
        label="77° to 78°",
        probability=probability,
        lower_confidence=probability - 0.1,
        empirical_probability=probability,
        normal_probability=probability,
        effective_n=200,
    )


def _cheap_yes_decision(*, yes_ask: float = 0.08) -> TradeDecision:
    return TradeDecision(
        ticker="KXHIGHTSFO-TEST-B75.5",
        label="74° to 75°",
        action="BUY_YES",
        approved=True,
        probability=0.14,
        probability_lcb=0.05,
        yes_bid=max(0.01, yes_ask - 0.01),
        yes_ask=yes_ask,
        spread=0.01,
        fee_per_contract=0.006,
        cost_per_contract=yes_ask + 0.006,
        edge=0.06,
        edge_lcb=-0.03,
        kelly_fraction=0.01,
        recommended_contracts=5.0,
        expected_profit=0.3,
        reasons=[],
        strike_type="between",
        floor_strike=74.0,
        cap_strike=75.0,
    )


def _stopped_no_decision() -> TradeDecision:
    return TradeDecision(
        ticker="KXHIGHTSFO-TEST-B82.5",
        label="82° or above",
        action="BUY_NO",
        approved=True,
        probability=0.35,
        probability_lcb=0.25,
        yes_bid=0.19,
        yes_ask=0.21,
        spread=0.02,
        fee_per_contract=0.013,
        cost_per_contract=0.803,
        edge=0.05,
        edge_lcb=0.01,
        kelly_fraction=0.01,
        recommended_contracts=3.0,
        expected_profit=0.2,
        reasons=[],
        side="NO",
        entry_bid=0.77,
        entry_ask=0.79,
        strike_type="above",
        floor_strike=82.0,
        cap_strike=None,
    )


class _FakeKalshiClient:
    yes_bid = 0.03
    yes_ask = 0.05
    no_bid = 0.95
    no_ask = 0.97
    yes_sub_title = "74° to 75°"
    strike_type = "between"
    floor_strike = 74.0
    cap_strike = 75.0

    def get_market(self, ticker: str) -> MarketBin:
        return MarketBin(
            ticker=ticker,
            event_ticker="KXHIGHTSFO-TEST",
            title="Highest temperature in San Francisco?",
            yes_sub_title=self.yes_sub_title,
            strike_type=self.strike_type,
            floor_strike=self.floor_strike,
            cap_strike=self.cap_strike,
            yes_bid=self.yes_bid,
            yes_ask=self.yes_ask,
            no_bid=self.no_bid,
            no_ask=self.no_ask,
            yes_bid_size=10.0,
            yes_ask_size=10.0,
            status="active",
        )


class _FakeYesStopClient(_FakeKalshiClient):
    yes_bid = 0.06
    yes_ask = 0.08
    no_bid = 0.92
    no_ask = 0.94


class _FakeYesSoftStopClient(_FakeKalshiClient):
    yes_bid = 0.075
    yes_ask = 0.09
    no_bid = 0.91
    no_ask = 0.925


class _FakeNoStopClient(_FakeKalshiClient):
    yes_bid = 0.53
    yes_ask = 0.55
    no_bid = 0.45
    no_ask = 0.47
    yes_sub_title = "82° or above"
    strike_type = "above"
    floor_strike = 82.0
    cap_strike = None


class _FakeNoConvergedClient(_FakeKalshiClient):
    """A NO favorite whose price has converged up to the model's fair value."""

    yes_bid = 0.04
    yes_ask = 0.06
    no_bid = 0.94
    no_ask = 0.96
    yes_sub_title = "82° or above"
    strike_type = "above"
    floor_strike = 82.0
    cap_strike = None


def test_monitor_edge_based_take_profit_banks_a_converged_no_favorite():
    """A NO favorite bought at 0.80 whose price has converged to the model's fair
    value (0.93) is banked. The old %-of-cost take-profit targeted 0.80*1.35=1.08,
    which is unreachable, so the favorite silently rode to settlement instead."""
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _stopped_no_decision())
        # Model YES probability 0.07 -> NO fair value 0.93; the live NO bid 0.94
        # nets just above fair value, so the convergence take-profit fires.
        store.record_probabilities(
            "2026-06-12",
            [_probability("KXHIGHTSFO-TEST-B82.5", 0.07)],
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeNoConvergedClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["id"] == order_id
        assert row["status"] == "PAPER_CLOSED"
        assert row["realized_pnl"] > 0
        with store.connect() as conn:
            action = conn.execute(
                """
                SELECT action FROM paper_monitor_snapshots
                WHERE order_id = ? ORDER BY id DESC LIMIT 1
                """,
                (order_id,),
            ).fetchone()[0]
        assert action == "CLOSE_TAKE_PROFIT"


def test_monitor_holds_a_still_mispriced_no_favorite_instead_of_riding_blind():
    """The same favorite, but the price has NOT yet reached fair value (NO bid
    0.84 nets below 0.93): hold for further convergence rather than scalp early."""

    class _BelowFairValueClient(_FakeNoConvergedClient):
        no_bid = 0.84
        no_ask = 0.86

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _stopped_no_decision())
        store.record_probabilities(
            "2026-06-12",
            [_probability("KXHIGHTSFO-TEST-B82.5", 0.07)],
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _BelowFairValueClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["status"] == "PAPER_FILLED"  # held open


def test_latest_model_probability_returns_fresh_snapshot():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        store.record_probabilities(
            "2026-06-10", [_probability("KXHIGHTSFO-TEST-B77.5", 0.62)]
        )
        value = store.latest_model_probability("2026-06-10", "KXHIGHTSFO-TEST-B77.5")
        assert value is not None
        assert abs(value - 0.62) < 1e-9


def test_latest_model_probability_ignores_stale_snapshot():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        store.record_probabilities(
            "2026-06-10", [_probability("KXHIGHTSFO-TEST-B77.5", 0.62)]
        )
        stale = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE probability_snapshots SET created_at = ?", (stale,))
        assert store.latest_model_probability("2026-06-10", "KXHIGHTSFO-TEST-B77.5") is None


def test_latest_model_probability_missing_market_is_none():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        assert store.latest_model_probability("2026-06-10", "KXHIGHTSFO-TEST-T78") is None


def test_paper_monitor_hard_floor_disables_model_veto():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _cheap_yes_decision())
        store.record_probabilities(
            "2026-06-12",
            [_probability("KXHIGHTSFO-TEST-B75.5", 0.14)],
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeKalshiClient), redirect_stdout(out):
            code = main(
                [
                    "--db-path",
                    str(db_path),
                    "--no-color",
                    "paper-monitor",
                    "--model-veto-max-loss-pct",
                    "60",
                ]
            )

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["id"] == order_id
        assert row["status"] == "PAPER_CLOSED"
        assert row["realized_pnl"] < 0
        with store.connect() as conn:
            action = conn.execute(
                """
                SELECT action
                FROM paper_monitor_snapshots
                WHERE order_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()[0]
        assert action == "CLOSE_STOP_LOSS"
        assert "HOLD_MODEL_VETO" not in out.getvalue()


def test_paper_monitor_yes_stop_loss_does_not_model_veto_before_hard_floor():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            "2026-06-12",
            _cheap_yes_decision(yes_ask=0.09),
        )
        store.record_probabilities(
            "2026-06-12",
            [_probability("KXHIGHTSFO-TEST-B75.5", 0.15)],
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeYesStopClient), redirect_stdout(out):
            code = main(
                [
                    "--db-path",
                    str(db_path),
                    "--no-color",
                    "paper-monitor",
                    "--model-veto-max-loss-pct",
                    "60",
                ]
            )

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["id"] == order_id
        assert row["status"] == "PAPER_CLOSED"
        with store.connect() as conn:
            action = conn.execute(
                """
                SELECT action
                FROM paper_monitor_snapshots
                WHERE order_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()[0]
        assert action == "CLOSE_STOP_LOSS"
        assert "HOLD_MODEL_VETO" not in out.getvalue()


def test_paper_monitor_yes_uses_tighter_default_stop_loss():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            "2026-06-12",
            _cheap_yes_decision(yes_ask=0.09),
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeYesSoftStopClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["id"] == order_id
        assert row["status"] == "PAPER_CLOSED"
        with store.connect() as conn:
            action, reason = conn.execute(
                """
                SELECT action, reason
                FROM paper_monitor_snapshots
                WHERE order_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()
        assert action == "CLOSE_STOP_LOSS"
        assert "25.0%" in reason
        assert "HOLD_MODEL_VETO" not in out.getvalue()


def test_paper_monitor_no_stop_loss_can_model_veto_before_hard_floor():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _stopped_no_decision())
        store.record_probabilities(
            "2026-06-12",
            [_probability("KXHIGHTSFO-TEST-B82.5", 0.10)],
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeNoStopClient), redirect_stdout(out):
            code = main(
                [
                    "--db-path",
                    str(db_path),
                    "--no-color",
                    "paper-monitor",
                    "--model-veto-max-loss-pct",
                    "60",
                ]
            )

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["id"] == order_id
        assert row["status"] == "PAPER_FILLED"
        with store.connect() as conn:
            action = conn.execute(
                """
                SELECT action
                FROM paper_monitor_snapshots
                WHERE order_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()[0]
        assert action == "HOLD_MODEL_VETO"
        assert "above entry cost" in out.getvalue()
        assert "HOLD order" in out.getvalue()


def test_paper_monitor_no_veto_requires_model_to_cover_entry_cost():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _stopped_no_decision())
        store.record_probabilities(
            "2026-06-12",
            [_probability("KXHIGHTSFO-TEST-B82.5", 0.35)],
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeNoStopClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["id"] == order_id
        assert row["status"] == "PAPER_CLOSED"
        with store.connect() as conn:
            action = conn.execute(
                """
                SELECT action
                FROM paper_monitor_snapshots
                WHERE order_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()[0]
        assert action == "CLOSE_STOP_LOSS"
        assert "HOLD_MODEL_VETO" not in out.getvalue()


def test_paper_monitor_default_no_can_veto_past_old_45_pct_floor():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _stopped_no_decision())
        store.record_probabilities(
            "2026-06-12",
            [_probability("KXHIGHTSFO-TEST-B82.5", 0.10)],
        )

        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FakeNoStopClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])

        assert code == 0
        row = store.paper_orders(1)[0]
        assert row["id"] == order_id
        assert row["status"] == "PAPER_FILLED"
        with store.connect() as conn:
            action = conn.execute(
                """
                SELECT action
                FROM paper_monitor_snapshots
                WHERE order_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (order_id,),
            ).fetchone()[0]
        assert action == "HOLD_MODEL_VETO"
        assert "HOLD order" in out.getvalue()


def test_monitor_surfaces_a_non_network_error_instead_of_silent_hold():
    """A programming/auth error must propagate (loud, non-zero exit), not be
    masked as a benign FETCH_FAILED HOLD that leaves every position unmanaged."""

    class _BrokenClient:
        def get_market(self, ticker):
            raise ValueError("unexpected bug")

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        store.record_paper_order("2026-06-12", _cheap_yes_decision())
        err = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _BrokenClient), redirect_stderr(err):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])
        assert code == 1  # surfaced, not swallowed
        with store.connect() as conn:
            actions = [r[0] for r in conn.execute("SELECT action FROM paper_monitor_snapshots").fetchall()]
        assert "FETCH_FAILED" not in actions


def test_monitor_holds_open_position_on_transient_kalshi_unavailable():
    """A genuinely transient outage still holds the position as FETCH_FAILED."""

    class _FlakyClient:
        def get_market(self, ticker):
            raise KalshiUnavailable("kalshi down")

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _cheap_yes_decision())
        out = StringIO()
        with patch("sfo_kalshi_quant.cli.KalshiPublicClient", _FlakyClient), redirect_stdout(out):
            code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])
        assert code == 0
        assert store.paper_orders(1)[0]["status"] == "PAPER_FILLED"
        with store.connect() as conn:
            action = conn.execute(
                "SELECT action FROM paper_monitor_snapshots WHERE order_id=? ORDER BY id DESC LIMIT 1",
                (order_id,),
            ).fetchone()[0]
        assert action == "FETCH_FAILED"
