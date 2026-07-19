from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.maker_fills import normalize_public_trade
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.replay import (
    ReplayEvent,
    ReplayOrder,
    SettlementFact,
    build_exec_v3_events,
    replay_from_database,
    run_replay,
)
from sfo_kalshi_quant.restatement import restate


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


def test_replay_clears_better_price_queue_before_filling_below_bid() -> None:
    order = ReplayOrder(
        order_id="below-bid",
        placed_at=_at(0),
        target_date="2026-07-02",
        ticker="KXHIGHTSFO-BELOW-BID",
        side="NO",
        limit_price=0.71,
        contracts=5,
        fee_per_contract=0,
        queue_ahead=100,
        queue_price=0.72,
    )

    result = run_replay(
        [
            ReplayEvent(_at(0), "order", order.ticker, order=order),
            ReplayEvent(
                _at(1),
                "trade",
                order.ticker,
                side="NO",
                price=0.72,
                quantity=100,
                taker_book_side="bid",
            ),
            ReplayEvent(
                _at(2),
                "trade",
                order.ticker,
                side="NO",
                price=0.71,
                quantity=5,
                taker_book_side="bid",
            ),
        ]
    )

    assert result.filled == 1
    assert [event["event"] for event in result.events].count("FILL_MAKER") == 1


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


def test_replay_matches_runtime_when_queue_is_above_order_limit() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        decision = TradeDecision(
            ticker="KXHIGHTSFO-BELOW-BID",
            label="below bid",
            action="BUY_NO",
            approved=True,
            probability=0.82,
            probability_lcb=0.78,
            yes_bid=0.25,
            yes_ask=0.29,
            spread=0.04,
            fee_per_contract=0.0,
            cost_per_contract=0.71,
            edge=0.11,
            edge_lcb=0.07,
            kelly_fraction=0.01,
            recommended_contracts=5.0,
            expected_profit=0.55,
            reasons=[],
            side="NO",
            entry_bid=0.72,
            entry_ask=0.75,
            entry_bid_size=100.0,
            entry_ask_size=100.0,
            limit_price=0.71,
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
        queue_trade = {
            "trade_id": "below-bid-queue",
            "created_time": (placed_at + timedelta(seconds=1)).isoformat(),
            "taker_book_side": "bid",
            "yes_price_dollars": "0.28",
            "no_price_dollars": "0.72",
            "count_fp": "100.00",
        }
        fill_trade = {
            "trade_id": "below-bid-fill",
            "created_time": (placed_at + timedelta(seconds=2)).isoformat(),
            "taker_book_side": "bid",
            "yes_price_dollars": "0.29",
            "no_price_dollars": "0.71",
            "count_fp": "5.00",
        }

        store.apply_maker_trade_batch(decision.ticker, [queue_trade])
        after_queue = store.paper_order(order_id)
        assert after_queue is not None
        assert after_queue["status"] == "PAPER_LIMIT_RESTING"
        assert after_queue["queue_remaining"] == 0

        store.apply_maker_trade_batch(decision.ticker, [fill_trade])
        runtime_order = store.paper_order(order_id)
        assert runtime_order is not None
        assert runtime_order["status"] == "PAPER_FILLED"
        assert runtime_order["filled_contracts"] == 5

        restated = next(
            row for row in restate(db_path)["orders"] if row["order_id"] == order_id
        )
        assert "EXEC_V4_PRICE_INVALID" not in restated["findings"]

        replay = replay_from_database(db_path, {})
        assert replay["filled"] == 1
        assert [event["event"] for event in replay["events"]].count("FILL_MAKER") == 1


def test_build_exec_v3_events_orders_trades_and_settlements_map_to_typed_events() -> None:
    order = ReplayOrder(
        order_id="o1", placed_at=_at(0), target_date="2026-07-02",
        ticker="KXHIGHTSFO-X", side="YES", limit_price=0.30,
        contracts=5, fee_per_contract=0, queue_ahead=10,
    )
    trade = normalize_public_trade(
        {
            "trade_id": "t1",
            "created_time": _at(1).isoformat(),
            "taker_book_side": "ask",
            "yes_price": 0.30,
            "count": 9,
        }
    )
    assert trade is not None
    settlement = SettlementFact(
        ticker="KXHIGHTSFO-X",
        target_date="2026-07-02",
        settled_at=_at(10),
        resolved_yes=True,
    )

    events = build_exec_v3_events(
        orders=[order],
        trades=[("KXHIGHTSFO-X", trade)],
        settlements=[settlement],
    )

    kinds = [event.kind for event in events]
    assert kinds == ["order", "trade", "settlement"]
    order_event, trade_event, settlement_event = events
    assert order_event.order is order
    assert order_event.occurred_at == order.placed_at
    assert trade_event.ticker == "KXHIGHTSFO-X"
    assert trade_event.side == "YES"
    assert trade_event.taker_book_side == "ask"
    assert trade_event.price == 0.30
    assert trade_event.quantity == 9
    assert settlement_event.ticker == "KXHIGHTSFO-X"
    assert settlement_event.target_date == "2026-07-02"
    assert settlement_event.resolved_yes is True


def test_build_exec_v3_events_is_pure_and_deterministic() -> None:
    order = ReplayOrder(
        order_id="o1", placed_at=_at(0), target_date="2026-07-02",
        ticker="KXHIGHTSFO-X", side="YES", limit_price=0.30,
        contracts=5, fee_per_contract=0,
    )
    settlement = SettlementFact("KXHIGHTSFO-X", "2026-07-02", _at(10), True)

    first = build_exec_v3_events(orders=[order], settlements=[settlement])
    second = build_exec_v3_events(orders=[order], settlements=[settlement])

    assert first == second


def test_build_exec_v3_events_immediate_order_matches_hand_built_replay() -> None:
    """Parity: an immediate/crossing order plus settlement, assembled through
    build_exec_v3_events, must replay to the exact same money outcome as the
    same events hand-built directly (the pattern every other test in this
    file uses) -- pinning the new constructor to the existing engine's own
    fill/fee/settlement math rather than a second, drifting implementation.
    """

    order = ReplayOrder(
        order_id="cross-1", placed_at=_at(0), target_date="2026-07-02",
        ticker="KXHIGHTSFO-CROSS", side="YES", limit_price=0.40,
        contracts=3, fee_per_contract=0.01, immediate=True,
    )
    settlement = SettlementFact("KXHIGHTSFO-CROSS", "2026-07-02", _at(5), True)

    hand_built = [
        ReplayEvent(_at(0), "order", order.ticker, order=order),
        ReplayEvent(
            _at(5), "settlement", order.ticker,
            target_date="2026-07-02", resolved_yes=True,
        ),
    ]
    constructed = build_exec_v3_events(orders=[order], settlements=[settlement])

    hand_result = run_replay(list(hand_built))
    constructed_result = run_replay(list(constructed))

    assert constructed_result.realized_pnl == hand_result.realized_pnl
    assert constructed_result.filled == hand_result.filled == 1
    assert constructed_result.settled == hand_result.settled == 1
    assert constructed_result.ending_cash == hand_result.ending_cash


def test_build_exec_v3_events_maker_fill_matches_hand_built_replay() -> None:
    """Parity for the resting-maker/queue-ahead path (audit EX-01 semantics):
    a normalized public trade routed through build_exec_v3_events must
    produce the identical FILL_MAKER outcome as the same trade hand-built
    directly into a ReplayEvent."""

    order = ReplayOrder(
        order_id="maker-1", placed_at=_at(0), target_date="2026-07-02",
        ticker="KXHIGHTSFO-MAKER", side="YES", limit_price=0.30,
        contracts=5, fee_per_contract=0, queue_ahead=0,
    )
    raw_trade = {
        "trade_id": "maker-fill-1",
        "created_time": _at(1).isoformat(),
        "taker_book_side": "ask",
        "yes_price": 0.30,
        "count": 5,
    }
    normalized = normalize_public_trade(raw_trade)
    assert normalized is not None

    hand_built = [
        ReplayEvent(_at(0), "order", order.ticker, order=order),
        ReplayEvent(
            _at(1), "trade", order.ticker, side="YES", price=0.30,
            quantity=5, taker_book_side="ask",
        ),
    ]
    constructed = build_exec_v3_events(
        orders=[order], trades=[(order.ticker, normalized)]
    )

    hand_result = run_replay(list(hand_built))
    constructed_result = run_replay(list(constructed))

    assert constructed_result.filled == hand_result.filled == 1
    assert (
        [row["event"] for row in constructed_result.events].count("FILL_MAKER")
        == [row["event"] for row in hand_result.events].count("FILL_MAKER")
        == 1
    )
    assert constructed_result.ending_cash == hand_result.ending_cash


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
