from __future__ import annotations

import os
import shutil
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
AWS_DIR = ROOT / "trading" / "deploy" / "aws"
PULL_SCRIPT = AWS_DIR / "pull_paper_db.sh"

TIMERS = (
    "sfo-forecaster-refresh.timer",
    "sfo-operational-publish.timer",
    "sfo-strategy-lab-refresh.timer",
    "sfo-dataset-backfill.timer",
    "sfo-kalshi-paper-scan.timer",
    "sfo-kalshi-paper-monitor.timer",
    "sfo-kalshi-paper-settle.timer",
    "sfo-kalshi-paper-prune.timer",
    "sfo-forecast-freshness.timer",
)


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _fake_transfer_tools(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake bin"
    fake_bin.mkdir()
    log = tmp_path / "ssh.log"
    _write_executable(
        fake_bin / "ssh",
        f"""#!{sys.executable}
import os, subprocess, sys
from pathlib import Path
Path(os.environ['FAKE_SSH_LOG']).open('a', encoding='utf-8').write(' '.join(sys.argv[1:]) + '\\n')
command = sys.argv[-1]
data = sys.stdin.buffer.read()
result = subprocess.run(['/bin/bash', '-c', command], input=data)
raise SystemExit(result.returncode)
""",
    )
    _write_executable(
        fake_bin / "rsync",
        f"""#!{sys.executable}
import os, shlex, shutil, sys
source, destination = sys.argv[-2:]
source = source.split(':', 1)[1]
open(os.environ['FAKE_RSYNC_LOG'], 'a', encoding='utf-8').write(source + '\\n')
source = shlex.split(source)[0]
if os.environ.get('FAKE_RSYNC_CORRUPT') == '1':
    open(destination, 'wb').write(b'not a sqlite database')
else:
    shutil.copyfile(source, destination)
""",
    )
    return fake_bin, log


def _pull_env(tmp_path: Path, remote_db: Path, local_db: Path) -> dict[str, str]:
    fake_bin, log = _fake_transfer_tools(tmp_path)
    key = tmp_path / "operator key.pem"
    key.write_text("test key", encoding="utf-8")
    remote_tmp_dir = tmp_path / "remote snapshots"
    remote_tmp_dir.mkdir()
    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_SSH_LOG": str(log),
        "FAKE_RSYNC_LOG": str(tmp_path / "rsync.log"),
        "WEATHEREDGE_ENV_FILE": str(tmp_path / "missing env"),
        "EC2_IP": "ec2-current.example",
        "EC2_KEY": str(key),
        "LIGHTSAIL_IP": "legacy-invalid.example",
        "LIGHTSAIL_KEY": str(tmp_path / "legacy missing key"),
        "REMOTE_USER": "ubuntu",
        "REMOTE_DB": str(remote_db),
        "REMOTE_TMP_DIR": str(remote_tmp_dir),
        "LOCAL_DB": str(local_db),
    }


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI required")
def test_pull_uses_verified_backup_with_wal_and_quoted_paths(tmp_path: Path) -> None:
    remote_db = tmp_path / "remote state" / "paper journal.db"
    remote_db.parent.mkdir()
    writer = sqlite3.connect(remote_db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, value TEXT)")
    writer.execute("INSERT INTO events(value) VALUES ('before reader')")
    writer.commit()
    reader = sqlite3.connect(remote_db)
    reader.execute("BEGIN")
    assert reader.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    writer.execute("INSERT INTO events(value) VALUES ('committed in wal')")
    writer.commit()

    local_db = tmp_path / "local state" / "paper copy.db"
    env = _pull_env(tmp_path, remote_db, local_db)
    result = subprocess.run(
        ["bash", str(PULL_SCRIPT)], env=env, capture_output=True, text=True
    )

    reader.close()
    writer.close()
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(local_db) as copied:
        assert copied.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copied.execute("SELECT value FROM events ORDER BY id").fetchall() == [
            ("before reader",),
            ("committed in wal",),
        ]
    assert "ec2-current.example" in Path(env["FAKE_SSH_LOG"]).read_text()
    assert "legacy-invalid.example" not in Path(env["FAKE_SSH_LOG"]).read_text()
    assert "remote\\ snapshots" in Path(env["FAKE_RSYNC_LOG"]).read_text()
    assert not list(Path(env["REMOTE_TMP_DIR"]).iterdir())
    assert not list(local_db.parent.glob(f".{local_db.name}.pull.*"))


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI required")
def test_pull_integrity_failure_preserves_existing_local_database(tmp_path: Path) -> None:
    remote_db = tmp_path / "remote.db"
    with sqlite3.connect(remote_db) as db:
        db.execute("CREATE TABLE incoming(value TEXT)")
        db.execute("INSERT INTO incoming VALUES ('new')")
    local_db = tmp_path / "local state" / "paper.db"
    local_db.parent.mkdir()
    with sqlite3.connect(local_db) as db:
        db.execute("CREATE TABLE sentinel(value TEXT)")
        db.execute("INSERT INTO sentinel VALUES ('keep me')")

    env = _pull_env(tmp_path, remote_db, local_db)
    env["FAKE_RSYNC_CORRUPT"] = "1"
    result = subprocess.run(
        ["bash", str(PULL_SCRIPT)], env=env, capture_output=True, text=True
    )

    assert result.returncode != 0
    with sqlite3.connect(local_db) as db:
        assert db.execute("SELECT value FROM sentinel").fetchone()[0] == "keep me"
    assert not list(Path(env["REMOTE_TMP_DIR"]).iterdir())
    assert not list(local_db.parent.glob(".paper.db.pull.*"))


def test_no_timers_helper_stops_and_disables_every_existing_timer(tmp_path: Path) -> None:
    helper = AWS_DIR / "disable_systemd_timers.sh"
    fake = tmp_path / "systemctl"
    log = tmp_path / "systemctl.log"
    _write_executable(
        fake,
        """#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "$FAKE_SYSTEMCTL_LOG"
if [[ "$1" == list-unit-files ]]; then echo "$2 enabled"; exit 0; fi
if [[ "$1" == disable && "${FAIL_DISABLE:-}" == "$2" ]]; then exit 42; fi
exit 0
""",
    )
    result = subprocess.run(
        ["bash", str(helper)],
        env={**os.environ, "SYSTEMCTL_BIN": str(fake), "FAKE_SYSTEMCTL_LOG": str(log)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    for timer in TIMERS:
        assert f"stop {timer}" in calls
        assert f"disable {timer}" in calls
    installer = (AWS_DIR / "install_systemd_notimers.sh").read_text()
    assert "disable_systemd_timers.sh" in installer


def test_no_timers_helper_propagates_real_disable_failure(tmp_path: Path) -> None:
    helper = AWS_DIR / "disable_systemd_timers.sh"
    fake = tmp_path / "systemctl"
    log = tmp_path / "systemctl.log"
    _write_executable(
        fake,
        """#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "$FAKE_SYSTEMCTL_LOG"
if [[ "$1" == list-unit-files ]]; then echo "$2 enabled"; exit 0; fi
if [[ "$1" == disable && "$2" == sfo-operational-publish.timer ]]; then exit 42; fi
exit 0
""",
    )
    result = subprocess.run(
        ["bash", str(helper)],
        env={**os.environ, "SYSTEMCTL_BIN": str(fake), "FAKE_SYSTEMCTL_LOG": str(log)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 42


def test_no_timers_helper_propagates_timer_discovery_failure(tmp_path: Path) -> None:
    helper = AWS_DIR / "disable_systemd_timers.sh"
    fake = tmp_path / "systemctl"
    _write_executable(
        fake,
        """#!/usr/bin/env bash
if [[ "$1" == list-unit-files ]]; then
  echo "systemd unavailable" >&2
  exit 43
fi
exit 0
""",
    )
    result = subprocess.run(
        ["bash", str(helper)],
        env={**os.environ, "SYSTEMCTL_BIN": str(fake)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 43
    assert "systemd unavailable" in result.stderr


def test_deprecated_sync_wrapper_only_forwards_to_box() -> None:
    wrapper = AWS_DIR / "sync_to_lightsail.sh"
    text = wrapper.read_text()
    assert "DEPRECATED" in text
    assert 'exec "$SCRIPT_DIR/sync_to_box.sh" "$@"' in text
    assert "LIGHTSAIL_IP" not in text
    assert "rsync" not in text


def test_forecaster_filter_preserves_all_sqlite_sidecars() -> None:
    text = (AWS_DIR / "forecaster-runtime.rsync-filter").read_text()
    for pattern in ("*-wal", "*-shm", "*.db-wal", "*.db-shm"):
        assert pattern in text


def test_local_examples_default_to_demo_but_production_example_is_explicit() -> None:
    assert "KALSHI_ENV=demo" in (ROOT / ".env.example").read_text()
    assert "KALSHI_ENV=demo" in (ROOT / "trading" / ".env.example").read_text()
    production = (AWS_DIR / "sfo-weather.env.example").read_text()
    assert "KALSHI_ENV=prod" in production
    assert "SFO_LIVE_TRADING_ENABLED=0" in production


def test_reusable_redesign_prompt_requires_safe_delivery_workflow() -> None:
    prompt = (ROOT / "docs" / "prompts" / "site-redesign-fable5.md").read_text()
    for phrase in (
        "feature branch or isolated worktree",
        "full test suite",
        "independent review",
        "pull request",
        "explicit operator approval",
    ):
        assert phrase in prompt
    assert "Commit to `main`" not in prompt
    assert "committed to `main` and pushed" not in prompt


def test_social_metadata_uses_versioned_purpose_built_card() -> None:
    html = (ROOT / "index.html").read_text()
    name = "og-weatheredge-v2.png"
    assert name in html
    assert 'property="og:image:alt"' in html
    assert 'name="twitter:image:alt"' in html
    image = ROOT / "public" / name
    assert image.exists()
    with image.open("rb") as stream:
        assert stream.read(8) == b"\x89PNG\r\n\x1a\n"
        length = struct.unpack(">I", stream.read(4))[0]
        assert stream.read(4) == b"IHDR"
        width, height = struct.unpack(">II", stream.read(8))
    assert length == 13
    assert (width, height) == (1200, 630)


def test_forecaster_cadence_is_exact_in_active_docs() -> None:
    phrase = "twice hourly from 05:10 through 18:40 PT and hourly overnight"
    for path in (
        ROOT / "docs" / "aws_deployment.md",
        ROOT / "forecaster" / "README.md",
        AWS_DIR / "README.md",
    ):
        assert phrase in " ".join(path.read_text().split())
