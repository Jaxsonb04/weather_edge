import gzip
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sfo_kalshi_quant.archive import (
    archive_pending,
    build_features,
    cleanup_local,
    gate_missing_days,
    open_manifest,
)
from sfo_kalshi_quant.db import PaperStore


def _utc_day(days_ago: int) -> str:
    return (datetime.now(UTC).date() - timedelta(days=days_ago)).isoformat()


def _insert_decision(conn: sqlite3.Connection, **overrides) -> int:
    row = {
        "created_at": f"{_utc_day(2)}T10:00:00+00:00",
        "target_date": _utc_day(2),
        "market_ticker": "KXHIGHTSFO-TEST-B68.5",
        "label": "68-69",
        "action": "BUY_NO",
        "side": "NO",
        "approved": 0,
        "signal_approved": 0,
        "probability": 0.7,
        "probability_lcb": 0.6,
        "kelly_fraction": 0.05,
        "recommended_contracts": 0,
        "recommended_spend": 0.0,
        "expected_profit": 0.0,
        "trade_quality_score": 0.5,
        "model_probability": 0.72,
        "market_probability": 0.65,
        "yes_bid": 0.30,
        "yes_ask": 0.36,
        "spread": 0.06,
        "edge": 0.04,
        "edge_lcb": 0.01,
        "fee_per_contract": 0.02,
        "cost_per_contract": 0.66,
        "entry_bid_size": 100,
        "entry_ask_size": 120,
        "strike_type": "between",
        "floor_strike": 68.0,
        "cap_strike": 69.0,
        "market_close_time": f"{_utc_day(2)}T23:00:00+00:00",
        "risk_profile": "live",
        "forecast_predicted_high_f": 68.4,
        "forecast_source_spread_f": 2.1,
        "forecast_lead_hours": 9.0,
        "forecast_method": "emos_wmean",
        "reasons_json": json.dumps(["edge below min"]),
        "diagnostics_json": json.dumps({"ladder": [1, 2, 3]}),
        "prediction_features_json": json.dumps({"regime": "marine"}),
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    cur = conn.execute(
        f"INSERT INTO decision_snapshots ({cols}) VALUES ({marks})",
        tuple(row.values()),
    )
    conn.commit()
    return int(cur.lastrowid)


def _read_archive(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as inp:
        return [json.loads(line) for line in inp]


def _make_store(tmp_path: Path) -> tuple[PaperStore, sqlite3.Connection]:
    store = PaperStore(tmp_path / "paper.db")
    conn = sqlite3.connect(tmp_path / "paper.db")
    return store, conn


def test_export_roundtrip_is_lossless(tmp_path: Path) -> None:
    _, conn = _make_store(tmp_path)
    ids = [
        _insert_decision(conn),
        _insert_decision(conn, approved=1, created_at=f"{_utc_day(2)}T10:05:00+00:00"),
        _insert_decision(conn, created_at=f"{_utc_day(1)}T09:00:00+00:00", target_date=_utc_day(1)),
    ]
    archive_dir = tmp_path / "archive"
    exported = archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)
    assert exported > 0

    day_file = archive_dir / "decision_snapshots" / f"dt={_utc_day(2)}.jsonl.gz"
    records = _read_archive(day_file)
    assert [r["id"] for r in records] == ids[:2]

    db_rows = conn.execute(
        "SELECT * FROM decision_snapshots WHERE id IN (?, ?) ORDER BY id",
        ids[:2],
    ).fetchall()
    columns = [d[0] for d in conn.execute("SELECT * FROM decision_snapshots LIMIT 1").description]
    for record, db_row in zip(records, db_rows):
        assert record == dict(zip(columns, db_row))


def test_archive_pending_is_idempotent(tmp_path: Path) -> None:
    _, conn = _make_store(tmp_path)
    _insert_decision(conn)
    archive_dir = tmp_path / "archive"
    first = archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)
    second = archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)
    assert first > 0
    assert second == 0


def test_gate_blocks_until_every_day_is_archived(tmp_path: Path) -> None:
    _, conn = _make_store(tmp_path)
    _insert_decision(conn)
    archive_dir = tmp_path / "archive"

    missing_before = gate_missing_days(tmp_path / "paper.db", archive_dir)
    assert ("decision_snapshots", _utc_day(2)) in missing_before

    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)
    assert gate_missing_days(tmp_path / "paper.db", archive_dir) == []

    manifest = open_manifest(archive_dir)
    manifest.execute(
        "DELETE FROM archive_files WHERE table_name='decision_snapshots' AND day=?",
        (_utc_day(1),),
    )
    manifest.commit()
    manifest.close()
    assert ("decision_snapshots", _utc_day(1)) in gate_missing_days(
        tmp_path / "paper.db", archive_dir
    )


def test_merge_sources_recover_pruned_rows_by_id(tmp_path: Path) -> None:
    _, conn = _make_store(tmp_path)
    kept = _insert_decision(conn, approved=1)
    pruned = _insert_decision(conn, created_at=f"{_utc_day(2)}T10:10:00+00:00")

    # Backup taken before the prune: holds both rows.
    backup = tmp_path / "backup.db"
    src = sqlite3.connect(tmp_path / "paper.db")
    dst = sqlite3.connect(backup)
    src.backup(dst)
    src.close()
    dst.close()

    # Simulate the prune deleting the rejection from the live DB.
    conn.execute("DELETE FROM decision_snapshots WHERE id = ?", (pruned,))
    conn.commit()

    archive_dir = tmp_path / "archive"
    archive_pending(
        tmp_path / "paper.db", archive_dir, merge_dbs=[backup], log=lambda *_: None
    )
    records = _read_archive(
        archive_dir / "decision_snapshots" / f"dt={_utc_day(2)}.jsonl.gz"
    )
    assert [r["id"] for r in records] == [kept, pruned]


def test_merge_source_missing_columns_yields_null(tmp_path: Path) -> None:
    _, conn = _make_store(tmp_path)
    _insert_decision(conn, approved=1)

    old_schema = tmp_path / "old.db"
    old = sqlite3.connect(old_schema)
    old.execute(
        "CREATE TABLE decision_snapshots ("
        "id INTEGER PRIMARY KEY, created_at TEXT, target_date TEXT, "
        "market_ticker TEXT, side TEXT, approved INTEGER, reasons_json TEXT)"
    )
    old.execute(
        "INSERT INTO decision_snapshots (id, created_at, target_date, market_ticker, side, approved, reasons_json) "
        "VALUES (99991, ?, ?, 'KXHIGHTSFO-OLD-B60.5', 'YES', 0, '[]')",
        (f"{_utc_day(2)}T08:00:00+00:00", _utc_day(2)),
    )
    old.commit()
    old.close()

    archive_dir = tmp_path / "archive"
    archive_pending(
        tmp_path / "paper.db", archive_dir, merge_dbs=[old_schema], log=lambda *_: None
    )
    records = _read_archive(
        archive_dir / "decision_snapshots" / f"dt={_utc_day(2)}.jsonl.gz"
    )
    legacy = [r for r in records if r["id"] == 99991][0]
    assert legacy["market_ticker"] == "KXHIGHTSFO-OLD-B60.5"
    assert legacy["diagnostics_json"] is None
    assert legacy["risk_profile"] is None


def test_features_rollup_entry_labels_and_histogram(tmp_path: Path) -> None:
    _, conn = _make_store(tmp_path)
    day = _utc_day(2)
    first = _insert_decision(
        conn, created_at=f"{day}T10:00:00+00:00", target_date=day,
        approved=0, yes_bid=0.30, yes_ask=0.36,
        reasons_json=json.dumps(["edge below min"]),
    )
    entry = _insert_decision(
        conn, created_at=f"{day}T10:05:00+00:00", target_date=day,
        approved=1, yes_bid=0.32, yes_ask=0.38, cost_per_contract=0.62,
        reasons_json="[]",
    )
    _insert_decision(
        conn, created_at=f"{day}T12:00:00+00:00", target_date=day,
        approved=0, yes_bid=0.20, yes_ask=0.26,
        reasons_json=json.dumps(["edge below min"]),
    )

    weather_db = tmp_path / "weather.db"
    w = sqlite3.connect(weather_db)
    w.execute(
        "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
        "max_temperature_f REAL, is_final INTEGER NOT NULL DEFAULT 1)"
    )
    w.execute(
        "INSERT INTO cli_settlements VALUES ('KSFO', ?, 68.0, 1)", (day,)
    )
    w.commit()
    w.close()

    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)
    features_db = tmp_path / "features.db"
    written = build_features(
        archive_dir, features_db, weather_db, tmp_path / "paper.db",
        log=lambda *_: None,
    )
    assert written == 1

    f = sqlite3.connect(features_db)
    f.row_factory = sqlite3.Row
    row = f.execute("SELECT * FROM market_side_day").fetchone()
    assert row["n_ticks"] == 3
    assert row["approved_ticks"] == 1
    assert row["entry_snapshot_id"] == entry
    assert row["first_yes_bid"] == 0.30
    assert row["last_yes_bid"] == 0.20
    assert row["station_id"] == "KSFO"
    # High 68.0 is inside [68, 69] -> YES resolves; the NO side lost.
    assert row["settlement_high_f"] == 68.0
    assert row["side_won"] == 0
    hist = json.loads(row["reasons_histogram_json"])
    assert hist == {"edge below min": 2}
    # Entry mid 0.35, last pre-close mid 0.23; NO side gained as YES fell.
    assert abs(row["clv_vs_close"] - 0.12) < 1e-9
    assert row["traded"] == 0
    assert first  # silence lint on unused id


def test_features_exclude_booked_high_until_cli_truth_is_final(tmp_path: Path) -> None:
    _, conn = _make_store(tmp_path)
    day = _utc_day(2)
    _insert_decision(
        conn, created_at=f"{day}T10:05:00+00:00", target_date=day,
        approved=1, side="NO", risk_profile="live",
    )
    conn.execute(
        """
        INSERT INTO paper_orders (
            created_at, target_date, market_ticker, label, action, side,
            contracts, yes_ask, fee_per_contract, cost_per_contract,
            probability, probability_lcb, edge, edge_lcb, expected_profit,
            status, reasons_json, settled_at, settlement_high_f, realized_pnl
        ) VALUES (?, ?, 'KXHIGHTSFO-TEST-B68.5', '68-69', 'BUY_NO', 'NO',
                  1, .36, .02, .66, .7, .6, .04, .01, 0,
                  'PAPER_SETTLED', '[]', ?, 69, -.66)
        """,
        (f"{day}T10:05:00+00:00", day, f"{day}T23:30:00+00:00"),
    )
    order_id = conn.execute("SELECT id FROM paper_orders").fetchone()[0]
    conn.execute(
        "INSERT INTO paper_settlement_verifications "
        "(order_id, checked_at, market_ticker, target_date, booked_high_f, "
        "final_high_f, verification_status) VALUES (?, ?, ?, ?, 69, NULL, 'MISSING_FINAL')",
        (order_id, f"{day}T23:40:00+00:00", "KXHIGHTSFO-TEST-B68.5", day),
    )
    conn.commit()

    weather_db = tmp_path / "weather.db"
    with sqlite3.connect(weather_db) as w:
        w.execute(
            "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
            "max_temperature_f REAL, is_final INTEGER NOT NULL DEFAULT 1)"
        )
        w.execute(
            "INSERT INTO cli_settlements VALUES ('KSFO', ?, 70, 0)", (day,)
        )
    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)
    features_db = tmp_path / "features.db"

    build_features(
        archive_dir, features_db, weather_db, tmp_path / "paper.db",
        log=lambda *_: None,
    )
    with sqlite3.connect(features_db) as f:
        assert f.execute(
            "SELECT settlement_high_f FROM market_side_day"
        ).fetchone()[0] is None

    with sqlite3.connect(weather_db) as w:
        w.execute("UPDATE cli_settlements SET is_final=1")
    build_features(
        archive_dir, features_db, weather_db, tmp_path / "paper.db",
        log=lambda *_: None,
    )
    with sqlite3.connect(features_db) as f:
        assert f.execute(
            "SELECT settlement_high_f FROM market_side_day"
        ).fetchone()[0] == 70.0


def test_cleanup_deletes_only_uploaded_files(tmp_path: Path) -> None:
    _, conn = _make_store(tmp_path)
    old_day = (datetime.now(UTC).date() - timedelta(days=40)).isoformat()
    _insert_decision(conn, created_at=f"{old_day}T10:00:00+00:00", target_date=old_day)
    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)

    day_file = archive_dir / "decision_snapshots" / f"dt={old_day}.jsonl.gz"
    assert day_file.exists()

    # Not uploaded -> cleanup must refuse to delete it.
    assert cleanup_local(archive_dir, keep_days=30, log=lambda *_: None) == 0
    assert day_file.exists()

    manifest = open_manifest(archive_dir)
    manifest.execute(
        "UPDATE archive_files SET uploaded_at='2026-07-10T00:00:00+00:00', "
        "upload_target='s3://test/x' WHERE table_name='decision_snapshots' AND day=?",
        (old_day,),
    )
    manifest.commit()
    manifest.close()

    assert cleanup_local(archive_dir, keep_days=30, log=lambda *_: None) == 1
    assert not day_file.exists()
