from datetime import UTC, datetime, timedelta

from sfo_kalshi_quant.replay import ReplayEvent, ReplayOrder, run_replay


def _at(minutes: int) -> datetime:
    return datetime(2026, 7, 1, 12, tzinfo=UTC) + timedelta(minutes=minutes)


def test_replay_requires_later_trade_and_clears_queue_before_fill() -> None:
    order = ReplayOrder(
        order_id="o1", placed_at=_at(0), target_date="2026-07-02",
        ticker="KXHIGHTSFO-X", side="YES", limit_price=0.30,
        contracts=5, fee_per_contract=0, queue_ahead=10,
    )
    result = run_replay([
        ReplayEvent(_at(0), "trade", order.ticker, side="YES", price=0.29, quantity=100, taker_book_side="bid"),
        ReplayEvent(_at(0), "order", order.ticker, order=order),
        ReplayEvent(_at(1), "trade", order.ticker, side="YES", price=0.30, quantity=9, taker_book_side="bid"),
        ReplayEvent(_at(2), "trade", order.ticker, side="YES", price=0.30, quantity=6, taker_book_side="bid"),
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
        ReplayEvent(_at(16), "trade", order.ticker, side="YES", price=0.20, quantity=100, taker_book_side="bid"),
        ReplayEvent(_at(20), "settlement", order.ticker, target_date="2026-07-02", resolved_yes=True),
    ])

    assert result.filled == 0
    assert result.cancelled == 1
    assert result.settled == 0
    assert result.ending_cash == 1000.0
    assert result.promotion_eligible is False


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
