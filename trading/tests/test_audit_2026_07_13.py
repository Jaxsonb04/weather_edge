"""Second-audit (2026-07-13) execution-integrity specification tests.

Batch 0 of docs/AUDIT-PLAN-2026-07-13.md: these tests encode the REQUIRED
behavior for the ranked findings and were written red-first against the frozen
audit baseline. Every fixture marked "production-derived" mirrors real rows
read read-only from the AWS production paper_trading.db at the audit snapshot.

Evidence freeze:
- audit HEAD: 2a1a771a749c78ef03175b79b3f75a1f5b60239b (main == origin/main)
- baseline suite at HEAD: 959 passed, 1 failed (forecaster layout test, TEST-01)
- production fixtures: paper orders 188 (PHIL 86-87 NO, -$11.63), 226/227 and
  261/262 (SEA NO maker fills sharing identical public trade-id lists across a
  live and a research order), 311 (DAL 89-90 NO research, held through
  catastrophic loss by the basket veto, closed at -96.1%).

Official direction semantics (docs.kalshi.com/getting_started/order_direction):
a resting YES bid is filled by a taker with taker_book_side == "ask"; a resting
NO bid is filled by a taker with taker_book_side == "bid". A public trade has
exactly one aggressor direction and finite volume that can be consumed once.
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sfo_kalshi_quant.account import strategy_fingerprint
from sfo_kalshi_quant.cli import _fill_resting_orders_against_live_book, main
from sfo_kalshi_quant.colors import Color
from sfo_kalshi_quant.config import StrategyConfig, strategy_config_for_profile
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import (
    BucketProbability,
    ForecastOutcome,
    IntradaySnapshot,
    MarketBin,
    TradeDecision,
)
from sfo_kalshi_quant.probability import ResidualCalibrator
from sfo_kalshi_quant.research_policy import TARGET_POLICY

AUDIT_HEAD = "2a1a771a749c78ef03175b79b3f75a1f5b60239b"


# ---------------------------------------------------------------------------
# shared builders
# ---------------------------------------------------------------------------


def _decision(
    ticker: str = "KXHIGHTSEA-26JUL13-B82.5",
    *,
    side: str = "NO",
    limit_price: float = 0.82,
    contracts: float = 10.0,
    cost: float | None = None,
    floor: float = 82.0,
    cap: float = 83.0,
) -> TradeDecision:
    price = float(limit_price)
    action = "BUY_NO" if side == "NO" else "BUY_YES"
    return TradeDecision(
        ticker=ticker,
        label=f"{int(floor)}° to {int(cap)}°",
        action=action,
        approved=True,
        probability=0.75,
        probability_lcb=0.65,
        yes_bid=(1.0 - price - 0.02) if side == "NO" else price,
        yes_ask=(1.0 - price) if side == "NO" else price + 0.02,
        spread=0.02,
        fee_per_contract=0.0,
        cost_per_contract=cost if cost is not None else price,
        edge=0.10,
        edge_lcb=0.02,
        kelly_fraction=0.02,
        recommended_contracts=contracts,
        expected_profit=1.0,
        reasons=[],
        side=side,
        entry_bid=price,
        entry_ask=price + 0.02,
        entry_bid_size=0.0,
        entry_ask_size=100.0,
        strike_type="between",
        floor_strike=floor,
        cap_strike=cap,
        limit_price=price,
    )


def _resting_order(
    store: PaperStore,
    target_date: str,
    decision: TradeDecision,
    *,
    created_at: datetime,
    queue_ahead: float = 0.0,
    risk_profile: str = "live",
    strategy_config: StrategyConfig | None = None,
) -> int:
    order_id = store.record_paper_order(
        target_date,
        decision,
        status="PAPER_LIMIT_RESTING",
        entry_mode="limit",
        strategy_config=strategy_config or StrategyConfig(),
        risk_profile=risk_profile,
    )
    assert order_id is not None
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_orders SET created_at=?, expires_at=?, limit_price=?, "
            "entry_bid_size=?, queue_remaining=? WHERE id=?",
            (
                created_at.isoformat(),
                (created_at + timedelta(minutes=15)).isoformat(),
                decision.limit_price,
                queue_ahead,
                queue_ahead,
                order_id,
            ),
        )
    return order_id


def _move_to_target_account(
    store: PaperStore,
    order_id: int,
    *,
    market_ticker: str,
) -> None:
    """Adapt pre-sleeve audit fixtures without bypassing account uniqueness."""

    with store.connect() as conn:
        research_strategy_fingerprint = strategy_fingerprint(
            strategy_config_for_profile("research"), entry_mode="limit"
        )
        conn.execute(
            """
            UPDATE paper_orders
            SET market_ticker=?, risk_profile='research', account_id=?,
                research_sleeve=?, research_policy_version=?,
                policy_fingerprint=?, sleeve=?, strategy_fingerprint=?
            WHERE id=?
            """,
            (
                market_ticker,
                TARGET_POLICY.account_id,
                TARGET_POLICY.sleeve.value,
                TARGET_POLICY.policy_version,
                TARGET_POLICY.policy_fingerprint,
                TARGET_POLICY.sleeve.value,
                research_strategy_fingerprint,
                order_id,
            ),
        )
        conn.execute(
            "UPDATE paper_account_ledger SET account_id=? WHERE order_id=?",
            (TARGET_POLICY.account_id, order_id),
        )


def test_target_audit_fixture_has_coherent_research_identity() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        ticker = "KXHIGHTSEA-26JUL13-B82.5"
        order_id = _resting_order(
            store,
            "2026-07-13",
            _decision(
                ticker + "-TARGET",
                side="NO",
                limit_price=0.70,
                contracts=8.0,
            ),
            created_at=NOW - timedelta(minutes=9),
            risk_profile="research",
            strategy_config=strategy_config_for_profile("research"),
        )
        _move_to_target_account(store, order_id, market_ticker=ticker)

        row = store.paper_order(order_id)

    assert row is not None
    assert row["risk_profile"] == "research"
    assert row["account_id"] == TARGET_POLICY.account_id
    assert row["research_sleeve"] == TARGET_POLICY.sleeve.value
    assert row["research_policy_version"] == TARGET_POLICY.policy_version
    assert row["policy_fingerprint"] == TARGET_POLICY.policy_fingerprint
    assert row["sleeve"] == TARGET_POLICY.sleeve.value
    assert row["strategy_fingerprint"] == strategy_fingerprint(
        strategy_config_for_profile("research"), entry_mode="limit"
    )


def _trade(
    trade_id: str,
    *,
    yes_price: float,
    quantity: float,
    taker_book_side: str,
    created_time: datetime,
) -> dict[str, object]:
    return {
        "trade_id": trade_id,
        "count_fp": f"{quantity:.2f}",
        "yes_price_dollars": f"{yes_price:.4f}",
        "no_price_dollars": f"{1.0 - yes_price:.4f}",
        "taker_book_side": taker_book_side,
        "taker_outcome_side": "no" if taker_book_side == "ask" else "yes",
        "created_time": created_time.isoformat(),
    }


class _TradesClient:
    def __init__(self, trades: list[dict[str, object]]) -> None:
        self._trades = trades
        self.calls = 0

    def get_trades(self, **_kwargs) -> dict[str, object]:
        self.calls += 1
        return {"trades": list(self._trades)}


def _filled_quantities(store: PaperStore, order_ids: list[int]) -> dict[int, float]:
    """Quantity each order actually booked as a capital-consuming maker fill.

    Orders that were not filled contribute 0. When the fill evidence carries
    explicit per-trade allocations (the corrected allocator), the allocated
    sum is used; a full fill without allocations counts as the whole order.
    """

    quantities: dict[int, float] = {}
    for order_id in order_ids:
        row = store.paper_order(order_id)
        assert row is not None
        if str(row["status"]) != "PAPER_FILLED":
            quantities[order_id] = 0.0
            continue
        evidence = json.loads(row["fill_evidence_json"] or "{}")
        allocations = evidence.get("allocations")
        if isinstance(allocations, dict) and allocations:
            quantities[order_id] = sum(float(value) for value in allocations.values())
        else:
            quantities[order_id] = float(row["contracts"])
    return quantities


NOW = datetime(2026, 7, 13, 16, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# EX-01 -- maker fills must respect aggressor direction and volume conservation
# ---------------------------------------------------------------------------


def test_ex01_yes_resting_bid_fills_only_from_ask_taker() -> None:
    """A resting YES bid is lifted by an ask-side (NO) taker, per the official
    order-direction semantics. The current filter demands taker_book_side ==
    'bid' for YES makers, so the correct aggressor direction never fills."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        _resting_order(
            store,
            "2026-07-13",
            _decision(side="YES", limit_price=0.30),
            created_at=NOW - timedelta(minutes=5),
        )
        client = _TradesClient(
            [
                _trade(
                    "T-ASK",
                    yes_price=0.30,
                    quantity=50.0,
                    taker_book_side="ask",
                    created_time=NOW - timedelta(minutes=1),
                )
            ]
        )

        filled = _fill_resting_orders_against_live_book(
            store, client, Color.from_no_color(True)
        )

        assert filled == 1, (
            "a resting YES bid must fill from a taker_book_side='ask' aggressor"
        )


def test_ex01_yes_resting_bid_must_not_fill_from_bid_taker() -> None:
    """taker_book_side == 'bid' means the aggressor bought YES against the NO
    book. That trade fills resting NO bids, never a resting YES bid."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        yes_id = _resting_order(
            store,
            "2026-07-13",
            _decision(side="YES", limit_price=0.30),
            created_at=NOW - timedelta(minutes=5),
        )
        client = _TradesClient(
            [
                _trade(
                    "T-BID",
                    yes_price=0.30,
                    quantity=50.0,
                    taker_book_side="bid",
                    created_time=NOW - timedelta(minutes=1),
                )
            ]
        )

        _fill_resting_orders_against_live_book(store, client, Color.from_no_color(True))

        row = store.paper_order(yes_id)
        assert str(row["status"]) == "PAPER_LIMIT_RESTING", (
            "a bid-side taker must not fill a resting YES bid"
        )


def test_ex01_single_trade_never_fills_both_yes_and_no_makers() -> None:
    """One public trade has exactly one aggressor direction, so incompatible
    resting YES and NO orders can never both be filled by it."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        yes_id = _resting_order(
            store,
            "2026-07-13",
            _decision(
                "KXHIGHTSEA-26JUL13-B82.5", side="YES", limit_price=0.30, contracts=10.0
            ),
            created_at=NOW - timedelta(minutes=5),
        )
        no_id = _resting_order(
            store,
            "2026-07-13",
            _decision(
                "KXHIGHTSEA-26JUL13-B82.5", side="NO", limit_price=0.70, contracts=10.0
            ),
            created_at=NOW - timedelta(minutes=5),
        )
        client = _TradesClient(
            [
                _trade(
                    "T-ONE-DIRECTION",
                    yes_price=0.30,
                    quantity=100.0,
                    taker_book_side="bid",
                    created_time=NOW - timedelta(minutes=1),
                )
            ]
        )

        _fill_resting_orders_against_live_book(store, client, Color.from_no_color(True))

        yes_status = str(store.paper_order(yes_id)["status"])
        no_status = str(store.paper_order(no_id)["status"])
        assert not (yes_status == "PAPER_FILLED" and no_status == "PAPER_FILLED"), (
            "one trade produced both a YES and a NO maker fill"
        )
        assert no_status == "PAPER_FILLED", (
            "the bid-side taker should fill the resting NO bid"
        )


def test_ex01_trade_volume_is_allocated_once_across_same_side_orders() -> None:
    """Quantity 10 cannot fully fill two 8-contract same-price orders. The
    earlier order takes 8; the later order may receive at most the residual 2.
    The orders use separate accounts because active-order uniqueness is now
    account-scoped; public tape volume is still conserved globally."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        first = _resting_order(
            store,
            "2026-07-13",
            _decision(side="NO", limit_price=0.70, contracts=8.0),
            created_at=NOW - timedelta(minutes=10),
        )
        second = _resting_order(
            store,
            "2026-07-13",
            _decision(
                "KXHIGHTSEA-26JUL13-B82.5-TARGET",
                side="NO",
                limit_price=0.70,
                contracts=8.0,
            ),
            created_at=NOW - timedelta(minutes=9),
            risk_profile="research",
            strategy_config=strategy_config_for_profile("research"),
        )
        _move_to_target_account(
            store, second, market_ticker="KXHIGHTSEA-26JUL13-B82.5"
        )
        client = _TradesClient(
            [
                _trade(
                    "T-TEN",
                    yes_price=0.30,
                    quantity=10.0,
                    taker_book_side="bid",
                    created_time=NOW - timedelta(minutes=1),
                )
            ]
        )

        _fill_resting_orders_against_live_book(store, client, Color.from_no_color(True))

        quantities = _filled_quantities(store, [first, second])
        assert quantities[first] + quantities[second] <= 10.0 + 1e-9, (
            "orders booked more maker volume than the public trade contained: "
            f"{quantities}"
        )
        assert quantities[second] <= 2.0 + 1e-9, (
            "the later order may only receive the residual after price-time priority"
        )


def test_ex01_equal_price_priority_earlier_order_wins() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        first = _resting_order(
            store,
            "2026-07-13",
            _decision(side="NO", limit_price=0.70, contracts=8.0),
            created_at=NOW - timedelta(minutes=10),
        )
        second = _resting_order(
            store,
            "2026-07-13",
            _decision(
                "KXHIGHTSEA-26JUL13-B82.5-TARGET",
                side="NO",
                limit_price=0.70,
                contracts=8.0,
            ),
            created_at=NOW - timedelta(minutes=9),
            risk_profile="research",
            strategy_config=strategy_config_for_profile("research"),
        )
        _move_to_target_account(
            store, second, market_ticker="KXHIGHTSEA-26JUL13-B82.5"
        )
        client = _TradesClient(
            [
                _trade(
                    "T-EIGHT",
                    yes_price=0.30,
                    quantity=8.0,
                    taker_book_side="bid",
                    created_time=NOW - timedelta(minutes=1),
                )
            ]
        )

        _fill_resting_orders_against_live_book(store, client, Color.from_no_color(True))

        quantities = _filled_quantities(store, [first, second])
        assert quantities[second] == 0.0, (
            "equal-price priority: the earlier order must consume the trade first"
        )


def test_ex01_rerunning_the_same_trade_batch_creates_no_duplicate_fill() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        order_id = _resting_order(
            store,
            "2026-07-13",
            _decision(side="NO", limit_price=0.70, contracts=8.0),
            created_at=NOW - timedelta(minutes=10),
        )
        client = _TradesClient(
            [
                _trade(
                    "T-IDEMPOTENT",
                    yes_price=0.30,
                    quantity=20.0,
                    taker_book_side="bid",
                    created_time=NOW - timedelta(minutes=1),
                )
            ]
        )

        first_pass = _fill_resting_orders_against_live_book(
            store, client, Color.from_no_color(True)
        )
        second_pass = _fill_resting_orders_against_live_book(
            store, client, Color.from_no_color(True)
        )

        assert first_pass == 1
        assert second_pass == 0
        with store.connect() as conn:
            fills = conn.execute(
                "SELECT COUNT(*) FROM paper_account_ledger "
                "WHERE order_id=? AND event_type='ENTRY_FILL'",
                (order_id,),
            ).fetchone()[0]
        assert fills == 1


def test_ex01_volume_is_not_recredited_across_monitor_passes() -> None:
    """Verifier-confirmed defect: an order filled in pass N left the resting
    set, so pass N+1 re-credited the volume it consumed to the next resting
    order -- the 261/262 double credit spread across two passes. Persisted
    volume claims must make consumed volume unavailable forever."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        first = _resting_order(
            store,
            "2026-07-13",
            _decision(side="NO", limit_price=0.70, contracts=8.0),
            created_at=NOW - timedelta(minutes=10),
        )
        second = _resting_order(
            store,
            "2026-07-13",
            _decision(
                "KXHIGHTSEA-26JUL13-B82.5-TARGET",
                side="NO",
                limit_price=0.70,
                contracts=8.0,
            ),
            created_at=NOW - timedelta(minutes=9),
            risk_profile="research",
            strategy_config=strategy_config_for_profile("research"),
        )
        _move_to_target_account(
            store, second, market_ticker="KXHIGHTSEA-26JUL13-B82.5"
        )
        client = _TradesClient(
            [
                _trade(
                    "T-ONLY",
                    yes_price=0.30,
                    quantity=8.0,
                    taker_book_side="bid",
                    created_time=NOW - timedelta(minutes=1),
                )
            ]
        )

        first_pass = _fill_resting_orders_against_live_book(
            store, client, Color.from_no_color(True)
        )
        second_pass = _fill_resting_orders_against_live_book(
            store, client, Color.from_no_color(True)
        )

        assert first_pass == 1
        assert second_pass == 0, (
            "the second pass re-credited volume already consumed in pass one"
        )
        assert str(store.paper_order(first)["status"]) == "PAPER_FILLED"
        assert str(store.paper_order(second)["status"]) == "PAPER_LIMIT_RESTING"


def test_ex02_partial_close_replays_as_targeted_exit_of_parent() -> None:
    """Verifier-confirmed defect: partial-close lot rows entered the replay as
    independent orders and their exit event flattened the parent early. A lot
    row must replay as a targeted quantity exit of its parent position."""

    from sfo_kalshi_quant.replay import replay_from_database

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            "2026-07-13",
            replace(
                _decision(
                    "KXHIGHTSEA-26JUL13-B82.5",
                    side="NO",
                    limit_price=0.80,
                    cost=0.82,
                    contracts=10.0,
                ),
                entry_ask=0.80,
            ),
            risk_profile="live",
        )
        assert order_id is not None
        store.close_paper_order(
            order_id,
            0.94,
            max_quantity=3.0,
            liquidity_evidence={
                "displayed_bid_size": 3.0,
                "observed_at": datetime.now(UTC).isoformat(),
                "source": "test_orderbook",
            },
        )
        assert store.settle_paper_orders("2026-07-13", 85.0) == 1  # NO wins
        with store.connect() as conn:
            settled = conn.execute(
                "SELECT market_ticker, target_date, settlement_high_f "
                "FROM paper_orders WHERE id=?",
                (order_id,),
            ).fetchone()
            assert settled is not None
            conn.execute(
                "INSERT INTO paper_settlement_verifications ("
                "order_id, checked_at, market_ticker, target_date, "
                "booked_high_f, final_high_f, verification_status"
                ") VALUES (?, ?, ?, ?, ?, ?, 'MATCH')",
                (
                    order_id,
                    datetime.now(UTC).isoformat(),
                    settled[0],
                    settled[1],
                    settled[2],
                    settled[2],
                ),
            )

        result = replay_from_database(
            db_path, {("KXHIGHTSEA", "2026-07-13"): 85.0}
        )

        assert result["available"] is True
        assert result["placed"] == 1, "a partial-close lot must not replay as an order"
        assert "settlement without a filled replay position" not in result[
            "promotion_block_reasons"
        ]
        # 3 contracts exited at net ~0.94-fee, then the REMAINING 7 settled as
        # winners at $1 -- the settlement must find the parent still open with
        # exactly the unexited quantity (7 x (1 - cost) ~ +$1.2, not 10x).
        assert result["settled"] == 2
        assert result["ending_realized_equity"] > 1000.0
        settle_events = [
            event for event in result["events"] if event["event"] == "SETTLE"
        ]
        assert len(settle_events) == 1
        assert 0.5 < settle_events[0]["amount"] < 2.0


def test_ex01_production_regression_shared_trade_ids_orders_261_262() -> None:
    """Production-derived fixture: live order 261 (33 NO @ 0.82, queue 8.18) and
    research order 262 (11 NO @ 0.82, queue 8.18) on KXHIGHTSEA-26JUL13-B82.5
    were both credited the same five public trade ids. A public trade's volume
    is finite: the sum of capital-consuming maker allocations per trade id must
    never exceed that trade's quantity, and the persisted evidence must carry
    the per-order allocation, not just a repeated list of source trade ids."""

    trade_quantities = {
        "ec3a2ebd-e312-5744-80e6-d14cb8d8c0de": 30.0,
        "92390aba-12e8-5da2-88e2-7a6f1b7d37bc": 25.0,
        "8c805d0d-619a-50de-9487-9200dd5eeb93": 22.0,
        "3c7bab7f-1fcb-558a-8ca6-06d826269f22": 18.0,
        "043c5ff6-cb85-55c3-b1bb-f5a61b7201bd": 14.0,
    }  # 109 contracts total, as recorded in both production fill evidences
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        live_id = _resting_order(
            store,
            "2026-07-13",
            _decision("KXHIGHTSEA-26JUL13-B82.5", side="NO", limit_price=0.82, contracts=33.0),
            created_at=datetime(2026, 7, 12, 16, 20, 18, tzinfo=UTC),
            queue_ahead=8.18,
            risk_profile="live",
        )
        research_id = _resting_order(
            store,
            "2026-07-13",
            _decision("KXHIGHTSEA-26JUL13-B82.5", side="NO", limit_price=0.82, contracts=11.0),
            created_at=datetime(2026, 7, 12, 16, 20, 28, tzinfo=UTC),
            queue_ahead=8.18,
            risk_profile="research",
        )
        trades = [
            _trade(
                trade_id,
                yes_price=0.18,
                quantity=quantity,
                taker_book_side="bid",
                created_time=datetime(2026, 7, 12, 16, 30, tzinfo=UTC)
                + timedelta(minutes=index),
            )
            for index, (trade_id, quantity) in enumerate(trade_quantities.items())
        ]
        client = _TradesClient(trades)

        _fill_resting_orders_against_live_book(store, client, Color.from_no_color(True))

        consumed_by_trade: dict[str, float] = {key: 0.0 for key in trade_quantities}
        for order_id in (live_id, research_id):
            row = store.paper_order(order_id)
            if row is None or str(row["status"]) != "PAPER_FILLED":
                continue
            evidence = json.loads(row["fill_evidence_json"] or "{}")
            if evidence.get("research_shadow") or evidence.get("counterfactual"):
                continue  # declared rule: research shadows observe, never consume
            allocations = evidence.get("allocations")
            assert isinstance(allocations, dict) and allocations, (
                f"capital-consuming maker fill for order {order_id} must persist "
                "per-trade allocations, not only a repeated trade-id list"
            )
            for trade_id, quantity in allocations.items():
                assert trade_id in consumed_by_trade, f"unknown trade id {trade_id}"
                consumed_by_trade[trade_id] += float(quantity)
        for trade_id, consumed in consumed_by_trade.items():
            assert consumed <= trade_quantities[trade_id] + 1e-9, (
                f"public trade {trade_id} volume was credited more than once: "
                f"{consumed} > {trade_quantities[trade_id]}"
            )


# ---------------------------------------------------------------------------
# EX-02 -- exits cannot book more quantity than the recorded liquidity supports
# ---------------------------------------------------------------------------


class _ThinBidNoClient:
    """A converged NO favorite whose displayed NO bid supports only 3 contracts."""

    def get_market(self, ticker: str) -> MarketBin:
        return MarketBin(
            ticker=ticker,
            event_ticker="KXHIGHTSEA-26JUL13",
            title="Highest temperature in Seattle?",
            yes_sub_title="82° to 83°",
            strike_type="between",
            floor_strike=82.0,
            cap_strike=83.0,
            yes_bid=0.04,
            yes_ask=0.06,
            no_bid=0.94,
            no_ask=0.96,
            yes_bid_size=10.0,
            yes_ask_size=10.0,
            status="active",
            raw={"no_bid_size_fp": 3.0},
        )


def _yes_probability(ticker: str, probability: float) -> BucketProbability:
    return BucketProbability(
        ticker=ticker,
        label="82° to 83°",
        probability=probability,
        lower_confidence=max(0.0, probability - 0.05),
        empirical_probability=probability,
        normal_probability=probability,
        effective_n=200,
    )


def test_ex02_full_close_cannot_exceed_displayed_bid_size() -> None:
    """10 contracts with a displayed top-bid size of 3 must not become fully
    closed at that price. At most the displayed size may be realized; the
    remainder stays open."""

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            "2026-07-13",
            _decision(
                "KXHIGHTSEA-26JUL13-B82.5",
                side="NO",
                limit_price=0.80,
                cost=0.82,
                contracts=10.0,
            ),
            risk_profile="live",
        )
        assert order_id is not None
        # Model YES 0.10 -> NO fair value 0.90; displayed NO bid 0.94 nets above
        # fair value, so the convergence take-profit fires.
        store.record_probabilities(
            "2026-07-13", [_yes_probability("KXHIGHTSEA-26JUL13-B82.5", 0.10)]
        )

        with (
            patch("sfo_kalshi_quant.cli.KalshiPublicClient", _ThinBidNoClient),
            redirect_stdout(StringIO()),
        ):
            assert main(["--db-path", str(db_path), "--no-color", "paper-monitor"]) == 0

        with store.connect() as conn:
            remaining = conn.execute(
                "SELECT COALESCE(SUM(contracts), 0) FROM paper_orders "
                "WHERE market_ticker='KXHIGHTSEA-26JUL13-B82.5' "
                "AND status='PAPER_FILLED' AND settled_at IS NULL AND closed_at IS NULL",
            ).fetchone()[0]
            closed = conn.execute(
                "SELECT COALESCE(SUM(contracts), 0) FROM paper_orders "
                "WHERE market_ticker='KXHIGHTSEA-26JUL13-B82.5' "
                "AND status='PAPER_CLOSED'",
            ).fetchone()[0]
        assert float(closed) <= 3.0 + 1e-9, (
            f"closed {closed} contracts at a bid displaying size 3"
        )
        assert float(remaining) >= 7.0 - 1e-9, (
            "the quantity beyond displayed liquidity must remain open"
        )


def test_ex02_close_decision_persists_liquidity_evidence() -> None:
    """Every executed close must record the bid size / executed quantity it
    relied on, so the exit can be audited and replayed against recorded depth."""

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = store.record_paper_order(
            "2026-07-13",
            _decision(
                "KXHIGHTSEA-26JUL13-B82.5",
                side="NO",
                limit_price=0.80,
                cost=0.82,
                contracts=3.0,
            ),
            risk_profile="live",
        )
        assert order_id is not None
        store.record_probabilities(
            "2026-07-13", [_yes_probability("KXHIGHTSEA-26JUL13-B82.5", 0.10)]
        )

        with (
            patch("sfo_kalshi_quant.cli.KalshiPublicClient", _ThinBidNoClient),
            redirect_stdout(StringIO()),
        ):
            assert main(["--db-path", str(db_path), "--no-color", "paper-monitor"]) == 0

        row = store.paper_order(order_id)
        assert str(row["status"]) == "PAPER_CLOSED"
        diagnostics = json.loads(row["outcome_diagnostics_json"] or "{}")
        execution = diagnostics.get("exit_execution") or {}
        assert float(execution.get("executed_quantity", -1.0)) == 3.0, (
            "close must persist the executed quantity"
        )
        assert float(execution.get("displayed_bid_size", -1.0)) == 3.0, (
            "close must persist the displayed liquidity it consumed"
        )


# ---------------------------------------------------------------------------
# RK-01 -- the catastrophic stop has unconditional priority over basket vetoes
# ---------------------------------------------------------------------------


class _CatastrophicNoBasketClient:
    """Two research NO legs; the stopped leg trades at a catastrophic loss."""

    def get_market(self, ticker: str) -> MarketBin:
        if ticker.endswith("B89.5"):
            no_bid, no_ask = 0.10, 0.12
            floor, cap, label = 89.0, 90.0, "89° to 90°"
        else:
            no_bid, no_ask = 0.72, 0.74
            floor, cap, label = 91.0, 92.0, "91° to 92°"
        return MarketBin(
            ticker=ticker,
            event_ticker="KXHIGHTDAL-26JUL13",
            title="Highest temperature in Dallas?",
            yes_sub_title=label,
            strike_type="between",
            floor_strike=floor,
            cap_strike=cap,
            yes_bid=max(0.0, 1.0 - no_ask),
            yes_ask=max(0.0, 1.0 - no_bid),
            no_bid=no_bid,
            no_ask=no_ask,
            yes_bid_size=50.0,
            yes_ask_size=50.0,
            status="active",
        )


def test_rk01_basket_veto_cannot_override_catastrophic_stop() -> None:
    """Production-derived fixture (order 311): research NO on Dallas 89-90°F
    entered at 0.72 was held by the same-day NO basket veto at roughly -87%
    ROI -- far beyond the 60% catastrophic floor -- and finally closed at
    -96.1%. Catastrophic stop discipline is unconditional: no basket or model
    veto may hold a leg past the catastrophic loss floor."""

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        stopped_id = store.record_paper_order(
            "2026-07-13",
            _decision(
                "KXHIGHTDAL-26JUL13-B89.5",
                side="NO",
                limit_price=0.70,
                cost=0.72,
                contracts=12.0,
                floor=89.0,
                cap=90.0,
            ),
            risk_profile="research",
        )
        store.record_paper_order(
            "2026-07-13",
            _decision(
                "KXHIGHTDAL-26JUL13-B91.5",
                side="NO",
                limit_price=0.82,
                cost=0.84,
                contracts=5.0,
                floor=91.0,
                cap=92.0,
            ),
            risk_profile="research",
        )
        # A fresh model read still "supports" the stopped leg (NO p=0.80 clears
        # the veto floor at the 0.72 entry cost), the exact condition the basket
        # veto used in production to hold order 311 through its collapse.
        store.record_probabilities(
            "2026-07-13", [_yes_probability("KXHIGHTDAL-26JUL13-B89.5", 0.20)]
        )

        out = StringIO()
        with (
            patch("sfo_kalshi_quant.cli.KalshiPublicClient", _CatastrophicNoBasketClient),
            redirect_stdout(out),
        ):
            assert main(["--db-path", str(db_path), "--no-color", "paper-monitor"]) == 0

        row = store.paper_order(stopped_id)
        assert str(row["status"]) == "PAPER_CLOSED", (
            "a catastrophic-loss leg must close even when a research basket "
            f"model still supports it (status={row['status']})"
        )
        with store.connect() as conn:
            action = conn.execute(
                "SELECT action FROM paper_monitor_snapshots "
                "WHERE order_id=? ORDER BY id DESC LIMIT 1",
                (stopped_id,),
            ).fetchone()[0]
        assert action == "CLOSE_STOP_LOSS"


def test_rk01_exit_decision_carries_catastrophic_priority() -> None:
    """The exit decision engine must expose catastrophic priority in its typed
    result so downstream vetoes are structurally unable to override it."""

    from sfo_kalshi_quant.exits import decide_exit

    signal = decide_exit(
        side="NO",
        entry_cost=0.72,
        net_exit=0.08,
        stop_loss_net=0.72 * 0.65,
        model_side_probability=0.66,
        model_veto_buffer=0.08,
        model_veto_max_loss_roi=0.60,
        legacy_take_profit_net=None,
        stop_loss_pct=35.0,
    )
    assert signal.action == "STOP_LOSS"
    assert getattr(signal, "catastrophic", False) is True, (
        "a stop past the catastrophic floor must be marked unoverridable"
    )


# ---------------------------------------------------------------------------
# MD-01 -- nonfinal raw observations cannot create exact settlement certainty
# ---------------------------------------------------------------------------


def _phil_bins() -> list[MarketBin]:
    bins: list[MarketBin] = []
    for floor, cap in ((82.0, 83.0), (84.0, 85.0), (86.0, 87.0), (88.0, 89.0)):
        bins.append(
            MarketBin(
                ticker=f"KXHIGHPHIL-26JUL10-B{floor + 0.5}",
                event_ticker="KXHIGHPHIL-26JUL10",
                title="Highest temperature in Philadelphia?",
                yes_sub_title=f"{int(floor)}° to {int(cap)}°",
                strike_type="between",
                floor_strike=floor,
                cap_strike=cap,
                yes_bid=0.10,
                yes_ask=0.12,
                no_bid=0.88,
                no_ask=0.90,
                yes_bid_size=50.0,
                yes_ask_size=50.0,
                status="active",
            )
        )
    bins.append(
        MarketBin(
            ticker="KXHIGHPHIL-26JUL10-T89.5",
            event_ticker="KXHIGHPHIL-26JUL10",
            title="Highest temperature in Philadelphia?",
            yes_sub_title="90° or above",
            strike_type="greater",
            floor_strike=89.0,
            cap_strike=None,
            yes_bid=0.02,
            yes_ask=0.04,
            no_bid=0.96,
            no_ask=0.98,
            yes_bid_size=50.0,
            yes_ask_size=50.0,
            status="active",
        )
    )
    return bins


def _calibration_outcomes() -> list[ForecastOutcome]:
    start = date(2026, 1, 1)
    rows = []
    for idx in range(220):
        predicted = 80.0 + (idx % 10) * 0.7
        residual = [-3, -2, -1, 0, 1, 2, 3, 4, -1, 1][idx % 10]
        rows.append(
            ForecastOutcome(
                local_date=start + timedelta(days=idx),
                predicted_high_f=predicted,
                actual_high_f=predicted + residual,
            )
        )
    return rows


def test_md01_nonfinal_raw_observation_must_not_zero_boundary_bin() -> None:
    """Production-derived fixture (order 188): the Philadelphia station's raw
    nonfinal maximum reached 87.8°F, which zeroed the 86-87°F bin, yet the
    official integer daily climate report settled at 87°F and the bin resolved
    YES. A raw nonfinal observation is not an exact settlement value: bins the
    official integer report can still reach must keep positive probability."""

    calibrator = ResidualCalibrator(
        _calibration_outcomes(), StrategyConfig(min_conditional_samples=20)
    )
    intraday = IntradaySnapshot(
        target_date=date(2026, 7, 10),
        observed_high_f=87.8,
        latest_temp_f=87.8,
        latest_observed_at=datetime(2026, 7, 10, 18, 0, tzinfo=UTC).isoformat(),
        remaining_forecast_high_f=None,
        forecast_fetched_at=None,
        observation_count=40,
        observed_high_source="nws_station_observations",
        is_complete=False,
    )

    probabilities = calibrator.bucket_probabilities(
        _phil_bins(),
        86.0,
        observed_high_f=87.8,
        intraday=intraday,
    )

    boundary = probabilities["KXHIGHPHIL-26JUL10-B86.5"]
    assert boundary.probability > 0.0, (
        "raw nonfinal 87.8°F must not make the 86-87°F bin impossible: the "
        "official integer report can still round/settle at 87°F"
    )


def test_md01_exact_point_mass_requires_final_truth() -> None:
    """With is_complete final truth the point mass stays allowed (control)."""

    calibrator = ResidualCalibrator(
        _calibration_outcomes(), StrategyConfig(min_conditional_samples=20)
    )
    intraday = IntradaySnapshot(
        target_date=date(2026, 7, 10),
        observed_high_f=87.0,
        latest_temp_f=87.0,
        latest_observed_at=datetime(2026, 7, 11, 9, 0, tzinfo=UTC).isoformat(),
        remaining_forecast_high_f=None,
        forecast_fetched_at=None,
        observation_count=60,
        observed_high_source="nws_daily_high_ground_truth",
        is_complete=True,
    )

    probabilities = calibrator.bucket_probabilities(
        _phil_bins(),
        86.0,
        observed_high_f=87.0,
        intraday=intraday,
    )

    boundary = probabilities["KXHIGHPHIL-26JUL10-B86.5"]
    assert boundary.intraday_probability == 1.0
    assert boundary.probability == max(
        row.probability for row in probabilities.values()
    )


# ---------------------------------------------------------------------------
# AC-01 -- research experiments cannot reduce production-intent live capacity
# ---------------------------------------------------------------------------


def test_ac01_research_loss_cannot_pause_or_shrink_live_entries() -> None:
    """A research experiment that loses money must not consume live-profile
    capacity: no shared daily-loss pause, no drawdown pause, no cash reduction
    applied against a subsequent live entry."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        research_id = store.record_paper_order(
            "2026-07-13",
            _decision(
                "KXHIGHTDAL-26JUL13-B89.5",
                side="NO",
                limit_price=0.78,
                cost=0.80,
                contracts=250.0,
                floor=89.0,
                cap=90.0,
            ),
            risk_profile="research",
        )
        assert research_id is not None
        closed = store.close_paper_order(research_id, 0.01)
        assert float(closed["realized_pnl"]) < -150.0  # a catastrophic research day

        capacity = store.account_policy_capacity(
            target_date="2026-07-13",
            market_ticker="KXHIGHTSEA-26JUL13-B82.5",
            risk_profile="live",
            requested_spend=20.0,
        )

        assert float(capacity["allowed_spend"]) > 0.0, (
            "research losses must not pause or shrink production-intent live "
            f"entries (blocked: {capacity['reason']})"
        )


def test_ac01_live_and_research_ledgers_reconcile_independently() -> None:
    """Each ledger reconciles on its own: research activity moves only the
    research shadow account, and the live (shared) account is bit-for-bit
    indifferent to it."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        live_before = store.shared_account_state()
        research_id = store.record_paper_order(
            "2026-07-13",
            _decision(
                "KXHIGHTDAL-26JUL13-B89.5",
                side="NO",
                limit_price=0.78,
                cost=0.80,
                contracts=20.0,
                floor=89.0,
                cap=90.0,
            ),
            risk_profile="research",
        )
        assert research_id is not None
        store.close_paper_order(research_id, 0.10)

        live_after = store.shared_account_state()
        research_after = store.research_account_state()

        assert live_after["available_cash"] == live_before["available_cash"]
        assert live_after["realized_equity"] == live_before["realized_equity"]
        assert live_after["drawdown"] == live_before["drawdown"]
        assert research_after is not None
        assert research_after["realized_equity"] < research_after["initial_capital"]


# ---------------------------------------------------------------------------
# DB-01 -- store initialization must be process/thread safe
# ---------------------------------------------------------------------------


def test_db01_concurrent_fresh_init_bootstraps_exactly_one_account(tmp_path: Path) -> None:
    """Concurrent init creates each account/opening once without schema races."""

    for attempt in range(40):
        db_path = tmp_path / f"race-{attempt}.db"
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(PaperStore, db_path) for _ in range(16)]
            errors = []
            for future in futures:
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 -- collecting race evidence
                    errors.append(f"{type(exc).__name__}: {exc}")
        assert not errors, f"attempt {attempt}: concurrent init raised {errors}"
        with sqlite3.connect(db_path) as conn:
            accounts = conn.execute(
                "SELECT account_id, COUNT(*), MIN(initial_capital), "
                "MIN(opening_cash), MIN(high_water_equity) "
                "FROM paper_accounts GROUP BY account_id"
            ).fetchall()
            openings = conn.execute(
                "SELECT account_id, COUNT(*), SUM(amount) FROM paper_account_ledger "
                "WHERE event_type='OPENING_CASH' GROUP BY account_id"
            ).fetchall()
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        # Legacy shared/shadow and both isolated research sleeves are each
        # bootstrapped exactly once at the unchanged $1,000 opening capital.
        assert sorted(accounts) == [
            ("paper-research-motion-v1", 1, 1000.0, 1000.0, 1000.0),
            ("paper-research-shadow", 1, 1000.0, 1000.0, 1000.0),
            ("paper-research-target-v1", 1, 1000.0, 1000.0, 1000.0),
            ("paper-shared", 1, 1000.0, 1000.0, 1000.0),
        ], f"attempt {attempt}: unexpected account rows {accounts}"
        assert sorted(openings) == [
            ("paper-research-motion-v1", 1, 1000.0),
            ("paper-research-shadow", 1, 1000.0),
            ("paper-research-target-v1", 1, 1000.0),
            ("paper-shared", 1, 1000.0),
        ], f"attempt {attempt}: unexpected opening events {openings}"
        assert integrity == "ok"
