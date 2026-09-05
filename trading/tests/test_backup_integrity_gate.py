"""The independently downloaded snapshot supplies the complete recovery proof."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys


BACKUP_HELPER = Path(__file__).resolve().parents[1] / "deploy/aws/backup_paper_db.sh"


def _run_backup(tmp_path: Path, fault: str = ""):
    db_path = tmp_path / "paper.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
        conn.execute("INSERT INTO parent VALUES (1)")
        conn.execute("INSERT INTO child VALUES (?)", (2 if fault == "foreign_key" else 1,))
    original_bytes = db_path.read_bytes()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    shim = f"#!{sys.executable}\n" + '''
import json, os, shlex, shutil, subprocess, sys
from pathlib import Path

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["BACKUP_TEST_EVENTS"], "a") as handle:
    handle.write(json.dumps({"tool": tool, "args": args}) + "\\n")
if tool == "sqlite3":
    if os.environ["BACKUP_TEST_FAULT"] == "foreign_key_error" and args[-1] == "PRAGMA foreign_key_check;":
        print("injected foreign-key query failure", file=sys.stderr)
        raise SystemExit(7)
    result = subprocess.run([os.environ["BACKUP_REAL_SQLITE"], *args])
    if result.returncode == 0 and args[-1].startswith(".backup "):
        if os.environ["BACKUP_TEST_FAULT"] == "integrity":
            snapshot = Path(shlex.split(args[-1])[1])
            # Corrupt the snapshot before hashing/upload: transport preserves
            # its checksum, but real SQLite must reject the downloaded bytes.
            with snapshot.open("r+b") as handle:
                handle.write(b"not a SQLite db!!")
    raise SystemExit(result.returncode)
if tool == "mv":
    raise SystemExit(subprocess.run([os.environ["BACKUP_REAL_MV"], *args]).returncode)
if tool == "df":
    print("Filesystem 1024-blocks Used Available Capacity Mounted on")
    print("test 2000000000 1 1000000000 1% /")
    raise SystemExit(0)
if args[:2] in (["sts", "get-caller-identity"], ["s3api", "get-bucket-location"]):
    raise SystemExit(0)
if args[:2] != ["s3", "cp"]:
    raise SystemExit(2)
source, destination = args[2:4]
store = Path(os.environ["BACKUP_TEST_S3"])
store.mkdir(exist_ok=True)
if source.startswith("s3://"):
    shutil.copyfile(store / source.rsplit("/", 1)[-1], destination)
    if os.environ["BACKUP_TEST_FAULT"] == "checksum":
        with open(destination, "ab") as handle:
            handle.write(b"changed during transport")
else:
    shutil.copyfile(source, store / destination.rsplit("/", 1)[-1])
'''
    for name in ("aws", "sqlite3", "mv", "df"):
        executable = fake_bin / name
        executable.write_text(shim)
        executable.chmod(0o755)
    events_path = tmp_path / "events.jsonl"
    result = subprocess.run(
        ["bash", str(BACKUP_HELPER), "backup", str(db_path)],
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "SFO_WEATHEREDGE_ENV_FILE": str(tmp_path / "missing.env"),
            "SFO_ARCHIVE_S3_BUCKET": "test-backup",
            "SFO_ARCHIVE_AWS_CLI": str(fake_bin / "aws"),
            "SFO_DATABASE_BACKUP_S3_PREFIX": "database-snapshots",
            "SFO_DATABASE_BACKUP_DIR": str(tmp_path / "backups"),
            "SFO_DATABASE_BACKUP_KEEP_DAYS": "7",
            "SFO_ALLOW_EMPTY_DATABASE_DEPLOY": "0",
            "BACKUP_REAL_SQLITE": shutil.which("sqlite3") or "",
            "BACKUP_REAL_MV": shutil.which("mv") or "",
            "BACKUP_TEST_FAULT": fault,
            "BACKUP_TEST_EVENTS": str(events_path),
            "BACKUP_TEST_S3": str(tmp_path / "s3"),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert db_path.read_bytes() == original_bytes, "backup must never change the live database"
    assert not list((tmp_path / "backups").glob(".restore-check.*"))
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    return result, events


def _checks(events):
    return [event for event in events if event["tool"] == "sqlite3" and "PRAGMA" in event["args"][-1]]


def _download_index(events):
    return next(
        index for index, event in enumerate(events)
        if event["tool"] == "aws" and event["args"][:2] == ["s3", "cp"]
        and event["args"][2].startswith("s3://")
    )


def _assert_not_promoted(tmp_path, result, events):
    assert result.returncode != 0
    assert "WEATHEREDGE_BACKUP_SNAPSHOT=" not in result.stdout
    assert "verified off-host database backup" not in result.stdout
    assert "database backup: verification complete" not in result.stderr
    assert not any(event["tool"] == "mv" for event in events)
    assert not list((tmp_path / "backups").glob("paper_trading-*.sqlite3"))


def test_backup_runs_full_integrity_and_foreign_keys_once_on_downloaded_copy(tmp_path):
    result, events = _run_backup(tmp_path)
    assert result.returncode == 0, result.stderr
    checks = _checks(events)
    assert [event["args"][-1] for event in checks] == [
        "PRAGMA integrity_check;", "PRAGMA foreign_key_check;",
    ]
    download_index = _download_index(events)
    promote_index = next(index for index, event in enumerate(events) if event["tool"] == "mv")
    for check in checks:
        assert ".restore-check." in check["args"][-2]
        assert download_index < events.index(check) < promote_index
    snapshot_line = next(line for line in result.stdout.splitlines() if line.startswith("WEATHEREDGE_BACKUP_SNAPSHOT="))
    snapshot = Path(snapshot_line.partition("=")[2])
    assert snapshot.is_file()
    with sqlite3.connect(snapshot) as conn:
        assert conn.execute("SELECT parent_id FROM child").fetchall() == [(1,)]


def test_corrupt_checksum_matching_download_cannot_pass_integrity_or_promote(tmp_path):
    result, events = _run_backup(tmp_path, "integrity")
    _assert_not_promoted(tmp_path, result, events)
    checks = _checks(events)
    assert len(checks) == 1
    assert checks[0]["args"][-1] == "PRAGMA integrity_check;"
    assert events.index(checks[0]) > _download_index(events)
    assert "not a database" in result.stderr


def test_checksum_mismatch_stops_before_sqlite_checks_and_promotion(tmp_path):
    result, events = _run_backup(tmp_path, "checksum")
    _assert_not_promoted(tmp_path, result, events)
    assert _checks(events) == []
    assert "downloaded backup checksum mismatch" in result.stderr


def test_downloaded_foreign_key_violation_cannot_promote_even_with_valid_integrity(tmp_path):
    result, events = _run_backup(tmp_path, "foreign_key")
    _assert_not_promoted(tmp_path, result, events)
    checks = _checks(events)
    assert [event["args"][-1] for event in checks] == [
        "PRAGMA integrity_check;", "PRAGMA foreign_key_check;",
    ]
    assert all(events.index(check) > _download_index(events) for check in checks)
    assert "downloaded backup failed foreign_key_check" in result.stderr


def test_foreign_key_command_error_cannot_be_mistaken_for_no_violations(tmp_path):
    result, events = _run_backup(tmp_path, "foreign_key_error")
    _assert_not_promoted(tmp_path, result, events)
    assert "injected foreign-key query failure" in result.stderr


def test_backup_reports_sanitized_progress_only_on_stderr(tmp_path):
    result, _ = _run_backup(tmp_path)
    assert result.returncode == 0, result.stderr
    markers = [line for line in result.stderr.splitlines() if line.startswith("database backup: ")]
    assert markers == [
        "database backup: creating SQLite snapshot",
        "database backup: hashing snapshot",
        "database backup: uploading snapshot and checksum",
        "database backup: downloading restore copy",
        "database backup: verifying restored checksum",
        "database backup: checking restored SQLite integrity",
        "database backup: checking restored foreign keys",
        "database backup: verification complete",
    ]
    assert not any(line.startswith("database backup: ") for line in result.stdout.splitlines())
