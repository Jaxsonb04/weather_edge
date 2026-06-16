"""Regression tests for the 2026-06-15 audit fixes.

Covers: take-profit exit labeling, guaranteed-payoff group legs being held by
the monitor, settlement not clobbering a closed order's realized PnL, and
resting limit orders expiring at settlement instead of leaking forever.
"""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sfo_kalshi_quant.cli import main
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import MarketBin, TradeDecision


def _yes_decision(ticker: str = "KXHIGHTSFO-TEST-B75.5", *, yes_ask: float = 0.08) -> TradeDecision:
    return TradeDecision(
        ticker=ticker,
        label="74° to 75°",
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
        recommended_contracts=5.0,
        expected_profit=0.3,
        reasons=[],
        side="YES",
        strike_type="between",
        floor_strike=74.0,
        cap_strike=75.0,
    )


class _FakeProfitClient:
    """A live book where the YES bid is far above entry, so a YES position is
    deep in take-profit territory."""

    def get_market(self, ticker: str) -> MarketBin:
        return MarketBin(
            ticker=ticker,
            event_ticker="KXHIGHTSFO-TEST",
            title="Highest temperature in San Francisco?",
            yes_sub_title="74° to 75°",
            strike_type="between",
            floor_strike=74.0,
            cap_strike=75.0,
            yes_bid=0.30,
            yes_ask=0.32,
            no_bid=0.68,
            no_ask=0.70,
            yes_bid_size=10.0,
            yes_ask_size=10.0,
            status="active",
        )


def _run_monitor(db_path: Path, client) -> str:
    out = StringIO()
    with patch("sfo_kalshi_quant.cli.KalshiPublicClient", client), redirect_stdout(out):
        code = main(["--db-path", str(db_path), "--no-color", "paper-monitor"])
    assert code == 0
    return out.getvalue()


def _latest_action(store: PaperStore, order_id: int):
    with store.connect() as conn:
        row = conn.execute(
            """
            SELECT action FROM paper_monitor_snapshots
            WHERE order_id = ? ORDER BY id DESC LIMIT 1
            """,
            (order_id,),
        ).fetchone()
    return row[0] if row else None


def test_take_profit_exit_is_labeled_take_profit():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _yes_decision())

        _run_monitor(db_path, _FakeProfitClient)

        row = store.paper_orders(1)[0]
        assert row["status"] == "PAPER_CLOSED"
        assert row["realized_pnl"] > 0
        assert _latest_action(store, order_id) == "CLOSE_TAKE_PROFIT"


def test_monitor_holds_guaranteed_group_legs_to_settlement():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        leg_a = store.record_paper_order(
            "2026-06-12", _yes_decision("KXHIGHTSFO-TEST-B75.5"), group_id="ARB-abc123"
        )
        leg_b = store.record_paper_order(
            "2026-06-12", _yes_decision("KXHIGHTSFO-TEST-B77.5"), group_id="ARB-abc123"
        )

        # The same book that take-profits a lone position must NOT dismantle a
        # grouped leg, or the guaranteed payoff becomes naked directional risk.
        _run_monitor(db_path, _FakeProfitClient)

        for order_id in (leg_a, leg_b):
            row = store.open_paper_order(order_id)
            assert row is not None, "grouped leg must stay open"
            assert row["status"] == "PAPER_FILLED"
            assert _latest_action(store, order_id) == "HOLD_GUARANTEED_LEG"


def test_settlement_does_not_overwrite_a_closed_orders_pnl():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        closed_id = store.record_paper_order("2026-06-12", _yes_decision("KXHIGHTSFO-TEST-B75.5"))
        open_id = store.record_paper_order("2026-06-12", _yes_decision("KXHIGHTSFO-TEST-B77.5"))

        closed_row = store.close_paper_order(closed_id, 0.20)
        closed_pnl = closed_row["realized_pnl"]

        settled = store.settle_paper_orders("2026-06-12", 80.0)

        # Only the still-open order settles; the closed one keeps its PnL/status.
        assert settled == 1
        again = store.paper_orders(50)
        by_id = {row["id"]: row for row in again}
        assert by_id[closed_id]["status"] == "PAPER_CLOSED"
        assert abs(by_id[closed_id]["realized_pnl"] - closed_pnl) < 1e-9
        assert by_id[open_id]["status"] == "PAPER_SETTLED"


def test_resting_limit_order_expires_at_settlement():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        resting_id = store.record_paper_order(
            "2026-06-12",
            _yes_decision(),
            status="PAPER_LIMIT_RESTING",
            entry_mode="limit",
        )

        store.settle_paper_orders("2026-06-12", 80.0)

        row = store.paper_orders(50)[0]
        assert row["id"] == resting_id
        assert row["status"] == "PAPER_EXPIRED"
        assert row["realized_pnl"] == 0.0
        assert row["settled_at"] is not None
