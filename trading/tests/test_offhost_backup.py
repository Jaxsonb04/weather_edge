"""Space-safe backup keeps independent recovery proof and frozen lineage."""
from __future__ import annotations

import gzip
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest


HELPER = Path(__file__).resolve().parents[1] / "deploy/aws/backup_paper_db_offhost.py"
spec = importlib.util.spec_from_file_location("offhost_backup", HELPER)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def database(path, *, orphan=False):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
        conn.execute("INSERT INTO parent VALUES (1)")
        conn.execute("INSERT INTO child VALUES (?)", (2 if orphan else 1,))
    return path


def compressed(tmp_path, *, orphan=False):
    source = database(tmp_path / "source.db", orphan=orphan)
    archive = tmp_path / "backup.gz"
    identity = backup.compress_snapshot(source, archive, 1024**2)
    return source, archive, identity


def test_compressed_snapshot_restores_all_rows_without_changing_source(tmp_path):
    source, archive, identity = compressed(tmp_path)
    original = source.read_bytes()
    restored = tmp_path / "restored.db"
    backup.verify_archive(archive, restored, identity)
    assert restored.read_bytes() == original
    assert source.read_bytes() == original
    assert identity["raw_sha256"] == hashlib.sha256(original).hexdigest()
    with sqlite3.connect(restored) as conn:
        assert conn.execute("SELECT parent_id FROM child").fetchall() == [(1,)]


def test_compression_hard_cap_never_promotes_an_oversized_archive(tmp_path):
    source = database(tmp_path / "source.db")
    with pytest.raises(ValueError, match="compressed backup exceeds"):
        backup.compress_snapshot(source, tmp_path / "backup.gz", 32)


def test_nonempty_wal_refuses_physical_snapshot_before_compression(tmp_path):
    source = database(tmp_path / "source.db")
    source.with_name(source.name + "-wal").write_bytes(b"uncheckpointed")
    with pytest.raises(ValueError, match="WAL"):
        backup.compress_snapshot(source, tmp_path / "backup.gz", 1024**2)


def test_lineage_rejects_same_size_database_write(tmp_path):
    source, _, identity = compressed(tmp_path)
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE parent SET id=3")
    with pytest.raises(ValueError, match="lineage"):
        backup.assert_identity(source, identity["source_identity"])


def test_lineage_rejects_replaced_database_and_hot_journal(tmp_path):
    source, _, identity = compressed(tmp_path)
    replacement = source.with_suffix(".new")
    replacement.write_bytes(source.read_bytes())
    replacement.replace(source)
    with pytest.raises(ValueError, match="lineage"):
        backup.assert_identity(source, identity["source_identity"])
    source.with_name(source.name + "-journal").write_bytes(b"active")
    with pytest.raises(ValueError, match="journal"):
        backup.source_identity(source)


def test_changed_download_fails_before_sqlite_validation(tmp_path):
    _, archive, identity = compressed(tmp_path)
    archive.write_bytes(archive.read_bytes()[:-10])
    with pytest.raises(ValueError, match="compressed checksum"):
        backup.verify_archive(archive, tmp_path / "restored.db", identity)


def test_checksum_matching_nondatabase_cannot_pass_integrity(tmp_path):
    archive = tmp_path / "backup.gz"
    raw = b"not sqlite" * 100
    archive.write_bytes(gzip.compress(raw))
    identity = {"raw_sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": len(raw),
                "compressed_sha256": backup.file_hash(archive), "compressed_bytes": archive.stat().st_size}
    with pytest.raises(sqlite3.DatabaseError):
        backup.verify_archive(archive, tmp_path / "restored.db", identity)


def test_valid_integrity_with_foreign_key_violation_cannot_pass(tmp_path):
    _, archive, identity = compressed(tmp_path, orphan=True)
    with pytest.raises(ValueError, match="foreign_key_check"):
        backup.verify_archive(archive, tmp_path / "restored.db", identity)


def policy(tmp_path):
    evidence = tmp_path / "cost-evidence.json"
    evidence.write_text(json.dumps({"audit": "fixture"}))
    return {"verified_month": "2026-10", "verified_free_egress_bytes": 4 * 1024**3,
            "monthly_egress_budget_bytes": 4 * 1024**3, "max_compressed_bytes": 1024**3,
            "no_incremental_spend_verified": True, "cost_evidence_file": str(evidence),
            "cost_evidence_sha256": backup.file_hash(evidence)}


@pytest.mark.parametrize("change", [
    {"verified_month": "2026-09"}, {"no_incremental_spend_verified": False},
    {"verified_free_egress_bytes": 1}, {"max_compressed_bytes": True},
    {"max_compressed_bytes": 9 * 1024**3}, {"cost_evidence_sha256": "0" * 64},
])
def test_policy_refuses_unverified_cost_stale_month_and_insufficient_allowance(tmp_path, change):
    value = {**policy(tmp_path), **change}
    with pytest.raises(ValueError):
        backup.validate_policy(value, "2026-10")


def test_failed_attempts_keep_atomic_worst_case_monthly_reservations(tmp_path):
    value = policy(tmp_path)
    maximum = backup.validate_policy(value, "2026-10")
    receipt = tmp_path / "egress.json"
    backup.reserve_egress(receipt, value, "2026-10", maximum)
    backup.reserve_egress(receipt, value, "2026-10", maximum)
    with pytest.raises(ValueError, match="exhausted"):
        backup.reserve_egress(receipt, value, "2026-10", maximum)
    assert json.loads(receipt.read_text())["reserved_bytes"] == 4 * 1024**3


def test_local_raw_restore_requires_filevault_and_same_home_volume(tmp_path, monkeypatch):
    monkeypatch.setattr(backup.subprocess, "check_output", lambda *a, **k: "FileVault is Off.\n")
    with pytest.raises(ValueError, match="FileVault"):
        backup.require_encrypted_local_restore(tmp_path)


def test_internal_restore_refuses_direct_execution_before_reading_specification(tmp_path):
    environment = dict(os.environ)
    environment.pop("WEATHEREDGE_LOCAL_BUDGET_ACTIVE", None)
    result = subprocess.run(
        [sys.executable, str(HELPER), "--verify-local", str(tmp_path / "unread-specification.json")],
        capture_output=True, text=True, env=environment, check=False,
    )
    assert result.returncode == 1
    assert "must be launched through its resource budget" in result.stderr
    assert "FileNotFoundError" not in result.stderr


def run_orchestration(tmp_path, monkeypatch, fault="", mode="backup"):
    """Mock transports; execute the actual stdin worker and SQLite restoration."""
    source = database(tmp_path / "source.db")
    store = tmp_path / "s3"
    store.mkdir()
    marker = tmp_path / "maintenance"
    marker.touch()
    aws = tmp_path / "fake-aws"
    aws.write_text(f"#!{sys.executable}\n" + '''
import json, os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ['OFFHOST_TEST_S3'])
fault = os.environ.get('OFFHOST_TEST_FAULT', '')
if args[:2] == ['s3api', 'head-object']:
    path = root / args[args.index('--key')+1]
    print(json.dumps({'ServerSideEncryption': None if fault == 'sse' else 'AES256',
                      'VersionId': 'fixture-version-1', 'ContentLength': path.stat().st_size}))
elif args[:2] == ['s3', 'cp']:
    src, dst = args[2:4]
    if src.startswith('s3://'):
        path = root / src.split('/',3)[3]
        raw = path.read_bytes()
        if fault == 'manifest' and src.endswith('.manifest.json'):
            value = json.loads(raw); value['raw_sha256'] = '0'*64
            raw = json.dumps(value).encode()
        if fault == 'corrupt' and src.endswith('.gz'):
            raw = bytes([raw[0]^1]) + raw[1:]
        sys.stdout.buffer.write(raw)
    else:
        assert '--sse' in args and args[args.index('--sse')+1] == 'AES256'
        path = root / dst.split('/',3)[3]
        path.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(src,path)
''')
    aws.chmod(0o700)
    config = tmp_path / "weatheredge.env"
    config.write_text(f"SFO_ARCHIVE_S3_BUCKET=fixture-backups\nSFO_ARCHIVE_AWS_CLI={aws}\n")
    value = policy(tmp_path)
    value["verified_month"] = backup.datetime.now(backup.timezone.utc).strftime("%Y-%m")
    if fault == "policy":
        value["no_incremental_spend_verified"] = False
    cost_policy = tmp_path / "policy.json"
    cost_policy.write_text(json.dumps(value))
    compute_state = tmp_path / "research-worker"
    compute_config = tmp_path / "worker-config.json"
    compute_config.write_text(json.dumps({"state_dir": str(compute_state)} if fault != "compute-config" else {}))
    receipt = tmp_path / "operator" / "receipt.json"
    events = []

    class FixtureBudget:
        def __init__(self, *args):
            pass

        def run(self, command, *, stdin=None, stdout=None, stderr=None):
            # Every transport and the independent restore must share the actual
            # configured worker's exclusion, including failures and cleanup.
            with (compute_state / "worker.lock").open("a") as competing:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(competing, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if command[0] == "ssh":
                remote = shlex.split(command[-1])
                action = remote[remote.index("--remote-action") + 1]
                payload = json.loads(remote[remote.index("--payload") + 1])
                events.append(action)
                payload["units"] = []  # Fixture has no native systemd services.
                if fault == "lineage" and action == "assert-lineage":
                    with sqlite3.connect(source) as conn:
                        conn.execute("UPDATE parent SET id=3")
                code = stdin.read().replace(b"/etc/weatheredge.env", str(config).encode()).replace(
                    b"/run/weatheredge-deploy-maintenance", str(marker).encode())
                result = subprocess.run([sys.executable, "-", "--remote-action", action,
                                         "--payload", json.dumps(payload)], input=code,
                                        stdout=stdout, stderr=stderr)
            else:
                events.append("independent-restore")
                result = subprocess.run(command, stdout=stdout, stderr=stderr,
                                        env={**os.environ, "WEATHEREDGE_LOCAL_BUDGET_ACTIVE": "1"})
                if fault == "compute-change":
                    compute_config.write_text(json.dumps({"state_dir": str(compute_state), "changed": True}))
            result.check_returncode()

        def receipt(self):
            return {"fixture": True}

    monkeypatch.setenv("OFFHOST_TEST_S3", str(store))
    monkeypatch.setenv("OFFHOST_TEST_FAULT", fault)
    monkeypatch.setitem(sys.modules, "guards", SimpleNamespace(
        Budget=FixtureBudget, host_snapshot=lambda *args: {}, policy=lambda value: {}))
    monkeypatch.setattr(backup, "require_encrypted_local_restore", lambda *args: None)
    monkeypatch.setattr(sys, "argv", [str(HELPER), mode, "--policy", str(cost_policy),
                        "--receipt", str(receipt), "--database", str(source), "--target", "fixture",
                        "--key", str(tmp_path / "key"), "--deploy-id", "20261011-fixture-123",
                        "--candidate-sha", "a" * 40, "--local-compute-config", str(compute_config)])
    competing_lock = None
    if fault == "compute-lock":
        compute_state.mkdir()
        competing_lock = (compute_state / "worker.lock").open("a")
        fcntl.flock(competing_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        backup.main()
        failure = None
    except Exception as error:
        failure = error
    finally:
        if competing_lock is not None:
            competing_lock.close()
    if compute_state.exists():
        with (compute_state / "worker.lock").open("a") as after:
            fcntl.flock(after, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return receipt, events, failure, store


def test_full_orchestration_proves_s3_roundtrip_before_unchanged_lineage_and_receipt(tmp_path, monkeypatch):
    receipt, events, failure, store = run_orchestration(tmp_path, monkeypatch)
    assert failure is None, failure
    assert events == ["preflight", "backup", "manifest", "download", "independent-restore", "assert-lineage"]
    saved = json.loads(receipt.read_text())
    assert saved["verified"] is True
    assert saved["checks"]["integrity_check"] == saved["checks"]["foreign_key_check"] == "ok"
    assert saved["metadata"]["sse"] == "AES256"
    assert saved["metadata"]["archive_version_id"] == "fixture-version-1"
    assert saved["local_compute_config_sha256"] == backup.file_hash(tmp_path / "worker-config.json")
    assert len(list(store.rglob("*.gz"))) == 1
    assert len(list(store.rglob("*.manifest.json"))) == 1
    assert not list(receipt.parent.glob(".offhost-check-*"))
    assert not list(tmp_path.glob(".compressed-deploy-*"))


@pytest.mark.parametrize("fault", ["manifest", "corrupt", "sse", "lineage"])
def test_orchestration_never_receipts_changed_manifest_bad_download_missing_sse_or_changed_source(tmp_path, monkeypatch, fault):
    receipt, events, failure, _ = run_orchestration(tmp_path, monkeypatch, fault)
    assert failure is not None
    assert not receipt.exists()
    if fault in ("manifest", "sse"):
        assert "independent-restore" not in events
    if fault == "lineage":
        assert events[-2:] == ["independent-restore", "assert-lineage"]


def test_invalid_cost_policy_refuses_before_any_remote_action(tmp_path, monkeypatch):
    receipt, events, failure, store = run_orchestration(tmp_path, monkeypatch, "policy", "preflight")
    assert isinstance(failure, ValueError)
    assert events == []
    assert not receipt.exists()
    assert not list(store.iterdir())


@pytest.mark.parametrize("fault", ["compute-lock", "compute-config"])
def test_worker_lock_contention_and_invalid_config_refuse_before_any_remote_action(tmp_path, monkeypatch, fault):
    receipt, events, failure, store = run_orchestration(tmp_path, monkeypatch, fault, "preflight")
    assert isinstance(failure, ValueError)
    assert events == []
    assert not receipt.exists()
    assert not list(store.iterdir())


def test_changed_compute_configuration_cannot_promote_backup_receipt(tmp_path, monkeypatch):
    receipt, events, failure, _ = run_orchestration(tmp_path, monkeypatch, "compute-change")
    assert isinstance(failure, ValueError)
    assert "local compute configuration changed" in str(failure)
    assert "independent-restore" in events
    assert not receipt.exists()
