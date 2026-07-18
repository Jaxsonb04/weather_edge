"""Unit tests for the shared maker-fill normalizer/allocator (audit EX-01)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sfo_kalshi_quant.maker_fills import (
    PublicAggressorTrade,
    RestingMakerOrder,
    allocate_maker_fills,
    normalize_public_trade,
)

T0 = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def _trade(
    trade_id: str,
    *,
    maker_side: str,
    yes_price: str,
    quantity: str,
    minutes: int = 10,
) -> PublicAggressorTrade:
    return PublicAggressorTrade(
        trade_id=trade_id,
        created_at=T0 + timedelta(minutes=minutes),
        maker_side=maker_side,  # type: ignore[arg-type]
        yes_price=Decimal(yes_price),
        quantity=Decimal(quantity),
    )


def _order(
    order_id: int,
    *,
    side: str,
    limit: str,
    quantity: str,
    queue: str = "0",
    queue_price: str | None = None,
    minutes: int = 0,
    active_until: datetime | None = None,
) -> RestingMakerOrder:
    return RestingMakerOrder(
        order_id=order_id,
        side=side,  # type: ignore[arg-type]
        limit_price=Decimal(limit),
        quantity=Decimal(quantity),
        queue_ahead=Decimal(queue),
        placed_at=T0 + timedelta(minutes=minutes),
        queue_price=Decimal(queue_price) if queue_price is not None else None,
        active_until=active_until,
    )


def test_normalize_maps_taker_book_side_to_the_complementary_maker() -> None:
    base = {
        "trade_id": "T",
        "created_time": (T0 + timedelta(minutes=1)).isoformat(),
        "yes_price_dollars": "0.30",
        "count_fp": "10.00",
    }
    assert normalize_public_trade({**base, "taker_book_side": "bid"}).maker_side == "NO"
    assert normalize_public_trade({**base, "taker_book_side": "ask"}).maker_side == "YES"
    assert normalize_public_trade({**base, "taker_outcome_side": "yes"}).maker_side == "NO"
    assert normalize_public_trade({**base, "taker_outcome_side": "no"}).maker_side == "YES"


def test_normalize_refuses_unprovable_trades() -> None:
    base = {
        "trade_id": "T",
        "created_time": (T0 + timedelta(minutes=1)).isoformat(),
        "yes_price_dollars": "0.30",
        "count_fp": "10.00",
        "taker_book_side": "bid",
    }
    assert normalize_public_trade({**base, "taker_book_side": ""}) is None
    assert normalize_public_trade({**base, "trade_id": ""}) is None
    assert normalize_public_trade({**base, "count_fp": "0"}) is None
    assert normalize_public_trade({**base, "yes_price_dollars": "1.50"}) is None
    assert normalize_public_trade({**base, "is_block_trade": True}) is None
    assert normalize_public_trade({**base, "created_time": "not-a-time"}) is None


def test_allocator_consumes_queue_before_quantity() -> None:
    orders = [_order(1, side="NO", limit="0.70", quantity="8", queue="5")]
    trades = [_trade("A", maker_side="NO", yes_price="0.30", quantity="12")]

    allocation = allocate_maker_fills(trades, orders)[1]

    assert allocation.queue_consumed == Decimal(5)
    assert allocation.filled_quantity == Decimal(7)
    assert not allocation.complete


def test_allocator_clears_better_price_queue_before_filling_below_bid() -> None:
    order = _order(
        1,
        side="NO",
        limit="0.71",
        quantity="5",
        queue="100",
        queue_price="0.72",
    )
    trades = [
        _trade("QUEUE", maker_side="NO", yes_price="0.28", quantity="100"),
        _trade(
            "FILL",
            maker_side="NO",
            yes_price="0.29",
            quantity="5",
            minutes=11,
        ),
    ]

    allocation = allocate_maker_fills(trades, [order])[1]

    assert allocation.complete
    assert allocation.queue_consumed == Decimal("100")
    assert allocation.filled_quantity == Decimal("5")
    assert allocation.consumption_by_trade() == {
        "QUEUE": {
            "queue_quantity": 100.0,
            "fill_quantity": 0.0,
            "total_quantity": 100.0,
        },
        "FILL": {
            "queue_quantity": 0.0,
            "fill_quantity": 5.0,
            "total_quantity": 5.0,
        },
    }


def test_allocator_accumulates_across_trades_until_complete() -> None:
    orders = [_order(1, side="NO", limit="0.70", quantity="8", queue="5")]
    trades = [
        _trade("A", maker_side="NO", yes_price="0.30", quantity="12", minutes=10),
        _trade("B", maker_side="NO", yes_price="0.31", quantity="1", minutes=11),
    ]

    allocation = allocate_maker_fills(trades, orders)[1]

    assert allocation.complete
    assert allocation.allocations_by_trade() == {"A": 7.0, "B": 1.0}


def test_allocator_gives_better_price_priority_over_time() -> None:
    orders = [
        _order(1, side="NO", limit="0.70", quantity="8", minutes=0),
        _order(2, side="NO", limit="0.72", quantity="8", minutes=5),
    ]
    trades = [_trade("A", maker_side="NO", yes_price="0.28", quantity="8")]

    allocations = allocate_maker_fills(trades, orders)

    assert allocations[2].complete  # higher bid outranks earlier placement
    assert allocations[1].filled_quantity == Decimal(0)


def test_allocator_never_fills_from_wrong_direction_or_earlier_trades() -> None:
    orders = [_order(1, side="YES", limit="0.30", quantity="5", minutes=10)]
    trades = [
        _trade("EARLY", maker_side="YES", yes_price="0.30", quantity="50", minutes=5),
        _trade("WRONG-SIDE", maker_side="NO", yes_price="0.30", quantity="50", minutes=15),
        _trade("ABOVE-LIMIT", maker_side="YES", yes_price="0.31", quantity="50", minutes=16),
    ]

    allocation = allocate_maker_fills(trades, orders)[1]

    assert allocation.filled_quantity == Decimal(0)


def test_allocator_includes_cutoff_trade_and_excludes_just_after() -> None:
    cutoff = T0 + timedelta(minutes=10)
    orders = [
        _order(
            1,
            side="NO",
            limit="0.70",
            quantity="2",
            active_until=cutoff,
        )
    ]
    trades = [
        _trade("AT-CUTOFF", maker_side="NO", yes_price="0.30", quantity="1"),
        _trade(
            "AFTER-CUTOFF",
            maker_side="NO",
            yes_price="0.30",
            quantity="1",
            minutes=10,
        ),
    ]
    trades[1] = PublicAggressorTrade(
        **{
            **trades[1].__dict__,
            "created_at": cutoff + timedelta(microseconds=1),
        }
    )

    allocation = allocate_maker_fills(trades, orders)[1]

    assert allocation.allocations_by_trade() == {"AT-CUTOFF": 1.0}


def test_omitted_active_until_preserves_unbounded_allocation() -> None:
    order = _order(1, side="NO", limit="0.70", quantity="1")
    late_trade = _trade(
        "LATE",
        maker_side="NO",
        yes_price="0.30",
        quantity="1",
        minutes=10_000,
    )

    assert order.active_until is None
    assert allocate_maker_fills([late_trade], [order])[1].complete


def test_inactive_higher_priority_order_does_not_consume_later_volume() -> None:
    cutoff = T0 + timedelta(minutes=9)
    orders = [
        _order(
            1,
            side="NO",
            limit="0.72",
            quantity="5",
            active_until=cutoff,
        ),
        _order(2, side="NO", limit="0.71", quantity="5", minutes=1),
    ]
    trades = [_trade("AFTER-CANCEL", maker_side="NO", yes_price="0.29", quantity="5")]

    allocations = allocate_maker_fills(trades, orders)

    assert allocations[1].filled_quantity == Decimal(0)
    assert allocations[2].complete


def test_allocator_is_deterministic_and_conserves_volume() -> None:
    orders = [
        _order(1, side="NO", limit="0.70", quantity="8", minutes=0),
        _order(2, side="NO", limit="0.70", quantity="8", minutes=1),
        _order(3, side="YES", limit="0.30", quantity="4", minutes=2),
    ]
    trades = [
        _trade("A", maker_side="NO", yes_price="0.30", quantity="10", minutes=10),
        _trade("B", maker_side="YES", yes_price="0.30", quantity="3", minutes=11),
    ]

    first = allocate_maker_fills(trades, orders)
    second = allocate_maker_fills(list(reversed(trades)), orders)

    for allocations in (first, second):
        consumed_no = allocations[1].filled_quantity + allocations[2].filled_quantity
        assert consumed_no <= Decimal(10)
        assert allocations[1].complete
        assert allocations[2].filled_quantity == Decimal(2)
        assert allocations[3].filled_quantity == Decimal(3)
    assert {
        key: allocation.allocations_by_trade() for key, allocation in first.items()
    } == {key: allocation.allocations_by_trade() for key, allocation in second.items()}
