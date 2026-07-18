"""Exec-v4 restatement must reproduce the shared maker allocator exactly."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.maker_fills import EXECUTION_MODEL_VERSION
from sfo_kalshi_quant.replay import replay_from_database
from sfo_kalshi_quant.restatement import restate
from test_audit_2026_07_13 import _decision, _resting_order, _trade


T0 = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
TICKER = "KXHIGHTSEA-26JUL17-B82.5"
TARGET_DATE = "2026-07-17"
TRUTH = {("KXHIGHTSEA", TARGET_DATE): 85.0}


def _store(path: Path) -> PaperStore:
    store = PaperStore(path)
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_account_ledger SET created_at=? "
            "WHERE idempotency_key=?",
            (
                (T0 - timedelta(minutes=1)).isoformat(),
                f"execution:{EXECUTION_MODEL_VERSION}",
            ),
        )
    return store


def _order(
    store: PaperStore,
    *,
    ticker: str = TICKER,
    limit_price: float = 0.72,
    entry_bid: float | None = None,
    contracts: float = 5.0,
    queue_ahead: float = 100.0,
    placed_at: datetime = T0,
    risk_profile: str = "live",
) -> int:
    decision = _decision(
        ticker,
        side="NO",
        limit_price=limit_price,
        contracts=contracts,
    )
    if entry_bid is not None:
        decision = replace(decision, entry_bid=entry_bid)
    return _resting_order(
        store,
        TARGET_DATE,
        decision,
        created_at=placed_at,
        queue_ahead=queue_ahead,
        risk_profile=risk_profile,
    )


def _apply(
    store: PaperStore,
    trade_id: str,
    *,
    no_price: float,
    quantity: float,
    at: datetime | None = None,
) -> None:
    store.apply_maker_trade_batch(
        TICKER,
        [
            _trade(
                trade_id,
                yes_price=1.0 - no_price,
                quantity=quantity,
                taker_book_side="bid",
                created_time=at or T0 + timedelta(minutes=1),
            )
        ],
    )


def _settle(store: PaperStore) -> None:
    assert store.settle_paper_orders(TARGET_DATE, 85.0) >= 1


def _result(db_path: Path, order_id: int) -> dict[str, object]:
    return next(
        row for row in restate(db_path)["orders"] if row["order_id"] == order_id
    )


def _assert_unverified_and_readiness_ineligible(
    db_path: Path,
    order_id: int,
    reason: str,
) -> None:
    result = _result(db_path, order_id)
    assert result["verification"] == "UNVERIFIABLE"
    assert reason in result["findings"]
    readiness = replay_from_database(db_path, TRUTH)
    assert readiness["verified_decisions"] == 0
    assert readiness["post_boundary_days"] == 0
    assert readiness["promotion_eligible"] is False
    assert any(
        "unverified execution evidence" in reason
        for reason in readiness["promotion_block_reasons"]
    )


def _rewrite_consumption(
    store: PaperStore,
    order_id: int,
    trade_id: str,
    *,
    queue: float,
    fill: float,
) -> None:
    amounts = {
        "queue_quantity": queue,
        "fill_quantity": fill,
        "total_quantity": queue + fill,
    }
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_maker_allocations "
            "SET queue_quantity=?, fill_quantity=?, evidence_json=? "
            "WHERE order_id=? AND trade_id=?",
            (queue, fill, json.dumps(amounts, sort_keys=True), order_id, trade_id),
        )
        evidence = json.loads(
            conn.execute(
                "SELECT fill_evidence_json FROM paper_orders WHERE id=?",
                (order_id,),
            ).fetchone()[0]
        )
        evidence["consumptions"][trade_id] = amounts
        evidence["allocations"] = {trade_id: fill} if fill > 0 else {}
        conn.execute(
            "UPDATE paper_orders SET fill_evidence_json=? WHERE id=?",
            (json.dumps(evidence, sort_keys=True), order_id),
        )


def test_restatement_verifies_valid_exec_v4_partial_queue_and_fill() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store)
        _apply(store, "valid-queue-fill", no_price=0.72, quantity=105.0)
        _settle(store)

        result = _result(db_path, order_id)

        assert result["verification"] == "VERIFIED"
        assert result["findings"] == []


def test_restatement_rejects_skipped_at_touch_queue() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store)
        _apply(store, "skipped-at-touch-queue", no_price=0.72, quantity=105.0)
        _settle(store)
        _rewrite_consumption(
            store,
            order_id,
            "skipped-at-touch-queue",
            queue=0.0,
            fill=5.0,
        )

        _assert_unverified_and_readiness_ineligible(
            db_path, order_id, "EXEC_V4_ALLOCATION_REPLAY_MISMATCH"
        )


def test_restatement_rejects_below_bid_queue_price_misconsumption() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, limit_price=0.71, entry_bid=0.72)
        _apply(store, "below-bid-queue", no_price=0.71, quantity=105.0)
        _settle(store)
        _rewrite_consumption(
            store,
            order_id,
            "below-bid-queue",
            queue=95.0,
            fill=5.0,
        )

        _assert_unverified_and_readiness_ineligible(
            db_path, order_id, "EXEC_V4_ALLOCATION_REPLAY_MISMATCH"
        )


def test_restatement_rejects_price_time_priority_inversion() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        higher_id = _order(
            store,
            limit_price=0.72,
            queue_ahead=0.0,
            risk_profile="research",
        )
        lower_id = _order(
            store,
            limit_price=0.71,
            queue_ahead=0.0,
            placed_at=T0 + timedelta(seconds=1),
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET account_id='paper-shared' WHERE id=?",
                (higher_id,),
            )
        _apply(store, "priority-inversion", no_price=0.70, quantity=5.0)
        with store.connect() as conn:
            higher_evidence = conn.execute(
                "SELECT fill_evidence_json FROM paper_orders WHERE id=?",
                (higher_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE paper_maker_allocations SET order_id=? WHERE order_id=?",
                (lower_id, higher_id),
            )
            conn.execute(
                "UPDATE paper_orders SET status='PAPER_EXPIRED', "
                "filled_contracts=0, remaining_contracts=0, "
                "fill_evidence_json=NULL WHERE id=?",
                (higher_id,),
            )
            conn.execute(
                "UPDATE paper_orders SET status='PAPER_FILLED', contracts=5, "
                "filled_contracts=5, remaining_contracts=0, "
                "fill_evidence_json=? WHERE id=?",
                (higher_evidence, lower_id),
            )
        _settle(store)

        _assert_unverified_and_readiness_ineligible(
            db_path, lower_id, "EXEC_V4_ALLOCATION_REPLAY_MISMATCH"
        )


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("order", "EXEC_V4_ORDER_VERSION_MISMATCH"),
        ("allocation", "EXEC_V4_ALLOCATION_VERSION_MISMATCH"),
        ("evidence", "EXEC_V4_EVIDENCE_VERSION_MISMATCH"),
    ],
)
def test_restatement_rejects_v3_rows_attached_to_v4_evidence(
    target: str,
    reason: str,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, queue_ahead=0.0)
        _apply(store, "mixed-version", no_price=0.72, quantity=5.0)
        _settle(store)
        with store.connect() as conn:
            if target == "order":
                conn.execute(
                    "UPDATE paper_orders SET execution_model_version=? WHERE id=?",
                    ("exec-v3-2026-07-14", order_id),
                )
            elif target == "allocation":
                conn.execute(
                    "UPDATE paper_maker_allocations "
                    "SET execution_model_version=? WHERE order_id=?",
                    ("exec-v3-2026-07-14", order_id),
                )
            else:
                evidence = json.loads(
                    conn.execute(
                        "SELECT fill_evidence_json FROM paper_orders WHERE id=?",
                        (order_id,),
                    ).fetchone()[0]
                )
                evidence["execution_model_version"] = "exec-v3-2026-07-14"
                conn.execute(
                    "UPDATE paper_orders SET fill_evidence_json=? WHERE id=?",
                    (json.dumps(evidence, sort_keys=True), order_id),
                )

        _assert_unverified_and_readiness_ineligible(db_path, order_id, reason)


def test_restatement_rejects_missing_trade_allocation() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, queue_ahead=0.0)
        _apply(store, "missing-allocation", no_price=0.72, quantity=5.0)
        _settle(store)
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM paper_maker_allocations WHERE order_id=?",
                (order_id,),
            )

        _assert_unverified_and_readiness_ineligible(
            db_path, order_id, "EXEC_V4_ALLOCATION_MISSING"
        )


def test_restatement_rejects_duplicate_trade_allocation() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, queue_ahead=0.0)
        _apply(store, "duplicate-allocation", no_price=0.72, quantity=5.0)
        _settle(store)
        with store.connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM paper_maker_allocations WHERE order_id=?",
                (order_id,),
            ).fetchone()
            conn.execute(
                "INSERT INTO paper_maker_allocations ("
                "created_at, execution_model_version, market_ticker, trade_id, "
                "order_id, trade_created_at, maker_side, side_price, "
                "queue_quantity, fill_quantity, counterfactual, evidence_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["created_at"],
                    "exec-v3-2026-07-14",
                    row["market_ticker"],
                    row["trade_id"],
                    row["order_id"],
                    row["trade_created_at"],
                    row["maker_side"],
                    row["side_price"],
                    0.0,
                    0.0,
                    row["counterfactual"],
                    json.dumps(
                        {
                            "queue_quantity": 0.0,
                            "fill_quantity": 0.0,
                            "total_quantity": 0.0,
                        },
                        sort_keys=True,
                    ),
                ),
            )

        _assert_unverified_and_readiness_ineligible(
            db_path, order_id, "EXEC_V4_ALLOCATION_DUPLICATE"
        )


def test_restatement_rejects_tampered_trade_allocation() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, queue_ahead=0.0)
        _apply(store, "tampered-allocation", no_price=0.72, quantity=5.0)
        _settle(store)
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_maker_allocations SET trade_created_at=? "
                "WHERE order_id=?",
                ((T0 + timedelta(minutes=2)).isoformat(), order_id),
            )

        _assert_unverified_and_readiness_ineligible(
            db_path, order_id, "EXEC_V4_ALLOCATION_REPLAY_MISMATCH"
        )


def test_restatement_fails_closed_without_initial_queue_evidence() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, queue_ahead=0.0)
        _apply(store, "missing-queue-evidence", no_price=0.72, quantity=5.0)
        _settle(store)
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET entry_bid_size=NULL WHERE id=?",
                (order_id,),
            )

        _assert_unverified_and_readiness_ineligible(
            db_path, order_id, "INSUFFICIENT_REPLAY_EVIDENCE"
        )


@pytest.mark.parametrize(
    ("target", "column", "value", "reason"),
    [
        (
            "order",
            "limit_price",
            "bad",
            "EXEC_V4_ORDER_LIMIT_PRICE_INVALID",
        ),
        (
            "order",
            "entry_bid",
            "NaN",
            "EXEC_V4_ORDER_ENTRY_BID_INVALID",
        ),
        (
            "order",
            "entry_bid_size",
            float("inf"),
            "EXEC_V4_ORDER_ENTRY_BID_SIZE_INVALID",
        ),
        (
            "order",
            "requested_contracts",
            None,
            "EXEC_V4_ORDER_REQUESTED_QUANTITY_INVALID",
        ),
        (
            "order",
            "filled_contracts",
            "bad",
            "EXEC_V4_ORDER_FILLED_QUANTITY_INVALID",
        ),
        (
            "order",
            "created_at",
            "",
            "EXEC_V4_ORDER_PLACED_AT_INVALID",
        ),
        (
            "allocation",
            "side_price",
            "bad",
            "EXEC_V4_ALLOCATION_SIDE_PRICE_INVALID",
        ),
        (
            "allocation",
            "queue_quantity",
            -1.0,
            "EXEC_V4_ALLOCATION_QUEUE_QUANTITY_INVALID",
        ),
        (
            "allocation",
            "fill_quantity",
            float("inf"),
            "EXEC_V4_ALLOCATION_FILL_QUANTITY_INVALID",
        ),
        (
            "allocation",
            "counterfactual",
            "bad",
            "EXEC_V4_ALLOCATION_COUNTERFACTUAL_INVALID",
        ),
        (
            "allocation",
            "trade_created_at",
            "",
            "EXEC_V4_ALLOCATION_TRADE_TIME_INVALID",
        ),
        (
            "allocation",
            "trade_id",
            "",
            "EXEC_V4_ALLOCATION_TRADE_ID_INVALID",
        ),
        (
            "allocation",
            "order_id",
            "bad",
            "EXEC_V4_ALLOCATION_ORDER_ID_INVALID",
        ),
        (
            "tape",
            "count",
            "bad",
            "EXEC_V4_TAPE_QUANTITY_INVALID",
        ),
        (
            "tape",
            "yes_price",
            float("inf"),
            "EXEC_V4_TAPE_YES_PRICE_INVALID",
        ),
        (
            "tape",
            "created_time",
            "",
            "EXEC_V4_TAPE_TIME_INVALID",
        ),
        (
            "tape",
            "trade_id",
            "",
            "EXEC_V4_TAPE_TRADE_ID_INVALID",
        ),
        (
            "tape",
            "is_block_trade",
            "bad",
            "EXEC_V4_TAPE_BLOCK_FLAG_INVALID",
        ),
    ],
)
def test_restatement_fails_closed_on_malformed_replay_fields(
    target: str,
    column: str,
    value: object,
    reason: str,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, queue_ahead=0.0)
        trade_id = f"malformed-{target}-{column}"
        _apply(store, trade_id, no_price=0.72, quantity=5.0)
        _settle(store)
        table = {
            "order": "paper_orders",
            "allocation": "paper_maker_allocations",
            "tape": "dataset_kalshi_trades",
        }[target]
        predicate = {
            "order": "id=?",
            "allocation": "order_id=?",
            "tape": "trade_id=?",
        }[target]
        identity: object = trade_id if target == "tape" else order_id
        connection = (
            sqlite3.connect(db_path)
            if target == "allocation" and column == "order_id"
            else store.connect()
        )
        with connection as conn:
            conn.execute(
                f"UPDATE {table} SET {column}=? WHERE {predicate}",
                (value, identity),
            )

        _assert_unverified_and_readiness_ineligible(db_path, order_id, reason)


def test_restatement_fails_closed_on_malformed_prior_claim_quantity() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, queue_ahead=0.0)
        trade_id = "malformed-prior-claim"
        _apply(store, trade_id, no_price=0.72, quantity=5.0)
        _settle(store)
        with store.connect() as conn:
            conn.execute(
                "INSERT INTO maker_volume_claims ("
                "created_at, market_ticker, trade_id, order_id, quantity"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    T0.isoformat(),
                    TICKER,
                    trade_id,
                    order_id + 1000,
                    "bad",
                ),
            )

        _assert_unverified_and_readiness_ineligible(
            db_path, order_id, "EXEC_V4_PRIOR_CLAIM_QUANTITY_INVALID"
        )
