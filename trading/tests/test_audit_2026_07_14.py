"""Third-audit execution and accounting regression tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import pytest

from sfo_kalshi_quant.cli import _fill_resting_orders_against_live_book
from sfo_kalshi_quant.colors import Color
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.maker_fills import (
    EXECUTION_MODEL_VERSION,
    PublicAggressorTrade,
    RestingMakerOrder,
    allocate_maker_fills,
)
from sfo_kalshi_quant.replay import replay_from_database
from sfo_kalshi_quant.restatement import _exit_findings, restate
from sfo_kalshi_quant.strategy_lab.build import _weekly_goal_payload
from test_audit_2026_07_13 import _TradesClient, _decision, _resting_order, _trade


T0 = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def test_exec_v3_exposes_queue_and_fill_consumption_per_trade() -> None:
    """Queue depletion is finite public volume, not free bookkeeping."""

    order = RestingMakerOrder(
        order_id=1,
        side="NO",
        limit_price=Decimal("0.72"),
        quantity=Decimal("10"),
        queue_ahead=Decimal("100"),
        placed_at=T0,
    )
    trade = PublicAggressorTrade(
        trade_id="T-110",
        created_at=T0 + timedelta(minutes=1),
        maker_side="NO",
        yes_price=Decimal("0.28"),
        quantity=Decimal("110"),
    )

    allocation = allocate_maker_fills([trade], [order])[1]

    assert allocation.consumption_by_trade() == {
        "T-110": {
            "queue_quantity": 100.0,
            "fill_quantity": 10.0,
            "total_quantity": 110.0,
        }
    }


def test_exec_v3_queue_consumption_cannot_fill_a_later_order_on_restart() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        first = _resting_order(
            store,
            "2026-07-14",
            _decision(side="NO", limit_price=0.72, contracts=10.0),
            created_at=T0,
            queue_ahead=100.0,
        )
        second = _resting_order(
            store,
            "2026-07-14",
            _decision(
                ticker="KXHIGHTSEA-26JUL14-B81.5",
                side="NO",
                limit_price=0.70,
                contracts=50.0,
                floor=81.0,
                cap=82.0,
            ),
            created_at=T0 + timedelta(seconds=1),
        )
        client = _TradesClient(
            [
                _trade(
                    "T-QUEUE-AND-FILL",
                    yes_price=0.28,
                    quantity=110.0,
                    taker_book_side="bid",
                    created_time=T0 + timedelta(minutes=1),
                )
            ]
        )

        assert _fill_resting_orders_against_live_book(
            store, client, Color.from_no_color(True)
        ) == 1
        assert _fill_resting_orders_against_live_book(
            store, client, Color.from_no_color(True)
        ) == 0

        assert store.paper_order(first)["status"] == "PAPER_FILLED"
        assert store.paper_order(second)["status"] == "PAPER_LIMIT_RESTING"
        assert store.maker_volume_claims_for_ticker(
            "KXHIGHTSEA-26JUL13-B82.5"
        )["T-QUEUE-AND-FILL"] == 110.0


def test_exec_v3_consumed_trade_cannot_fill_replacement_order() -> None:
    """A completed order leaving the resting set cannot free old tape volume."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        first = _resting_order(
            store,
            "2026-07-14",
            _decision(side="NO", limit_price=0.72, contracts=10.0),
            created_at=T0,
            queue_ahead=100.0,
        )
        trade = _trade(
            "T-CONSUMED-FOREVER",
            yes_price=0.28,
            quantity=110.0,
            taker_book_side="bid",
            created_time=T0 + timedelta(minutes=1),
        )
        client = _TradesClient([trade])

        assert _fill_resting_orders_against_live_book(
            store, client, Color.from_no_color(True)
        ) == 1
        store.close_paper_order(
            first,
            0.80,
            max_quantity=10.0,
            liquidity_evidence={
                "displayed_depth": 10.0,
                "source": "test_depth",
                "observed_at": (T0 + timedelta(minutes=2)).isoformat(),
            },
        )
        replacement = _resting_order(
            store,
            "2026-07-14",
            _decision(side="NO", limit_price=0.72, contracts=10.0),
            created_at=T0 + timedelta(seconds=1),
        )

        assert _fill_resting_orders_against_live_book(
            store, client, Color.from_no_color(True)
        ) == 0
        assert store.paper_order(replacement)["status"] == "PAPER_LIMIT_RESTING"


def test_exec_v3_duplicate_trade_in_one_batch_is_consumed_once() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        order_id = _resting_order(
            store,
            "2026-07-14",
            _decision(side="NO", limit_price=0.72, contracts=10.0),
            created_at=T0,
        )
        trade = _trade(
            "T-DUPLICATE-PAGE",
            yes_price=0.28,
            quantity=6.0,
            taker_book_side="bid",
            created_time=T0 + timedelta(minutes=1),
        )

        store.apply_maker_trade_batch(
            "KXHIGHTSEA-26JUL13-B82.5", [trade, dict(trade)]
        )

        row = store.paper_order(order_id)
        assert row["status"] == "PAPER_PARTIALLY_FILLED"
        assert row["filled_contracts"] == 6.0
        assert store.maker_volume_claims_for_ticker(
            "KXHIGHTSEA-26JUL13-B82.5"
        )["T-DUPLICATE-PAGE"] == 6.0


def test_exec_v3_partial_fill_survives_restart_and_ttl_expiry() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        order_id = _resting_order(
            store,
            "2026-07-14",
            _decision(side="NO", limit_price=0.72, contracts=10.0),
            created_at=T0,
        )
        client = _TradesClient(
            [
                _trade(
                    "T-PARTIAL",
                    yes_price=0.28,
                    quantity=5.0,
                    taker_book_side="bid",
                    created_time=T0 + timedelta(minutes=1),
                )
            ]
        )

        assert _fill_resting_orders_against_live_book(
            store, client, Color.from_no_color(True)
        ) == 0
        first = store.paper_order(order_id)
        assert first["status"] == "PAPER_PARTIALLY_FILLED"
        assert first["requested_contracts"] == 10.0
        assert first["filled_contracts"] == 5.0
        assert first["remaining_contracts"] == 5.0
        assert first["contracts"] == 5.0
        assert first["reserved_cost"] == 5.0 * first["cost_per_contract"]

        assert _fill_resting_orders_against_live_book(
            store, client, Color.from_no_color(True)
        ) == 0
        second = store.paper_order(order_id)
        assert second["filled_contracts"] == 5.0

        assert store.expire_stale_resting_orders(
            now=(T0 + timedelta(hours=1)).isoformat()
        ) == 1
        expired = store.paper_order(order_id)
        assert expired["status"] == "PAPER_PARTIAL_EXPIRED"
        assert expired["contracts"] == 5.0
        assert expired["reserved_cost"] == 0.0
        assert {row["id"] for row in store.open_paper_orders()} == {order_id}

        assert store.settle_paper_orders("2026-07-14", 85.0) == 1
        settled = store.paper_order(order_id)
        assert settled["status"] == "PAPER_SETTLED"
        assert settled["contracts"] == 5.0


def test_exec_v3_partial_fill_is_included_in_account_risk() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        order_id = _resting_order(
            store,
            "2026-07-14",
            _decision(side="NO", limit_price=0.72, contracts=10.0),
            created_at=T0,
        )
        _fill_resting_orders_against_live_book(
            store,
            _TradesClient(
                [
                    _trade(
                        "T-ACCOUNT-PARTIAL",
                        yes_price=0.28,
                        quantity=5.0,
                        taker_book_side="bid",
                        created_time=T0 + timedelta(minutes=1),
                    )
                ]
            ),
            Color.from_no_color(True),
        )

        order = store.paper_order(order_id)
        state = store.shared_account_state()
        assert state is not None
        assert state["open_cost_basis"] == order["contracts"] * order["cost_per_contract"]
        assert state["reservations"] == order["reserved_cost"]
        assert state["realized_equity"] == 1_000.0


def test_exec_v3_closing_partial_fill_cancels_unfilled_reservation() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        order_id = _resting_order(
            store,
            "2026-07-14",
            _decision(side="NO", limit_price=0.72, contracts=10.0),
            created_at=T0,
        )
        _fill_resting_orders_against_live_book(
            store,
            _TradesClient(
                [
                    _trade(
                        "T-CLOSE-PARTIAL",
                        yes_price=0.28,
                        quantity=5.0,
                        taker_book_side="bid",
                        created_time=T0 + timedelta(minutes=1),
                    )
                ]
            ),
            Color.from_no_color(True),
        )

        closed = store.close_paper_order(
            order_id,
            0.80,
            max_quantity=5.0,
            liquidity_evidence={
                "displayed_depth": 5.0,
                "source": "test_depth",
                "observed_at": (T0 + timedelta(minutes=2)).isoformat(),
            },
        )

        assert closed["status"] == "PAPER_CLOSED"
        assert closed["contracts"] == 5.0
        assert closed["remaining_contracts"] == 0.0
        assert closed["reserved_cost"] == 0.0
        assert store.shared_account_state()["reservations"] == 0.0


def test_exec_v3_queue_only_progress_is_idempotent_and_later_completes() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        order_id = _resting_order(
            store,
            "2026-07-14",
            _decision(side="NO", limit_price=0.72, contracts=5.0),
            created_at=T0,
            queue_ahead=10.0,
        )
        first_trade = _trade(
            "T-QUEUE-ONLY",
            yes_price=0.28,
            quantity=5.0,
            taker_book_side="bid",
            created_time=T0 + timedelta(minutes=1),
        )

        _fill_resting_orders_against_live_book(
            store, _TradesClient([first_trade]), Color.from_no_color(True)
        )
        first = store.paper_order(order_id)
        assert first["status"] == "PAPER_LIMIT_RESTING"
        assert first["queue_remaining"] == 5.0
        assert first["filled_contracts"] == 0.0

        _fill_resting_orders_against_live_book(
            store, _TradesClient([first_trade]), Color.from_no_color(True)
        )
        assert store.paper_order(order_id)["queue_remaining"] == 5.0

        second_trade = _trade(
            "T-FINISH",
            yes_price=0.28,
            quantity=10.0,
            taker_book_side="bid",
            created_time=T0 + timedelta(minutes=2),
        )
        assert _fill_resting_orders_against_live_book(
            store,
            _TradesClient([first_trade, second_trade]),
            Color.from_no_color(True),
        ) == 1
        filled = store.paper_order(order_id)
        assert filled["status"] == "PAPER_FILLED"
        assert filled["queue_remaining"] == 0.0
        assert filled["filled_contracts"] == 5.0
        assert filled["remaining_contracts"] == 0.0


def test_exec_v3_cutover_expires_unreplayable_legacy_resting_orders() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = _resting_order(
            store,
            "2026-07-14",
            _decision(side="NO", limit_price=0.72, contracts=5.0),
            created_at=T0,
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET execution_model_version='exec-v2-2026-07-13' "
                "WHERE id=?",
                (order_id,),
            )

        PaperStore(db_path)

        expired = store.paper_order(order_id)
        assert expired["status"] == "PAPER_EXPIRED"
        assert expired["reserved_cost"] == 0.0
        assert expired["remaining_contracts"] == 0.0
        assert store.shared_account_state()["reservations"] == 0.0


def test_exec_v3_journals_raw_trade_before_using_it_as_fill_evidence() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        _resting_order(
            store,
            "2026-07-14",
            _decision(side="NO", limit_price=0.72, contracts=2.0),
            created_at=T0,
        )
        payload = _trade(
            "T-ARCHIVED",
            yes_price=0.28,
            quantity=2.0,
            taker_book_side="bid",
            created_time=T0 + timedelta(minutes=1),
        )

        _fill_resting_orders_against_live_book(
            store, _TradesClient([payload]), Color.from_no_color(True)
        )

        with store.connect() as conn:
            row = conn.execute(
                "SELECT ticker, count, raw_json FROM dataset_kalshi_trades "
                "WHERE trade_id='T-ARCHIVED'"
            ).fetchone()
        assert row is not None
        assert row[0] == "KXHIGHTSEA-26JUL13-B82.5"
        assert row[1] == 2.0
        assert '"taker_book_side": "bid"' in row[2]


def test_exec_v3_exit_verification_requires_contemporaneous_sufficient_depth() -> None:
    closed = {
        "status": "PAPER_CLOSED",
        "closed_at": (T0 + timedelta(minutes=2)).isoformat(),
    }

    assert _exit_findings(
        closed,
        {"exit_execution": {"executed_quantity": 5.0}},
    ) == ["EXIT_DEPTH_UNVERIFIED"]
    assert _exit_findings(
        closed,
        {
            "exit_execution": {
                "executed_quantity": 5.0,
                "displayed_bid_size": 3.0,
                "source": "monitor_market_lookup",
                "observed_at": (T0 + timedelta(minutes=1)).isoformat(),
            }
        },
    ) == ["EXIT_DEPTH_INSUFFICIENT"]
    assert _exit_findings(
        closed,
        {
            "exit_execution": {
                "executed_quantity": 5.0,
                "displayed_bid_size": 5.0,
                "source": "monitor_market_lookup",
                "observed_at": (T0 - timedelta(minutes=10)).isoformat(),
                "verification_status": "VERIFIED",
            }
        },
    ) == ["EXIT_DEPTH_STALE"]
    assert _exit_findings(
        closed,
        {
            "exit_execution": {
                "executed_quantity": 5.0,
                "displayed_bid_size": 5.0,
                "source": "monitor_market_lookup",
                "observed_at": (T0 + timedelta(minutes=1)).isoformat(),
                "verification_status": "VERIFIED",
            }
        },
    ) == []


def test_exit_evidence_cannot_override_computed_execution_truth() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        order_id = store.record_paper_order(
            "2026-07-14",
            _decision(side="NO", contracts=2.0),
        )
        assert order_id is not None

        closed = store.close_paper_order(
            order_id,
            0.80,
            max_quantity=2.0,
            liquidity_evidence={
                "displayed_depth": 2.0,
                "source": "test_depth",
                "observed_at": (T0 - timedelta(hours=1)).isoformat(),
                "executed_quantity": 999.0,
                "verification_status": "VERIFIED",
            },
        )
        execution = json.loads(closed["outcome_diagnostics_json"])["exit_execution"]
        assert execution["executed_quantity"] == 2.0
        assert execution["verification_status"] == "STALE"


def test_exec_v3_restatement_requires_immutable_allocation_and_tape() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = _resting_order(
            store,
            "2026-07-14",
            _decision(side="NO", limit_price=0.72, contracts=2.0),
            created_at=T0,
        )
        payload = _trade(
            "T-RESTATE",
            yes_price=0.28,
            quantity=2.0,
            taker_book_side="bid",
            created_time=T0 + timedelta(minutes=1),
        )
        _fill_resting_orders_against_live_book(
            store, _TradesClient([payload]), Color.from_no_color(True)
        )
        store.settle_paper_orders("2026-07-14", 85.0)

        verified = next(
            row for row in restate(db_path)["orders"] if row["order_id"] == order_id
        )
        assert verified["verification"] == "VERIFIED"
        assert verified["findings"] == []

        with store.connect() as conn:
            conn.execute(
                "DELETE FROM dataset_kalshi_trades WHERE trade_id='T-RESTATE'"
            )
        missing_tape = next(
            row for row in restate(db_path)["orders"] if row["order_id"] == order_id
        )
        assert missing_tape["verification"] == "UNVERIFIABLE"
        assert "EXEC_V3_TAPE_MISSING" in missing_tape["findings"]


def test_exec_v3_partial_close_inherits_parent_entry_findings() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = _resting_order(
            store,
            "2026-07-14",
            _decision(side="NO", limit_price=0.72, contracts=4.0),
            created_at=T0,
        )
        payload = _trade(
            "T-PARTIAL-RESTATE",
            yes_price=0.28,
            quantity=4.0,
            taker_book_side="bid",
            created_time=T0 + timedelta(minutes=1),
        )
        _fill_resting_orders_against_live_book(
            store, _TradesClient([payload]), Color.from_no_color(True)
        )
        observed_at = datetime.now(UTC).isoformat()
        child = store.close_paper_order(
            order_id,
            0.80,
            max_quantity=2.0,
            liquidity_evidence={
                "displayed_depth": 2.0,
                "source": "test_depth",
                "observed_at": observed_at,
            },
        )
        assert child["parent_order_id"] == order_id

        with store.connect() as conn:
            conn.execute(
                "DELETE FROM dataset_kalshi_trades WHERE trade_id='T-PARTIAL-RESTATE'"
            )
        child_result = next(
            row for row in restate(db_path)["orders"] if row["order_id"] == child["id"]
        )
        assert child_result["verification"] == "UNVERIFIABLE"
        assert "EXEC_V3_TAPE_MISSING" in child_result["findings"]


def _verified_terminal_readiness_root(
    store: PaperStore,
    *,
    trade_id: str,
    ticker: str = "KXHIGHTSEA-26JUL13-B82.5",
    floor: float = 82.0,
    cap: float = 83.0,
) -> int:
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_account_ledger SET created_at=? "
            "WHERE event_type='EXECUTION_SEMANTICS_TRANSITION'",
            ((T0 - timedelta(minutes=1)).isoformat(),),
        )
    order_id = _resting_order(
        store,
        "2026-07-14",
        _decision(
            ticker,
            side="NO",
            limit_price=0.72,
            contracts=4.0,
            floor=floor,
            cap=cap,
        ),
        created_at=T0,
    )
    payload = _trade(
        trade_id,
        yes_price=0.28,
        quantity=4.0,
        taker_book_side="bid",
        created_time=T0 + timedelta(minutes=1),
    )
    _fill_resting_orders_against_live_book(
        store, _TradesClient([payload]), Color.from_no_color(True)
    )
    assert store.settle_paper_orders("2026-07-14", 85.0) == 1
    return order_id


def test_readiness_aggregates_verified_partial_close_lots_into_one_decision() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_account_ledger SET created_at=? "
                "WHERE event_type='EXECUTION_SEMANTICS_TRANSITION'",
                ((T0 - timedelta(minutes=1)).isoformat(),),
            )
            conn.execute(
                "INSERT INTO paper_account_ledger "
                "(created_at, account_id, event_type, amount, idempotency_key) "
                "VALUES (?, 'paper-shared', 'EXECUTION_SEMANTICS_TRANSITION', 0, ?)",
                (
                    (T0 - timedelta(minutes=2)).isoformat(),
                    "execution:exec-v2-2026-07-13",
                ),
            )
        order_id = _resting_order(
            store,
            "2026-07-14",
            _decision(side="NO", limit_price=0.72, contracts=4.0),
            created_at=T0,
        )
        payload = _trade(
            "T-PARTIAL-READINESS",
            yes_price=0.28,
            quantity=4.0,
            taker_book_side="bid",
            created_time=T0 + timedelta(minutes=1),
        )
        _fill_resting_orders_against_live_book(
            store, _TradesClient([payload]), Color.from_no_color(True)
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
        assert store.settle_paper_orders("2026-07-14", 85.0) == 1
        with store.connect() as conn:
            expected_pnl, expected_capital = conn.execute(
                "SELECT SUM(realized_pnl), SUM(contracts * cost_per_contract) "
                "FROM paper_orders WHERE id=? OR parent_order_id=?",
                (order_id, order_id),
            ).fetchone()

        result = replay_from_database(
            db_path, {("KXHIGHTSEA", "2026-07-14"): 85.0}
        )
        assert result["semantics_boundary"] == (T0 - timedelta(minutes=1)).isoformat()
        metrics = result["readiness_metrics"]
        assert metrics["counts"]["settled_decisions"] == 1
        assert metrics["candidate"]["realized_pnl"] == round(expected_pnl, 4)
        assert metrics["candidate"]["capital_at_risk"] == round(expected_capital, 4)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_id", "paper-research-shadow"),
        ("execution_model_version", "exec-v2-2026-07-13"),
        ("created_at", (T0 - timedelta(minutes=2)).isoformat()),
    ],
)
def test_readiness_rejects_group_with_scope_mismatched_child(
    field: str,
    value: str,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_account_ledger SET created_at=? "
                "WHERE event_type='EXECUTION_SEMANTICS_TRANSITION'",
                ((T0 - timedelta(minutes=1)).isoformat(),),
            )
        order_id = _resting_order(
            store,
            "2026-07-14",
            _decision(side="NO", limit_price=0.72, contracts=4.0),
            created_at=T0,
        )
        payload = _trade(
            "T-INVALID-READINESS",
            yes_price=0.28,
            quantity=4.0,
            taker_book_side="bid",
            created_time=T0 + timedelta(minutes=1),
        )
        _fill_resting_orders_against_live_book(
            store, _TradesClient([payload]), Color.from_no_color(True)
        )
        child = store.close_paper_order(
            order_id,
            0.80,
            max_quantity=2.0,
            liquidity_evidence={
                "displayed_depth": 2.0,
                "source": "test_depth",
                "observed_at": datetime.now(UTC).isoformat(),
            },
        )
        assert store.settle_paper_orders("2026-07-14", 85.0) == 1
        with store.connect() as conn:
            conn.execute(
                f"UPDATE paper_orders SET {field}=? WHERE id=?",
                (value, child["id"]),
            )
        verified = {
            row["order_id"]
            for row in restate(db_path)["orders"]
            if row["verification"] == "VERIFIED"
        }
        assert {order_id, child["id"]} <= verified

        result = replay_from_database(
            db_path, {("KXHIGHTSEA", "2026-07-14"): 85.0}
        )

        assert result["verified_decisions"] == 0
        metrics = result["readiness_metrics"]
        assert metrics["counts"]["settled_decisions"] == 0
        assert metrics["by_cohort"] == {}
        assert result["post_boundary_days"] == 0
        assert result["promotion_eligible"] is False


@pytest.mark.parametrize("risk_profile", ["research", "not-a-profile"])
def test_readiness_excludes_non_live_shared_account_roots(
    risk_profile: str,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = _verified_terminal_readiness_root(
            store, trade_id=f"T-{risk_profile.upper()}-READINESS"
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders "
                "SET risk_profile=?, account_id='paper-shared' WHERE id=?",
                (risk_profile, order_id),
            )
        verified = next(
            row for row in restate(db_path)["orders"] if row["order_id"] == order_id
        )
        assert verified["verification"] == "VERIFIED"

        result = replay_from_database(
            db_path, {("KXHIGHTSEA", "2026-07-14"): 85.0}
        )

        assert result["source_orders"] == 1
        assert result["verified_decisions"] == 0
        metrics = result["readiness_metrics"]
        assert metrics["counts"]["settled_decisions"] == 0
        assert metrics["by_cohort"] == {}
        assert result["post_boundary_days"] == 0


def test_readiness_treats_legacy_null_root_profile_as_live() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = _verified_terminal_readiness_root(
            store, trade_id="T-NULL-PROFILE-READINESS"
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET risk_profile=NULL WHERE id=?",
                (order_id,),
            )

        result = replay_from_database(
            db_path, {("KXHIGHTSEA", "2026-07-14"): 85.0}
        )

        assert result["source_orders"] == 1
        assert result["verified_decisions"] == 1
        metrics = result["readiness_metrics"]
        assert metrics["counts"]["settled_decisions"] == 1
        assert result["post_boundary_days"] == 1


@pytest.mark.parametrize(
    ("risk_profile", "expected_post_boundary_days"),
    [("research", 1), ("not-a-profile", 0)],
)
def test_readiness_mixed_day_ignores_research_but_rejects_invalid_profile(
    risk_profile: str,
    expected_post_boundary_days: int,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        live_id = _verified_terminal_readiness_root(
            store, trade_id="T-MIXED-LIVE-READINESS"
        )
        other_id = _verified_terminal_readiness_root(
            store,
            trade_id="T-MIXED-OTHER-READINESS",
            ticker="KXHIGHTSEA-26JUL14-B84.5",
            floor=84.0,
            cap=85.0,
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders "
                "SET risk_profile=?, account_id='paper-shared' WHERE id=?",
                (risk_profile, other_id),
            )
        verified = {
            row["order_id"]
            for row in restate(db_path)["orders"]
            if row["verification"] == "VERIFIED"
        }
        assert {live_id, other_id} <= verified

        result = replay_from_database(
            db_path, {("KXHIGHTSEA", "2026-07-14"): 85.0}
        )

        assert result["source_orders"] == 2
        assert result["verified_decisions"] == 1
        metrics = result["readiness_metrics"]
        assert metrics["counts"]["settled_decisions"] == 1
        assert metrics["by_cohort"]["post_exec_v3_live"]["trades"] == 1
        assert result["post_boundary_days"] == expected_post_boundary_days


@pytest.mark.parametrize(
    ("child_version", "expected_source_orders", "expected_post_boundary_days"),
    [
        (EXECUTION_MODEL_VERSION, 3, 1),
        ("exec-v2-2026-07-13", 2, 0),
    ],
)
def test_readiness_research_group_is_neutral_only_with_consistent_scope(
    child_version: str,
    expected_source_orders: int,
    expected_post_boundary_days: int,
) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        live_id = _verified_terminal_readiness_root(
            store, trade_id="T-RESEARCH-SCOPE-LIVE"
        )
        research_id = _resting_order(
            store,
            "2026-07-14",
            _decision(
                "KXHIGHTSEA-26JUL14-B84.5",
                side="NO",
                limit_price=0.72,
                contracts=4.0,
                floor=84.0,
                cap=85.0,
            ),
            created_at=T0,
        )
        payload = _trade(
            "T-RESEARCH-SCOPE-PARTIAL",
            yes_price=0.28,
            quantity=4.0,
            taker_book_side="bid",
            created_time=T0 + timedelta(minutes=1),
        )
        _fill_resting_orders_against_live_book(
            store, _TradesClient([payload]), Color.from_no_color(True)
        )
        child = store.close_paper_order(
            research_id,
            0.80,
            max_quantity=2.0,
            liquidity_evidence={
                "displayed_depth": 2.0,
                "source": "test_depth",
                "observed_at": datetime.now(UTC).isoformat(),
            },
        )
        assert store.settle_paper_orders("2026-07-14", 85.0) == 1
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders "
                "SET risk_profile='research', account_id='paper-shared' "
                "WHERE id=? OR parent_order_id=?",
                (research_id, research_id),
            )
            conn.execute(
                "UPDATE paper_orders SET execution_model_version=? WHERE id=?",
                (child_version, child["id"]),
            )
        verified = {
            row["order_id"]
            for row in restate(db_path)["orders"]
            if row["verification"] == "VERIFIED"
        }
        assert {live_id, research_id, child["id"]} <= verified

        result = replay_from_database(
            db_path, {("KXHIGHTSEA", "2026-07-14"): 85.0}
        )

        assert result["source_orders"] == expected_source_orders
        assert result["verified_decisions"] == 1
        metrics = result["readiness_metrics"]
        assert metrics["counts"]["settled_decisions"] == 1
        assert result["post_boundary_days"] == expected_post_boundary_days


def test_weekly_goal_counts_only_consecutive_completed_five_percent_weeks() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        now_pt = datetime.now().astimezone(ZoneInfo("America/Los_Angeles"))
        this_monday = (now_pt - timedelta(days=now_pt.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_account_ledger SET created_at=? "
                "WHERE idempotency_key='execution:exec-v3-2026-07-14'",
                ((this_monday - timedelta(days=21)).astimezone(UTC).isoformat(),),
            )
        weekly_rows = (
            (this_monday - timedelta(days=11), 50.0),
            (this_monday - timedelta(days=4), 52.5),
            (this_monday + timedelta(days=1), 55.125),
        )
        for index, (resolved_at, pnl) in enumerate(weekly_rows):
            order_id = store.record_paper_order(
                f"2026-07-{10 + index:02d}", _decision(), risk_profile="live"
            )
            with store.connect() as conn:
                conn.execute(
                    "UPDATE paper_orders SET status='PAPER_SETTLED', realized_pnl=?, "
                    "settled_at=? WHERE id=?",
                    (pnl, resolved_at.astimezone(UTC).isoformat(), order_id),
                )

        goal = _weekly_goal_payload(store, {"realized_equity": 1157.625})

        assert goal["weekly_realized_return"] == 0.05
        assert goal["completed_week_success_streak"] == 2
        assert goal["evidence_boundary"]


def test_weekly_goal_streak_excludes_weeks_before_exec_v3_boundary() -> None:
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        now_pt = datetime.now().astimezone(ZoneInfo("America/Los_Angeles"))
        this_monday = (now_pt - timedelta(days=now_pt.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        order_id = store.record_paper_order(
            "2026-07-01", _decision(), risk_profile="live"
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET status='PAPER_SETTLED', realized_pnl=50, "
                "settled_at=? WHERE id=?",
                (
                    (this_monday - timedelta(days=4)).astimezone(UTC).isoformat(),
                    order_id,
                ),
            )
            conn.execute(
                "UPDATE paper_account_ledger SET created_at=? "
                "WHERE idempotency_key='execution:exec-v3-2026-07-14'",
                ((this_monday + timedelta(days=1)).astimezone(UTC).isoformat(),),
            )

        goal = _weekly_goal_payload(store, {"realized_equity": 1050.0})

        assert goal["completed_week_success_streak"] == 0
        assert goal["first_full_evidence_week"] == (this_monday + timedelta(days=7)).isoformat()
        assert goal["current_week_evidence_qualified"] is False
