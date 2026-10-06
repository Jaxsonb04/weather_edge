"""Independent paper accounts must not consume one another's public tape."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from sfo_kalshi_quant.account import RESEARCH_ACCOUNT_ID
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.maker_fills import EXECUTION_MODEL_VERSION
from test_audit_2026_07_13 import _decision, _resting_order, _trade


T0 = datetime(2026, 10, 5, 12, tzinfo=UTC)
TICKER = "KXHIGHTSEA-26OCT05-B82.5"
TARGET = "2026-10-05"


def _order(store, *, research=False, contracts=8.0, placed_at=T0, queue=0.0):
    return _resting_order(
        store, TARGET, _decision(TICKER, limit_price=0.72, contracts=contracts),
        created_at=placed_at, queue_ahead=queue,
        risk_profile="research" if research else "live",
    )


def _tape(quantity=10.0):
    return _trade(
        "account-isolation-tape", yes_price=0.28, quantity=quantity,
        taker_book_side="bid", created_time=T0 + timedelta(minutes=3),
    )


def test_independent_accounts_each_receive_full_evidenced_tape(tmp_path):
    path = tmp_path / "paper.db"
    store = PaperStore(path)
    live = _order(store, queue=2.0)
    research = _order(store, research=True, placed_at=T0 + timedelta(seconds=1), queue=2.0)

    updates = store.apply_maker_trade_batch(TICKER, [_tape()])

    assert {row["order_id"] for row in updates} == {live, research}
    assert [store.paper_order(order)["filled_contracts"] for order in (live, research)] == [8.0, 8.0]
    assert [store.paper_order(order)["queue_remaining"] for order in (live, research)] == [0.0, 0.0]
    with store.connect() as conn:
        ledger_count = conn.execute("SELECT COUNT(*) FROM paper_account_ledger").fetchone()[0]
        allocation_count = conn.execute("SELECT COUNT(*) FROM paper_maker_allocations").fetchone()[0]
    restarted = PaperStore(path)
    assert restarted.apply_maker_trade_batch(TICKER, [_tape()]) == []
    with restarted.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_account_ledger").fetchone()[0] == ledger_count
        assert conn.execute("SELECT COUNT(*) FROM paper_maker_allocations").fetchone()[0] == allocation_count


def test_same_account_still_conserves_tape_between_orders_and_restarts(tmp_path):
    path = tmp_path / "paper.db"
    store = PaperStore(path)
    first = _order(store)
    second = _order(store, research=True, placed_at=T0 + timedelta(seconds=1))
    with store.connect() as conn:
        # Legacy multi-lot exposure predates the account-scoped uniqueness
        # guard; only this fixture may create duplicate active exposure.
        account = store.paper_order(first)["account_id"]
        conn.execute("DROP INDEX ux_paper_orders_open_market_side_profile")
        conn.execute("UPDATE paper_orders SET account_id=? WHERE id=?", (account, second))
    store.apply_maker_trade_batch(TICKER, [_tape()])
    assert store.paper_order(first)["filled_contracts"] == 8.0
    assert store.paper_order(second)["filled_contracts"] == 2.0
    # The completed lot is no longer resting. Its persisted consumption must
    # still prevent the remaining lot from reclaiming tape after a restart.
    # Recreate the canonical index now that only one active partial remains.
    with store.connect() as conn:
        conn.execute("UPDATE paper_orders SET status='PAPER_CLOSED', closed_at=? WHERE id=?", (T0.isoformat(), first))
    restarted = PaperStore(path)
    assert restarted.apply_maker_trade_batch(TICKER, [_tape()]) == []
    assert restarted.paper_order(second)["filled_contracts"] == 2.0


@pytest.mark.parametrize("historical_null_account", [False, True])
def test_closed_historical_claims_from_both_tables_stay_account_scoped(tmp_path, historical_null_account):
    path = tmp_path / "paper.db"
    store = PaperStore(path)
    historical = _order(store, contracts=4.0)
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_orders SET status='PAPER_CLOSED', closed_at=?, contracts=4, "
            "filled_contracts=4, remaining_contracts=0, reserved_cost=0, "
            "execution_model_version='exec-v3-2026-07-14' WHERE id=?",
            (T0.isoformat(), historical),
        )
        if historical_null_account:
            conn.execute("UPDATE paper_orders SET account_id=NULL WHERE id=?", (historical,))
        conn.execute(
            "INSERT INTO maker_volume_claims (created_at, market_ticker, trade_id, order_id, quantity) "
            "VALUES (?, ?, ?, ?, 4)",
            (T0.isoformat(), TICKER, _tape()["trade_id"], historical),
        )
        conn.execute(
            "INSERT INTO paper_maker_allocations (created_at, execution_model_version, market_ticker, "
            "trade_id, order_id, trade_created_at, maker_side, side_price, queue_quantity, fill_quantity, "
            "counterfactual, evidence_json) VALUES (?, 'exec-v3-2026-07-14', ?, ?, ?, ?, 'NO', .72, 2, 1, 0, '{}')",
            (T0.isoformat(), TICKER, _tape()["trade_id"], historical, _tape()["created_time"]),
        )
    frozen_historical = dict(store.paper_order(historical))
    live = _order(store, placed_at=T0 + timedelta(minutes=1))
    research = _order(store, research=True, placed_at=T0 + timedelta(minutes=1, seconds=1))
    if historical_null_account:
        with store.connect() as conn:
            conn.execute("UPDATE paper_orders SET account_id=NULL WHERE id=?", (live,))

    store.apply_maker_trade_batch(TICKER, [_tape()])

    assert store.paper_order(live)["filled_contracts"] == 3.0
    assert store.paper_order(research)["filled_contracts"] == 8.0
    assert dict(store.paper_order(historical)) == frozen_historical
    assert PaperStore(path).apply_maker_trade_batch(TICKER, [_tape()]) == []


def test_unattributed_orphan_claim_remains_conservative_for_every_account(tmp_path):
    path = tmp_path / "paper.db"
    store = PaperStore(path)
    live = _order(store)
    research = _order(store, research=True)
    with store.connect() as conn:
        # This historical claim table predates a foreign-key owner constraint.
        # An unknown owner must not turn already consumed tape into new fills.
        conn.execute(
            "INSERT INTO maker_volume_claims (created_at, market_ticker, trade_id, order_id, quantity) "
            "VALUES (?, ?, ?, 999999, 3)",
            (T0.isoformat(), TICKER, _tape()["trade_id"]),
        )
    store.apply_maker_trade_batch(TICKER, [_tape()])
    assert [store.paper_order(order)["filled_contracts"] for order in (live, research)] == [7.0, 7.0]
    # Each account's own persisted seven-unit claim plus the three unknown
    # units exhausts the tape after a process restart, independently.
    assert PaperStore(path).apply_maker_trade_batch(TICKER, [_tape()]) == []
    assert [store.paper_order(order)["filled_contracts"] for order in (live, research)] == [7.0, 7.0]


def test_legacy_per_order_shadow_simulation_remains_independent(tmp_path):
    store = PaperStore(tmp_path / "paper.db")
    live = _order(store)
    shadow = _order(store, research=True)
    second_shadow = _resting_order(
        store, TARGET, _decision(TICKER + "-SHADOW", limit_price=0.72, contracts=8.0),
        created_at=T0 + timedelta(seconds=1),
    )
    with store.connect() as conn:
        conn.execute("DROP INDEX ux_paper_orders_open_market_side_profile")
        conn.execute("UPDATE paper_orders SET account_id=? WHERE id=?", (RESEARCH_ACCOUNT_ID, shadow))
        conn.execute(
            "UPDATE paper_orders SET market_ticker=?, account_id=? WHERE id=?",
            (TICKER, RESEARCH_ACCOUNT_ID, second_shadow),
        )
    store.apply_maker_trade_batch(TICKER, [_tape()])
    assert [store.paper_order(order)["filled_contracts"] for order in (live, shadow, second_shadow)] == [8.0, 8.0, 8.0]
    with store.connect() as conn:
        counterfactual = dict(conn.execute("SELECT order_id, counterfactual FROM paper_maker_allocations"))
    assert counterfactual == {live: 0, shadow: 1, second_shadow: 1}
    assert json.loads(store.paper_order(shadow)["fill_evidence_json"])["research_shadow"] is True
    assert json.loads(store.paper_order(second_shadow)["fill_evidence_json"])["allocation_scope"] == "legacy_per_order_shadow"


@pytest.mark.parametrize("legacy_shadow", [False, True])
def test_zero_fill_expiry_has_current_scope_and_is_idempotent(tmp_path, legacy_shadow):
    path = tmp_path / "paper.db"
    store = PaperStore(path)
    order_id = _order(store)
    if legacy_shadow:
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET account_id=? WHERE id=?",
                (RESEARCH_ACCOUNT_ID, order_id),
            )
    watermark = (T0 + timedelta(minutes=21)).isoformat()
    assert store.expire_stale_resting_orders(
        now=watermark, reconciled_through_by_ticker={TICKER: watermark},
    ) == 1
    row = dict(store.paper_order(order_id))
    evidence = json.loads(row["fill_evidence_json"])
    assert evidence["model"] == "maker_allocator_price_time_v5"
    assert evidence["execution_model_version"] == EXECUTION_MODEL_VERSION
    assert evidence["allocation_scope"] == (
        "legacy_per_order_shadow" if legacy_shadow else "economic_account"
    )
    assert evidence["counterfactual"] is legacy_shadow
    assert evidence["research_shadow"] is legacy_shadow
    assert evidence["tape_reconciled_through"] == watermark
    assert row["status"] == "PAPER_EXPIRED"
    with store.connect() as conn:
        ledger_count = conn.execute("SELECT COUNT(*) FROM paper_account_ledger").fetchone()[0]
    restarted = PaperStore(path)
    assert restarted.expire_stale_resting_orders(
        now=watermark, reconciled_through_by_ticker={TICKER: watermark},
    ) == 0
    assert dict(restarted.paper_order(order_id)) == row
    with restarted.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_account_ledger").fetchone()[0] == ledger_count
