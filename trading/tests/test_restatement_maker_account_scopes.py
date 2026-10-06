"""Restatement uses the same independent account scenarios as execution."""
import json

from sfo_kalshi_quant.maker_fills import EXECUTION_MODEL_VERSION
from sfo_kalshi_quant.replay import replay_from_database
from test_restatement_exec_v4_replay import (
    T0, TRUTH, _apply, _order, _result, _settle, _store,
)
from datetime import timedelta


def test_v5_each_account_can_verify_the_same_public_tape(tmp_path):
    path = tmp_path / "paper.db"
    store = _store(path)
    live = _order(store, queue_ahead=0)
    research = _order(store, queue_ahead=0, risk_profile="research", placed_at=T0 + timedelta(seconds=1))
    _apply(store, "independent-scenarios", no_price=.72, quantity=5)
    _settle(store)

    assert store.paper_order(live)["filled_contracts"] == 5
    assert store.paper_order(research)["filled_contracts"] == 5
    assert _result(path, live)["verification"] == "VERIFIED"
    assert _result(path, research)["verification"] == "VERIFIED"
    # Research's independently simulated fill cannot increase live readiness.
    assert replay_from_database(path, TRUTH)["verified_decisions"] == 1


def test_known_other_account_corruption_does_not_poison_v5_live_scenario(tmp_path):
    path = tmp_path / "paper.db"
    store = _store(path)
    live = _order(store, queue_ahead=0)
    research = _order(store, queue_ahead=0, risk_profile="research", placed_at=T0 + timedelta(seconds=1))
    _apply(store, "independent-scenarios", no_price=.72, quantity=5)
    _settle(store)
    with store.connect() as conn:
        conn.execute("UPDATE paper_maker_allocations SET fill_quantity=-1 WHERE order_id=?", (research,))
    assert _result(path, live)["verification"] == "VERIFIED"
    assert _result(path, research)["verification"] == "UNVERIFIABLE"


def test_original_v4_generation_is_preserved_and_excluded_from_v5_readiness(tmp_path):
    path = tmp_path / "paper.db"
    store = _store(path)
    historical = _order(store, queue_ahead=0)
    _apply(store, "historical-generation", no_price=.72, quantity=5)
    _settle(store)
    with store.connect() as conn:
        evidence = json.loads(store.paper_order(historical)["fill_evidence_json"])
        evidence["model"] = "maker_allocator_price_time_v4"
        evidence["execution_model_version"] = "exec-v4-2026-07-17"
        evidence.pop("allocation_scope", None)
        conn.execute("UPDATE paper_orders SET execution_model_version='exec-v4-2026-07-17', fill_evidence_json=? WHERE id=?", (json.dumps(evidence), historical))
        conn.execute("UPDATE paper_maker_allocations SET execution_model_version='exec-v4-2026-07-17' WHERE order_id=?", (historical,))
        ledger = conn.execute("SELECT id, details_json, idempotency_key FROM paper_account_ledger WHERE order_id=? AND event_type='ENTRY_FILL'", (historical,)).fetchall()
        for ledger_id, details_json, idempotency_key in ledger:
            details = json.loads(details_json)
            details["execution_model_version"] = "exec-v4-2026-07-17"
            conn.execute("UPDATE paper_account_ledger SET details_json=?, idempotency_key=? WHERE id=?", (json.dumps(details), idempotency_key.replace(EXECUTION_MODEL_VERSION, "exec-v4-2026-07-17"), ledger_id))
    original = dict(store.paper_order(historical))

    assert _result(path, historical)["verification"] == "VERIFIED"
    assert replay_from_database(path, TRUTH)["verified_decisions"] == 0
    assert dict(store.paper_order(historical)) == original
