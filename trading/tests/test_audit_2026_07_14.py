"""Third-audit execution and accounting regression tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import pytest

from sfo_kalshi_quant.account import (
    LIVE_STABILITY_ACCOUNT_ID,
    strategy_fingerprint,
)
from sfo_kalshi_quant.cli import _fill_resting_orders_against_live_book
from sfo_kalshi_quant.colors import Color
from sfo_kalshi_quant.config import StrategyConfig
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


def _verify_final_truth(store: PaperStore) -> None:
    result = store.verify_paper_settlements(
        {("KXHIGHTSEA", "2026-07-14"): 85.0},
        intervals={"KXHIGHTSEA": ("2026-07-14", "2026-07-14")},
    )
    assert result["mismatches"] == 0


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
        _verify_final_truth(store)
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
        state = store.live_account_state()
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
        assert store.live_account_state()["reservations"] == 0.0


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


def test_exec_v4_cutover_expires_exec_v3_resting_orders_without_rewriting() -> None:
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
                "UPDATE paper_orders SET execution_model_version='exec-v3-2026-07-14' "
                "WHERE id=?",
                (order_id,),
            )

        PaperStore(db_path)

        expired = store.paper_order(order_id)
        assert expired["status"] == "PAPER_EXPIRED"
        assert expired["reserved_cost"] == 0.0
        assert expired["remaining_contracts"] == 0.0
        assert expired["execution_model_version"] == "exec-v3-2026-07-14"
        assert store.live_account_state()["reservations"] == 0.0


def test_exec_v4_cutover_freezes_exec_v3_partial_remainder_once() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        order_id = _resting_order(
            store,
            "2026-07-14",
            _decision(side="NO", limit_price=0.72, contracts=10.0),
            created_at=T0,
        )
        first_trade = _trade(
            "T-V3-PARTIAL",
            yes_price=0.28,
            quantity=4.0,
            taker_book_side="bid",
            created_time=T0 + timedelta(minutes=1),
        )
        store.apply_maker_trade_batch(
            "KXHIGHTSEA-26JUL13-B82.5", [first_trade]
        )
        partial = store.paper_order(order_id)
        assert partial["status"] == "PAPER_PARTIALLY_FILLED"
        assert partial["filled_contracts"] == 4.0
        assert partial["remaining_contracts"] == 6.0
        reserved_remainder = float(partial["reserved_cost"])
        historical_evidence = json.loads(partial["fill_evidence_json"])
        historical_evidence["model"] = "maker_allocator_price_time_v3"
        historical_evidence["execution_model_version"] = "exec-v3-2026-07-14"
        historical_evidence_json = json.dumps(historical_evidence, sort_keys=True)
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET execution_model_version=?, "
                "fill_evidence_json=? WHERE id=?",
                (
                    "exec-v3-2026-07-14",
                    historical_evidence_json,
                    order_id,
                ),
            )
            conn.execute(
                "UPDATE paper_maker_allocations SET execution_model_version=? "
                "WHERE order_id=?",
                ("exec-v3-2026-07-14", order_id),
            )

        PaperStore(db_path)

        frozen = store.paper_order(order_id)
        assert frozen["status"] == "PAPER_PARTIAL_EXPIRED"
        assert frozen["contracts"] == 4.0
        assert frozen["filled_contracts"] == 4.0
        assert frozen["remaining_contracts"] == 0.0
        assert frozen["queue_remaining"] == 0.0
        assert frozen["reserved_cost"] == 0.0
        assert frozen["execution_model_version"] == "exec-v3-2026-07-14"
        assert frozen["fill_evidence_json"] == historical_evidence_json
        cutover = json.loads(frozen["outcome_diagnostics_json"])
        assert cutover["previous_execution_model_version"] == "exec-v3-2026-07-14"
        assert cutover["cutover_execution_model_version"] == EXECUTION_MODEL_VERSION
        assert "execution_model_version" not in cutover
        release_key = (
            f"order:{order_id}:{EXECUTION_MODEL_VERSION}:cutover-release"
        )
        with store.connect() as conn:
            releases = conn.execute(
                "SELECT amount FROM paper_account_ledger "
                "WHERE order_id=? AND event_type='RESERVATION_RELEASE' "
                "AND idempotency_key=?",
                (order_id, release_key),
            ).fetchall()
        assert [amount for (amount,) in releases] == [reserved_remainder]
        assert store.live_account_state()["reservations"] == 0.0

        PaperStore(db_path)
        with store.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM paper_account_ledger "
                "WHERE idempotency_key=?",
                (release_key,),
            ).fetchone()[0] == 1

        later_trade = _trade(
            "T-V4-MUST-NOT-FILL-V3",
            yes_price=0.28,
            quantity=6.0,
            taker_book_side="bid",
            created_time=T0 + timedelta(minutes=2),
        )
        assert _fill_resting_orders_against_live_book(
            store,
            _TradesClient([later_trade]),
            Color.from_no_color(True),
        ) == 0
        unchanged = store.paper_order(order_id)
        assert unchanged["status"] == "PAPER_PARTIAL_EXPIRED"
        assert unchanged["contracts"] == 4.0
        assert unchanged["filled_contracts"] == 4.0
        assert unchanged["execution_model_version"] == "exec-v3-2026-07-14"
        assert unchanged["fill_evidence_json"] == historical_evidence_json
        assert {row["id"] for row in store.open_paper_orders()} == {order_id}

        assert store.settle_paper_orders("2026-07-14", 85.0) == 1
        _verify_final_truth(store)
        settled = store.paper_order(order_id)
        assert settled["status"] == "PAPER_SETTLED"
        assert settled["contracts"] == 4.0
        assert settled["execution_model_version"] == "exec-v3-2026-07-14"
        assert settled["fill_evidence_json"] == historical_evidence_json


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
        _verify_final_truth(store)

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
        assert "EXEC_V4_TAPE_MISSING" in missing_tape["findings"]


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
        assert "EXEC_V4_TAPE_MISSING" in child_result["findings"]


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
    _verify_final_truth(store)
    return order_id


def test_readiness_strict_fingerprint_uses_only_complete_target_day_cohorts() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        required_fingerprint = strategy_fingerprint(
            StrategyConfig(), entry_mode="limit"
        )
        required_fingerprints = {
            "KXHIGHTSEA": required_fingerprint,
            "KXHIGHTSFO": "sfo-current-fingerprint",
        }
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_account_ledger SET created_at=? "
                "WHERE event_type='EXECUTION_SEMANTICS_TRANSITION'",
                ((T0 - timedelta(minutes=1)).isoformat(),),
            )

        def seed_terminal_root(
            target_date: str,
            *,
            created_at: datetime,
            ticker: str,
            trade_id: str,
            floor: float,
            cap: float,
            fingerprint: str = required_fingerprint,
        ) -> int:
            order_id = _resting_order(
                store,
                target_date,
                _decision(
                    ticker,
                    side="NO",
                    limit_price=0.72,
                    contracts=4.0,
                    floor=floor,
                    cap=cap,
                ),
                created_at=created_at,
            )
            payload = _trade(
                trade_id,
                yes_price=0.28,
                quantity=4.0,
                taker_book_side="bid",
                created_time=created_at + timedelta(minutes=1),
            )
            _fill_resting_orders_against_live_book(
                store, _TradesClient([payload]), Color.from_no_color(True)
            )
            assert store.settle_paper_orders(target_date, 85.0) == 1
            series = ticker.split("-", 1)[0]
            verification = store.verify_paper_settlements(
                {(series, target_date): 85.0},
                intervals={series: (target_date, target_date)},
            )
            assert verification["mismatches"] == 0
            with store.connect() as conn:
                conn.execute(
                    "UPDATE paper_orders SET strategy_fingerprint=? WHERE id=?",
                    (fingerprint, order_id),
                )
            return order_id

        # An earlier live generation cannot establish the current cohort boundary.
        seed_terminal_root(
            "2026-07-14",
            created_at=T0,
            ticker="KXHIGHTSEA-26JUL14-B82.5",
            trade_id="T-OLD-FINGERPRINT",
            floor=82.0,
            cap=83.0,
            fingerprint="previous-live-fingerprint",
        )
        first_matching_at = T0 + timedelta(days=1)
        seed_terminal_root(
            "2026-07-15",
            created_at=first_matching_at,
            ticker="KXHIGHTSEA-26JUL15-B82.5",
            trade_id="T-CURRENT-MIXED-DAY",
            floor=82.0,
            cap=83.0,
        )
        seed_terminal_root(
            "2026-07-15",
            created_at=first_matching_at + timedelta(seconds=1),
            ticker="KXHIGHTSEA-26JUL15-B84.5",
            trade_id="T-WRONG-MIXED-DAY",
            floor=84.0,
            cap=85.0,
            fingerprint="different-live-fingerprint",
        )
        seed_terminal_root(
            "2026-07-16",
            created_at=T0 + timedelta(days=2),
            ticker="KXHIGHTSEA-26JUL16-B82.5",
            trade_id="T-CURRENT-INCOMPLETE-DAY",
            floor=82.0,
            cap=83.0,
        )
        # A same-fingerprint root that has not resolved makes the weather day
        # incomplete; the resolved sibling must not contribute available-case
        # economics on its own.
        incomplete_order_id = _resting_order(
            store,
            "2026-07-16",
            _decision(
                "KXHIGHTSEA-26JUL16-B84.5",
                side="NO",
                limit_price=0.72,
                contracts=4.0,
                floor=84.0,
                cap=85.0,
            ),
            created_at=T0 + timedelta(days=2, seconds=1),
        )
        _fill_resting_orders_against_live_book(
            store,
            _TradesClient(
                [
                    _trade(
                        "T-CURRENT-STILL-OPEN",
                        yes_price=0.28,
                        quantity=4.0,
                        taker_book_side="bid",
                        created_time=T0 + timedelta(days=2, minutes=1),
                    )
                ]
            ),
            Color.from_no_color(True),
        )
        assert store.paper_order(incomplete_order_id)["status"] == "PAPER_FILLED"
        pure_order_id = seed_terminal_root(
            "2026-07-17",
            created_at=T0 + timedelta(days=3),
            ticker="KXHIGHTSEA-26JUL17-B82.5",
            trade_id="T-CURRENT-PURE-DAY",
            floor=82.0,
            cap=83.0,
        )
        sfo_order_id = seed_terminal_root(
            "2026-07-17",
            created_at=T0 + timedelta(days=3, seconds=1),
            ticker="KXHIGHTSFO-26JUL17-B82.5",
            trade_id="T-CURRENT-PURE-SFO",
            floor=82.0,
            cap=83.0,
            fingerprint=required_fingerprints["KXHIGHTSFO"],
        )
        zero_fill_created_at = T0 + timedelta(days=3, seconds=2)
        zero_fill_order_id = _resting_order(
            store,
            "2026-07-17",
            _decision(
                "KXHIGHTSEA-26JUL17-B86.5",
                side="NO",
                limit_price=0.72,
                contracts=4.0,
                floor=86.0,
                cap=87.0,
            ),
            created_at=zero_fill_created_at,
        )
        assert (
            store.expire_stale_resting_orders(
                now=(zero_fill_created_at + timedelta(minutes=21)).isoformat(),
                reconciled_through_by_ticker={
                    "KXHIGHTSEA-26JUL17-B86.5": (
                        zero_fill_created_at + timedelta(minutes=21)
                    ).isoformat()
                },
            )
            == 1
        )
        assert store.paper_order(zero_fill_order_id)["status"] == "PAPER_EXPIRED"
        with store.connect() as conn:
            expected_pnl = float(
                conn.execute(
                    "SELECT SUM(realized_pnl) FROM paper_orders WHERE id IN (?, ?)",
                    (pure_order_id, sfo_order_id),
                ).fetchone()[0]
            )
            watermarked_evidence = json.loads(
                conn.execute(
                    "SELECT fill_evidence_json FROM paper_orders WHERE id=?",
                    (zero_fill_order_id,),
                ).fetchone()[0]
            )

        settlements = {
            ("KXHIGHTSEA", "2026-07-14"): 85.0,
            ("KXHIGHTSEA", "2026-07-15"): 85.0,
            ("KXHIGHTSEA", "2026-07-16"): 85.0,
            ("KXHIGHTSEA", "2026-07-17"): 85.0,
            ("KXHIGHTSFO", "2026-07-17"): 85.0,
        }
        unwatermarked_evidence = dict(watermarked_evidence)
        unwatermarked_evidence.pop("tape_reconciled_through")
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET fill_evidence_json=? WHERE id=?",
                (json.dumps(unwatermarked_evidence), zero_fill_order_id),
            )
        unwatermarked = replay_from_database(
            db_path,
            settlements,
            required_strategy_fingerprint=required_fingerprints,
        )
        assert unwatermarked["post_boundary_days"] == 0
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET fill_evidence_json=? WHERE id=?",
                (json.dumps(watermarked_evidence), zero_fill_order_id),
            )

        result = replay_from_database(
            db_path,
            settlements,
            required_strategy_fingerprint=required_fingerprints,
        )

        assert result["evidence_boundary"] == first_matching_at.isoformat()
        assert result["post_boundary_days"] == 1
        assert result["source_orders"] == 3
        assert result["verified_decisions"] == 2
        metrics = result["readiness_metrics"]
        assert metrics["counts"] == {
            "settled_decisions": 2,
            "independent_days": 1,
        }
        assert metrics["candidate"]["realized_pnl"] == round(expected_pnl, 4)
        assert metrics["semantics_boundary"] == (
            T0 - timedelta(minutes=1)
        ).isoformat()
        assert metrics["evidence_boundary"] == first_matching_at.isoformat()
        assert metrics["source_cohort"] == result["evidence_scope"]["source_cohort"]
        assert result["evidence_scope"]["strategy_fingerprint"] is None
        assert result["evidence_scope"]["strategy_fingerprints_by_series"] == (
            required_fingerprints
        )
        assert result["evidence_scope"]["evidence_boundary"] == (
            first_matching_at.isoformat()
        )
        assert result["evidence_scope"]["complete_target_days_only"] is True
        assert result["evidence_scope"]["strategy_fingerprint_semantics"] == (
            "policy_config_entry_mode_and_behavior_version"
        )
        assert result["evidence_scope"]["immutable_model_lineage_persisted"] is False
        assert any(
            "immutable forecast/model/calibration lineage" in reason
            for reason in result["promotion_block_reasons"]
        )
        assert any(
            "fingerprint" in reason
            for reason in result["promotion_block_reasons"]
        )

        # A zero-fill request from the prior policy still makes the target day
        # a mixed-policy observation. Its creation predates the current cohort
        # boundary, and its lack of PnL must not make it disappear from the
        # atomic weather-day inclusion rule.
        pre_boundary_expired_id = _resting_order(
            store,
            "2026-07-17",
            _decision(
                "KXHIGHTSEA-26JUL17-B88.5",
                side="NO",
                limit_price=0.72,
                contracts=4.0,
                floor=88.0,
                cap=89.0,
            ),
            created_at=T0,
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET strategy_fingerprint=? WHERE id=?",
                ("previous-live-fingerprint", pre_boundary_expired_id),
            )
        pre_boundary_row = store.paper_order(pre_boundary_expired_id)
        pre_boundary_expiry = datetime.fromisoformat(
            str(pre_boundary_row["expires_at"])
        )
        pre_boundary_watermark = pre_boundary_expiry + timedelta(
            minutes=5, seconds=1
        )
        assert store.expire_stale_resting_orders(
            now=pre_boundary_watermark.isoformat(),
            reconciled_through_by_ticker={
                str(pre_boundary_row["market_ticker"]): (
                    pre_boundary_watermark.isoformat()
                )
            },
        ) == 1

        mixed_policy_day = replay_from_database(
            db_path,
            settlements,
            required_strategy_fingerprint=required_fingerprints,
        )
        assert mixed_policy_day["evidence_boundary"] == first_matching_at.isoformat()
        assert mixed_policy_day["post_boundary_days"] == 0
        assert mixed_policy_day["readiness_metrics"]["counts"] == {
            "settled_decisions": 0,
            "independent_days": 0,
        }


def test_readiness_strict_fingerprint_rejects_mixed_child_lot_day() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        required_fingerprint = strategy_fingerprint(
            StrategyConfig(), entry_mode="limit"
        )
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
        _fill_resting_orders_against_live_book(
            store,
            _TradesClient(
                [
                    _trade(
                        "T-MIXED-FINGERPRINT-CHILD",
                        yes_price=0.28,
                        quantity=4.0,
                        taker_book_side="bid",
                        created_time=T0 + timedelta(minutes=1),
                    )
                ]
            ),
            Color.from_no_color(True),
        )
        child = store.close_paper_order(
            order_id,
            0.80,
            max_quantity=2.0,
            liquidity_evidence={
                "displayed_depth": 2.0,
                "source": "test_depth",
                "observed_at": (T0 + timedelta(minutes=2)).isoformat(),
            },
        )
        assert store.settle_paper_orders("2026-07-14", 85.0) == 1
        _verify_final_truth(store)
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET strategy_fingerprint=? WHERE id=?",
                ("different-live-fingerprint", child["id"]),
            )

        result = replay_from_database(
            db_path,
            {("KXHIGHTSEA", "2026-07-14"): 85.0},
            required_strategy_fingerprint=required_fingerprint,
        )

        assert result["evidence_boundary"] == T0.isoformat()
        assert result["post_boundary_days"] == 0
        assert result["source_orders"] == 0
        assert result["verified_decisions"] == 0
        assert result["readiness_metrics"]["counts"] == {
            "settled_decisions": 0,
            "independent_days": 0,
        }
        assert result["evidence_scope"]["fingerprint_mismatch_target_days"] == 1


def test_exec_v4_boundary_excludes_v3_evidence_without_rewriting() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        historical_id = _verified_terminal_readiness_root(
            store,
            trade_id="T-HISTORICAL-V3",
            ticker="KXHIGHTSEA-26JUL14-B82.5",
        )
        current_id = _verified_terminal_readiness_root(
            store,
            trade_id="T-CURRENT-V4",
            ticker="KXHIGHTSEA-26JUL14-B84.5",
            floor=84.0,
            cap=85.0,
        )
        with store.connect() as conn:
            historical_evidence = json.loads(
                conn.execute(
                    "SELECT fill_evidence_json FROM paper_orders WHERE id=?",
                    (historical_id,),
                ).fetchone()[0]
            )
            historical_evidence["model"] = "maker_allocator_price_time_v3"
            conn.execute(
                "UPDATE paper_orders SET execution_model_version=?, "
                "fill_evidence_json=? WHERE id=?",
                (
                    "exec-v3-2026-07-14",
                    json.dumps(historical_evidence, sort_keys=True),
                    historical_id,
                ),
            )
            conn.execute(
                "UPDATE paper_maker_allocations SET execution_model_version=? "
                "WHERE order_id=?",
                ("exec-v3-2026-07-14", historical_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO paper_account_ledger "
                "(created_at, account_id, event_type, amount, idempotency_key) "
                "VALUES (?, 'paper-shared', 'EXECUTION_SEMANTICS_TRANSITION', 0, ?)",
                (
                    (T0 - timedelta(minutes=2)).isoformat(),
                    "execution:exec-v3-2026-07-14",
                ),
            )
            conn.execute(
                "UPDATE paper_account_ledger SET created_at=? "
                "WHERE idempotency_key='execution:exec-v4-2026-07-17'",
                ((T0 - timedelta(minutes=1)).isoformat(),),
            )

        replay = replay_from_database(
            db_path, {("KXHIGHTSEA", "2026-07-14"): 85.0}
        )

        assert replay["source_orders"] == 1
        assert replay["execution_model_version"] == "exec-v4-2026-07-17"
        assert replay["semantics_boundary"] == (
            T0 - timedelta(minutes=1)
        ).isoformat()
        restated = {
            row["order_id"]: row for row in restate(db_path)["orders"]
        }
        assert restated[historical_id]["verification"] == "UNVERIFIABLE"
        assert restated[historical_id]["execution_model_version"] == (
            "exec-v3-2026-07-14"
        )
        assert restated[historical_id]["fill_evidence_model"] == (
            "maker_allocator_price_time_v3"
        )
        assert "EXEC_V3_HISTORICAL_SEMANTICS" in restated[historical_id]["findings"]
        assert restated[current_id]["verification"] == "VERIFIED"
        assert restated[current_id]["execution_model_version"] == (
            EXECUTION_MODEL_VERSION
        )
        with store.connect() as conn:
            current_version, current_evidence = conn.execute(
                "SELECT execution_model_version, fill_evidence_json "
                "FROM paper_orders WHERE id=?",
                (current_id,),
            ).fetchone()
            assert current_version == EXECUTION_MODEL_VERSION
            assert json.loads(current_evidence)["model"] == (
                "maker_allocator_price_time_v4"
            )
            assert conn.execute(
                "SELECT DISTINCT execution_model_version "
                "FROM paper_maker_allocations WHERE order_id=?",
                (current_id,),
            ).fetchone()[0] == EXECUTION_MODEL_VERSION
            assert EXECUTION_MODEL_VERSION in conn.execute(
                "SELECT reason FROM paper_monitor_snapshots "
                "WHERE order_id=? AND action='LIMIT_FILLED'",
                (current_id,),
            ).fetchone()[0]
            assert conn.execute(
                "SELECT execution_model_version FROM paper_orders WHERE id=?",
                (historical_id,),
            ).fetchone()[0] == "exec-v3-2026-07-14"
            assert conn.execute(
                "SELECT DISTINCT execution_model_version "
                "FROM paper_maker_allocations WHERE order_id=?",
                (historical_id,),
            ).fetchone()[0] == "exec-v3-2026-07-14"


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
        _verify_final_truth(store)
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
        _verify_final_truth(store)
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
        group_evidence_invalid = field in {
            "account_id",
            "execution_model_version",
        }
        assert (order_id in verified) is not group_evidence_invalid
        assert (child["id"] in verified) is not group_evidence_invalid
        if field == "execution_model_version":
            assert child["id"] not in verified
            child_result = next(
                row
                for row in restate(db_path)["orders"]
                if row["order_id"] == child["id"]
            )
            assert "EXEC_V4_ORDER_VERSION_MISMATCH" in child_result["findings"]

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

        assert result["source_orders"] == 0
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

        assert result["source_orders"] == 1
        assert result["verified_decisions"] == 1
        metrics = result["readiness_metrics"]
        assert metrics["counts"]["settled_decisions"] == 1
        assert metrics["by_cohort"]["post_exec_v4_live"]["trades"] == 1
        assert result["post_boundary_days"] == expected_post_boundary_days


def test_readiness_mixed_day_ignores_valid_pre_boundary_research_history() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        live_id = _verified_terminal_readiness_root(
            store, trade_id="T-MIXED-LIVE-POST-BOUNDARY"
        )
        research_id = _verified_terminal_readiness_root(
            store,
            trade_id="T-MIXED-RESEARCH-PRE-BOUNDARY",
            ticker="KXHIGHTSEA-26JUL14-B84.5",
            floor=84.0,
            cap=85.0,
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET risk_profile='research', "
                "account_id='paper-shared', created_at=? WHERE id=?",
                ((T0 - timedelta(minutes=2)).isoformat(), research_id),
            )

        result = replay_from_database(
            db_path, {("KXHIGHTSEA", "2026-07-14"): 85.0}
        )

        assert result["source_orders"] == 1
        assert result["verified_decisions"] == 1
        assert result["readiness_metrics"]["counts"]["settled_decisions"] == 1
        assert result["post_boundary_days"] == 1
        assert live_id != research_id


@pytest.mark.parametrize(
    (
        "child_version",
        "expected_source_orders",
        "expected_post_boundary_days",
        "child_verified",
    ),
    [
        (EXECUTION_MODEL_VERSION, 1, 1, True),
        ("exec-v2-2026-07-13", 1, 0, False),
    ],
)
def test_readiness_research_group_is_neutral_only_with_consistent_scope(
    child_version: str,
    expected_source_orders: int,
    expected_post_boundary_days: int,
    child_verified: bool,
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
        _verify_final_truth(store)
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
        restated = {
            row["order_id"]: row for row in restate(db_path)["orders"]
        }
        verified_ids = {
            order_id
            for order_id, row in restated.items()
            if row["verification"] == "VERIFIED"
        }
        assert live_id in verified_ids
        assert (research_id in verified_ids) is child_verified
        assert (restated[child["id"]]["verification"] == "VERIFIED") is (
            child_verified
        )
        if not child_verified:
            assert "EXEC_V4_ORDER_VERSION_MISMATCH" in restated[child["id"]][
                "findings"
            ]

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
                "UPDATE paper_accounts SET created_at=? "
                "WHERE account_id=?",
                (
                    (this_monday - timedelta(days=21)).astimezone(UTC).isoformat(),
                    LIVE_STABILITY_ACCOUNT_ID,
                ),
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

        goal = _weekly_goal_payload(
            store,
            {
                "account_id": LIVE_STABILITY_ACCOUNT_ID,
                "created_at": (
                    this_monday - timedelta(days=21)
                ).astimezone(UTC).isoformat(),
                "realized_equity": 1157.625,
            },
        )

        assert goal["weekly_realized_return"] == 0.05
        assert goal["completed_week_success_streak"] == 2
        assert goal["evidence_boundary"]


def test_weekly_goal_streak_excludes_weeks_before_current_execution_boundary() -> None:
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
                "UPDATE paper_accounts SET created_at=? "
                "WHERE account_id=?",
                (
                    (this_monday + timedelta(days=1)).astimezone(UTC).isoformat(),
                    LIVE_STABILITY_ACCOUNT_ID,
                ),
            )

        goal = _weekly_goal_payload(
            store,
            {
                "account_id": LIVE_STABILITY_ACCOUNT_ID,
                "created_at": (
                    this_monday + timedelta(days=1)
                ).astimezone(UTC).isoformat(),
                "realized_equity": 1050.0,
            },
        )

        assert goal["completed_week_success_streak"] == 0
        assert goal["first_full_evidence_week"] == (this_monday + timedelta(days=7)).isoformat()
        assert goal["current_week_evidence_qualified"] is False
