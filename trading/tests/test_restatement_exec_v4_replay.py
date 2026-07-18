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
from sfo_kalshi_quant.paper_pnl import settled_position_pnl
from sfo_kalshi_quant.replay import replay_from_database
from sfo_kalshi_quant.restatement import restate
from test_audit_2026_07_13 import _decision, _resting_order, _trade


T0 = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
TICKER = "KXHIGHTSEA-26JUL17-B82.5"
TICKER_2 = "KXHIGHTSEA-26JUL17-B84.5"
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
    ticker: str = TICKER,
) -> None:
    store.apply_maker_trade_batch(
        ticker,
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
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT id, market_ticker, target_date, settlement_high_f "
            "FROM paper_orders WHERE status='PAPER_SETTLED'"
        ).fetchall()
        for order_id, ticker, target_date, booked_high in rows:
            conn.execute(
                "INSERT OR REPLACE INTO paper_settlement_verifications "
                "(order_id, checked_at, market_ticker, target_date, "
                "booked_high_f, final_high_f, verification_status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'MATCH')",
                (
                    order_id,
                    datetime.now(UTC).isoformat(),
                    ticker,
                    target_date,
                    booked_high,
                    booked_high,
                ),
            )


def _result(db_path: Path, order_id: int) -> dict[str, object]:
    return next(
        row for row in restate(db_path)["orders"] if row["order_id"] == order_id
    )


def _settlement_check_update(
    store: PaperStore,
    order_id: int,
    **values: object,
) -> None:
    assignments = ", ".join(f"{column}=?" for column in values)
    with store.connect() as conn:
        conn.execute(
            f"UPDATE paper_settlement_verifications SET {assignments} "
            "WHERE order_id=?",
            (*values.values(), order_id),
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


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing", "SETTLEMENT_VERIFICATION_REQUIRED"),
        ("missing-final", "SETTLEMENT_VERIFICATION_REQUIRED"),
        ("unknown", "SETTLEMENT_VERIFICATION_REQUIRED"),
        ("null-final", "SETTLEMENT_VERIFICATION_FINAL_HIGH_INVALID"),
        ("implausible-final", "SETTLEMENT_VERIFICATION_FINAL_HIGH_INVALID"),
        ("mismatched-final", "SETTLEMENT_VERIFICATION_FINAL_HIGH_MISMATCH"),
        ("mismatched-booked", "SETTLEMENT_VERIFICATION_BOOKED_HIGH_MISMATCH"),
        ("bad-time", "SETTLEMENT_VERIFICATION_CHECKED_AT_INVALID"),
        ("early-time", "SETTLEMENT_VERIFICATION_CHECKED_AT_INVALID"),
    ],
)
def test_restatement_requires_valid_matching_settlement_evidence(
    mutation: str,
    reason: str,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, queue_ahead=0.0)
        _apply(store, f"settlement-{mutation}", no_price=0.72, quantity=5.0)
        _settle(store)
        if mutation == "missing":
            with store.connect() as conn:
                conn.execute(
                    "DELETE FROM paper_settlement_verifications WHERE order_id=?",
                    (order_id,),
                )
        elif mutation == "missing-final":
            _settlement_check_update(
                store,
                order_id,
                verification_status="MISSING_FINAL",
                final_high_f=None,
            )
        elif mutation == "unknown":
            _settlement_check_update(
                store, order_id, verification_status="SURPRISING"
            )
        elif mutation == "null-final":
            _settlement_check_update(store, order_id, final_high_f=None)
        elif mutation == "implausible-final":
            _settlement_check_update(store, order_id, final_high_f=999.0)
        elif mutation == "mismatched-final":
            _settlement_check_update(store, order_id, final_high_f=84.0)
        elif mutation == "mismatched-booked":
            _settlement_check_update(store, order_id, booked_high_f=84.0)
        elif mutation == "bad-time":
            _settlement_check_update(store, order_id, checked_at="")
        else:
            _settlement_check_update(store, order_id, checked_at=T0.isoformat())

        _assert_unverified_and_readiness_ineligible(db_path, order_id, reason)


@pytest.mark.parametrize("delta", [0.01, 999.0], ids=["one-cent", "sentinel"])
def test_restatement_recomputes_settlement_pnl(delta: float) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, queue_ahead=0.0)
        _apply(store, "settlement-pnl", no_price=0.72, quantity=5.0)
        _settle(store)
        with store.connect() as conn:
            pnl = float(
                conn.execute(
                    "SELECT realized_pnl FROM paper_orders WHERE id=?", (order_id,)
                ).fetchone()[0]
            )
            conn.execute(
                "UPDATE paper_orders SET realized_pnl=? WHERE id=?",
                (pnl + delta, order_id),
            )

        _assert_unverified_and_readiness_ineligible(
            db_path, order_id, "REALIZED_PNL_MISMATCH"
        )


@pytest.mark.parametrize("target", ["resolved", "diagnostics"])
def test_restatement_rejects_inconsistent_settlement_outcome(target: str) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, queue_ahead=0.0)
        _apply(store, "settlement-outcome", no_price=0.72, quantity=5.0)
        _settle(store)
        with store.connect() as conn:
            if target == "resolved":
                conn.execute(
                    "UPDATE paper_orders SET resolved_yes=1-resolved_yes WHERE id=?",
                    (order_id,),
                )
            else:
                outcome = json.loads(
                    conn.execute(
                        "SELECT outcome_diagnostics_json FROM paper_orders WHERE id=?",
                        (order_id,),
                    ).fetchone()[0]
                )
                outcome["outcome"]["realized_pnl"] = 999.0
                conn.execute(
                    "UPDATE paper_orders SET outcome_diagnostics_json=? WHERE id=?",
                    (json.dumps(outcome, sort_keys=True), order_id),
                )

        _assert_unverified_and_readiness_ineligible(
            db_path,
            order_id,
            "SETTLEMENT_OUTCOME_MISMATCH"
            if target == "resolved"
            else "OUTCOME_DIAGNOSTICS_MISMATCH",
        )


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
                "fill_evidence_json=NULL, cancelled_at=? WHERE id=?",
                ((T0 + timedelta(minutes=2)).isoformat(), higher_id),
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
    ("cancel_offset", "verification"),
    [
        (timedelta(0), "VERIFIED"),
        (timedelta(microseconds=-1), "UNVERIFIABLE"),
    ],
    ids=["exact-cutoff-inclusive", "just-after-cutoff"],
)
def test_restatement_honors_authoritative_cancelled_at_for_partial_expiry(
    cancel_offset: timedelta,
    verification: str,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, contracts=5.0, queue_ahead=0.0)
        trade_at = T0 + timedelta(minutes=1)
        _apply(
            store,
            "partial-expiry-cutoff",
            no_price=0.72,
            quantity=2.0,
            at=trade_at,
        )
        _settle(store)
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET expires_at=?, cancelled_at=? WHERE id=?",
                (
                    (T0 + timedelta(seconds=30)).isoformat(),
                    (trade_at + cancel_offset).isoformat(),
                    order_id,
                ),
            )

        result = _result(db_path, order_id)
        assert result["verification"] == verification
        if verification == "UNVERIFIABLE":
            assert "EXEC_V4_ALLOCATION_REPLAY_MISMATCH" in result["findings"]


def test_restatement_stops_closed_root_at_closed_at() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, queue_ahead=0.0)
        trade_at = T0 + timedelta(minutes=1)
        _apply(store, "closed-root-cutoff", no_price=0.72, quantity=5.0, at=trade_at)
        store.close_paper_order(
            order_id,
            0.80,
            max_quantity=5.0,
            liquidity_evidence={
                "displayed_depth": 5.0,
                "source": "test_depth",
                "observed_at": trade_at.isoformat(),
            },
        )
        cutoff = trade_at - timedelta(microseconds=1)
        with store.connect() as conn:
            outcome = json.loads(
                conn.execute(
                    "SELECT outcome_diagnostics_json FROM paper_orders WHERE id=?",
                    (order_id,),
                ).fetchone()[0]
            )
            outcome["exit_execution"]["observed_at"] = cutoff.isoformat()
            conn.execute(
                "UPDATE paper_orders SET closed_at=?, outcome_diagnostics_json=? "
                "WHERE id=?",
                (cutoff.isoformat(), json.dumps(outcome, sort_keys=True), order_id),
            )

        _assert_unverified_and_readiness_ineligible(
            db_path, order_id, "EXEC_V4_ALLOCATION_REPLAY_MISMATCH"
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
            "order",
            "cancelled_at",
            "",
            "EXEC_V4_ORDER_CANCELLED_AT_INVALID",
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


def _resolved_order(store: PaperStore, lifecycle: str) -> int:
    order_id = _order(store, queue_ahead=0.0)
    _apply(
        store,
        f"malformed-{lifecycle}-lifecycle",
        no_price=0.72,
        quantity=5.0,
    )
    if lifecycle == "settled":
        _settle(store)
    else:
        store.close_paper_order(
            order_id,
            0.80,
            max_quantity=5.0,
            liquidity_evidence={
                "displayed_depth": 5.0,
                "source": "test_depth",
                "observed_at": datetime.now(UTC).isoformat(),
            },
        )
    return order_id


@pytest.mark.parametrize(
    ("lifecycle", "target", "field", "value", "reason"),
    [
        ("settled", "order", "realized_pnl", "bad", "REALIZED_PNL_INVALID"),
        ("settled", "order", "realized_pnl", None, "REALIZED_PNL_INVALID"),
        ("settled", "order", "realized_pnl", "NaN", "REALIZED_PNL_INVALID"),
        (
            "settled",
            "order",
            "realized_pnl",
            float("inf"),
            "REALIZED_PNL_INVALID",
        ),
        ("closed", "order", "realized_pnl", "bad", "REALIZED_PNL_INVALID"),
        ("settled", "order", "settled_at", "", "SETTLED_AT_INVALID"),
        ("settled", "order", "settled_at", None, "SETTLED_AT_INVALID"),
        (
            "settled",
            "order",
            "settlement_high_f",
            "bad",
            "SETTLEMENT_HIGH_INVALID",
        ),
        (
            "settled",
            "order",
            "settlement_high_f",
            float("nan"),
            "SETTLEMENT_HIGH_INVALID",
        ),
        (
            "settled",
            "order",
            "settlement_high_f",
            float("inf"),
            "SETTLEMENT_HIGH_INVALID",
        ),
        (
            "settled",
            "order",
            "settlement_high_f",
            999.0,
            "SETTLEMENT_HIGH_INVALID",
        ),
        (
            "settled",
            "order",
            "settlement_high_f",
            None,
            "SETTLEMENT_HIGH_INVALID",
        ),
        ("settled", "order", "resolved_yes", "bad", "RESOLVED_YES_INVALID"),
        ("settled", "order", "resolved_yes", 2, "RESOLVED_YES_INVALID"),
        ("settled", "order", "parent_order_id", "bad", "PARENT_ORDER_ID_INVALID"),
        ("settled", "order", "contracts", "bad", "ORDER_CONTRACTS_INVALID"),
        (
            "settled",
            "order",
            "contracts",
            "NaN",
            "ORDER_CONTRACTS_INVALID",
        ),
        (
            "settled",
            "order",
            "contracts",
            float("inf"),
            "ORDER_CONTRACTS_INVALID",
        ),
        ("closed", "order", "exit_price", "bad", "EXIT_PRICE_INVALID"),
        ("closed", "order", "exit_price", None, "EXIT_PRICE_INVALID"),
        ("closed", "order", "exit_price", "NaN", "EXIT_PRICE_INVALID"),
        (
            "closed",
            "order",
            "exit_price",
            float("inf"),
            "EXIT_PRICE_INVALID",
        ),
        ("closed", "order", "exit_price", -0.1, "EXIT_PRICE_INVALID"),
        (
            "closed",
            "order",
            "exit_fee_per_contract",
            "bad",
            "EXIT_FEE_INVALID",
        ),
        (
            "closed",
            "order",
            "exit_fee_per_contract",
            None,
            "EXIT_FEE_INVALID",
        ),
        (
            "closed",
            "order",
            "exit_fee_per_contract",
            float("inf"),
            "EXIT_FEE_INVALID",
        ),
        (
            "closed",
            "order",
            "exit_fee_per_contract",
            float("nan"),
            "EXIT_FEE_INVALID",
        ),
        (
            "closed",
            "order",
            "exit_fee_per_contract",
            -0.01,
            "EXIT_FEE_INVALID",
        ),
        ("closed", "order", "closed_at", "", "CLOSED_AT_INVALID"),
        ("closed", "order", "closed_at", None, "CLOSED_AT_INVALID"),
        (
            "closed",
            "outcome",
            "executed_quantity",
            "bad",
            "EXIT_EXECUTED_QUANTITY_INVALID",
        ),
        (
            "closed",
            "outcome",
            "executed_quantity",
            None,
            "EXIT_EXECUTED_QUANTITY_INVALID",
        ),
        (
            "closed",
            "outcome",
            "executed_quantity",
            "NaN",
            "EXIT_EXECUTED_QUANTITY_INVALID",
        ),
        (
            "closed",
            "outcome",
            "executed_quantity",
            float("inf"),
            "EXIT_EXECUTED_QUANTITY_INVALID",
        ),
        (
            "closed",
            "outcome",
            "executed_quantity",
            -1.0,
            "EXIT_EXECUTED_QUANTITY_INVALID",
        ),
        (
            "closed",
            "outcome",
            "displayed_depth",
            "bad",
            "EXIT_DISPLAYED_DEPTH_INVALID",
        ),
        (
            "closed",
            "outcome",
            "displayed_depth",
            None,
            "EXIT_DISPLAYED_DEPTH_INVALID",
        ),
        (
            "closed",
            "outcome",
            "displayed_depth",
            "NaN",
            "EXIT_DISPLAYED_DEPTH_INVALID",
        ),
        (
            "closed",
            "outcome",
            "displayed_depth",
            float("inf"),
            "EXIT_DISPLAYED_DEPTH_INVALID",
        ),
        (
            "closed",
            "outcome",
            "displayed_depth",
            -1.0,
            "EXIT_DISPLAYED_DEPTH_INVALID",
        ),
        (
            "closed",
            "outcome",
            "observed_at",
            "",
            "EXIT_OBSERVED_AT_INVALID",
        ),
    ],
)
def test_restatement_never_raises_on_malformed_lifecycle_fields(
    lifecycle: str,
    target: str,
    field: str,
    value: object,
    reason: str,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _resolved_order(store, lifecycle)
        if target == "order":
            connection = (
                sqlite3.connect(db_path)
                if field == "parent_order_id"
                else store.connect()
            )
            with connection as conn:
                conn.execute(
                    f"UPDATE paper_orders SET {field}=? WHERE id=?",
                    (value, order_id),
                )
        else:
            with store.connect() as conn:
                outcome = json.loads(
                    conn.execute(
                        "SELECT outcome_diagnostics_json FROM paper_orders WHERE id=?",
                        (order_id,),
                    ).fetchone()[0]
                )
                outcome["exit_execution"][field] = value
                conn.execute(
                    "UPDATE paper_orders SET outcome_diagnostics_json=? WHERE id=?",
                    (json.dumps(outcome), order_id),
                )

        _assert_unverified_and_readiness_ineligible(db_path, order_id, reason)
        if field == "realized_pnl":
            report = restate(db_path)
            assert _result(db_path, order_id)["realized_pnl"] is None
            json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("verification_ticker", [TICKER, None], ids=["ticker", "global"])
def test_restatement_propagates_malformed_settlement_verification_identity(
    verification_ticker: str | None,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        first_id = _order(store, queue_ahead=0.0)
        second_id = _order(
            store,
            queue_ahead=0.0,
            placed_at=T0 + timedelta(seconds=1),
            risk_profile="research",
        )
        _apply(store, "malformed-settlement-identity", no_price=0.72, quantity=10.0)
        _settle(store)
        with store.connect() as conn:
            conn.execute("DROP TABLE paper_settlement_verifications")
            conn.execute(
                "CREATE TABLE paper_settlement_verifications ("
                "order_id TEXT PRIMARY KEY, checked_at TEXT, market_ticker TEXT, "
                "target_date TEXT, booked_high_f REAL, final_high_f REAL, "
                "verification_status TEXT)"
            )
            conn.execute(
                "INSERT INTO paper_settlement_verifications VALUES ("
                "'bad', ?, ?, ?, 85, 85, 'MATCH')",
                (T0.isoformat(), verification_ticker, TARGET_DATE),
            )

        _assert_unverified_and_readiness_ineligible(
            db_path, first_id, "SETTLEMENT_VERIFICATION_ORDER_ID_INVALID"
        )
        research_result = _result(db_path, second_id)
        assert research_result["verification"] == "UNVERIFIABLE"
        assert (
            "SETTLEMENT_VERIFICATION_ORDER_ID_INVALID"
            in research_result["findings"]
        )


def test_restatement_rejects_malformed_exit_execution_container() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _resolved_order(store, "closed")
        with store.connect() as conn:
            outcome = json.loads(
                conn.execute(
                    "SELECT outcome_diagnostics_json FROM paper_orders WHERE id=?",
                    (order_id,),
                ).fetchone()[0]
            )
            outcome["exit_execution"] = "bad"
            conn.execute(
                "UPDATE paper_orders SET outcome_diagnostics_json=? WHERE id=?",
                (json.dumps(outcome), order_id),
            )

        _assert_unverified_and_readiness_ineligible(
            db_path, order_id, "EXIT_EXECUTION_INVALID"
        )


def _two_ticker_candidates(store: PaperStore) -> tuple[int, int]:
    first_id = _order(store, ticker=TICKER, queue_ahead=0.0)
    second_id = _order(
        store,
        ticker=TICKER_2,
        queue_ahead=0.0,
        placed_at=T0 + timedelta(seconds=1),
    )
    _apply(store, "identity-first", no_price=0.72, quantity=5.0)
    _apply(
        store,
        "identity-second",
        no_price=0.72,
        quantity=5.0,
        ticker=TICKER_2,
    )
    _settle(store)
    return first_id, second_id


def _relax_table_affinity(conn: sqlite3.Connection, table: str) -> None:
    columns = [
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    ]
    source = f"{table}_identity_source"
    column_sql = ", ".join(f'"{column}"' for column in columns)
    conn.execute(f"ALTER TABLE {table} RENAME TO {source}")
    conn.execute(f"CREATE TABLE {table} ({column_sql})")
    conn.execute(
        f"INSERT INTO {table} ({column_sql}) "
        f"SELECT {column_sql} FROM {source}"
    )
    conn.execute(f"DROP TABLE {source}")


def _corrupt_shared_evidence_ticker(
    store: PaperStore,
    source: str,
    value: object,
    order_id: int,
) -> None:
    table, column = {
        "allocation": ("paper_maker_allocations", "market_ticker"),
        "claim": ("maker_volume_claims", "market_ticker"),
        "tape": ("dataset_kalshi_trades", "ticker"),
    }[source]
    with store.connect() as conn:
        _relax_table_affinity(conn, table)
        if source == "claim":
            conn.execute(
                "INSERT INTO maker_volume_claims "
                "(created_at, market_ticker, trade_id, order_id, quantity) "
                "VALUES (?, ?, 'identity-claim', ?, 1)",
                (T0.isoformat(), value, order_id),
            )
        else:
            trade_id = "identity-first"
            conn.execute(
                f"UPDATE {table} SET {column}=? WHERE trade_id=?",
                (value, trade_id),
            )


@pytest.mark.parametrize(
    ("ticker_value", "reason_suffix"),
    [
        pytest.param("", "INVALID", id="empty"),
        pytest.param("   ", "INVALID", id="whitespace"),
        pytest.param(None, "INVALID", id="null"),
        pytest.param(17, "INVALID", id="numeric"),
        pytest.param(sqlite3.Binary(b"bad"), "INVALID", id="blob"),
        pytest.param(
            "KXUNKNOWN-26JUL17-B82.5",
            "UNRESOLVABLE",
            id="unknown",
        ),
    ],
)
@pytest.mark.parametrize(
    ("source", "reason_prefix"),
    [
        ("claim", "EXEC_V4_PRIOR_CLAIM_TICKER"),
        ("allocation", "EXEC_V4_ALLOCATION_TICKER"),
        ("tape", "EXEC_V4_TAPE_TICKER"),
    ],
)
def test_restatement_propagates_invalid_shared_evidence_ticker_globally(
    source: str,
    reason_prefix: str,
    ticker_value: object,
    reason_suffix: str,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        first_id, second_id = _two_ticker_candidates(store)
        _corrupt_shared_evidence_ticker(store, source, ticker_value, first_id)
        expected = f"{reason_prefix}_{reason_suffix}"

        for order_id in (first_id, second_id):
            result = _result(db_path, order_id)
            assert result["verification"] == "UNVERIFIABLE"
            assert expected in result["findings"]

        readiness = replay_from_database(db_path, TRUTH)
        assert readiness["verified_decisions"] == 0
        assert any(
            "unverified execution evidence" in reason
            for reason in readiness["promotion_block_reasons"]
        )


@pytest.mark.parametrize(
    ("source", "field", "value", "reason"),
    [
        (
            "allocation",
            "order_id",
            999_999,
            "EXEC_V4_ALLOCATION_ORDER_ID_UNRESOLVABLE",
        ),
        (
            "allocation",
            "trade_id",
            "missing-allocation-trade",
            "EXEC_V4_ALLOCATION_TRADE_ID_UNRESOLVABLE",
        ),
        (
            "claim",
            "order_id",
            999_999,
            "EXEC_V4_PRIOR_CLAIM_ORDER_ID_UNRESOLVABLE",
        ),
        (
            "claim",
            "trade_id",
            "missing-claim-trade",
            "EXEC_V4_PRIOR_CLAIM_TRADE_ID_UNRESOLVABLE",
        ),
    ],
)
def test_restatement_keeps_unresolvable_shared_references_ticker_scoped(
    source: str,
    field: str,
    value: object,
    reason: str,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        first_id, second_id = _two_ticker_candidates(store)
        connection = (
            sqlite3.connect(db_path)
            if source == "allocation" and field == "order_id"
            else store.connect()
        )
        with connection as conn:
            if source == "allocation":
                conn.execute(
                    f"UPDATE paper_maker_allocations SET {field}=? "
                    "WHERE trade_id='identity-first'",
                    (value,),
                )
            else:
                order_id = value if field == "order_id" else first_id
                trade_id = value if field == "trade_id" else "identity-first"
                conn.execute(
                    "INSERT INTO maker_volume_claims "
                    "(created_at, market_ticker, trade_id, order_id, quantity) "
                    "VALUES (?, ?, ?, ?, 1)",
                    (T0.isoformat(), TICKER, trade_id, order_id),
                )

        first = _result(db_path, first_id)
        assert first["verification"] == "UNVERIFIABLE"
        assert reason in first["findings"]
        assert _result(db_path, second_id)["verification"] == "VERIFIED"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "market_ticker",
            TICKER_2,
            "SETTLEMENT_VERIFICATION_TICKER_MISMATCH",
        ),
        (
            "target_date",
            "2026-07-18",
            "SETTLEMENT_VERIFICATION_TARGET_DATE_MISMATCH",
        ),
    ],
)
def test_restatement_rejects_inconsistent_settlement_attribution(
    field: str,
    value: object,
    reason: str,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        first_id, second_id = _two_ticker_candidates(store)
        verification = {
            "market_ticker": TICKER,
            "target_date": TARGET_DATE,
        }
        verification[field] = value
        with store.connect() as conn:
            conn.execute(
                f"UPDATE paper_settlement_verifications SET {field}=? "
                "WHERE order_id=?",
                (
                    value,
                    first_id,
                ),
            )

        first = _result(db_path, first_id)
        assert first["verification"] == "UNVERIFIABLE"
        assert reason in first["findings"]
        assert _result(db_path, second_id)["verification"] == "VERIFIED"


@pytest.mark.parametrize("source", ["claim", "settlement"])
def test_restatement_does_not_poison_candidates_for_resolvable_rejected_order(
    source: str,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        candidate_id = _order(store, queue_ahead=0.0)
        _apply(store, "identity-rejected-owner", no_price=0.72, quantity=5.0)
        _settle(store)
        rejected_id = _order(
            store,
            queue_ahead=0.0,
            placed_at=T0 + timedelta(seconds=1),
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET status='REJECTED' WHERE id=?",
                (rejected_id,),
            )
            if source == "claim":
                conn.execute(
                    "INSERT INTO maker_volume_claims "
                    "(created_at, market_ticker, trade_id, order_id, quantity) "
                    "VALUES (?, ?, 'identity-rejected-owner', ?, 0)",
                    (T0.isoformat(), TICKER, rejected_id),
                )
            else:
                conn.execute(
                    "INSERT INTO paper_settlement_verifications "
                    "(order_id, checked_at, market_ticker, target_date, "
                    "booked_high_f, final_high_f, verification_status) "
                    "VALUES (?, ?, ?, ?, 85, 85, 'MATCH')",
                    (rejected_id, T0.isoformat(), TICKER, TARGET_DATE),
                )

        result = _result(db_path, candidate_id)
        assert result["verification"] == "VERIFIED"


def _partial_lot_group(store: PaperStore) -> tuple[int, tuple[int, int]]:
    root_id = _order(store, contracts=4.0, queue_ahead=0.0)
    _apply(store, "logical-partials-a", no_price=0.72, quantity=2.0)
    _apply(
        store,
        "logical-partials-b",
        no_price=0.72,
        quantity=2.0,
        at=T0 + timedelta(minutes=2),
    )
    child_ids: list[int] = []
    for quantity in (1.0, 1.0):
        child = store.close_paper_order(
            root_id,
            0.80,
            max_quantity=quantity,
            liquidity_evidence={
                "displayed_depth": quantity,
                "source": "test_depth",
                "observed_at": datetime.now(UTC).isoformat(),
            },
        )
        child_ids.append(int(child["id"]))
    _settle(store)
    return root_id, (child_ids[0], child_ids[1])


def test_valid_multi_child_decision_is_replayed_as_one_verified_decision() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        root_id, child_ids = _partial_lot_group(store)

        assert all(
            _result(db_path, order_id)["verification"] == "VERIFIED"
            for order_id in (root_id, *child_ids)
        )
        replay = replay_from_database(db_path, TRUTH)
        assert replay["source_orders"] == 3
        assert replay["verified_decisions"] == 1
        assert replay["readiness_metrics"]["counts"]["settled_decisions"] == 1


def test_invalid_child_excludes_entire_logical_decision_from_replay() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        root_id, child_ids = _partial_lot_group(store)
        with store.connect() as conn:
            outcome = json.loads(
                conn.execute(
                    "SELECT outcome_diagnostics_json FROM paper_orders WHERE id=?",
                    (child_ids[0],),
                ).fetchone()[0]
            )
            outcome["exit_execution"]["executed_quantity"] = "bad"
            conn.execute(
                "UPDATE paper_orders SET outcome_diagnostics_json=? WHERE id=?",
                (json.dumps(outcome, sort_keys=True), child_ids[0]),
            )

        assert _result(db_path, root_id)["verification"] == "UNVERIFIABLE"
        assert _result(db_path, child_ids[0])["verification"] == "UNVERIFIABLE"
        replay = replay_from_database(db_path, TRUTH)
        assert replay["source_orders"] == 0
        assert replay["verified_decisions"] == 0
        assert replay["readiness_metrics"]["counts"]["settled_decisions"] == 0
        assert replay["readiness_metrics"]["candidate"]["realized_pnl"] == 0.0


def _coordinate_settled_accounting(
    store: PaperStore,
    order_id: int,
    *,
    contracts: float | None = None,
    cost_per_contract: float | None = None,
) -> None:
    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM paper_orders WHERE id=?", (order_id,)
        ).fetchone()
        assert row is not None
        quantity = contracts if contracts is not None else float(row["contracts"])
        cost = (
            cost_per_contract
            if cost_per_contract is not None
            else float(row["cost_per_contract"])
        )
        resolved_yes = bool(row["resolved_yes"])
        position_won = (
            resolved_yes if str(row["side"]) == "YES" else not resolved_yes
        )
        pnl = settled_position_pnl(quantity, cost, position_won)
        outcome = json.loads(row["outcome_diagnostics_json"])
        outcome["entry"]["contracts"] = quantity
        outcome["entry"]["cost_per_contract"] = cost
        outcome["outcome"]["realized_pnl"] = pnl
        outcome["outcome"]["pnl_per_contract"] = pnl / quantity
        conn.execute(
            "UPDATE paper_orders SET contracts=?, cost_per_contract=?, "
            "realized_pnl=?, outcome_diagnostics_json=? WHERE id=?",
            (
                quantity,
                cost,
                pnl,
                json.dumps(outcome, sort_keys=True),
                order_id,
            ),
        )


def test_coordinated_entry_cost_tamper_is_excluded_from_readiness() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, queue_ahead=0.0)
        _apply(store, "cost-authority", no_price=0.72, quantity=5.0)
        _settle(store)
        _coordinate_settled_accounting(
            store, order_id, cost_per_contract=0.01
        )

        _assert_unverified_and_readiness_ineligible(
            db_path, order_id, "EXEC_V4_ENTRY_COST_MISMATCH"
        )
        replay = replay_from_database(db_path, TRUTH)
        assert replay["source_orders"] == 0
        assert replay["readiness_metrics"]["candidate"]["capital_at_risk"] == 0.0
        assert replay["readiness_metrics"]["candidate"]["realized_pnl"] == 0.0


def test_coordinated_terminal_contract_tamper_is_excluded_from_readiness() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, queue_ahead=0.0)
        _apply(store, "quantity-authority", no_price=0.72, quantity=5.0)
        _settle(store)
        _coordinate_settled_accounting(store, order_id, contracts=100.0)

        _assert_unverified_and_readiness_ineligible(
            db_path,
            order_id,
            "EXEC_V4_LOGICAL_RESOLVED_QUANTITY_MISMATCH",
        )
        replay = replay_from_database(db_path, TRUTH)
        assert replay["source_orders"] == 0
        assert replay["readiness_metrics"]["candidate"]["capital_at_risk"] == 0.0
        assert replay["readiness_metrics"]["candidate"]["realized_pnl"] == 0.0


def test_closed_child_contracts_must_equal_exit_executed_quantity() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        root_id, child_ids = _partial_lot_group(store)
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET contracts=2 WHERE id=?", (child_ids[0],)
            )

        for order_id in (root_id, *child_ids):
            result = _result(db_path, order_id)
            assert result["verification"] == "UNVERIFIABLE"
            assert "EXEC_V4_EXIT_QUANTITY_MISMATCH" in result["findings"]
        assert replay_from_database(db_path, TRUTH)["source_orders"] == 0


def test_partial_open_multi_fill_quantity_balance_remains_verified() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, contracts=5.0, queue_ahead=0.0)
        _apply(store, "partial-open-a", no_price=0.72, quantity=1.0)
        _apply(
            store,
            "partial-open-b",
            no_price=0.72,
            quantity=1.0,
            at=T0 + timedelta(minutes=2),
        )

        assert _result(db_path, order_id)["verification"] == "VERIFIED"
        replay = replay_from_database(db_path, TRUTH)
        assert replay["source_orders"] == 1
        assert replay["verified_decisions"] == 0


def test_closed_partial_expiry_uses_earlier_cancelled_at_boundary() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, contracts=5.0, queue_ahead=0.0)
        first_trade_at = T0 + timedelta(minutes=1)
        cancelled_at = T0 + timedelta(minutes=20)
        late_trade_at = T0 + timedelta(minutes=21)
        closed_at = T0 + timedelta(minutes=22)
        _apply(
            store,
            "before-cancel",
            no_price=0.72,
            quantity=2.0,
            at=first_trade_at,
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET status='PAPER_PARTIAL_EXPIRED', "
                "cancelled_at=?, remaining_contracts=0, queue_remaining=0, "
                "reserved_cost=0 WHERE id=?",
                (cancelled_at.isoformat(), order_id),
            )
        _apply(
            store,
            "after-cancel",
            no_price=0.72,
            quantity=3.0,
            at=late_trade_at,
        )
        store.close_paper_order(
            order_id,
            0.80,
            max_quantity=2.0,
            liquidity_evidence={
                "displayed_depth": 2.0,
                "source": "test_depth",
                "observed_at": datetime.now(UTC).isoformat(),
            },
        )
        with store.connect() as conn:
            outcome = json.loads(
                conn.execute(
                    "SELECT outcome_diagnostics_json FROM paper_orders WHERE id=?",
                    (order_id,),
                ).fetchone()[0]
            )
            outcome["outcome"]["resolved_at"] = closed_at.isoformat()
            outcome["exit_execution"]["observed_at"] = closed_at.isoformat()
            conn.execute(
                "UPDATE paper_orders SET closed_at=?, outcome_diagnostics_json=? "
                "WHERE id=?",
                (closed_at.isoformat(), json.dumps(outcome, sort_keys=True), order_id),
            )

        result = _result(db_path, order_id)
        assert result["verification"] == "VERIFIED"
        assert result["findings"] == []


def test_partial_expiry_requires_expiry_after_order_placement() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, contracts=5.0, queue_ahead=0.0)
        _apply(store, "invalid-expiry-context", no_price=0.72, quantity=2.0)
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET status='PAPER_PARTIAL_EXPIRED', "
                "expires_at=?, cancelled_at=?, remaining_contracts=0 WHERE id=?",
                (
                    (T0 - timedelta(minutes=1)).isoformat(),
                    (T0 + timedelta(minutes=2)).isoformat(),
                    order_id,
                ),
            )

        result = _result(db_path, order_id)
        assert result["verification"] == "UNVERIFIABLE"
        assert "EXEC_V4_ORDER_EXPIRES_AT_NOT_LATER" in result["findings"]
        assert replay_from_database(db_path, TRUTH)["source_orders"] == 0


def test_v4_semantics_cannot_escape_logical_gate_by_model_tamper() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        root_id, child_ids = _partial_lot_group(store)
        with store.connect() as conn:
            evidence = json.loads(
                conn.execute(
                    "SELECT fill_evidence_json FROM paper_orders WHERE id=?",
                    (root_id,),
                ).fetchone()[0]
            )
            evidence["model"] = "maker_allocator_price_time_v3"
            conn.execute(
                "UPDATE paper_orders SET fill_evidence_json=? WHERE id=?",
                (json.dumps(evidence, sort_keys=True), root_id),
            )

        for order_id in (root_id, *child_ids):
            assert _result(db_path, order_id)["verification"] == "UNVERIFIABLE"
        replay = replay_from_database(db_path, TRUTH)
        assert replay["source_orders"] == 0
        assert replay["verified_decisions"] == 0


def test_genuine_exec_v3_row_remains_historical_and_outside_v4_gate() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, queue_ahead=0.0)
        _apply(store, "genuine-v3", no_price=0.72, quantity=5.0)
        _settle(store)
        with store.connect() as conn:
            evidence = json.loads(
                conn.execute(
                    "SELECT fill_evidence_json FROM paper_orders WHERE id=?",
                    (order_id,),
                ).fetchone()[0]
            )
            evidence["model"] = "maker_allocator_price_time_v3"
            evidence["execution_model_version"] = "exec-v3-2026-07-14"
            conn.execute(
                "UPDATE paper_orders SET execution_model_version=?, "
                "fill_evidence_json=? WHERE id=?",
                (
                    "exec-v3-2026-07-14",
                    json.dumps(evidence, sort_keys=True),
                    order_id,
                ),
            )
            conn.execute(
                "UPDATE paper_maker_allocations SET execution_model_version=? "
                "WHERE order_id=?",
                ("exec-v3-2026-07-14", order_id),
            )

        result = _result(db_path, order_id)
        assert result["verification"] == "UNVERIFIABLE"
        assert "EXEC_V3_HISTORICAL_SEMANTICS" in result["findings"]
        assert replay_from_database(db_path, TRUTH)["source_orders"] == 0


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("fill-model", "EXEC_V4_FILL_MODEL_MISMATCH"),
        ("evidence-model", "EXEC_V4_EVIDENCE_MODEL_MISMATCH"),
        ("both-models", "EXEC_V4_FILL_MODEL_MISMATCH"),
        ("missing-allocation", "EXEC_V4_ALLOCATION_MISSING"),
    ],
)
def test_persisted_v4_allocation_prevents_maker_identity_opt_out(
    mutation: str,
    reason: str,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, queue_ahead=0.0)
        _apply(store, f"identity-{mutation}", no_price=0.72, quantity=5.0)
        _settle(store)
        with store.connect() as conn:
            if mutation in {"evidence-model", "both-models"}:
                evidence = json.loads(
                    conn.execute(
                        "SELECT fill_evidence_json FROM paper_orders WHERE id=?",
                        (order_id,),
                    ).fetchone()[0]
                )
                evidence["model"] = "maker_allocator_price_time_v3"
                conn.execute(
                    "UPDATE paper_orders SET fill_evidence_json=? WHERE id=?",
                    (json.dumps(evidence, sort_keys=True), order_id),
                )
            if mutation in {"fill-model", "both-models"}:
                conn.execute(
                    "UPDATE paper_orders SET fill_model='immediate_visible_quote' "
                    "WHERE id=?",
                    (order_id,),
                )
            if mutation == "missing-allocation":
                conn.execute(
                    "DELETE FROM paper_maker_allocations WHERE order_id=?",
                    (order_id,),
                )

        result = _result(db_path, order_id)
        assert result["verification"] == "UNVERIFIABLE"
        assert reason in result["findings"]
        replay = replay_from_database(db_path, TRUTH)
        assert replay["source_orders"] == 0
        assert replay["verified_decisions"] == 0
        assert replay["readiness_metrics"]["candidate"]["capital_at_risk"] == 0.0
        assert replay["readiness_metrics"]["candidate"]["realized_pnl"] == 0.0


def test_malformed_v4_allocation_identity_survives_triple_label_tamper() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _order(store, queue_ahead=0.0)
        _apply(store, "triple-identity", no_price=0.72, quantity=5.0)
        _settle(store)
        with store.connect() as conn:
            evidence = json.loads(
                conn.execute(
                    "SELECT fill_evidence_json FROM paper_orders WHERE id=?",
                    (order_id,),
                ).fetchone()[0]
            )
            evidence["model"] = "maker_allocator_price_time_v3"
            conn.execute(
                "UPDATE paper_orders SET fill_model='immediate_visible_quote', "
                "fill_evidence_json=? WHERE id=?",
                (json.dumps(evidence, sort_keys=True), order_id),
            )
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE paper_maker_allocations SET order_id='bad' WHERE order_id=?",
                (order_id,),
            )

        _assert_unverified_and_readiness_ineligible(
            db_path, order_id, "EXEC_V4_ALLOCATION_ORDER_ID_INVALID"
        )
        replay = replay_from_database(db_path, TRUTH)
        assert replay["source_orders"] == 0
        assert replay["readiness_metrics"]["candidate"]["realized_pnl"] == 0.0


def test_valid_rejected_v4_allocation_is_provably_unrelated() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        candidate_id = _order(store, queue_ahead=0.0)
        _apply(store, "candidate-owner", no_price=0.72, quantity=5.0)
        _settle(store)
        rejected_id = _order(
            store,
            queue_ahead=0.0,
            placed_at=T0 + timedelta(seconds=1),
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET status='REJECTED' WHERE id=?",
                (rejected_id,),
            )
        _apply(
            store,
            "rejected-owner",
            no_price=0.72,
            quantity=1.0,
            at=T0 + timedelta(minutes=3),
        )
        with store.connect() as conn:
            conn.execute(
                "INSERT INTO paper_maker_allocations ("
                "created_at, execution_model_version, market_ticker, trade_id, "
                "order_id, trade_created_at, maker_side, side_price, "
                "queue_quantity, fill_quantity, counterfactual, evidence_json"
                ") VALUES (?, ?, ?, 'rejected-owner', ?, ?, 'NO', 0.72, "
                "0, 0, 0, ?)",
                (
                    datetime.now(UTC).isoformat(),
                    EXECUTION_MODEL_VERSION,
                    TICKER,
                    rejected_id,
                    (T0 + timedelta(minutes=3)).isoformat(),
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

        assert _result(db_path, candidate_id)["verification"] == "VERIFIED"


def test_valid_genuine_v3_allocation_does_not_poison_v4_candidate() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        candidate_id = _order(store, queue_ahead=0.0)
        historical_id = _order(
            store,
            queue_ahead=0.0,
            placed_at=T0 + timedelta(seconds=1),
            risk_profile="research",
        )
        _apply(store, "mixed-generation", no_price=0.72, quantity=5.0)
        _settle(store)
        with store.connect() as conn:
            evidence = json.loads(
                conn.execute(
                    "SELECT fill_evidence_json FROM paper_orders WHERE id=?",
                    (historical_id,),
                ).fetchone()[0]
            )
            evidence["model"] = "maker_allocator_price_time_v3"
            evidence["execution_model_version"] = "exec-v3-2026-07-14"
            conn.execute(
                "UPDATE paper_orders SET execution_model_version=?, "
                "fill_evidence_json=? WHERE id=?",
                (
                    "exec-v3-2026-07-14",
                    json.dumps(evidence, sort_keys=True),
                    historical_id,
                ),
            )
            conn.execute(
                "UPDATE paper_maker_allocations SET execution_model_version=? "
                "WHERE order_id=?",
                ("exec-v3-2026-07-14", historical_id),
            )

        assert _result(db_path, candidate_id)["verification"] == "VERIFIED"
        historical = _result(db_path, historical_id)
        assert historical["verification"] == "UNVERIFIABLE"
        assert "EXEC_V3_HISTORICAL_SEMANTICS" in historical["findings"]


def _immediate_settled_order(
    store: PaperStore,
    *,
    entry_mode: str,
) -> int:
    decision = _decision(
        TICKER,
        side="NO",
        limit_price=0.72,
        contracts=5.0,
    )
    decision = replace(
        decision,
        entry_ask=0.72 if entry_mode == "limit" else 0.74,
        entry_ask_size=100.0,
        limit_price=0.72 if entry_mode == "limit" else None,
    )
    order_id = store.record_paper_order(
        TARGET_DATE,
        decision,
        status="PAPER_FILLED",
        entry_mode=entry_mode,
    )
    assert order_id is not None
    _settle(store)
    return order_id


def _assert_current_execution_excluded(
    db_path: Path,
    order_id: int,
    reason: str,
) -> None:
    result = _result(db_path, order_id)
    assert result["verification"] == "UNVERIFIABLE"
    assert reason in result["findings"]
    replay = replay_from_database(db_path, TRUTH)
    assert replay["source_orders"] == 0
    assert replay["verified_decisions"] == 0
    assert replay["readiness_metrics"]["candidate"]["realized_pnl"] == 0.0


@pytest.mark.parametrize("entry_mode", ["market", "limit"])
def test_valid_current_immediate_entry_is_verified_and_replayed(
    entry_mode: str,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _immediate_settled_order(store, entry_mode=entry_mode)

        result = _result(db_path, order_id)
        assert result["verification"] == "VERIFIED"
        assert result["findings"] == []
        replay = replay_from_database(db_path, TRUTH)
        assert replay["source_orders"] == 1
        assert replay["verified_decisions"] == 1


def test_current_immediate_missing_settlement_cannot_count_tampered_pnl() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _immediate_settled_order(store, entry_mode="market")
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM paper_settlement_verifications WHERE order_id=?",
                (order_id,),
            )
            conn.execute(
                "UPDATE paper_orders SET realized_pnl=999 WHERE id=?",
                (order_id,),
            )

        _assert_current_execution_excluded(
            db_path,
            order_id,
            "SETTLEMENT_VERIFICATION_REQUIRED",
        )


@pytest.mark.parametrize(
    ("column", "value", "reason"),
    [
        ("entry_price", 0.11, "CURRENT_ENTRY_PRICE_MISMATCH"),
        ("fee_per_contract", 0.33, "CURRENT_ENTRY_FEE_MISMATCH"),
        ("cost_per_contract", 0.44, "CURRENT_ENTRY_COST_MISMATCH"),
    ],
)
def test_current_immediate_rejects_entry_cost_tamper(
    column: str,
    value: float,
    reason: str,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _immediate_settled_order(store, entry_mode="market")
        with store.connect() as conn:
            conn.execute(
                f"UPDATE paper_orders SET {column}=? WHERE id=?",
                (value, order_id),
            )

        _assert_current_execution_excluded(db_path, order_id, reason)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("side", "CURRENT_ENTRY_SIDE_MISMATCH"),
        ("ask", "CURRENT_ENTRY_PRICE_MISMATCH"),
        ("depth", "CURRENT_ENTRY_DEPTH_INSUFFICIENT"),
        ("quantity", "CURRENT_ENTRY_QUANTITY_MISMATCH"),
    ],
)
def test_current_immediate_rejects_quote_tamper(
    mutation: str,
    reason: str,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _immediate_settled_order(store, entry_mode="market")
        with store.connect() as conn:
            if mutation == "depth":
                conn.execute(
                    "UPDATE paper_orders SET entry_ask_size=1 WHERE id=?",
                    (order_id,),
                )
            else:
                quote = json.loads(
                    conn.execute(
                        "SELECT quote_snapshot_json FROM paper_orders WHERE id=?",
                        (order_id,),
                    ).fetchone()[0]
                )
                if mutation == "side":
                    quote["side"] = "YES"
                elif mutation == "ask":
                    quote["ask"] = 0.11
                else:
                    quote["contracts"] = 4.0
                conn.execute(
                    "UPDATE paper_orders SET quote_snapshot_json=? WHERE id=?",
                    (json.dumps(quote, sort_keys=True), order_id),
                )

        _assert_current_execution_excluded(db_path, order_id, reason)


def test_current_immediate_rejects_malformed_fill_evidence() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _immediate_settled_order(store, entry_mode="market")
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET fill_evidence_json='{' WHERE id=?",
                (order_id,),
            )

        _assert_current_execution_excluded(
            db_path,
            order_id,
            "CURRENT_ENTRY_FILL_EVIDENCE_INVALID",
        )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing", "CURRENT_ENTRY_LEDGER_MISSING"),
        ("amount", "CURRENT_ENTRY_LEDGER_MISMATCH"),
        ("account", "CURRENT_ENTRY_LEDGER_MISMATCH"),
    ],
)
def test_current_immediate_rejects_entry_fill_ledger_tamper(
    mutation: str,
    reason: str,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = _store(db_path)
        order_id = _immediate_settled_order(store, entry_mode="limit")
        with store.connect() as conn:
            if mutation == "missing":
                conn.execute(
                    "DELETE FROM paper_account_ledger "
                    "WHERE order_id=? AND event_type='ENTRY_FILL'",
                    (order_id,),
                )
            elif mutation == "amount":
                conn.execute(
                    "UPDATE paper_account_ledger SET amount=999 "
                    "WHERE order_id=? AND event_type='ENTRY_FILL'",
                    (order_id,),
                )
            else:
                conn.execute(
                    "UPDATE paper_account_ledger SET account_id='wrong-account' "
                    "WHERE order_id=? AND event_type='ENTRY_FILL'",
                    (order_id,),
                )

        _assert_current_execution_excluded(db_path, order_id, reason)
