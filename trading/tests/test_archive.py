import gzip
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import sfo_kalshi_quant.archive as archive_module

from sfo_kalshi_quant.archive import (
    archive_pending,
    build_features,
    cleanup_local,
    gate_missing_days,
    open_manifest,
)
from sfo_kalshi_quant.db import PaperStore
from test_scan_context_normalization import _decision, _record


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


def test_scan_context_is_archived_gated_and_recoverable_with_decisions(tmp_path: Path) -> None:
    store, conn = _make_store(tmp_path)
    _record(store, [_decision(1)])
    day = _utc_day(3)
    conn.execute("UPDATE decision_snapshots SET created_at=?", (f"{day}T10:00:00+00:00",))
    conn.execute("UPDATE scan_context_snapshots SET created_at=?", (f"{day}T10:00:00+00:00",))
    conn.commit()

    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)

    decisions = _read_archive(
        archive_dir / "decision_snapshots" / f"dt={day}.jsonl.gz"
    )
    contexts = _read_archive(
        archive_dir / "scan_context_snapshots" / f"dt={day}.jsonl.gz"
    )
    assert decisions[0]["scan_context_id"] == contexts[0]["id"]
    assert gate_missing_days(tmp_path / "paper.db", archive_dir) == []

    backup = tmp_path / "backup.db"
    with sqlite3.connect(tmp_path / "paper.db") as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
    pruned = store.prune_decision_snapshots(full_days=1, dedup_days=2)
    assert pruned["dropped"] == 1
    assert pruned["contexts_dropped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM decision_snapshots").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM scan_context_snapshots").fetchone()[0] == 0
    recovery_dir = tmp_path / "recovery"
    archive_pending(
        tmp_path / "paper.db", recovery_dir, merge_dbs=[backup], log=lambda *_: None
    )
    recovered_decisions = _read_archive(
        recovery_dir / "decision_snapshots" / f"dt={day}.jsonl.gz"
    )
    recovered_contexts = _read_archive(
        recovery_dir / "scan_context_snapshots" / f"dt={day}.jsonl.gz"
    )
    assert recovered_decisions[0]["scan_context_id"] == recovered_contexts[0]["id"]


def test_gate_rejects_surviving_decision_with_missing_context_parent(tmp_path: Path) -> None:
    store, conn = _make_store(tmp_path)
    _record(store, [_decision(1)])
    day = _utc_day(2)
    conn.execute("UPDATE decision_snapshots SET created_at=?", (f"{day}T10:00:00+00:00",))
    conn.execute("UPDATE scan_context_snapshots SET created_at=?", (f"{day}T10:00:00+00:00",))
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM scan_context_snapshots")
    conn.commit()

    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)

    assert ("scan_context_snapshots", day) in gate_missing_days(
        tmp_path / "paper.db", archive_dir
    )


def test_gate_rejects_archived_decision_when_context_manifest_is_missing(
    tmp_path: Path,
) -> None:
    store, conn = _make_store(tmp_path)
    _record(store, [_decision(1)])
    day = _utc_day(2)
    conn.execute("UPDATE decision_snapshots SET created_at=?", (f"{day}T10:00:00+00:00",))
    conn.execute("UPDATE scan_context_snapshots SET created_at=?", (f"{day}T10:00:00+00:00",))
    conn.commit()
    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM decision_snapshots")
    conn.execute("DELETE FROM scan_context_snapshots")
    conn.commit()
    with sqlite3.connect(archive_dir / "manifest.db") as manifest:
        manifest.execute(
            "DELETE FROM archive_files WHERE table_name='scan_context_snapshots' "
            "AND day=? AND kind='day'",
            (day,),
        )

    assert ("scan_context_snapshots", day) in gate_missing_days(
        tmp_path / "paper.db", archive_dir
    )


def test_restore_archive_inserts_context_parent_first_and_is_fk_clean(
    tmp_path: Path,
) -> None:
    restore = getattr(archive_module, "restore_archive_days", None)
    assert callable(restore), "restore_archive_days is required"
    source, conn = _make_store(tmp_path)
    _record(source, [_decision(1)])
    day = _utc_day(2)
    conn.execute("UPDATE decision_snapshots SET created_at=?", (f"{day}T10:00:00+00:00",))
    conn.execute("UPDATE scan_context_snapshots SET created_at=?", (f"{day}T10:00:00+00:00",))
    conn.commit()
    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)

    restored_db = tmp_path / "restored.db"
    result = restore(archive_dir, restored_db, days=[day], log=lambda *_: None)

    with sqlite3.connect(restored_db) as restored:
        restored.execute("PRAGMA foreign_keys=ON")
        context_id = restored.execute(
            "SELECT id FROM scan_context_snapshots"
        ).fetchone()[0]
        decision_context_id = restored.execute(
            "SELECT scan_context_id FROM decision_snapshots"
        ).fetchone()[0]
        violations = restored.execute("PRAGMA foreign_key_check").fetchall()
    assert result["scan_context_snapshots"] == 1
    assert result["decision_snapshots"] == 1
    assert decision_context_id == context_id
    assert violations == []


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


def test_archive_id_floor_does_not_omit_lower_id_from_later_created_day(tmp_path: Path) -> None:
    _, conn = _make_store(tmp_path)
    older = _utc_day(2)
    newer = _utc_day(1)
    first_old = _insert_decision(
        conn, created_at=f"{older}T08:00:00+00:00", target_date=older
    )
    newer_id = _insert_decision(
        conn, created_at=f"{newer}T08:00:00+00:00", target_date=newer
    )
    backdated_high_id = _insert_decision(
        conn, created_at=f"{older}T09:00:00+00:00", target_date=older
    )
    assert first_old < newer_id < backdated_high_id

    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)

    newer_rows = _read_archive(
        archive_dir / "decision_snapshots" / f"dt={newer}.jsonl.gz"
    )
    assert [row["id"] for row in newer_rows] == [newer_id]
    assert gate_missing_days(tmp_path / "paper.db", archive_dir) == []


def test_gate_blocks_when_backdated_row_outgrows_manifest_coverage(tmp_path: Path) -> None:
    _, conn = _make_store(tmp_path)
    day = _utc_day(2)
    _insert_decision(conn, created_at=f"{day}T08:00:00+00:00", target_date=day)
    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)

    _insert_decision(conn, created_at=f"{day}T09:00:00+00:00", target_date=day)

    assert ("decision_snapshots", day) in gate_missing_days(
        tmp_path / "paper.db", archive_dir
    )


def test_gate_accepts_archive_superset_after_intentional_live_prune(tmp_path: Path) -> None:
    _, conn = _make_store(tmp_path)
    day = _utc_day(2)
    kept = _insert_decision(conn, created_at=f"{day}T08:00:00+00:00", target_date=day)
    pruned = _insert_decision(conn, created_at=f"{day}T09:00:00+00:00", target_date=day)
    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)

    conn.execute("DELETE FROM decision_snapshots WHERE id = ?", (pruned,))
    conn.commit()

    assert kept != pruned
    assert gate_missing_days(tmp_path / "paper.db", archive_dir) == []


def test_gate_accepts_merge_archive_superset_when_all_live_ids_are_covered(tmp_path: Path) -> None:
    _, conn = _make_store(tmp_path)
    day = _utc_day(2)
    live_id = _insert_decision(conn, created_at=f"{day}T08:00:00+00:00", target_date=day)
    backup_only_id = _insert_decision(
        conn, created_at=f"{day}T09:00:00+00:00", target_date=day
    )
    backup = tmp_path / "backup.db"
    with sqlite3.connect(tmp_path / "paper.db") as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
    conn.execute("DELETE FROM decision_snapshots WHERE id = ?", (backup_only_id,))
    conn.commit()

    archive_dir = tmp_path / "archive"
    archive_pending(
        tmp_path / "paper.db",
        archive_dir,
        merge_dbs=[backup],
        log=lambda *_: None,
    )

    assert live_id != backup_only_id
    assert gate_missing_days(tmp_path / "paper.db", archive_dir) == []


def test_gate_rejects_manifest_without_exact_id_coverage_metadata(tmp_path: Path) -> None:
    _, conn = _make_store(tmp_path)
    _insert_decision(conn)
    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)
    with sqlite3.connect(archive_dir / "manifest.db") as manifest:
        manifest.execute("UPDATE archive_files SET id_coverage_json = NULL")

    with pytest.raises(RuntimeError, match="re-export"):
        gate_missing_days(tmp_path / "paper.db", archive_dir)


def test_scheduled_archive_backfills_preupgrade_manifest_from_original_file(
    tmp_path: Path,
) -> None:
    _, conn = _make_store(tmp_path)
    day = _utc_day(2)
    ids = [
        _insert_decision(conn, created_at=f"{day}T08:00:00+00:00", target_date=day),
        _insert_decision(conn, created_at=f"{day}T09:00:00+00:00", target_date=day),
    ]
    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)
    path = archive_dir / "decision_snapshots" / f"dt={day}.jsonl.gz"
    original_bytes = path.read_bytes()
    original_hash = hashlib.sha256(original_bytes).hexdigest()
    with sqlite3.connect(archive_dir / "manifest.db") as manifest:
        manifest.execute(
            "UPDATE archive_files SET id_coverage_json=NULL "
            "WHERE table_name='decision_snapshots' AND day=? AND kind='day'",
            (day,),
        )

    assert archive_pending(
        tmp_path / "paper.db", archive_dir, log=lambda *_: None
    ) == 0

    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash
    assert path.read_bytes() == original_bytes
    with sqlite3.connect(archive_dir / "manifest.db") as manifest:
        rows, coverage = manifest.execute(
            "SELECT rows, id_coverage_json FROM archive_files "
            "WHERE table_name='decision_snapshots' AND day=? AND kind='day'",
            (day,),
        ).fetchone()
    assert rows == len(ids)
    assert json.loads(coverage) == [[ids[0], ids[-1]]]
    assert gate_missing_days(tmp_path / "paper.db", archive_dir) == []


def test_manifest_coverage_backfill_refuses_corrupted_original_file(tmp_path: Path) -> None:
    _, conn = _make_store(tmp_path)
    day = _utc_day(2)
    _insert_decision(conn, created_at=f"{day}T08:00:00+00:00", target_date=day)
    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)
    path = archive_dir / "decision_snapshots" / f"dt={day}.jsonl.gz"
    with sqlite3.connect(archive_dir / "manifest.db") as manifest:
        manifest.execute(
            "UPDATE archive_files SET id_coverage_json=NULL "
            "WHERE table_name='decision_snapshots' AND day=? AND kind='day'",
            (day,),
        )
    with gzip.open(path, "wt", encoding="utf-8") as out:
        out.write(json.dumps({"id": 999999}) + "\n")

    with pytest.raises(RuntimeError, match="recovery|corrupt|mismatch"):
        archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)


def test_manifest_coverage_backfill_fetches_verified_uploaded_copy(tmp_path: Path) -> None:
    _, conn = _make_store(tmp_path)
    day = _utc_day(2)
    row_id = _insert_decision(
        conn, created_at=f"{day}T08:00:00+00:00", target_date=day
    )
    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)
    path = archive_dir / "decision_snapshots" / f"dt={day}.jsonl.gz"
    uploaded_bytes = path.read_bytes()
    with sqlite3.connect(archive_dir / "manifest.db") as manifest:
        manifest.execute(
            "UPDATE archive_files SET id_coverage_json=NULL, "
            "upload_target='s3://verified/archive.jsonl.gz' "
            "WHERE table_name='decision_snapshots' AND day=? AND kind='day'",
            (day,),
        )
    path.unlink()

    def fake_aws(args: list[str]) -> SimpleNamespace:
        assert args[:3] == ["s3", "cp", "s3://verified/archive.jsonl.gz"]
        Path(args[3]).write_bytes(uploaded_bytes)
        return SimpleNamespace(returncode=0, stderr="")

    with patch("sfo_kalshi_quant.archive._aws", side_effect=fake_aws) as aws:
        assert archive_pending(
            tmp_path / "paper.db", archive_dir, log=lambda *_: None
        ) == 0

    aws.assert_called_once()
    assert not path.exists()
    with sqlite3.connect(archive_dir / "manifest.db") as manifest:
        coverage = manifest.execute(
            "SELECT id_coverage_json FROM archive_files "
            "WHERE table_name='decision_snapshots' AND day=? AND kind='day'",
            (day,),
        ).fetchone()[0]
    assert json.loads(coverage) == [[row_id, row_id]]


def test_context_ref_backfill_fetches_verified_uploaded_decision_copy(
    tmp_path: Path,
) -> None:
    store, conn = _make_store(tmp_path)
    _record(store, [_decision(1)])
    day = _utc_day(2)
    conn.execute("UPDATE decision_snapshots SET created_at=?", (f"{day}T08:00:00+00:00",))
    conn.execute("UPDATE scan_context_snapshots SET created_at=?", (f"{day}T08:00:00+00:00",))
    conn.commit()
    context_id = conn.execute("SELECT id FROM scan_context_snapshots").fetchone()[0]
    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)
    path = archive_dir / "decision_snapshots" / f"dt={day}.jsonl.gz"
    uploaded_bytes = path.read_bytes()
    with sqlite3.connect(archive_dir / "manifest.db") as manifest:
        manifest.execute(
            "UPDATE archive_files SET context_ref_coverage_json=NULL, "
            "upload_target='s3://verified/decision.jsonl.gz' "
            "WHERE table_name='decision_snapshots' AND day=? AND kind='day'",
            (day,),
        )
    path.unlink()

    def fake_aws(args: list[str]) -> SimpleNamespace:
        assert args[:3] == ["s3", "cp", "s3://verified/decision.jsonl.gz"]
        Path(args[3]).write_bytes(uploaded_bytes)
        return SimpleNamespace(returncode=0, stderr="")

    with patch("sfo_kalshi_quant.archive._aws", side_effect=fake_aws) as aws:
        assert archive_pending(
            tmp_path / "paper.db", archive_dir, log=lambda *_: None
        ) == 0

    aws.assert_called_once()
    assert not path.exists()
    with sqlite3.connect(archive_dir / "manifest.db") as manifest:
        coverage = manifest.execute(
            "SELECT context_ref_coverage_json FROM archive_files "
            "WHERE table_name='decision_snapshots' AND day=? AND kind='day'",
            (day,),
        ).fetchone()[0]
    assert json.loads(coverage) == [[context_id, context_id]]


def test_manifest_coverage_backfill_requires_one_time_original_restore(
    tmp_path: Path,
) -> None:
    _, conn = _make_store(tmp_path)
    day = _utc_day(2)
    _insert_decision(conn, created_at=f"{day}T08:00:00+00:00", target_date=day)
    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)
    path = archive_dir / "decision_snapshots" / f"dt={day}.jsonl.gz"
    with sqlite3.connect(archive_dir / "manifest.db") as manifest:
        manifest.execute(
            "UPDATE archive_files SET id_coverage_json=NULL, upload_target=NULL "
            "WHERE table_name='decision_snapshots' AND day=? AND kind='day'",
            (day,),
        )
    path.unlink()

    with pytest.raises(RuntimeError, match="Restore it once|verified upload_target"):
        archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)


def test_manifest_coverage_backfill_refuses_manifest_endpoint_mismatch(
    tmp_path: Path,
) -> None:
    _, conn = _make_store(tmp_path)
    day = _utc_day(2)
    row_id = _insert_decision(
        conn, created_at=f"{day}T08:00:00+00:00", target_date=day
    )
    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)
    with sqlite3.connect(archive_dir / "manifest.db") as manifest:
        manifest.execute(
            "UPDATE archive_files SET id_coverage_json=NULL, max_id=? "
            "WHERE table_name='decision_snapshots' AND day=? AND kind='day'",
            (row_id + 1, day),
        )

    with pytest.raises(RuntimeError, match="invalid ID coverage|re-export"):
        archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)

    with sqlite3.connect(archive_dir / "manifest.db") as manifest:
        assert manifest.execute(
            "SELECT id_coverage_json FROM archive_files "
            "WHERE table_name='decision_snapshots' AND day=? AND kind='day'",
            (day,),
        ).fetchone()[0] is None


def test_gate_rejects_widened_id_ranges_not_matching_manifest_count_and_endpoints(
    tmp_path: Path,
) -> None:
    _, conn = _make_store(tmp_path)
    day = _utc_day(2)
    row_id = _insert_decision(
        conn, created_at=f"{day}T08:00:00+00:00", target_date=day
    )
    archive_dir = tmp_path / "archive"
    archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)
    with sqlite3.connect(archive_dir / "manifest.db") as manifest:
        manifest.execute(
            "UPDATE archive_files SET id_coverage_json=? "
            "WHERE table_name='decision_snapshots' AND day=? AND kind='day'",
            (json.dumps([[row_id, row_id + 10]]), day),
        )

    with pytest.raises(RuntimeError, match="invalid ID coverage|re-export"):
        gate_missing_days(tmp_path / "paper.db", archive_dir)


def test_gate_rejects_legacy_manifest_without_row_counts_with_reexport_guidance(
    tmp_path: Path,
) -> None:
    _, conn = _make_store(tmp_path)
    _insert_decision(conn)
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    with sqlite3.connect(archive_dir / "manifest.db") as manifest:
        manifest.execute(
            "CREATE TABLE archive_files ("
            "table_name TEXT, day TEXT, kind TEXT, path TEXT, "
            "PRIMARY KEY (table_name, day, kind))"
        )
        manifest.execute(
            "INSERT INTO archive_files VALUES "
            "('decision_snapshots', ?, 'day', 'legacy.jsonl.gz')",
            (_utc_day(2),),
        )

    with pytest.raises(RuntimeError, match="re-export"):
        gate_missing_days(tmp_path / "paper.db", archive_dir)


def test_scheduled_archive_rejects_unverifiable_legacy_manifest_with_reexport_guidance(
    tmp_path: Path,
) -> None:
    _, conn = _make_store(tmp_path)
    day = _utc_day(2)
    _insert_decision(conn, created_at=f"{day}T08:00:00+00:00", target_date=day)
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    with sqlite3.connect(archive_dir / "manifest.db") as manifest:
        manifest.execute(
            "CREATE TABLE archive_files ("
            "table_name TEXT, day TEXT, kind TEXT, path TEXT, "
            "PRIMARY KEY (table_name, day, kind))"
        )
        manifest.execute(
            "INSERT INTO archive_files VALUES (?, ?, 'day', ?)",
            (
                "decision_snapshots",
                day,
                f"decision_snapshots/dt={day}.jsonl.gz",
            ),
        )

    with pytest.raises(RuntimeError, match="re-export|restore"):
        archive_pending(tmp_path / "paper.db", archive_dir, log=lambda *_: None)

    with sqlite3.connect(archive_dir / "manifest.db") as manifest:
        columns = {
            str(row[1]) for row in manifest.execute("PRAGMA table_info(archive_files)")
        }
    assert {"rows", "sha256", "min_id", "max_id", "id_coverage_json"} <= columns


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


def test_gate_accepts_legacy_decision_schema_without_context_column(
    tmp_path: Path,
) -> None:
    day = _utc_day(2)
    legacy_db = tmp_path / "legacy.db"
    with sqlite3.connect(legacy_db) as conn:
        conn.execute(
            "CREATE TABLE decision_snapshots (id INTEGER PRIMARY KEY, "
            "created_at TEXT, target_date TEXT, market_ticker TEXT)"
        )
        conn.execute(
            "INSERT INTO decision_snapshots VALUES (1, ?, ?, 'LEGACY')",
            (f"{day}T10:00:00+00:00", day),
        )
    archive_dir = tmp_path / "archive"
    archive_pending(
        legacy_db, archive_dir, include_full=False, log=lambda *_: None
    )

    assert gate_missing_days(legacy_db, archive_dir) == []


def test_merge_exports_table_missing_from_old_main_schema(tmp_path: Path) -> None:
    main = tmp_path / "old-main.db"
    with sqlite3.connect(main):
        pass
    backup_store = PaperStore(tmp_path / "normalized-backup.db")
    _record(backup_store, [_decision(1)])
    day = _utc_day(2)
    with backup_store.connect() as conn:
        conn.execute(
            "UPDATE decision_snapshots SET created_at=?",
            (f"{day}T10:00:00+00:00",),
        )
        conn.execute(
            "UPDATE scan_context_snapshots SET created_at=?",
            (f"{day}T10:00:00+00:00",),
        )

    archive_dir = tmp_path / "archive"
    archive_pending(
        main,
        archive_dir,
        merge_dbs=[tmp_path / "normalized-backup.db"],
        include_full=False,
        log=lambda *_: None,
    )

    decisions = _read_archive(
        archive_dir / "decision_snapshots" / f"dt={day}.jsonl.gz"
    )
    contexts = _read_archive(
        archive_dir / "scan_context_snapshots" / f"dt={day}.jsonl.gz"
    )
    assert decisions[0]["scan_context_id"] == contexts[0]["id"]
    assert contexts[0]["schema_version"] == 1


def test_merge_exports_ordered_union_of_old_main_and_new_backup_columns(
    tmp_path: Path,
) -> None:
    day = _utc_day(2)
    main = tmp_path / "old-main.db"
    with sqlite3.connect(main) as conn:
        conn.execute(
            "CREATE TABLE decision_snapshots (id INTEGER PRIMARY KEY, "
            "created_at TEXT, target_date TEXT, market_ticker TEXT)"
        )
        conn.execute(
            "INSERT INTO decision_snapshots VALUES "
            "(1, ?, ?, 'OLD-MAIN')",
            (f"{day}T09:00:00+00:00", day),
        )

    backup_store = PaperStore(tmp_path / "new-backup.db")
    _record(backup_store, [_decision(1)])
    with sqlite3.connect(tmp_path / "new-backup.db") as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("UPDATE scan_context_snapshots SET id=77, created_at=?", (f"{day}T10:00:00+00:00",))
        conn.execute(
            "UPDATE decision_snapshots SET id=999, scan_context_id=77, created_at=?",
            (f"{day}T10:00:00+00:00",),
        )

    archive_dir = tmp_path / "archive"
    archive_pending(
        main,
        archive_dir,
        merge_dbs=[tmp_path / "new-backup.db"],
        include_full=False,
        log=lambda *_: None,
    )

    rows = _read_archive(
        archive_dir / "decision_snapshots" / f"dt={day}.jsonl.gz"
    )
    assert [row["id"] for row in rows] == [1, 999]
    assert rows[0]["scan_context_id"] is None
    assert rows[0]["diagnostics_json"] is None
    assert rows[1]["scan_context_id"] == 77
    assert json.loads(rows[1]["diagnostics_json"])["schema_version"] == 2


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
