"""Regression tests for the 2026-06-15 audit fixes.

Covers: take-profit exit labeling, guaranteed-payoff group legs being held by
the monitor, settlement not clobbering a closed order's realized PnL, and
resting limit orders expiring at settlement instead of leaking forever.
"""

from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from sfo_kalshi_quant.cli import main
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.fees import quadratic_fee_average_per_contract
from sfo_kalshi_quant.models import MarketBin, TradeDecision
from sfo_kalshi_quant.paper import ArbitrageContainmentError, PaperTrader


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


def test_grouped_only_monitor_book_makes_zero_market_requests():
    class NoQuoteClient:
        batch_calls = 0
        single_calls = 0

        def get_markets(self, _tickers):
            self.batch_calls += 1
            raise AssertionError("guaranteed groups do not need quotes")

        def get_market(self, _ticker):
            self.single_calls += 1
            raise AssertionError("guaranteed groups do not need quotes")

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _yes_decision())
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET group_id='ARB-GUARANTEED' WHERE id=?",
                (order_id,),
            )
        client = NoQuoteClient()

        output = _run_monitor(db_path, lambda: client)

        assert "HOLD_GUARANTEED_LEG" not in output  # display text stays user-oriented
        assert client.batch_calls == 0
        assert client.single_calls == 0
        assert _latest_action(store, order_id) == "HOLD_GUARANTEED_LEG"


def test_mixed_monitor_fetches_only_ungrouped_and_degraded_tickers():
    needed = []

    class SelectiveClient:
        def get_markets(self, tickers):
            needed.extend(tickers)
            return [_FakeProfitClient().get_market(ticker) for ticker in tickers]

        def get_market(self, ticker):
            raise AssertionError(f"unexpected single fallback for {ticker}")

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        guaranteed = store.record_paper_order(
            "2026-06-12", _yes_decision("KXHIGHTSFO-TEST-GUARANTEED")
        )
        degraded = store.record_paper_order(
            "2026-06-12", _yes_decision("KXHIGHTSFO-TEST-DEGRADED")
        )
        ordinary = store.record_paper_order(
            "2026-06-12", _yes_decision("KXHIGHTSFO-TEST-ORDINARY")
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET group_id='ARB-GUARANTEED' WHERE id=?",
                (guaranteed,),
            )
            conn.execute(
                "UPDATE paper_orders SET group_id='DEGRADED-INCOMPLETE' WHERE id=?",
                (degraded,),
            )

        _run_monitor(db_path, SelectiveClient)

        assert len(needed) == 2
        assert set(needed) == {
            "KXHIGHTSFO-TEST-DEGRADED",
            "KXHIGHTSFO-TEST-ORDINARY",
        }


def test_monitor_does_not_treat_degraded_arbitrage_group_as_guaranteed():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            "2026-06-12", _yes_decision(), group_id="ARB-raced"
        )
        store.mark_arbitrage_group_degraded(
            [order_id],
            group_id="ARB-raced",
            reason="second leg rejected after preflight",
        )

        _run_monitor(db_path, _FakeProfitClient)

        assert store.paper_order(order_id)["status"] == "PAPER_CLOSED"
        assert _latest_action(store, order_id) == "CLOSE_TAKE_PROFIT"


def test_fatal_arbitrage_containment_degrades_active_leg_before_next_monitor():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        trader = PaperTrader(store)
        group_id = "ARB-fatal-race"
        order_id = store.record_paper_order(
            "2026-06-12", _yes_decision(), group_id=group_id
        )

        with (
            patch.object(
                store,
                "close_paper_order",
                side_effect=RuntimeError("simulated unresolved close race"),
            ),
            pytest.raises(ArbitrageContainmentError),
        ):
            trader._compensate_partial_arbitrage(
                [order_id], group_id=group_id, reason="second leg rejected"
            )

        active = store.paper_order(order_id)
        assert active["status"] == "PAPER_FILLED"
        assert str(active["group_id"]).startswith("DEGRADED-ARB-")

        _run_monitor(db_path, _FakeProfitClient)

        assert store.paper_order(order_id)["status"] == "PAPER_CLOSED"
        assert _latest_action(store, order_id) == "CLOSE_TAKE_PROFIT"


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


def test_paper_equity_tracks_realized_pnl_and_sizing_flag():
    from sfo_kalshi_quant.cli import _sizing_bankroll
    from sfo_kalshi_quant.config import StrategyConfig

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order("2026-06-12", _yes_decision())
        closed = store.close_paper_order(order_id, 0.20)  # a winning exit

        equity = store.paper_equity(1000.0)
        assert abs(equity - (1000.0 + closed["realized_pnl"])) < 1e-9

        notional_cfg = StrategyConfig(size_against_live_equity=False)
        equity_cfg = StrategyConfig(size_against_live_equity=True)
        assert _sizing_bankroll(store, notional_cfg, None) == notional_cfg.paper_bankroll
        assert abs(_sizing_bankroll(store, equity_cfg, None) - equity) < 1e-9


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


def _one_contract_yes(ticker: str, yes_ask: float) -> TradeDecision:
    return replace(_yes_decision(ticker, yes_ask=yes_ask), recommended_contracts=1.0)


def test_break_even_close_is_undecided_not_counted_as_a_loss():
    # Solve the exact July-2026 fee curve for the exit price whose net proceeds
    # equal the persisted entry cost.
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        win_id = store.record_paper_order("2026-06-12", _one_contract_yes("KXHIGHTSFO-TEST-WIN", 0.30))
        be_id = store.record_paper_order("2026-06-12", _one_contract_yes("KXHIGHTSFO-TEST-BE", 0.30))

        store.close_paper_order(win_id, 0.50)  # clear profit
        entry_cost = float(store.paper_order(be_id)["cost_per_contract"])
        lo, hi = entry_cost, min(0.99, entry_cost + 0.10)
        for _ in range(60):
            mid = (lo + hi) / 2
            fee = quadratic_fee_average_per_contract(
                mid, 1.0, series_ticker="KXHIGHTSFO"
            )
            if mid - fee < entry_cost:
                lo = mid
            else:
                hi = mid
        be_row = store.close_paper_order(be_id, hi)

        assert abs(be_row["realized_pnl"]) < 1e-9
        # Break-even is undecided: resolved_yes stays NULL instead of being
        # coerced to a loss by the old realized_pnl > 0 fallback.
        assert be_row["resolved_yes"] is None

        summary = store.market_backtest_summary()
        # Both closes deployed capital, so both are realized orders...
        assert summary["orders"] == 2
        # ...but the hit-rate denominator excludes the undecided break-even, so
        # the one decided trade (a win) gives 1.0, not 0.5 (the old bug bucketed
        # the break-even as a loss).
        assert summary["hit_rate"] == 1.0


def test_decided_close_records_resolved_yes_per_side():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        yes_id = store.record_paper_order("2026-06-12", _one_contract_yes("KXHIGHTSFO-TEST-Y", 0.30))
        no_decision = replace(
            _one_contract_yes("KXHIGHTSFO-TEST-N", 0.30),
            action="BUY_NO",
            side="NO",
            entry_ask=0.30,
        )
        no_id = store.record_paper_order("2026-06-12", no_decision)

        yes_row = store.close_paper_order(yes_id, 0.50)  # YES position profits
        no_row = store.close_paper_order(no_id, 0.50)  # NO position profits

        # A winning YES close resolved YES; a winning NO close resolved NO (0).
        assert yes_row["realized_pnl"] > 0 and yes_row["resolved_yes"] == 1
        assert no_row["realized_pnl"] > 0 and no_row["resolved_yes"] == 0


def test_settle_is_noop_when_filled_order_already_closed():
    # The monitor close and settle run on the same DB; once a filled order is
    # closed, settle must not re-touch it (count it) -- the BEGIN IMMEDIATE read
    # sees the closed row and the status guard makes the UPDATE a no-op.
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        order_id = store.record_paper_order("2026-06-12", _yes_decision())
        store.close_paper_order(order_id, 0.20)

        settled = store.settle_paper_orders("2026-06-12", 80.0)

        assert settled == 0
        row = store.paper_orders(50)[0]
        assert row["status"] == "PAPER_CLOSED"
