from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.account import strategy_fingerprint
from sfo_kalshi_quant.cli import _fill_resting_orders_against_live_book
from sfo_kalshi_quant.colors import Color
from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.paper import PaperTrader


def _decision(ticker: str = "KXHIGHTSFO-TEST-B68", **overrides) -> TradeDecision:
    values = {
        "ticker": ticker,
        "label": "68° to 69°",
        "action": "BUY_YES",
        "approved": True,
        "probability": 0.75,
        "probability_lcb": 0.65,
        "yes_bid": 0.29,
        "yes_ask": 0.40,
        "spread": 0.11,
        "fee_per_contract": 0.01,
        "cost_per_contract": 0.41,
        "edge": 0.34,
        "edge_lcb": 0.24,
        "kelly_fraction": 0.03,
        "recommended_contracts": 100.0,
        "expected_profit": 34.0,
        "reasons": ["portfolio PF-test: sleeve=yes_convex, growth=0.001"],
        "side": "YES",
        "entry_bid": 0.29,
        "entry_ask": 0.40,
        "entry_bid_size": 3.0,
        "entry_ask_size": 100.0,
        "strike_type": "between",
        "floor_strike": 68.0,
        "cap_strike": 69.0,
    }
    values.update(overrides)
    return TradeDecision(**values)


def test_shared_account_caps_position_and_rejects_sub_five_dollar_dust() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store, StrategyConfig())

        large = trader.place_approved("2026-07-10", [_decision()], bankroll=1000.0)
        dust = trader.place_approved(
            "2026-07-11",
            [_decision("KXHIGHTSFO-TEST-B70", recommended_contracts=2.0)],
            bankroll=1000.0,
        )

        assert len(large) == 1
        row = store.paper_order(large[0])
        # Per-position ceiling: min($30, 3% equity) since 2026-07-10.
        assert row["contracts"] * row["cost_per_contract"] <= 30.0 + 1e-9
        assert row["contracts"] * row["cost_per_contract"] >= 5.0
        assert dust == []
        assert row["strategy_fingerprint"] == strategy_fingerprint(
            StrategyConfig(), entry_mode="market"
        )


def test_ledger_reserves_fills_and_settles_cash_idempotently() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = _decision(recommended_contracts=20.0)
        order_id = store.record_paper_order(
            "2026-07-10",
            decision,
            status="PAPER_LIMIT_RESTING",
            entry_mode="limit",
            strategy_config=StrategyConfig(),
        )
        assert order_id is not None
        reserved = store.shared_account_state()
        assert reserved is not None
        assert reserved["reservations"] > 0
        assert reserved["available_cash"] < 1000

        filled = store.fill_resting_limit_order(order_id, evidence={"trade_id": "T1"})
        assert filled["filled_at"] is not None
        assert filled["reserved_cost"] == 0
        after_fill = store.shared_account_state()
        assert after_fill["reservations"] == 0
        assert round(after_fill["realized_equity"], 2) == 1000.0

        assert store.settle_paper_orders("2026-07-10", 68.0) == 1
        settled = store.shared_account_state()
        assert settled["realized_equity"] > 1000
        cash_once = settled["cash_balance"]
        assert store.settle_paper_orders("2026-07-10", 68.0) == 0
        assert store.shared_account_state()["cash_balance"] == cash_once


def test_legacy_flattening_is_folded_into_opening_cash_once() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        with store.connect() as conn:
            conn.execute("DELETE FROM paper_account_ledger")
            conn.execute("DELETE FROM paper_accounts")

        order_id = store.record_paper_order("2026-07-10", _decision())
        with store.connect() as conn:
            conn.execute("UPDATE paper_orders SET account_id=NULL WHERE id=?", (order_id,))
            conn.execute("DELETE FROM paper_account_ledger")
        assert store.paper_order(order_id)["account_id"] is None
        closed = store.close_paper_order(order_id, 0.50)
        expected_equity = 1000.0 + float(closed["realized_pnl"])

        cutover = PaperStore(db_path)
        state = cutover.shared_account_state()
        with cutover.connect() as conn:
            ledger = conn.execute(
                "SELECT event_type, amount FROM paper_account_ledger ORDER BY id"
            ).fetchall()

        assert round(state["cash_balance"], 6) == round(expected_equity, 6)
        assert ledger == [("OPENING_CASH", expected_equity)]


def test_resting_ttl_releases_reservation() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store, StrategyConfig(), entry_mode="limit")
        ids = trader.place_approved("2026-07-10", [_decision()], bankroll=1000.0)
        assert len(ids) == 1
        row = store.paper_order(ids[0])
        assert row["status"] == "PAPER_LIMIT_RESTING"
        assert row["expires_at"] is not None

        future = (datetime.fromisoformat(row["expires_at"]) + timedelta(seconds=1)).isoformat()
        assert store.expire_stale_resting_orders(now=future) == 1
        expired = store.paper_order(ids[0])
        assert expired["status"] == "PAPER_EXPIRED"
        assert expired["cancelled_at"] is not None
        assert store.shared_account_state()["available_cash"] == 1000.0


def test_research_position_is_capped_at_one_percent_of_equity() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store, StrategyConfig(), risk_profile="research")
        ids = trader.place_approved("2026-07-10", [_decision()], bankroll=1000.0)

        assert len(ids) == 1
        row = store.paper_order(ids[0])
        assert 5.0 <= row["contracts"] * row["cost_per_contract"] <= 10.0 + 1e-9
        assert row["sleeve"] == "research"


def test_daily_loss_and_drawdown_breakers_fail_closed() -> None:
    with TemporaryDirectory() as tmp:
        daily_store = PaperStore(Path(tmp) / "daily.db")
        daily_id = daily_store.record_paper_order(
            "2026-07-10", _decision(recommended_contracts=60.0), status="PAPER_FILLED"
        )
        daily_store.close_paper_order(daily_id, 0.01)
        daily_capacity = daily_store.account_policy_capacity(
            target_date="2026-07-11",
            market_ticker="KXHIGHNY-TEST-B70",
            risk_profile="live",
            requested_spend=20.0,
        )
        assert daily_capacity["allowed_spend"] == 0
        assert "daily loss" in str(daily_capacity["reason"])

        drawdown_store = PaperStore(Path(tmp) / "drawdown.db")
        drawdown_id = drawdown_store.record_paper_order(
            "2026-07-01", _decision(recommended_contracts=270.0), status="PAPER_FILLED"
        )
        drawdown_store.close_paper_order(drawdown_id, 0.01)
        old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        with drawdown_store.connect() as conn:
            conn.execute("UPDATE paper_orders SET closed_at=? WHERE id=?", (old, drawdown_id))
        capacity = drawdown_store.account_policy_capacity(
            target_date="2026-07-11",
            market_ticker="KXHIGHNY-TEST-B70",
            risk_profile="live",
            requested_spend=20.0,
        )
        # Half of min($30, 3% of drawn-down equity) under the 10% drawdown
        # haircut: ~13.4 on ~$892 equity (was ~8.9 under the $20/2% cap).
        assert 5.0 <= capacity["allowed_spend"] < 15.0

        paused_store = PaperStore(Path(tmp) / "paused.db")
        paused_id = paused_store.record_paper_order(
            "2026-07-01", _decision(recommended_contracts=400.0), status="PAPER_FILLED"
        )
        paused_store.close_paper_order(paused_id, 0.01)
        with paused_store.connect() as conn:
            conn.execute("UPDATE paper_orders SET closed_at=? WHERE id=?", (old, paused_id))
        paused = paused_store.account_policy_capacity(
            target_date="2026-07-11",
            market_ticker="KXHIGHNY-TEST-B70",
            risk_profile="live",
            requested_spend=20.0,
        )
        assert paused["allowed_spend"] == 0
        assert "15%" in str(paused["reason"])


def test_monitor_requires_later_trade_quantity_beyond_queue_ahead() -> None:
    class Client:
        def get_trades(self, **_kwargs):
            return {
                "trades": [
                    {
                        "trade_id": "T1",
                        "count_fp": "25.00",
                        "yes_price_dollars": "0.30",
                        "taker_book_side": "bid",
                        "created_time": datetime.now(UTC).isoformat(),
                    }
                ]
            }

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        order_id = store.record_paper_order(
            "2026-07-10",
            _decision(recommended_contracts=20.0),
            status="PAPER_LIMIT_RESTING",
            entry_mode="limit",
            strategy_config=StrategyConfig(),
        )
        assert order_id is not None
        with store.connect() as conn:
            old = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
            conn.execute("UPDATE paper_orders SET created_at=? WHERE id=?", (old, order_id))

        filled = _fill_resting_orders_against_live_book(
            store, Client(), Color.from_no_color(True)
        )

        assert filled == 1
        row = store.paper_order(order_id)
        assert row["status"] == "PAPER_FILLED"
        assert "T1" in row["fill_evidence_json"]


def test_resting_orders_sharing_market_fetch_trades_once() -> None:
    class Client:
        calls = 0

        def get_trades(self, **_kwargs):
            self.calls += 1
            return {
                "trades": [
                    {
                        "trade_id": "T-BOTH",
                        "count_fp": "100.00",
                        "yes_price_dollars": "0.30",
                        "no_price_dollars": "0.70",
                        "taker_book_side": "bid",
                        "created_time": datetime.now(UTC).isoformat(),
                    }
                ]
            }

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        yes_id = store.record_paper_order(
            "2026-07-10",
            _decision(recommended_contracts=10.0),
            status="PAPER_LIMIT_RESTING",
            entry_mode="limit",
            strategy_config=StrategyConfig(),
        )
        no_id = store.record_paper_order(
            "2026-07-10",
            _decision(
                action="BUY_NO",
                side="NO",
                probability=0.75,
                probability_lcb=0.65,
                entry_bid=0.69,
                entry_ask=0.70,
                cost_per_contract=0.71,
                recommended_contracts=10.0,
            ),
            status="PAPER_LIMIT_RESTING",
            entry_mode="limit",
            strategy_config=StrategyConfig(),
        )
        old = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET created_at=? WHERE id IN (?, ?)",
                (old, yes_id, no_id),
            )
        client = Client()

        filled = _fill_resting_orders_against_live_book(
            store, client, Color.from_no_color(True)
        )

        assert filled == 2
        assert client.calls == 1


def test_resting_fill_uses_relevant_trade_from_second_page() -> None:
    now = datetime.now(UTC).isoformat()

    def trade(trade_id: str, *, price: str, quantity: str = "1.00"):
        return {
            "trade_id": trade_id,
            "ticker": "KXHIGHTSFO-TEST-B68",
            "count_fp": quantity,
            "yes_price_dollars": price,
            "no_price_dollars": str(1.0 - float(price)),
            "taker_side": "yes",
            "taker_outcome_side": "yes",
            "taker_book_side": "bid",
            "created_time": now,
        }

    class Client:
        def __init__(self):
            self.cursors = []

        def get_trades(self, **kwargs):
            cursor = kwargs.get("cursor")
            self.cursors.append(cursor)
            if cursor is None:
                return {
                    "trades": [
                        trade(f"irrelevant-{index}", price="0.99")
                        for index in range(1000)
                    ],
                    "cursor": "page-2",
                }
            return {
                "trades": [trade("relevant-page-2", price="0.29", quantity="100.00")],
                "cursor": "",
            }

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        order_id = store.record_paper_order(
            "2026-07-10",
            _decision(recommended_contracts=10.0),
            status="PAPER_LIMIT_RESTING",
            entry_mode="limit",
            strategy_config=StrategyConfig(),
        )
        old = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET created_at=? WHERE id=?", (old, order_id)
            )
        client = Client()

        filled = _fill_resting_orders_against_live_book(
            store, client, Color.from_no_color(True)
        )

        assert filled == 1
        assert client.cursors == [None, "page-2"]
        assert store.paper_order(order_id)["status"] == "PAPER_FILLED"
        assert "relevant-page-2" in store.paper_order(order_id)["fill_evidence_json"]


def test_cursor_failure_discards_partial_ticker_but_continues_other_tickers() -> None:
    now = datetime.now(UTC).isoformat()

    def relevant_trade(ticker: str, trade_id: str):
        return {
            "trade_id": trade_id,
            "ticker": ticker,
            "count_fp": "100.00",
            "yes_price_dollars": "0.29",
            "no_price_dollars": "0.71",
            "taker_side": "yes",
            "taker_outcome_side": "yes",
            "taker_book_side": "bid",
            "created_time": now,
        }

    class Client:
        def __init__(self):
            self.calls = []

        def get_trades(self, **kwargs):
            ticker = kwargs["ticker"]
            cursor = kwargs.get("cursor")
            self.calls.append((ticker, cursor))
            if ticker.endswith("B68") and cursor is None:
                return {
                    "trades": [relevant_trade(ticker, "partial-a")],
                    "cursor": "broken-page",
                }
            if ticker.endswith("B68"):
                raise OSError("page two unavailable")
            return {
                "trades": [relevant_trade(ticker, "complete-b")],
                "cursor": "",
            }

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        first_id = store.record_paper_order(
            "2026-07-10",
            _decision("KXHIGHTSFO-TEST-B68", recommended_contracts=10.0),
            status="PAPER_LIMIT_RESTING",
            entry_mode="limit",
            strategy_config=StrategyConfig(),
        )
        second_id = store.record_paper_order(
            "2026-07-10",
            _decision("KXHIGHTSFO-TEST-B70", recommended_contracts=10.0),
            status="PAPER_LIMIT_RESTING",
            entry_mode="limit",
            strategy_config=StrategyConfig(),
        )
        old = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET created_at=? WHERE id IN (?, ?)",
                (old, first_id, second_id),
            )
        client = Client()

        filled = _fill_resting_orders_against_live_book(
            store, client, Color.from_no_color(True)
        )

        assert filled == 1
        assert store.paper_order(first_id)["status"] == "PAPER_LIMIT_RESTING"
        assert store.paper_order(second_id)["status"] == "PAPER_FILLED"
        assert client.calls == [
            ("KXHIGHTSFO-TEST-B68", None),
            ("KXHIGHTSFO-TEST-B68", "broken-page"),
            ("KXHIGHTSFO-TEST-B70", None),
        ]


def test_complete_book_iteration_has_no_50_or_100_order_truncation() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        for index in range(60):
            store.record_paper_order(
                "2026-07-10",
                _decision(f"KXHIGHTSFO-OPEN-{index}", recommended_contracts=1.0),
                status="PAPER_FILLED",
            )
        for index in range(110):
            store.record_paper_order(
                "2026-07-11",
                _decision(f"KXHIGHNY-REST-{index}", recommended_contracts=1.0),
                status="PAPER_LIMIT_RESTING",
                entry_mode="limit",
            )

        assert len(store.open_paper_orders()) == 60
        assert len(store.resting_paper_orders()) == 110
