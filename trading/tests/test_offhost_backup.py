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
            "cost_evidence_sha256": backup.file_hash(evidence),
            "verified_backup_bucket": "fixture-backups", "verified_backup_prefix": "database-snapshots",
            "verified_backup_region": "us-west-2", "verified_snapshot_retention_days": 35,
            "verified_bucket_versioning": "Enabled",
            "controls_verified_at": backup.datetime.now(backup.timezone.utc).isoformat()}


@pytest.mark.parametrize("change", [
    {"verified_month": "2026-09"}, {"no_incremental_spend_verified": False},
    {"verified_free_egress_bytes": 1}, {"max_compressed_bytes": True},
    {"max_compressed_bytes": 9 * 1024**3}, {"cost_evidence_sha256": "0" * 64},
    {"verified_backup_bucket": None}, {"verified_backup_prefix": "other/../snapshots"},
    {"verified_backup_region": None}, {"verified_snapshot_retention_days": None},
    {"verified_snapshot_retention_days": True}, {"verified_snapshot_retention_days": 1},
    {"verified_bucket_versioning": "Suspended"}, {"controls_verified_at": "2026-10-11T00:00:00"},
    {"controls_verified_at": "2026-10-10T00:00:00-07:00"},
    {"controls_verified_at": "2020-10-11T00:00:00Z"}, {"controls_verified_at": "2999-10-11T00:00:00Z"},
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
with (root / '.aws-actions').open('a') as log:
    log.write(json.dumps(args) + '\\n')
if args[:2] == ['s3api', 'head-object']:
    path = root / args[args.index('--key')+1]
    print(json.dumps({'ServerSideEncryption': None if fault == 'sse' else 'AES256',
                      'VersionId': 'fixture-version-1', 'ContentLength': path.stat().st_size}))
elif args[:2] == ['s3api', 'get-bucket-location']:
    location_reads = sum(json.loads(line)[:2] == ['s3api', 'get-bucket-location']
                         for line in (root / '.aws-actions').read_text().splitlines())
    changed = (fault == 'region-drift' and os.environ.get('OFFHOST_TEST_STAGE') == 'backup'
               or fault == 'preupload-region-drift' and location_reads >= 3
               or fault == 'premanifest-region-drift' and location_reads >= 4)
    region = 'us-east-1' if changed else 'us-west-2'
    print(json.dumps({'LocationConstraint': region}))
elif args[:2] == ['sts', 'get-caller-identity']:
    print('{}')
elif args[:2] == ['s3api', 'put-object']:
    if '--generate-cli-skeleton' in args:
        print(json.dumps({} if fault == 'paid-no-capability' else {'IfNoneMatch': '', 'ServerSideEncryption': ''}))
        sys.exit(0)
    assert os.environ.get('AWS_MAX_ATTEMPTS') == '1'
    assert args[args.index('--server-side-encryption')+1] == 'AES256'
    assert args[args.index('--if-none-match')+1] == '*'
    path = root / args[args.index('--key')+1]
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        sys.exit(1)
    shutil.copyfile(args[args.index('--body')+1],path)
    if (fault == 'paid-ambiguous' or fault == 'paid-second-part-ambiguous' and path.name.endswith('0002')
            or fault == 'paid-manifest-ambiguous' and path.name.endswith('.manifest.json')):
        sys.exit(1)
    print('{}')
elif args[:2] == ['s3api', 'get-object']:
    assert args[args.index('--version-id')+1] == 'fixture-version-1'
    assert os.environ.get('AWS_MAX_ATTEMPTS') == '1'
    raw = (root / args[args.index('--key')+1]).read_bytes()
    if fault == 'paid-corrupt':
        raw = bytes([raw[0]^1])+raw[1:]
    with os.fdopen(os.dup(int(args[-1].rsplit('/',1)[1])), 'wb') as body:
        body.write(raw)
    print(json.dumps({'ContentLength':len(raw)}))
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
    value = paid_policy(tmp_path) if fault.startswith("paid") else policy(tmp_path)
    value["verified_month"] = backup.datetime.now(backup.timezone.utc).strftime("%Y-%m")
    if fault == "policy":
        value["no_incremental_spend_verified"] = False
    if fault == "missing-retention":
        del value["verified_snapshot_retention_days"]
    if fault in ("bucket", "prefix", "region"):
        value[{"bucket": "verified_backup_bucket", "prefix": "verified_backup_prefix", "region": "verified_backup_region"}[fault]] = {
            "bucket": "other-backups", "prefix": "other-prefix", "region": "us-east-1"}[fault]
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
                if fault == "bucket-drift" and action == "backup":
                    config.write_text(config.read_text().replace("fixture-backups", "other-backups"))
                code = stdin.read().replace(b"/etc/weatheredge.env", str(config).encode()).replace(
                    b"/run/weatheredge-deploy-maintenance", str(marker).encode())
                if fault.startswith("paid"):
                    code = code.replace(b"PART_BYTES = 4 * GIB", b"PART_BYTES = 192")
                if fault == "paid-wrong-version" and action == "download":
                    payload["metadata"]["parts"][0]["version_id"] = "other-version"
                result = subprocess.run([sys.executable, "-", "--remote-action", action,
                                        "--payload", json.dumps(payload)], input=code,
                                        stdout=stdout, stderr=stderr,
                                        env={**os.environ, "OFFHOST_TEST_STAGE": action})
            else:
                events.append("independent-restore")
                result = subprocess.run(command, stdout=stdout, stderr=stderr,
                                        env={**os.environ, "WEATHEREDGE_LOCAL_BUDGET_ACTIVE": "1"})
                if fault == "compute-change":
                    compute_config.write_text(json.dumps({"state_dir": str(compute_state), "changed": True}))
                if fault == "cutover-target-drift":
                    config.write_text(config.read_text().replace("fixture-backups", "other-backups"))
                if fault == "control-evidence-change":
                    Path(value["cost_evidence_file"]).write_text('{"changed": true}')
            result.check_returncode()

        def receipt(self):
            return {"fixture": True}

    monkeypatch.setenv("OFFHOST_TEST_S3", str(store))
    monkeypatch.setenv("OFFHOST_TEST_FAULT", fault)
    monkeypatch.setitem(sys.modules, "guards", SimpleNamespace(
        Budget=FixtureBudget, host_snapshot=lambda *args: {}, policy=lambda value: {}))
    monkeypatch.setattr(backup, "REPO", tmp_path)
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


def test_missing_retention_evidence_refuses_before_any_remote_action(tmp_path, monkeypatch):
    receipt, events, failure, store = run_orchestration(tmp_path, monkeypatch, "missing-retention", "preflight")
    assert isinstance(failure, ValueError)
    assert events == []
    assert not receipt.exists()
    assert not list(store.iterdir())


@pytest.mark.parametrize("fault", ["bucket", "prefix", "region", "bucket-drift", "region-drift", "preupload-region-drift"])
def test_operator_target_mismatch_or_backup_drift_refuses_before_any_upload(tmp_path, monkeypatch, fault):
    receipt, events, failure, store = run_orchestration(tmp_path, monkeypatch, fault)
    assert failure is not None
    assert not receipt.exists()
    assert "independent-restore" not in events
    actions = [json.loads(line) for line in (store / ".aws-actions").read_text().splitlines()] if (store / ".aws-actions").exists() else []
    assert not any(action[:2] == ["s3", "cp"] for action in actions)
    assert not list(store.rglob("*.gz"))


def test_region_is_rechecked_before_manifest_upload(tmp_path, monkeypatch):
    receipt, events, failure, store = run_orchestration(tmp_path, monkeypatch, "premanifest-region-drift")
    assert failure is not None
    assert "independent-restore" not in events
    assert not receipt.exists()
    assert len(list(store.rglob("*.gz"))) == 1  # Unverified archive remains durable.
    assert not list(store.rglob("*.manifest.json"))


@pytest.mark.parametrize("fault", ["cutover-target-drift", "control-evidence-change"])
def test_cutover_target_or_control_evidence_drift_cannot_promote_receipt(tmp_path, monkeypatch, fault):
    receipt, events, failure, _ = run_orchestration(tmp_path, monkeypatch, fault)
    assert failure is not None
    assert "independent-restore" in events
    assert not receipt.exists()


@pytest.mark.parametrize("constraint,expected_region", [(None, "us-east-1"), ("EU", "eu-west-1"), ("us-west-2", "us-west-2")])
def test_bucket_region_lookup_normalizes_aws_legacy_location_values(tmp_path, monkeypatch, constraint, expected_region):
    value = policy(tmp_path)
    value["verified_backup_region"] = expected_region
    monkeypatch.setattr(backup, "remote_config", lambda: ("fixture-aws", "fixture-backups", "database-snapshots"))
    monkeypatch.setattr(backup.subprocess, "check_output", lambda *a, **k: json.dumps({"LocationConstraint": constraint}))
    assert backup.verified_remote_config({"backup_target": value}) == ("fixture-aws", "fixture-backups", "database-snapshots")


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


# Paid-policy fixtures are synthetic and cannot authorize an actual upload.
def paid_policy(tmp_path):
    value = policy(tmp_path)
    answer = tmp_path / "owner-answer.fixture.txt"
    answer.write_text("Synthetic fixture: one backup/cutover attempt up to 100 cents.")
    value.update({"spend_mode": "authorized-one-attempt", "no_incremental_spend_verified": False,
                  "authorized_incremental_spend_cents": 100,
                  "verified_all_in_cost_upper_bound_cents": 40, "authorized_backup_attempts": 1,
                  "spend_authorization_id": "fixture-owner-answer-20261012",
                  "spend_authorization_evidence_file": str(answer),
                  "spend_authorization_evidence_sha256": backup.file_hash(answer),
                  "verified_noncurrent_retention_days": 7, "verified_cost_horizon_days": 45,
                  "verified_storage_rate_microusd_per_gb_month": 26000,
                  "authorized_monthly_aws_budget_cents": 4000,
                  "monthly_costs_verified_at": backup.datetime.now(backup.timezone.utc).isoformat(),
                  "verified_monthly_aws_baseline_upper_bounds_cents": {
                      (backup.datetime.now(backup.timezone.utc) + backup.timedelta(days=offset)).strftime("%Y-%m"): 3500
                      for offset in range(46)}})
    return value


def test_explicit_paid_mode_keeps_no_increment_claim_false(tmp_path):
    value = paid_policy(tmp_path)
    assert backup.validate_policy(value, "2026-10") == value["max_compressed_bytes"]
    assert value["no_incremental_spend_verified"] is False
    del value["spend_mode"]
    with pytest.raises(ValueError, match="no-increase-spend"):
        backup.validate_policy(value, "2026-10")


@pytest.mark.parametrize("change", [
    {"spend_mode": "paid"}, {"no_incremental_spend_verified": True},
    {"authorized_incremental_spend_cents": 101}, {"authorized_incremental_spend_cents": True},
    {"authorized_incremental_spend_cents": 0}, {"authorized_incremental_spend_cents": 100.0},
    {"verified_all_in_cost_upper_bound_cents": 101}, {"verified_all_in_cost_upper_bound_cents": 0},
    {"verified_all_in_cost_upper_bound_cents": float("nan")},
    {"verified_all_in_cost_upper_bound_cents": True}, {"verified_all_in_cost_upper_bound_cents": 1},
    {"authorized_backup_attempts": 2}, {"authorized_backup_attempts": True},
    {"verified_noncurrent_retention_days": 0}, {"verified_cost_horizon_days": 42},
    {"verified_storage_rate_microusd_per_gb_month": True},
    {"verified_storage_rate_microusd_per_gb_month": 0},
    {"spend_authorization_id": ""}, {"spend_authorization_evidence_file": None},
    {"spend_authorization_evidence_sha256": "0" * 64},
])
def test_paid_policy_refuses_missing_answer_false_bounds_or_retention(tmp_path, change):
    with pytest.raises(ValueError):
        backup.validate_policy({**paid_policy(tmp_path), **change}, "2026-10")


@pytest.mark.parametrize("change", [
    {"authorized_monthly_aws_budget_cents": 4001},
    {"authorized_monthly_aws_budget_cents": True},
    {"authorized_monthly_aws_budget_cents": 0},
    {"verified_monthly_aws_baseline_upper_bounds_cents": {}},
    {"verified_monthly_aws_baseline_upper_bounds_cents": None},
    {"monthly_costs_verified_at": "invalid"},
    {"monthly_costs_verified_at": "2020-01-01T00:00:00Z"},
    {"monthly_costs_verified_at": "2100-01-01T00:00:00Z"},
    {"verified_cost_horizon_days": 91},
])
def test_paid_attempt_refuses_missing_stale_or_expanded_monthly_budget(tmp_path, change):
    with pytest.raises(ValueError):
        backup.validate_policy({**paid_policy(tmp_path), **change}, "2026-10")


@pytest.mark.parametrize("baseline", [3901, 5450, True, -1, 3500.0])
def test_paid_attempt_cannot_fit_by_ignoring_existing_aws_costs(tmp_path, baseline):
    value = paid_policy(tmp_path)
    month = backup.datetime.now(backup.timezone.utc).strftime("%Y-%m")
    value["verified_monthly_aws_baseline_upper_bounds_cents"][month] = baseline
    with pytest.raises(ValueError, match="month's budget"):
        backup.validate_policy(value, "2026-10")


def test_current_month_headroom_cannot_hide_next_month_cost_overrun(tmp_path):
    value = paid_policy(tmp_path)
    months = value["verified_monthly_aws_baseline_upper_bounds_cents"]
    del months[max(months)]
    with pytest.raises(ValueError, match="month's budget"):
        backup.validate_policy(value, "2026-10")


def test_paid_reservation_survives_failure_changed_id_and_utc_month_rollover(tmp_path):
    value = paid_policy(tmp_path)
    maximum = backup.validate_policy(value, "2026-10")
    ledger = tmp_path / "egress.json"
    backup.reserve_egress(ledger, value, "2026-10", maximum, deploy_id="20261012-fixture-first")
    first = json.loads(ledger.read_text())
    record = first["paid_authorizations"][value["spend_authorization_id"]]
    assert record["reserved_cost_cents"] == 100
    assert record["estimated_upper_bound_cents"] == 40
    assert record["reserved_attempts"] == 1
    # A successful or failed invocation gets no paid retry under this answer.
    for month, identity in (("2026-10", value["spend_authorization_id"]),
                            ("2026-11", value["spend_authorization_id"]),
                            ("2026-11", "fixture-changed-authorization-id")):
        changed = {**value, "verified_month": month, "spend_authorization_id": identity}
        with pytest.raises(ValueError, match="already reserved"):
            backup.reserve_egress(ledger, changed, month, maximum, deploy_id="20261101-fixture-second")
        assert json.loads(ledger.read_text()) == first
    # An ordinary zero-cost reservation in a new month must not erase paid state.
    free = {**policy(tmp_path), "verified_month": "2026-11"}
    backup.reserve_egress(ledger, free, "2026-11", maximum)
    assert json.loads(ledger.read_text())["paid_authorizations"] == first["paid_authorizations"]
    with pytest.raises(ValueError, match="already reserved"):
        backup.reserve_egress(ledger, value, "2026-11", maximum, deploy_id="20261101-fixture-third")


def test_direct_part_compression_is_one_gzip_and_keeps_aggregate_hard_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "PART_BYTES", 1024)
    source = database(tmp_path / "source.db")
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE incompressible (payload BLOB)")
        conn.execute("INSERT INTO incompressible VALUES (?)", (bytes(range(256))*3 + os.urandom(1000),))
    original = source.read_bytes()
    metadata, paths = backup.compress_snapshot_parts(source, tmp_path, 4096)
    joined = b"".join(path.read_bytes() for path in paths)
    assert 1 < len(paths) <= 2
    assert all(path.stat().st_size <= 1024 for path in paths)
    assert metadata["compressed_bytes"] == len(joined)
    assert metadata["compressed_sha256"] == hashlib.sha256(joined).hexdigest()
    assert gzip.decompress(joined) == original
    assert source.read_bytes() == original
    for number, (part, path) in enumerate(zip(metadata["parts"], paths, strict=True), 1):
        assert part["part_number"] == number
        assert part["compressed_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert not (tmp_path / "snapshot.sqlite3.gz").exists()


def test_part_compression_refuses_whole_archive_over_limit_before_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "PART_BYTES", 64)
    source = database(tmp_path / "source.db")
    with pytest.raises(ValueError, match="hard byte ceiling"):
        backup.compress_snapshot_parts(source, tmp_path, 32)


def test_paid_orchestration_uses_only_conditional_single_puts_and_pinned_part_gets(tmp_path, monkeypatch):
    receipt, events, failure, store = run_orchestration(tmp_path, monkeypatch, "paid")
    assert failure is None, failure
    saved = json.loads(receipt.read_text())
    assert saved["verified"] is True
    metadata = saved["metadata"]
    assert metadata["format"] == "single-put-parts-v1"
    assert len(metadata["parts"]) == 2
    assert sum(p["compressed_bytes"] for p in metadata["parts"]) == metadata["compressed_bytes"]
    assert saved["checks"]["integrity_check"] == saved["checks"]["foreign_key_check"] == "ok"
    actions = [json.loads(line) for line in (store / ".aws-actions").read_text().splitlines()]
    puts = [a for a in actions if a[:2] == ["s3api", "put-object"] and "--body" in a]
    gets = [a for a in actions if a[:2] == ["s3api", "get-object"]]
    assert len(puts) == 3  # Two <=4-GiB parts and their manifest.
    assert len(gets) == 2
    assert all(a[a.index("--if-none-match")+1] == "*" for a in puts)
    assert all(a[a.index("--version-id")+1] == "fixture-version-1" for a in gets)
    assert not any(a[:2] == ["s3", "cp"] and not a[2].startswith("s3://") for a in actions)
    ledger = json.loads((tmp_path / ".local/offhost-backup/egress-budget.json").read_text())
    assert len(ledger["paid_authorizations"]) == 1
    assert not list(tmp_path.glob(".compressed-deploy-*"))


@pytest.mark.parametrize("fault", ["paid-ambiguous", "paid-second-part-ambiguous",
                                    "paid-manifest-ambiguous", "paid-wrong-version", "paid-corrupt"])
def test_paid_partial_or_ambiguous_failures_keep_cost_reservation_without_receipt(tmp_path, monkeypatch, fault):
    receipt, events, failure, store = run_orchestration(tmp_path, monkeypatch, fault)
    assert failure is not None
    assert not receipt.exists()
    ledger = json.loads((tmp_path / ".local/offhost-backup/egress-budget.json").read_text())
    assert len(ledger["paid_authorizations"]) == 1
    assert next(iter(ledger["paid_authorizations"].values()))["reserved_cost_cents"] == 100
    assert not list(tmp_path.glob(".compressed-deploy-*"))
    actions = [json.loads(line) for line in (store / ".aws-actions").read_text().splitlines()]
    assert not any("multipart" in " ".join(a) for a in actions)
    if fault == "paid-ambiguous":
        assert sum(a[:2] == ["s3api", "put-object"] and "--body" in a for a in actions) == 1
        assert "download" not in events


def test_paid_preflight_does_not_consume_authorization_and_refuses_old_cli(tmp_path, monkeypatch):
    receipt, events, failure, store = run_orchestration(tmp_path, monkeypatch, "paid-no-capability", "preflight")
    assert failure is not None
    assert not receipt.exists()
    assert not (tmp_path / ".local/offhost-backup/egress-budget.json").exists()
    actions = [json.loads(line) for line in (store / ".aws-actions").read_text().splitlines()]
    assert not any("--body" in a for a in actions)
