from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.replay import (
    ReplayEvent,
    ReplayOrder,
    replay_from_database,
    run_replay,
)


def _at(minutes: int) -> datetime:
    return datetime(2026, 7, 1, 12, tzinfo=UTC) + timedelta(minutes=minutes)


def test_replay_requires_later_trade_and_clears_queue_before_fill() -> None:
    order = ReplayOrder(
        order_id="o1", placed_at=_at(0), target_date="2026-07-02",
        ticker="KXHIGHTSFO-X", side="YES", limit_price=0.30,
        contracts=5, fee_per_contract=0, queue_ahead=10,
    )
    result = run_replay([
        ReplayEvent(_at(0), "trade", order.ticker, side="YES", price=0.29, quantity=100, taker_book_side="ask"),
        ReplayEvent(_at(0), "order", order.ticker, order=order),
        ReplayEvent(_at(1), "trade", order.ticker, side="YES", price=0.30, quantity=9, taker_book_side="ask"),
        ReplayEvent(_at(2), "trade", order.ticker, side="YES", price=0.30, quantity=6, taker_book_side="ask"),
        ReplayEvent(_at(10), "settlement", order.ticker, target_date="2026-07-02", resolved_yes=True),
    ])

    assert result.filled == 1
    assert result.settled == 1
    assert result.realized_pnl == 3.5
    assert [row["event"] for row in result.events].count("FILL_MAKER") == 1


def test_replay_cancels_ttl_and_never_uses_future_settlement_as_fill_evidence() -> None:
    order = ReplayOrder(
        order_id="o1", placed_at=_at(0), target_date="2026-07-02",
        ticker="KXHIGHTSFO-X", side="YES", limit_price=0.30,
        contracts=5, fee_per_contract=0, queue_ahead=0,
    )
    result = run_replay([
        ReplayEvent(_at(0), "order", order.ticker, order=order),
        ReplayEvent(_at(16), "trade", order.ticker, side="YES", price=0.20, quantity=100, taker_book_side="ask"),
        ReplayEvent(_at(20), "settlement", order.ticker, target_date="2026-07-02", resolved_yes=True),
    ])

    assert result.filled == 0
    assert result.cancelled == 1
    assert result.settled == 0
    assert result.ending_cash == 1000.0
    assert result.promotion_eligible is False


def test_replay_matches_runtime_for_inside_spread_queue_priority() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        decision = TradeDecision(
            ticker="KXHIGHTSFO-INSIDE-SPREAD",
            label="inside spread",
            action="BUY_NO",
            approved=True,
            probability=0.82,
            probability_lcb=0.78,
            yes_bid=0.25,
            yes_ask=0.28,
            spread=0.03,
            fee_per_contract=0.0,
            cost_per_contract=0.73,
            edge=0.09,
            edge_lcb=0.05,
            kelly_fraction=0.01,
            recommended_contracts=5.0,
            expected_profit=0.45,
            reasons=[],
            side="NO",
            entry_bid=0.72,
            entry_ask=0.75,
            entry_bid_size=100.0,
            entry_ask_size=100.0,
            limit_price=0.73,
        )
        order_id = store.record_paper_order(
            "2026-07-18",
            decision,
            risk_profile="live",
            status="PAPER_LIMIT_RESTING",
            entry_mode="limit",
            strategy_config=StrategyConfig(),
        )
        assert order_id is not None
        resting = store.paper_order(order_id)
        assert resting is not None
        placed_at = datetime.fromisoformat(resting["created_at"])
        trade = {
            "trade_id": "replay-inside-spread-five-lot",
            "created_time": (placed_at + timedelta(seconds=1)).isoformat(),
            "taker_book_side": "bid",
            "yes_price_dollars": "0.27",
            "no_price_dollars": "0.73",
            "count_fp": "5.00",
        }

        store.apply_maker_trade_batch(decision.ticker, [trade])
        runtime_order = store.paper_order(order_id)
        assert runtime_order is not None
        assert runtime_order["status"] == "PAPER_FILLED"

        replay = replay_from_database(db_path, {})
        assert replay["filled"] == 1
        assert [event["event"] for event in replay["events"]].count("FILL_MAKER") == 1


def test_replay_log_growth_uses_each_days_opening_equity() -> None:
    first = ReplayOrder("a", _at(0), "2026-07-01", "A", "YES", 0.5, 2, 0, immediate=True)
    second = ReplayOrder("b", _at(20), "2026-07-02", "B", "YES", 0.5, 2, 0, immediate=True)
    result = run_replay([
        ReplayEvent(_at(0), "order", "A", order=first),
        ReplayEvent(_at(10), "settlement", "A", target_date="2026-07-01", resolved_yes=True),
        ReplayEvent(_at(20), "order", "B", order=second),
        ReplayEvent(_at(30), "settlement", "B", target_date="2026-07-02", resolved_yes=False),
    ])

    # +1 on $1000, then -1 on $1001 => log(1000/1000) / 2 == 0.
    assert abs(result.daily_log_growth or 0.0) < 1e-12
    assert result.ending_realized_equity == 1000.0
