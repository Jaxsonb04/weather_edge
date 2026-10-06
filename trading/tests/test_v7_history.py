"""A new research era must preserve the losing as well as winning evidence."""
from dataclasses import asdict
from pathlib import Path

from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.profile_identity import published_profile_key
from sfo_kalshi_quant.research_policy import TARGET_POLICY, TARGET_POLICY_V6
from sfo_kalshi_quant.store import schema


def test_v7_changes_identity_without_increasing_risk_or_rewriting_v6():
    assert TARGET_POLICY.policy_version == "research-target-roi-v7"
    assert TARGET_POLICY.account_id != TARGET_POLICY_V6.account_id
    assert TARGET_POLICY_V6.policy_fingerprint == "0fd9cc8ebf877a653806fe1a"
    old, new = asdict(TARGET_POLICY_V6), asdict(TARGET_POLICY)
    for key in ("account_id", "policy_version"):
        old.pop(key)
        new.pop(key)
    assert old == new
    assert published_profile_key(
        "research", account_id=TARGET_POLICY_V6.account_id,
        research_sleeve="target", research_policy_version=TARGET_POLICY_V6.policy_version,
        policy_fingerprint=TARGET_POLICY_V6.policy_fingerprint,
    ) == "research-target-v6"


def test_v7_migration_preserves_v6_ledger_and_order_bytes(tmp_path: Path, monkeypatch):
    db = tmp_path / "paper.db"
    with monkeypatch.context() as m:
        m.setattr(schema, "TARGET_POLICY", TARGET_POLICY_V6)
        m.setattr(schema, "ALL_RESEARCH_POLICIES", tuple(
            p for p in schema.ALL_RESEARCH_POLICIES if p.account_id != TARGET_POLICY.account_id
        ))
        old = PaperStore(db)
    with old.connect() as conn:
        conn.execute(
            "INSERT INTO paper_account_ledger (created_at, account_id, event_type, amount, idempotency_key) "
            "VALUES ('2026-10-01T00:00:00+00:00', ?, 'REALIZED_PNL', -17.5, 'v6-loss-preserved')",
            (TARGET_POLICY_V6.account_id,),
        )
        conn.execute(
            "INSERT INTO paper_orders (created_at,target_date,market_ticker,label,action,side,contracts,yes_ask,"
            "fee_per_contract,cost_per_contract,probability,probability_lcb,edge,edge_lcb,"
            "trade_quality_score,expected_profit,status,reasons_json,account_id,risk_profile,"
            "research_sleeve,research_policy_version,policy_fingerprint) "
            "VALUES ('2026-10-01T00:00:00+00:00','2026-10-02','KXHIGHTSFO-V6','test','BUY_NO','NO',"
            "2,0.5,0,0.5,0.6,0.55,0.1,0.05,50,0.2,'PAPER_FILLED','[]',?,'research','target',?,?)",
            (TARGET_POLICY_V6.account_id, TARGET_POLICY_V6.policy_version, TARGET_POLICY_V6.policy_fingerprint),
        )
        # This models an already initialized production order, including the
        # lifecycle fields that legacy migration normally fills once.
        conn.execute("UPDATE paper_orders SET requested_contracts=2, filled_contracts=2, remaining_contracts=0, queue_remaining=0, reserved_cost=0, execution_model_version='exec-v4-2026-07-17'")
        before_orders = conn.execute("SELECT * FROM paper_orders").fetchall()
        before_ledger = conn.execute("SELECT * FROM paper_account_ledger WHERE account_id=?", (TARGET_POLICY_V6.account_id,)).fetchall()
    PaperStore(db)
    reopened = PaperStore(db)
    with reopened.connect() as conn:
        assert conn.execute("SELECT * FROM paper_orders").fetchall() == before_orders
        assert conn.execute("SELECT * FROM paper_account_ledger WHERE account_id=?", (TARGET_POLICY_V6.account_id,)).fetchall() == before_ledger
        assert conn.execute("SELECT status FROM paper_accounts WHERE account_id=?", (TARGET_POLICY_V6.account_id,)).fetchone()[0] == "ARCHIVED"
        assert conn.execute("SELECT initial_capital,opening_cash,status FROM paper_accounts WHERE account_id=?", (TARGET_POLICY.account_id,)).fetchone() == (1000.0,1000.0,"ACTIVE")
        assert conn.execute("SELECT COUNT(*) FROM paper_account_ledger WHERE account_id=? AND event_type='OPENING_CASH'", (TARGET_POLICY.account_id,)).fetchone()[0] == 1


def test_nightly_archive_keeps_maker_evidence_and_frozen_daily_goals():
    from sfo_kalshi_quant.archive import FULL_TABLES
    assert {"maker_volume_claims", "paper_maker_allocations", "research_daily_goals"} <= set(FULL_TABLES)


def test_archive_exports_v7_evidence_rows_losslessly(tmp_path: Path):
    import gzip
    import json
    import sqlite3
    from sfo_kalshi_quant.archive import archive_pending, open_manifest

    db = tmp_path / "evidence.db"
    tables = ("research_daily_goals", "maker_volume_claims", "paper_maker_allocations",
              "research_plan_snapshots", "research_experiments", "research_evidence",
              "google_challenger_snapshots", "paper_settlement_verifications",
              "market_day_settlements", "ladder_bin_outcomes", "kalshi_market_resolutions",
              "paper_settlement_exchange_checks")
    # Standalone export must preserve all columns, independent of the runtime
    # schema version (including negative P&L and archived-era markers).
    with sqlite3.connect(db) as conn:
        for table in tables:
            conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, era TEXT, pnl REAL)")
            conn.execute(f"INSERT INTO {table} VALUES (1, 'v6', -17.5)")
    archive_dir = tmp_path / "archive"
    assert archive_pending(db, archive_dir, log=lambda *_: None) == len(tables)
    with open_manifest(archive_dir) as manifest:
        paths = dict(manifest.execute("SELECT table_name,path FROM archive_files WHERE kind='full'"))
    assert set(paths) == set(tables)
    for table in tables:
        with gzip.open(archive_dir / paths[table], "rt") as handle:
            assert [json.loads(line) for line in handle] == [{"id": 1, "era": "v6", "pnl": -17.5}]
    assert archive_pending(db, archive_dir, log=lambda *_: None) == 0
