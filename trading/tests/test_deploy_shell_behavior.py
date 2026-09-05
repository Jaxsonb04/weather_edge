from __future__ import annotations

import json
import importlib.util
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
AWS_DIR = ROOT / "trading" / "deploy" / "aws"
PULL_SCRIPT = AWS_DIR / "pull_paper_db.sh"

TIMERS = (
    "sfo-forecaster-refresh.timer",
    "weatheredge-google-nonsfo-refresh.timer",
    "weatheredge-apple-refresh.timer",
    "weatheredge-apple-purge.timer",
    "weatheredge-google-runtime-purge.timer",
    "sfo-operational-publish.timer",
    "sfo-strategy-lab-refresh.timer",
    "sfo-dataset-backfill.timer",
    "sfo-kalshi-paper-scan.timer",
    "sfo-kalshi-paper-monitor.timer",
    "sfo-kalshi-paper-settle.timer",
    "sfo-kalshi-paper-prune.timer",
    "sfo-forecast-freshness.timer",
    "sfo-scheduler-health.timer",
)
SERVICES = tuple(timer.removesuffix(".timer") + ".service" for timer in TIMERS)
PAPER_SCAN_RUNNER = AWS_DIR / "run_paper_scan_profiles.sh"


def _load_read_version_helper() -> str:
    """Extract the provenance-reading helper verbatim from sync_to_box.sh.

    The tests exercise the deploy script's real implementation rather than a
    copy, so drift between the two is impossible by construction.
    """

    text = (AWS_DIR / "sync_to_box.sh").read_text(encoding="utf-8")
    start = text.index("read_source_version_constant() {")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


_READ_VERSION_HELPER = _load_read_version_helper()


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _invoke_paper_scan_with_placement_flags(
    tmp_path: Path,
    **placement_flags: str,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    trading_root = tmp_path / "trading"
    trading_root.mkdir()
    call_log = tmp_path / "paper-scan-calls.jsonl"
    python_stub = tmp_path / "python-stub"
    _write_executable(
        python_stub,
        f"""#!{sys.executable}
import json, os, sys
with open(os.environ['PAPER_SCAN_CALL_LOG'], 'a', encoding='utf-8') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
""",
    )
    env = {
        **os.environ,
        "SFO_TRADING_ROOT": str(trading_root),
        "SFO_FORECASTER_ROOT": str(tmp_path / "forecaster"),
        "SFO_TRADING_PYTHON": str(python_stub),
        "SFO_KALSHI_DB": str(tmp_path / "paper.db"),
        "SFO_PAPER_SCAN_LOCK": str(tmp_path / "paper-scan.lock"),
        "PAPER_RISK_PROFILES": "live,research",
        "PAPER_SCAN_CALL_LOG": str(call_log),
        **placement_flags,
    }
    for name in (
        "PAPER_PLACE_LIVE",
        "PAPER_PLACE_RESEARCH_TARGET",
        "PAPER_PLACE_RESEARCH_MOTION",
        "SFO_PAPER_PLACE_ORDERS",
    ):
        if name not in placement_flags:
            env.pop(name, None)
    result = subprocess.run(
        ["bash", str(PAPER_SCAN_RUNNER)],
        env=env,
        capture_output=True,
        text=True,
    )
    calls = (
        [json.loads(line) for line in call_log.read_text().splitlines()]
        if call_log.exists()
        else []
    )
    return result, calls


def _run_paper_scan_with_placement_flags(
    tmp_path: Path,
    **placement_flags: str,
) -> list[list[str]]:
    result, calls = _invoke_paper_scan_with_placement_flags(
        tmp_path,
        **placement_flags,
    )
    assert result.returncode == 0, result.stderr
    return calls


def _paper_scan_call_for_profile(calls: list[list[str]], profile: str) -> list[str]:
    return next(call for call in calls if call[call.index("--risk-profile") + 1] == profile)


@pytest.mark.parametrize(
    ("placement_flags", "expected_live", "expected_target", "expected_motion"),
    [
        ({}, False, False, False),
        ({"PAPER_PLACE_LIVE": "TRUE"}, True, False, False),
        ({"PAPER_PLACE_RESEARCH_TARGET": "yes"}, False, True, False),
        ({"PAPER_PLACE_RESEARCH_MOTION": "1"}, False, False, False),
        (
            {
                "PAPER_PLACE_RESEARCH_TARGET": "on",
                "PAPER_PLACE_RESEARCH_MOTION": "Y",
            },
            False,
            True,
            False,
        ),
        (
            {
                "PAPER_PLACE_LIVE": "unknown",
                "PAPER_PLACE_RESEARCH_TARGET": "unknown",
                "PAPER_PLACE_RESEARCH_MOTION": "unknown",
                "SFO_PAPER_PLACE_ORDERS": "1",
            },
            False,
            False,
            False,
        ),
    ],
)
def test_paper_scan_placement_flags_are_default_off_and_account_isolated(
    tmp_path: Path,
    placement_flags: dict[str, str],
    expected_live: bool,
    expected_target: bool,
    expected_motion: bool,
) -> None:
    calls = _run_paper_scan_with_placement_flags(tmp_path, **placement_flags)
    assert len(calls) == 2
    live = _paper_scan_call_for_profile(calls, "live")
    research = _paper_scan_call_for_profile(calls, "research")

    assert ("--place-paper" in live) is expected_live
    assert "--place-research-target" not in live
    assert "--place-research-motion" not in live
    assert "--place-paper" not in research
    assert ("--place-research-target" in research) is expected_target
    assert ("--place-research-motion" in research) is expected_motion


@pytest.mark.parametrize(
    ("raw_profile", "placement_flags", "canonical_profile", "expected_flag"),
    [
        ("live", {"PAPER_PLACE_LIVE": "1"}, "live", "--place-paper"),
        ("BALANCED", {"PAPER_PLACE_LIVE": "1"}, "live", "--place-paper"),
        ("conservative", {"PAPER_PLACE_LIVE": "1"}, "live", "--place-paper"),
        (" REAL ", {"PAPER_PLACE_LIVE": "yes"}, "live", "--place-paper"),
        (
            "research",
            {"PAPER_PLACE_RESEARCH_TARGET": "1"},
            "research",
            "--place-research-target",
        ),
        (
            "fast_feedback",
            {"PAPER_PLACE_RESEARCH_TARGET": "on"},
            "research",
            "--place-research-target",
        ),
        (
            "FAST-FEEDBACK",
            {"PAPER_PLACE_RESEARCH_MOTION": "TRUE"},
            "research",
            None,
        ),
        (
            " ExPlOrAtOrY ",
            {"PAPER_PLACE_RESEARCH_TARGET": "y"},
            "research",
            "--place-research-target",
        ),
        (
            "FAST",
            {"PAPER_PLACE_RESEARCH_MOTION": "1"},
            "research",
            None,
        ),
        (
            "collector",
            {"PAPER_PLACE_RESEARCH_TARGET": "1"},
            "research",
            "--place-research-target",
        ),
        (
            "EXPLORE",
            {"PAPER_PLACE_RESEARCH_MOTION": "1"},
            "research",
            None,
        ),
    ],
)
def test_paper_scan_normalizes_supported_profile_aliases_before_dispatch(
    tmp_path: Path,
    raw_profile: str,
    placement_flags: dict[str, str],
    canonical_profile: str,
    expected_flag: str | None,
) -> None:
    calls = _run_paper_scan_with_placement_flags(
        tmp_path,
        PAPER_RISK_PROFILES=raw_profile,
        **placement_flags,
    )

    assert len(calls) == 1
    call = calls[0]
    assert call[call.index("--risk-profile") + 1] == canonical_profile
    if expected_flag is not None:
        assert expected_flag in call
    if canonical_profile == "live":
        assert "--place-research-target" not in call
        assert "--place-research-motion" not in call
    else:
        assert "--place-paper" not in call
        assert "--place-research-motion" not in call


@pytest.mark.parametrize("profiles", ("live,bogus", "bogus,research"))
def test_paper_scan_rejects_invalid_profile_csv_before_any_dispatch(
    tmp_path: Path,
    profiles: str,
) -> None:
    result, calls = _invoke_paper_scan_with_placement_flags(
        tmp_path,
        PAPER_RISK_PROFILES=profiles,
        PAPER_PLACE_LIVE="1",
        PAPER_PLACE_RESEARCH_TARGET="1",
        PAPER_PLACE_RESEARCH_MOTION="1",
    )

    assert result.returncode != 0
    assert "invalid paper risk profile: bogus" in result.stderr
    assert calls == []


def test_paper_scan_preserves_validated_order_and_ignores_empty_tokens(
    tmp_path: Path,
) -> None:
    calls = _run_paper_scan_with_placement_flags(
        tmp_path,
        PAPER_RISK_PROFILES=" , FAST_FEEDBACK, , conservative, ",
        PAPER_PLACE_LIVE="1",
        PAPER_PLACE_RESEARCH_MOTION="1",
    )

    assert [call[call.index("--risk-profile") + 1] for call in calls] == [
        "research",
        "live",
    ]
    assert "--place-research-motion" not in calls[0]
    assert "--place-paper" in calls[1]
    assert "--skip-context-snapshots" not in calls[0]
    assert "--skip-context-snapshots" in calls[1]


def _stub_clean_main_git(fake_bin: Path) -> None:
    _write_executable(
        fake_bin / "git",
        """#!/bin/sh
case "$*" in
  *"rev-parse HEAD") printf '0123456789abcdef0123456789abcdef01234567\n' ;;
  *"branch --show-current") printf 'main\n' ;;
  *"diff --quiet"*|*"diff --cached --quiet"*|*"ls-files --others --exclude-standard"*) ;;
  *) exit 1 ;;
esac
""",
    )


@pytest.mark.parametrize("change", ["head", "branch", "tracked", "staged", "untracked"])
@pytest.mark.parametrize("phase", ["backup", "transfer"])
def test_full_sync_rejects_source_changes_after_initial_clean_check(
    tmp_path: Path, change: str, phase: str,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "git",
        """#!/bin/sh
changed=0
[ ! -e "$SOURCE_CHANGED" ] || changed=1
case "$*" in
  *"rev-parse HEAD")
    if [ "$changed:$SOURCE_CHANGE" = 1:head ]; then echo different; else echo initial; fi ;;
  *"branch --show-current")
    if [ "$changed:$SOURCE_CHANGE" = 1:branch ]; then echo codex/other-task; else echo main; fi ;;
  *"diff --cached --quiet") [ "$changed:$SOURCE_CHANGE" != 1:staged ] ;;
  *"diff --quiet") [ "$changed:$SOURCE_CHANGE" != 1:tracked ] ;;
  *"ls-files --others --exclude-standard")
    if [ "$changed:$SOURCE_CHANGE" = 1:untracked ]; then echo unexpected.py; fi ;;
  *) exit 1 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "ssh",
        """#!/bin/sh
printf '%s\\n' "$*" >> "$SSH_LOG"
case "$*" in
  *"bash -s backup "*)
    if [ "$SOURCE_PHASE" = backup ]; then touch "$SOURCE_CHANGED"; fi
    echo 'WEATHEREDGE_BACKUP_SNAPSHOT=/opt/weatheredge/trading/data/backups/paper_trading-test.sqlite3' ;;
esac
""",
    )
    _write_executable(
        fake_bin / "rsync",
        """#!/bin/sh
printf '%s\\n' "$*" >> "$RSYNC_LOG"
if [ "$SOURCE_PHASE" = transfer ]; then touch "$SOURCE_CHANGED"; fi
""",
    )
    key = tmp_path / "test.pem"
    key.write_text("test")
    ssh_log, rsync_log = tmp_path / "ssh.log", tmp_path / "rsync.log"
    result = subprocess.run(
        ["bash", str(AWS_DIR / "sync_to_box.sh")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "WEATHEREDGE_ROOT": str(ROOT),
            "WEATHEREDGE_ENV_FILE": str(tmp_path / "missing.env"),
            "EC2_IP": "ec2.example", "EC2_KEY": str(key),
            "REMOTE_BASE": "/opt/weatheredge",
            "SOURCE_CHANGE": change, "SOURCE_PHASE": phase,
            "SOURCE_CHANGED": str(tmp_path / "changed"),
            "SSH_LOG": str(ssh_log), "RSYNC_LOG": str(rsync_log),
        },
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert f"Deploy source changed during {'backup verification' if phase == 'backup' else 'source transfer'}" in result.stderr
    remote_calls = ssh_log.read_text()
    assert "bash -s backup " in remote_calls
    assert "install_systemd_notimers.sh" not in remote_calls
    assert "bash -s restore" not in remote_calls
    if phase == "backup":
        assert not rsync_log.exists()
    else:
        assert rsync_log.exists()
        assert "build_info.json" not in rsync_log.read_text()


def test_database_backup_preflight_requires_off_host_target(tmp_path: Path) -> None:
    db_path = tmp_path / "paper.db"
    sqlite3.connect(db_path).close()
    result = subprocess.run(
        ["bash", str(AWS_DIR / "backup_paper_db.sh"), "preflight", str(db_path)],
        env={
            **os.environ,
            "SFO_WEATHEREDGE_ENV_FILE": str(tmp_path / "missing.env"),
            "SFO_ARCHIVE_S3_BUCKET": "",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "SFO_ARCHIVE_S3_BUCKET is required" in result.stderr
    assert not (tmp_path / "backups").exists()


def test_database_backup_preflight_rejects_insufficient_restore_space(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper.db"
    sqlite3.connect(db_path).close()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_aws = fake_bin / "aws"
    _write_executable(fake_aws, "#!/bin/sh\nexit 0\n")
    _write_executable(
        fake_bin / "df",
        "#!/bin/sh\n"
        "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'\n"
        "printf 'fake 1 1 0 100%% /\\n'\n",
    )

    result = subprocess.run(
        ["bash", str(AWS_DIR / "backup_paper_db.sh"), "preflight", str(db_path)],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SFO_WEATHEREDGE_ENV_FILE": str(tmp_path / "missing.env"),
            "SFO_ARCHIVE_S3_BUCKET": "weatheredge-test",
            "SFO_ARCHIVE_AWS_CLI": str(fake_aws),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "one snapshot + 1 GiB headroom" in result.stderr


def test_database_backup_round_trips_and_rechecks_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "paper.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))"
        )
        conn.execute("INSERT INTO parent VALUES (1)")
        conn.execute("INSERT INTO child VALUES (1, 1)")

    fake_aws = tmp_path / "aws"
    _write_executable(
        fake_aws,
        f"""#!{sys.executable}
import os, shutil, sys
from pathlib import Path

args = sys.argv[1:]
if args[:2] in (['sts', 'get-caller-identity'], ['s3api', 'get-bucket-location']):
    raise SystemExit(0)
if args[:2] != ['s3', 'cp']:
    raise SystemExit(2)
source, destination = args[2:4]
store = Path(os.environ['FAKE_S3'])
store.mkdir(parents=True, exist_ok=True)
if source.startswith('s3://'):
    shutil.copy2(store / source.rsplit('/', 1)[-1], destination)
else:
    shutil.copy2(source, store / destination.rsplit('/', 1)[-1])
""",
    )
    backup_dir = tmp_path / "backups"
    fake_s3 = tmp_path / "s3"
    result = subprocess.run(
        ["bash", str(AWS_DIR / "backup_paper_db.sh"), "backup", str(db_path)],
        env={
            **os.environ,
            "SFO_WEATHEREDGE_ENV_FILE": str(tmp_path / "missing.env"),
            "SFO_ARCHIVE_S3_BUCKET": "weatheredge-test",
            "SFO_ARCHIVE_AWS_CLI": str(fake_aws),
            "SFO_DATABASE_BACKUP_S3_PREFIX": "database-snapshots",
            "SFO_DATABASE_BACKUP_DIR": str(backup_dir),
            "SFO_DATABASE_BACKUP_KEEP_DAYS": "7",
            "SFO_ALLOW_EMPTY_DATABASE_DEPLOY": "0",
            "FAKE_S3": str(fake_s3),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "verified off-host database backup" in result.stdout
    assert len(list(backup_dir.glob("paper_trading-*.sqlite3"))) == 1
    assert len(list(backup_dir.glob("paper_trading-*.sqlite3.sha256"))) == 1
    assert len(list(fake_s3.glob("paper_trading-*.sqlite3"))) == 1
    assert len(list(fake_s3.glob("paper_trading-*.sqlite3.sha256"))) == 1
    assert not list(backup_dir.glob(".restore-check.*"))


def test_full_sync_transfers_root_install_inputs_from_arbitrary_cwd(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake bin"
    fake_bin.mkdir()
    _stub_clean_main_git(fake_bin)
    calls = tmp_path / "rsync-calls.jsonl"
    ssh_calls = tmp_path / "ssh-calls.jsonl"
    _write_executable(
        fake_bin / "ssh",
        f"""#!{sys.executable}
import json, os, sys
with open(os.environ['SSH_CALLS'], 'a', encoding='utf-8') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
if len(sys.argv) >= 3 and sys.argv[-2] == 'backup':
    print('WEATHEREDGE_BACKUP_SNAPSHOT=/opt/weatheredge/trading/data/backups/paper_trading-test.sqlite3')
""",
    )
    _write_executable(
        fake_bin / "rsync",
        f"""#!{sys.executable}
import json, os, shutil, sys
from pathlib import Path
with open(os.environ['RSYNC_CALLS'], 'a', encoding='utf-8') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
remote = Path(os.environ['FAKE_REMOTE_BASE'])
remote.mkdir(parents=True, exist_ok=True)
for token in sys.argv[1:-1]:
    source = Path(token)
    if source.is_file() and source.name in {{'pyproject.toml', 'README.md'}}:
        shutil.copy2(source, remote / source.name)
""",
    )
    key = tmp_path / "operator key.pem"
    key.write_text("test key")
    arbitrary_cwd = tmp_path / "unrelated cwd"
    arbitrary_cwd.mkdir()

    result = subprocess.run(
        ["bash", str(AWS_DIR / "sync_to_box.sh")],
        cwd=arbitrary_cwd,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "WEATHEREDGE_ROOT": str(ROOT),
            "WEATHEREDGE_ENV_FILE": str(tmp_path / "missing.env"),
            "EC2_IP": "ec2.example",
            "EC2_KEY": str(key),
            "REMOTE_USER": "ubuntu",
            "REMOTE_BASE": "/opt/weatheredge",
            "RSYNC_CALLS": str(calls),
            "SSH_CALLS": str(ssh_calls),
            "FAKE_REMOTE_BASE": str(tmp_path / "remote base"),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Restored 0 producer timer(s); watchdog restored last=0." in result.stdout
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    # Root packaging, the hashed lock, two source trees, and build provenance.
    assert len(invocations) == 5
    assert invocations[-1][-1].endswith("/forecaster/build_info.json")
    packaging = next(call for call in invocations if str(ROOT / "pyproject.toml") in call)
    assert str(ROOT / "README.md") in packaging
    assert packaging[-1] == "ubuntu@ec2.example:/opt/weatheredge/"
    assert str(key) in " ".join(packaging)
    locked = next(call for call in invocations if str(ROOT / "requirements/production.lock") in call)
    assert locked[-1] == "ubuntu@ec2.example:/opt/weatheredge/requirements/production.lock"
    remote = tmp_path / "remote base"
    assert (remote / "pyproject.toml").read_text() == (ROOT / "pyproject.toml").read_text()
    assert (remote / "README.md").read_text() == (ROOT / "README.md").read_text()

    expected_cleanup = [
        "/opt/weatheredge/trading/pyproject.toml",
        "/opt/weatheredge/trading/sfo_kalshi_quant/sfo-dataset-backfill.service.in",
        "/opt/weatheredge/trading/sfo_kalshi_quant/sfo-forecaster-refresh.service.in",
        "/opt/weatheredge/forecaster/forecast_tomorrow.py",
        "/opt/weatheredge/forecaster/load_to_db.py",
        "/opt/weatheredge/forecaster/combine_psv.py",
        "/opt/weatheredge/forecaster/eda.py",
        "/opt/weatheredge/forecaster/lstm_model.py",
        "/opt/weatheredge/forecaster/xgboost_model.py",
        "/opt/weatheredge/forecaster/ab_test.py",
        "/opt/weatheredge/forecaster/compare_models.py",
        "/opt/weatheredge/forecaster/features.py",
        "/opt/weatheredge/forecaster/forecast_validation.py",
        "/opt/weatheredge/forecaster/fetch_inland_history.py",
    ]
    remote_calls = [json.loads(line) for line in ssh_calls.read_text().splitlines()]
    cleanup = next(call for call in remote_calls if "rm" in call)
    assert cleanup[cleanup.index("--") + 1 :] == expected_cleanup
    assert not any(
        marker in path
        for path in expected_cleanup
        for marker in ("weather.db", "data/", "models/", "2016-2026 weather data")
    )


def test_full_sync_transfer_failure_never_runs_remote_cleanup(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _stub_clean_main_git(fake_bin)
    transfer_count = tmp_path / "transfer-count"
    ssh_log = tmp_path / "ssh.log"
    _write_executable(
        fake_bin / "ssh",
        """#!/bin/sh
printf '%s\n' "$*" >> "$SSH_LOG"
case "$*" in
  *"bash -s backup "*) printf '%s\n' \
    'WEATHEREDGE_BACKUP_SNAPSHOT=/opt/weatheredge/trading/data/backups/paper_trading-test.sqlite3' ;;
esac
""",
    )
    _write_executable(
        fake_bin / "rsync",
        """#!/bin/sh
count=0
if [ -f "$TRANSFER_COUNT" ]; then count=$(sed -n '1p' "$TRANSFER_COUNT"); fi
count=$((count + 1))
printf '%s\n' "$count" > "$TRANSFER_COUNT"
if [ "$count" -eq 2 ]; then exit 23; fi
exit 0
""",
    )
    key = tmp_path / "key.pem"
    key.write_text("test")

    result = subprocess.run(
        ["bash", str(AWS_DIR / "sync_to_box.sh")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "WEATHEREDGE_ROOT": str(ROOT),
            "WEATHEREDGE_ENV_FILE": str(tmp_path / "missing.env"),
            "EC2_IP": "ec2.example",
            "EC2_KEY": str(key),
            "REMOTE_BASE": "/opt/weatheredge",
            "TRANSFER_COUNT": str(transfer_count),
            "SSH_LOG": str(ssh_log),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 23
    assert transfer_count.read_text().strip() == "2"
    assert "rm -f" not in ssh_log.read_text()


def test_full_sync_quiesces_before_remote_mutation_and_stays_quiesced_on_transfer_failure(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _stub_clean_main_git(fake_bin)
    action_log = tmp_path / "actions.log"
    transfer_count = tmp_path / "transfer-count"
    _write_executable(
        fake_bin / "ssh",
        """#!/bin/sh
printf 'ssh|%s\n' "$*" >> "$ACTION_LOG"
case "$*" in
  *"bash -s backup "*) printf '%s\n' \
    'WEATHEREDGE_BACKUP_SNAPSHOT=/opt/weatheredge/trading/data/backups/paper_trading-test.sqlite3' ;;
esac
""",
    )
    _write_executable(
        fake_bin / "rsync",
        """#!/bin/sh
printf 'rsync|%s\n' "$*" >> "$ACTION_LOG"
count=0
if [ -f "$TRANSFER_COUNT" ]; then count=$(sed -n '1p' "$TRANSFER_COUNT"); fi
count=$((count + 1))
printf '%s\n' "$count" > "$TRANSFER_COUNT"
if [ "$count" -eq 2 ]; then exit 23; fi
exit 0
""",
    )
    key = tmp_path / "key.pem"
    key.write_text("test")

    result = subprocess.run(
        ["bash", str(AWS_DIR / "sync_to_box.sh")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "WEATHEREDGE_ROOT": str(ROOT),
            "WEATHEREDGE_ENV_FILE": str(tmp_path / "missing.env"),
            "EC2_IP": "ec2.example",
            "EC2_KEY": str(key),
            "REMOTE_BASE": "/opt/weatheredge",
            "TRANSFER_COUNT": str(transfer_count),
            "ACTION_LOG": str(action_log),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 23
    actions = action_log.read_text().splitlines()
    assert actions[0].endswith("bash -s preflight /opt/weatheredge/trading/data/paper_trading.db")
    assert actions[1].endswith("bash -s probe sfo-scheduler-health.timer")
    assert actions[2].endswith("bash -s probe weatheredge-apple-refresh.timer")
    assert actions[3].endswith("bash -s probe weatheredge-apple-purge.timer")
    assert actions[4].endswith("bash -s capture")
    assert "weatheredge-deploy-maintenance" in actions[5]
    assert actions[6].endswith("bash -s quiesce")
    assert actions[7].endswith("bash -s backup /opt/weatheredge/trading/data/paper_trading.db")
    assert "mkdir -p" in actions[8] and "chown" in actions[8]
    assert actions[9].startswith("rsync|")
    assert not any("enable" in action or "start" in action for action in actions)


def test_full_sync_reinstalls_units_and_restores_exact_enabled_timers_after_success(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _stub_clean_main_git(fake_bin)
    action_log = tmp_path / "actions.log"
    _write_executable(
        fake_bin / "ssh",
        f"""#!{sys.executable}
import os, sys
from pathlib import Path

args = sys.argv[1:]
data = sys.stdin.read()
with Path(os.environ['ACTION_LOG']).open('a', encoding='utf-8') as handle:
    handle.write('ssh|' + ' '.join(args) + '\\n')

if args[-4:] in (
    ['bash', '-s', 'probe', 'weatheredge-apple-refresh.timer'],
    ['bash', '-s', 'probe', 'weatheredge-apple-purge.timer'],
):
    raise SystemExit(10)
elif args[-3:] == ['bash', '-s', 'capture']:
    print('sfo-operational-publish.timer')
    print('sfo-strategy-lab-refresh.timer')
    print('sfo-forecast-freshness.timer')
elif len(args) >= 2 and args[-2] == 'backup':
    print('WEATHEREDGE_BACKUP_SNAPSHOT=/opt/weatheredge/trading/data/backups/paper_trading-test.sqlite3')
elif 'restore' in args:
    restored = args[args.index('restore') + 1:]
    with Path(os.environ['ACTION_LOG']).open('a', encoding='utf-8') as handle:
        handle.write('restore|' + ' '.join(restored) + '\\n')
""",
    )
    _write_executable(
        fake_bin / "rsync",
        "#!/bin/sh\nprintf 'rsync|%s\\n' \"$*\" >> \"$ACTION_LOG\"\n",
    )
    key = tmp_path / "key.pem"
    key.write_text("test")

    result = subprocess.run(
        ["bash", str(AWS_DIR / "sync_to_box.sh")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "WEATHEREDGE_ROOT": str(ROOT),
            "WEATHEREDGE_ENV_FILE": str(tmp_path / "missing.env"),
            "EC2_IP": "ec2.example",
            "EC2_KEY": str(key),
            "REMOTE_BASE": "/opt/weatheredge",
            "ACTION_LOG": str(action_log),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    actions = action_log.read_text().splitlines()
    preflight_idx = next(i for i, line in enumerate(actions) if "bash -s preflight" in line)
    capture_idx = next(i for i, line in enumerate(actions) if line.endswith("bash -s capture"))
    quiesce_idx = next(i for i, line in enumerate(actions) if line.endswith("bash -s quiesce"))
    backup_idx = next(i for i, line in enumerate(actions) if "bash -s backup" in line)
    first_rsync_idx = next(i for i, line in enumerate(actions) if line.startswith("rsync|"))
    install_idx = next(
        i for i, line in enumerate(actions) if "install_systemd_notimers.sh" in line
    )
    restore_indexes = [i for i, line in enumerate(actions) if line.startswith("restore|")]
    assert len(restore_indexes) == 4
    (
        apple_restore_idx,
        initial_writer_restore_idx,
        watchdog_restore_idx,
        post_writer_restore_idx,
    ) = restore_indexes
    seed_indexes = [
        i for i, line in enumerate(actions) if "sfo-strategy-lab-refresh.service" in line
        and "systemctl start" in line
    ]
    public_wait_indexes = [
        i for i, line in enumerate(actions) if "wait_for_publication_manifest.sh" in line
    ]
    freshness_idx = next(
        i
        for i, line in enumerate(actions)
        if "systemctl start sfo-forecast-freshness.service" in line
    )
    analysis_idx = next(
        i
        for i, line in enumerate(actions)
        if "refresh_strategy_analysis_cache.sh" in line
    )
    snapshot_removal_idx = next(
        i
        for i, line in enumerate(actions)
        if "rm -f -- '/opt/weatheredge/trading/data/backups/paper_trading-test.sqlite3'"
        in line
    )
    publisher_stop_idx = next(
        i
        for i, line in enumerate(actions)
        if "systemctl stop sfo-strategy-lab-refresh.timer "
        "sfo-operational-publish.timer" in line
    )
    assert len(seed_indexes) == 2
    assert len(public_wait_indexes) == 2
    assert preflight_idx < capture_idx < quiesce_idx < backup_idx < first_rsync_idx
    assert first_rsync_idx < install_idx
    assert (
        install_idx
        < analysis_idx
        < snapshot_removal_idx
        < apple_restore_idx
        < seed_indexes[0]
        < public_wait_indexes[0]
        < initial_writer_restore_idx
        < freshness_idx
        < watchdog_restore_idx
        < publisher_stop_idx
        < seed_indexes[1]
        < public_wait_indexes[1]
        < post_writer_restore_idx
    )
    assert actions[apple_restore_idx] == (
        "restore|weatheredge-apple-refresh.timer "
        "weatheredge-apple-purge.timer"
    )
    assert actions[initial_writer_restore_idx] == (
        "restore|sfo-strategy-lab-refresh.timer "
        "sfo-operational-publish.timer"
    )
    assert actions[watchdog_restore_idx] == "restore|sfo-forecast-freshness.timer"
    assert actions[post_writer_restore_idx] == (
        "restore|sfo-strategy-lab-refresh.timer "
        "sfo-operational-publish.timer"
    )
    assert "restored 2 producer timer(s); watchdog restored last=1" in result.stdout.lower()


def test_full_sync_restores_writers_when_post_analysis_drain_fails(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _stub_clean_main_git(fake_bin)
    action_log = tmp_path / "actions.log"
    _write_executable(
        fake_bin / "ssh",
        f"""#!{sys.executable}
import os, sys
from pathlib import Path

args = sys.argv[1:]
sys.stdin.read()
with Path(os.environ["ACTION_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write("ssh|" + " ".join(args) + "\\n")

if args[-3:] == ["bash", "-s", "capture"]:
    print("sfo-operational-publish.timer")
    print("sfo-strategy-lab-refresh.timer")
    print("sfo-forecast-freshness.timer")
elif len(args) >= 2 and args[-2] == "backup":
    print("WEATHEREDGE_BACKUP_SNAPSHOT=/opt/weatheredge/trading/data/backups/paper_trading-test.sqlite3")
elif "restore" in args:
    restored = args[args.index("restore") + 1:]
    with Path(os.environ["ACTION_LOG"]).open("a", encoding="utf-8") as handle:
        handle.write("restore|" + " ".join(restored) + "\\n")
elif any(
    "systemctl stop sfo-strategy-lab-refresh.timer" in arg
    for arg in args
):
    raise SystemExit(124)
""",
    )
    _write_executable(
        fake_bin / "rsync",
        "#!/bin/sh\nprintf 'rsync|%s\\n' \"$*\" >> \"$ACTION_LOG\"\n",
    )
    key = tmp_path / "key.pem"
    key.write_text("test")

    result = subprocess.run(
        ["bash", str(AWS_DIR / "sync_to_box.sh")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "WEATHEREDGE_ROOT": str(ROOT),
            "WEATHEREDGE_ENV_FILE": str(tmp_path / "missing.env"),
            "EC2_IP": "ec2.example",
            "EC2_KEY": str(key),
            "REMOTE_BASE": "/opt/weatheredge",
            "ACTION_LOG": str(action_log),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 124
    actions = action_log.read_text().splitlines()
    stop_idx = next(
        i
        for i, line in enumerate(actions)
        if "systemctl stop sfo-strategy-lab-refresh.timer" in line
    )
    post_restore_idx = max(
        i
        for i, line in enumerate(actions)
        if line == (
            "restore|sfo-strategy-lab-refresh.timer "
            "sfo-operational-publish.timer"
        )
    )
    assert stop_idx < post_restore_idx
    assert "post-analysis Strategy Lab publication failed (status=124)" in result.stderr


def test_full_sync_restores_all_held_timers_when_initial_seed_wait_fails(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _stub_clean_main_git(fake_bin)
    action_log = tmp_path / "actions.log"
    _write_executable(
        fake_bin / "ssh",
        f"""#!{sys.executable}
import os, sys
from pathlib import Path

args = sys.argv[1:]
sys.stdin.read()
with Path(os.environ["ACTION_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write("ssh|" + " ".join(args) + "\\n")

if args[-3:] == ["bash", "-s", "capture"]:
    print("sfo-operational-publish.timer")
    print("sfo-strategy-lab-refresh.timer")
    print("sfo-forecast-freshness.timer")
elif len(args) >= 2 and args[-2] == "backup":
    print("WEATHEREDGE_BACKUP_SNAPSHOT=/opt/weatheredge/trading/data/backups/paper_trading-test.sqlite3")
elif "restore" in args:
    restored = args[args.index("restore") + 1:]
    with Path(os.environ["ACTION_LOG"]).open("a", encoding="utf-8") as handle:
        handle.write("restore|" + " ".join(restored) + "\\n")
elif any("wait_for_publication_manifest.sh" in arg for arg in args):
    raise SystemExit(42)
""",
    )
    _write_executable(
        fake_bin / "rsync",
        "#!/bin/sh\nprintf 'rsync|%s\\n' \"$*\" >> \"$ACTION_LOG\"\n",
    )
    key = tmp_path / "key.pem"
    key.write_text("test")

    result = subprocess.run(
        ["bash", str(AWS_DIR / "sync_to_box.sh")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "WEATHEREDGE_ROOT": str(ROOT),
            "WEATHEREDGE_ENV_FILE": str(tmp_path / "missing.env"),
            "EC2_IP": "ec2.example",
            "EC2_KEY": str(key),
            "REMOTE_BASE": "/opt/weatheredge",
            "ACTION_LOG": str(action_log),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 42
    actions = action_log.read_text().splitlines()
    wait_idx = next(
        i
        for i, line in enumerate(actions)
        if "wait_for_publication_manifest.sh" in line
    )
    restore_idx = next(
        i
        for i, line in enumerate(actions)
        if line == (
            "restore|sfo-strategy-lab-refresh.timer "
            "sfo-operational-publish.timer "
            "sfo-forecast-freshness.timer"
        )
    )
    marker_release_idx = next(
        i
        for i, line in enumerate(actions)
        if "sudo rm -f -- '/run/weatheredge-deploy-maintenance'" in line
    )
    assert wait_idx < restore_idx
    assert restore_idx < marker_release_idx
    assert not any(
        line.endswith("bash -s quiesce")
        for line in actions[wait_idx + 1 :]
    )
    assert "initial Strategy Lab publication failed (status=42)" in result.stderr


def test_publication_wait_retries_until_exact_snapshot_is_public(tmp_path: Path) -> None:
    base = tmp_path / "weatheredge"
    forecaster = base / "forecaster"
    forecaster.mkdir(parents=True)
    local_manifest = {
        "snapshot_id": "fresh-snapshot",
        "provenance": {"source_sha": "abc123"},
    }
    (forecaster / "publication_manifest.json").write_text(
        json.dumps(local_manifest), encoding="utf-8"
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "curl-calls"
    _write_executable(
        fake_bin / "curl",
        f"""#!{sys.executable}
import json, os
from pathlib import Path

calls = Path(os.environ['CURL_CALLS'])
count = int(calls.read_text() or '0') if calls.exists() else 0
calls.write_text(str(count + 1))
snapshot = 'stale-snapshot' if count == 0 else 'fresh-snapshot'
print(json.dumps({{'snapshot_id': snapshot, 'provenance': {{'source_sha': 'abc123'}}}}))
""",
    )

    result = subprocess.run(
        ["bash", str(AWS_DIR / "wait_for_publication_manifest.sh")],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SFO_BASE_DIR": str(base),
            "SFO_TRADING_PYTHON": sys.executable,
            "SFO_PUBLISH_PAGES": "1",
            "SFO_PUBLICATION_MANIFEST_URL": "https://pages.example/manifest.json",
            "SFO_PUBLICATION_PROPAGATION_TIMEOUT_SECONDS": "4",
            "SFO_PUBLICATION_PROPAGATION_POLL_SECONDS": "1",
            "CURL_CALLS": str(calls),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert calls.read_text() == "2"
    assert "public publication snapshot matches local manifest" in result.stdout


def test_publication_wait_uses_immutable_expected_manifest(tmp_path: Path) -> None:
    base = tmp_path / "weatheredge"
    forecaster = base / "forecaster"
    forecaster.mkdir(parents=True)
    manifest = forecaster / "publication_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "snapshot_id": "expected-snapshot",
                "provenance": {"source_sha": "abc123"},
            }
        ),
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "curl",
        f"""#!{sys.executable}
import json, os
from pathlib import Path

Path(os.environ["LOCAL_MANIFEST"]).write_text(json.dumps({{
    "snapshot_id": "newer-recurring-snapshot",
    "provenance": {{"source_sha": "abc123"}},
}}))
print(json.dumps({{
    "snapshot_id": "expected-snapshot",
    "provenance": {{"source_sha": "abc123"}},
}}))
""",
    )

    result = subprocess.run(
        ["bash", str(AWS_DIR / "wait_for_publication_manifest.sh")],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SFO_BASE_DIR": str(base),
            "SFO_TRADING_PYTHON": sys.executable,
            "SFO_PUBLISH_PAGES": "1",
            "SFO_PUBLICATION_MANIFEST_URL": "https://pages.example/manifest.json",
            "SFO_PUBLICATION_PROPAGATION_TIMEOUT_SECONDS": "2",
            "SFO_PUBLICATION_PROPAGATION_POLL_SECONDS": "1",
            "LOCAL_MANIFEST": str(manifest),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "public publication snapshot matches local manifest" in result.stdout


@pytest.mark.parametrize("remote_delivered", (False, True))
def test_pages_publisher_waits_for_remote_delivery_before_push(
    tmp_path: Path,
    remote_delivered: bool,
) -> None:
    base = tmp_path / "weatheredge"
    forecaster = base / "forecaster"
    trading = base / "trading"
    webdist = base / "webdist"
    for directory in (forecaster, trading, webdist):
        directory.mkdir(parents=True)
    (webdist / "index.html").write_text("app", encoding="utf-8")
    for artifact in (
        "trading_signal.json",
        "forecast_data.json",
        "weather_story_data.json",
        "cities_data.json",
        "publication_manifest.json",
    ):
        (forecaster / artifact).write_text("{}\n", encoding="utf-8")
    deploy_key = tmp_path / "deploy-key"
    deploy_key.touch()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "flock", "#!/bin/sh\nexit 0\n")
    git_log = tmp_path / "git.log"
    _write_executable(
        fake_bin / "git",
        f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with open(os.environ["GIT_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(args) + "\\n")
if args[:1] == ["init"]:
    (Path(args[-1]) / ".git").mkdir(exist_ok=True)
elif args[:1] == ["show"]:
    print(json.dumps({{
        "snapshot_id": "remote-pending",
        "provenance": {{"source_sha": "abc1234"}},
    }}))
elif args[:3] == ["diff", "--cached", "--quiet"]:
    raise SystemExit(1)
raise SystemExit(0)
""",
    )
    _write_executable(
        fake_bin / "curl",
        f"""#!{sys.executable}
import json
print(json.dumps({{
    "snapshot_id": "public-older",
    "provenance": {{"source_sha": "abc1234"}},
}}))
""",
    )
    python_stub = tmp_path / "python-stub"
    _write_executable(
        python_stub,
        f"""#!{sys.executable}
import os
import sys

if sys.argv[1:2] == ["-"]:
    raise SystemExit(0 if os.environ["REMOTE_DELIVERED"] == "1" else 1)
if "--print-artifacts" in sys.argv:
    print("trading_signal.json")
    print("forecast_data.json")
    print("weather_story_data.json")
    print("cities_data.json")
    print("publication_manifest.json")
raise SystemExit(0)
""",
    )

    result = subprocess.run(
        ["bash", str(AWS_DIR / "publish_forecaster_pages.sh")],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SFO_PUBLISH_PAGES": "1",
            "SFO_BASE_DIR": str(base),
            "SFO_FORECASTER_ROOT": str(forecaster),
            "SFO_TRADING_ROOT": str(trading),
            "SFO_TRADING_PYTHON": str(python_stub),
            "SFO_WEBDIST_DIR": str(webdist),
            "SFO_PAGES_DEPLOY_KEY": str(deploy_key),
            "SFO_FORECASTER_GIT_REMOTE": "git@example.test:weatheredge.git",
            "SFO_PAGES_PROPAGATION_WAITER": str(
                AWS_DIR / "wait_for_publication_manifest.sh"
            ),
            "SFO_PUBLICATION_MANIFEST_URL": (
                "https://pages.example/publication_manifest.json"
            ),
            "SFO_PAGES_PENDING_PROPAGATION_TIMEOUT_SECONDS": "1",
            "SFO_PUBLICATION_PROPAGATION_POLL_SECONDS": "1",
            "SFO_ARTIFACT_GENERATION_LOCK": str(base / ".locks" / "artifact.lock"),
            "SFO_PAGES_LOCK": str(base / ".locks" / "pages.lock"),
            "GIT_LOG": str(git_log),
            "REMOTE_DELIVERED": "1" if remote_delivered else "0",
        },
        capture_output=True,
        text=True,
        timeout=5,
    )

    pushes = [
        line
        for line in git_log.read_text().splitlines()
        if line.startswith("push ")
    ]
    if remote_delivered:
        assert result.returncode == 0, result.stderr
        assert pushes == ["push origin HEAD:gh-pages"]
    else:
        # With workflow deploys, the successor push is what cancels a hung
        # deploy -- the gate waits briefly but never skips the cycle.
        assert result.returncode == 0, result.stderr
        assert "publishing anyway" in result.stderr + result.stdout
        assert pushes == ["push origin HEAD:gh-pages"]


@pytest.mark.parametrize(
    "remote_base",
    [
        "/",
        "//",
        "/opt/weather edge",
        "/opt//weatheredge",
        "/opt/weatheredge/",
        "/opt/./weatheredge",
        "/opt/weatheredge/.",
        "/./.",
        "/opt/weatheredge/../etc",
    ],
)
def test_full_sync_rejects_unsafe_remote_base_before_any_action(
    remote_base: str, tmp_path: Path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    action_log = tmp_path / "actions.log"
    for name in ("ssh", "rsync"):
        _write_executable(
            fake_bin / name,
            "#!/bin/sh\nprintf '%s\\n' \"$0 $*\" >> \"$ACTION_LOG\"\n",
        )
    key = tmp_path / "key.pem"
    key.write_text("test")

    result = subprocess.run(
        ["bash", str(AWS_DIR / "sync_to_box.sh")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "WEATHEREDGE_ROOT": str(ROOT),
            "WEATHEREDGE_ENV_FILE": str(tmp_path / "missing.env"),
            "EC2_IP": "ec2.example",
            "EC2_KEY": str(key),
            "REMOTE_BASE": remote_base,
            "ACTION_LOG": str(action_log),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "REMOTE_BASE" in result.stderr
    assert not action_log.exists()


def _install_verifier_module():
    path = AWS_DIR / "verify_trading_install.py"
    spec = importlib.util.spec_from_file_location("verify_trading_install", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeEntry:
    def __init__(self, name: str, value: str, group: str = "console_scripts") -> None:
        self.name = name
        self.value = value
        self.group = group


class _FakeDistribution:
    def __init__(self, name: str, entries: list[_FakeEntry]) -> None:
        self.metadata = {"Name": name}
        self.entry_points = entries


def test_install_verifier_rejects_duplicate_identical_distribution_metadata() -> None:
    verifier = _install_verifier_module()
    entry = _FakeEntry("sfo-kalshi", "sfo_kalshi_quant.cli:main")
    with pytest.raises(ValueError, match="exactly one WeatherEdge distribution"):
        verifier.validate_install(
            [_FakeDistribution("weatheredge", [entry]), _FakeDistribution("weatheredge", [entry])]
        )


def test_install_verifier_rejects_duplicate_identical_console_entries() -> None:
    verifier = _install_verifier_module()
    entry = _FakeEntry("sfo-kalshi", "sfo_kalshi_quant.cli:main")
    with pytest.raises(ValueError, match="exactly one sfo-kalshi console entry"):
        verifier.validate_install([_FakeDistribution("weatheredge", [entry, entry])])


def test_install_verifier_accepts_one_weatheredge_owner_and_entry() -> None:
    verifier = _install_verifier_module()
    entry = _FakeEntry("sfo-kalshi", "sfo_kalshi_quant.cli:main")
    verifier.validate_install([_FakeDistribution("WeatherEdge", [entry])])


def test_real_legacy_editable_upgrade_leaves_one_owner_and_console_script(
    tmp_path: Path,
) -> None:
    clean_python_env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    base = tmp_path / "remote base"
    legacy = base / "trading"
    package = legacy / "sfo_kalshi_quant"
    shutil.copytree(ROOT / "trading/sfo_kalshi_quant", package)
    shutil.copy2(ROOT / "pyproject.toml", base / "pyproject.toml")
    shutil.copy2(ROOT / "README.md", base / "README.md")
    shutil.copy2(ROOT / "trading/README.md", legacy / "README.md")
    (legacy / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'sfo-kalshi-quant'\n"
        "version = '0.1.0'\n"
        "readme = 'README.md'\n"
        "requires-python = '>=3.11'\n"
        "dependencies = []\n\n"
        "[project.scripts]\n"
        "sfo-kalshi = 'sfo_kalshi_quant.cli:main'\n\n"
        "[tool.setuptools.packages.find]\n"
        "include = ['sfo_kalshi_quant*']\n"
    )
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / "bin/python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "-e", str(legacy)],
        check=True,
        env=clean_python_env,
    )
    before = subprocess.run(
        [
            str(python),
            "-c",
            "from importlib.metadata import distribution; print(distribution('sfo-kalshi-quant').metadata['Name'])",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=clean_python_env,
    )
    assert before.stdout.strip() == "sfo-kalshi-quant"
    legacy_metadata = legacy / "sfo_kalshi_quant.egg-info"
    assert legacy_metadata.is_dir()

    # This is the exact state transition performed by sync_to_box.sh before an
    # installer runs: source stays in place, only the retired manifest is gone.
    (legacy / "pyproject.toml").unlink()
    site_packages = Path(
        subprocess.run(
            [
                str(python),
                "-c",
                "import sysconfig; print(sysconfig.get_paths()['purelib'])",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    interrupted_metadata = site_packages / "~eatheredge-0.1.0.dist-info"
    interrupted_metadata.mkdir()
    (interrupted_metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: weatheredge\nVersion: 0.1.0\n",
        encoding="utf-8",
    )

    # The production installer always applies the hashed runtime lock before
    # building the root editable project. Mirror that sequence so the test
    # also proves the pinned no-isolation build backend is sufficient.
    requirements = base / "requirements"
    requirements.mkdir()
    lock = requirements / "production.lock"
    shutil.copy2(ROOT / "requirements/production.lock", lock)
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "--require-hashes", "-r", str(lock)],
        check=True,
        env=clean_python_env,
    )

    result = subprocess.run(
        [
            "bash",
            str(AWS_DIR / "install_trading_project.sh"),
            str(base),
            str(python),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not legacy_metadata.exists()
    assert not interrupted_metadata.exists()
    owners = subprocess.run(
        [
            str(python),
            "-c",
            "from importlib.metadata import distributions; print(','.join(sorted(d.metadata['Name'] for d in distributions() if d.metadata['Name'].lower() in {'weatheredge','sfo-kalshi-quant'})))",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=clean_python_env,
    )
    assert owners.stdout.strip() == "weatheredge"
    help_result = subprocess.run(
        [str(venv / "bin/sfo-kalshi"), "--help"],
        capture_output=True,
        text=True,
        env=clean_python_env,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "usage: sfo-kalshi" in help_result.stdout


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
if os.environ.get('FAKE_SSH_FAIL_AFTER_BACKUP') == '1' and data and result.returncode == 0:
    raise SystemExit(45)
raise SystemExit(result.returncode)
""",
    )
    _write_executable(
        fake_bin / "rsync",
        f"""#!{sys.executable}
import os, shlex, shutil, stat, sys
source, destination = sys.argv[-2:]
source = source.split(':', 1)[1]
open(os.environ['FAKE_RSYNC_LOG'], 'a', encoding='utf-8').write(source + '\\n')
source = shlex.split(source)[0]
directory_mode = stat.S_IMODE(os.stat(os.path.dirname(source)).st_mode)
snapshot_mode = stat.S_IMODE(os.stat(source).st_mode)
open(os.environ['FAKE_REMOTE_META_LOG'], 'a', encoding='utf-8').write(
    f'{{source}}|{{directory_mode:o}}|{{snapshot_mode:o}}\\n'
)
if os.environ.get('FAKE_RSYNC_CORRUPT') == '1':
    open(destination, 'wb').write(b'not a sqlite database')
else:
    shutil.copyfile(source, destination)
""",
    )
    _write_executable(
        fake_bin / "stat",
        f"""#!{sys.executable}
import os, stat, sys
if sys.argv[1:3] != ['-c', '%a']:
    raise SystemExit('unsupported fake stat arguments')
if os.environ.get('FAKE_STAT_UNSAFE') == '1' and os.path.isdir(sys.argv[3]):
    print('755')
    raise SystemExit(0)
print(f'{{stat.S_IMODE(os.stat(sys.argv[3]).st_mode):o}}')
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
        "FAKE_REMOTE_META_LOG": str(tmp_path / "remote-meta.log"),
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
    second_result = subprocess.run(
        ["bash", str(PULL_SCRIPT)], env=env, capture_output=True, text=True
    )

    reader.close()
    writer.close()
    assert result.returncode == 0, result.stderr
    assert second_result.returncode == 0, second_result.stderr
    with sqlite3.connect(local_db) as copied:
        assert copied.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copied.execute("SELECT value FROM events ORDER BY id").fetchall() == [
            ("before reader",),
            ("committed in wal",),
        ]
    assert "ec2-current.example" in Path(env["FAKE_SSH_LOG"]).read_text()
    assert "legacy-invalid.example" not in Path(env["FAKE_SSH_LOG"]).read_text()
    assert "remote\\ snapshots" in Path(env["FAKE_RSYNC_LOG"]).read_text()
    metadata = [
        line.split("|")
        for line in Path(env["FAKE_REMOTE_META_LOG"]).read_text().splitlines()
    ]
    assert len(metadata) == 2
    assert len({row[0] for row in metadata}) == 2
    for snapshot, directory_mode, snapshot_mode in metadata:
        snapshot_path = Path(snapshot)
        assert snapshot_path.parent.parent == Path(env["REMOTE_TMP_DIR"])
        assert directory_mode == "700"
        assert snapshot_mode == "600"
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


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI required")
def test_remote_permission_failure_trap_removes_allocated_directory(
    tmp_path: Path,
) -> None:
    remote_db = tmp_path / "remote.db"
    with sqlite3.connect(remote_db) as db:
        db.execute("CREATE TABLE incoming(value TEXT)")
    local_db = tmp_path / "local" / "paper.db"
    local_db.parent.mkdir()
    with sqlite3.connect(local_db) as db:
        db.execute("CREATE TABLE sentinel(value TEXT)")
        db.execute("INSERT INTO sentinel VALUES ('keep me')")

    env = _pull_env(tmp_path, remote_db, local_db)
    env["FAKE_STAT_UNSAFE"] = "1"
    result = subprocess.run(
        ["bash", str(PULL_SCRIPT)], env=env, capture_output=True, text=True
    )

    assert result.returncode != 0
    assert "unsafe mode" in result.stderr
    assert not list(Path(env["REMOTE_TMP_DIR"]).iterdir())
    with sqlite3.connect(local_db) as db:
        assert db.execute("SELECT value FROM sentinel").fetchone()[0] == "keep me"


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI required")
def test_client_cleans_allocated_directory_when_ssh_fails_after_backup(
    tmp_path: Path,
) -> None:
    remote_db = tmp_path / "remote.db"
    with sqlite3.connect(remote_db) as db:
        db.execute("CREATE TABLE incoming(value TEXT)")
    local_db = tmp_path / "local" / "paper.db"
    local_db.parent.mkdir()
    with sqlite3.connect(local_db) as db:
        db.execute("CREATE TABLE sentinel(value TEXT)")
        db.execute("INSERT INTO sentinel VALUES ('keep me')")

    env = _pull_env(tmp_path, remote_db, local_db)
    env["FAKE_SSH_FAIL_AFTER_BACKUP"] = "1"
    result = subprocess.run(
        ["bash", str(PULL_SCRIPT)], env=env, capture_output=True, text=True
    )

    assert result.returncode == 45
    assert not list(Path(env["REMOTE_TMP_DIR"]).iterdir())
    with sqlite3.connect(local_db) as db:
        assert db.execute("SELECT value FROM sentinel").fetchone()[0] == "keep me"


def test_no_timers_helper_stops_and_disables_every_existing_timer(tmp_path: Path) -> None:
    helper = AWS_DIR / "disable_systemd_timers.sh"
    fake = tmp_path / "systemctl"
    log = tmp_path / "systemctl.log"
    _write_executable(
        fake,
        """#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "$FAKE_SYSTEMCTL_LOG"
if [[ "$1" == show ]]; then echo loaded; exit 0; fi
if [[ "$1" == list-unit-files ]]; then echo "$2 enabled"; exit 0; fi
if [[ "$1" == is-active ]]; then echo inactive; exit 3; fi
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
    for service in SERVICES:
        assert f"stop {service}" in calls
        assert f"is-active {service}" in calls
    installer = (AWS_DIR / "install_systemd_notimers.sh").read_text()
    assert "disable_systemd_timers.sh" in installer
    assert installer.index("disable_systemd_timers.sh") < installer.index("apt-get update")


def test_timer_state_helper_captures_and_restores_only_the_enabled_set(
    tmp_path: Path,
) -> None:
    helper = AWS_DIR / "disable_systemd_timers.sh"
    fake = tmp_path / "systemctl"
    log = tmp_path / "systemctl.log"
    selected = (
        "sfo-operational-publish.timer",
        "sfo-strategy-lab-refresh.timer",
        "sfo-forecast-freshness.timer",
    )
    _write_executable(
        fake,
        """#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "$FAKE_SYSTEMCTL_LOG"
if [[ "$1" == show ]]; then echo loaded; exit 0; fi
if [[ "$1" == is-enabled ]]; then
  case "$3" in
    sfo-operational-publish.timer|sfo-strategy-lab-refresh.timer|sfo-forecast-freshness.timer) exit 0 ;;
    *) exit 1 ;;
  esac
fi
if [[ "$1" == is-active ]]; then exit 0; fi
exit 0
""",
    )
    env = {**os.environ, "SYSTEMCTL_BIN": str(fake), "FAKE_SYSTEMCTL_LOG": str(log)}

    capture = subprocess.run(
        ["bash", str(helper), "capture"],
        env=env,
        capture_output=True,
        text=True,
    )
    restore = subprocess.run(
        ["bash", str(helper), "restore", *selected],
        env=env,
        capture_output=True,
        text=True,
    )

    assert capture.returncode == 0, capture.stderr
    assert tuple(capture.stdout.splitlines()) == selected
    assert restore.returncode == 0, restore.stderr
    assert "restored 3 previously enabled WeatherEdge timer(s)" in restore.stdout
    calls = log.read_text()
    assert "enable --now " + " ".join(selected) in calls
    for timer in selected:
        assert f"is-active --quiet {timer}" in calls


def test_timer_state_helper_distinguishes_new_timer_from_inspection_failure(
    tmp_path: Path,
) -> None:
    helper = AWS_DIR / "disable_systemd_timers.sh"
    fake = tmp_path / "systemctl"
    _write_executable(
        fake,
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == show ]]; then
  printf '%s\n' "${FAKE_LOAD_STATE:-not-found}"
  exit "${FAKE_SHOW_STATUS:-0}"
fi
exit 0
""",
    )
    env = {**os.environ, "SYSTEMCTL_BIN": str(fake)}

    missing = subprocess.run(
        ["bash", str(helper), "probe", "sfo-scheduler-health.timer"],
        env=env,
        capture_output=True,
        text=True,
    )
    loaded = subprocess.run(
        ["bash", str(helper), "probe", "sfo-scheduler-health.timer"],
        env={**env, "FAKE_LOAD_STATE": "loaded"},
        capture_output=True,
        text=True,
    )
    broken = subprocess.run(
        ["bash", str(helper), "probe", "sfo-scheduler-health.timer"],
        env={**env, "FAKE_SHOW_STATUS": "47"},
        capture_output=True,
        text=True,
    )

    assert missing.returncode == 10
    assert loaded.returncode == 0
    assert broken.returncode == 47
    assert "failed to inspect systemd unit" in broken.stderr


def test_no_timers_helper_propagates_real_disable_failure(tmp_path: Path) -> None:
    helper = AWS_DIR / "disable_systemd_timers.sh"
    fake = tmp_path / "systemctl"
    log = tmp_path / "systemctl.log"
    _write_executable(
        fake,
        """#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "$FAKE_SYSTEMCTL_LOG"
if [[ "$1" == show ]]; then echo loaded; exit 0; fi
if [[ "$1" == list-unit-files ]]; then echo "$2 enabled"; exit 0; fi
if [[ "$1" == is-active ]]; then echo inactive; exit 3; fi
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
if [[ "$1" == list-unit-files || "$1" == show ]]; then
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


def test_no_timers_helper_stops_loaded_service_without_unit_file(tmp_path: Path) -> None:
    helper = AWS_DIR / "disable_systemd_timers.sh"
    fake = tmp_path / "systemctl"
    log = tmp_path / "systemctl.log"
    _write_executable(
        fake,
        """#!/usr/bin/env bash
echo "$*" >> "$FAKE_SYSTEMCTL_LOG"
if [[ "$1" == list-unit-files ]]; then
  [[ "$2" == *.timer ]] && echo "$2 enabled"
  exit 0
fi
if [[ "$1" == show ]]; then echo loaded; exit 0; fi
if [[ "$1" == is-active ]]; then echo inactive; exit 3; fi
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
    assert "stop sfo-forecaster-refresh.service" in log.read_text()


def test_no_timers_helper_propagates_service_stop_failure(tmp_path: Path) -> None:
    helper = AWS_DIR / "disable_systemd_timers.sh"
    fake = tmp_path / "systemctl"
    _write_executable(
        fake,
        """#!/usr/bin/env bash
if [[ "$1" == show ]]; then echo loaded; exit 0; fi
if [[ "$1" == list-unit-files ]]; then echo "$2 enabled"; exit 0; fi
if [[ "$1" == stop && "$2" == sfo-operational-publish.service ]]; then exit 44; fi
if [[ "$1" == is-active ]]; then echo inactive; exit 3; fi
exit 0
""",
    )
    result = subprocess.run(
        ["bash", str(helper)],
        env={**os.environ, "SYSTEMCTL_BIN": str(fake)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 44


def test_no_timers_helper_rejects_service_that_remains_active(tmp_path: Path) -> None:
    helper = AWS_DIR / "disable_systemd_timers.sh"
    fake = tmp_path / "systemctl"
    _write_executable(
        fake,
        """#!/usr/bin/env bash
if [[ "$1" == show ]]; then echo loaded; exit 0; fi
if [[ "$1" == list-unit-files ]]; then echo "$2 enabled"; exit 0; fi
if [[ "$1" == is-active ]]; then echo active; exit 0; fi
exit 0
""",
    )
    result = subprocess.run(
        ["bash", str(helper)],
        env={**os.environ, "SYSTEMCTL_BIN": str(fake)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "remains active" in result.stderr


def test_no_timers_helper_accepts_loaded_service_in_failed_state(tmp_path: Path) -> None:
    helper = AWS_DIR / "disable_systemd_timers.sh"
    fake = tmp_path / "systemctl"
    _write_executable(
        fake,
        """#!/usr/bin/env bash
if [[ "$1" == show ]]; then echo loaded; exit 0; fi
if [[ "$1" == is-active ]]; then echo failed; exit 3; fi
exit 0
""",
    )
    result = subprocess.run(
        ["bash", str(helper)],
        env={**os.environ, "SYSTEMCTL_BIN": str(fake)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "existing WeatherEdge timers disabled" in result.stdout


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


# ---------------------------------------------------------------------------
# 2026-07-28: a deploy's own verified snapshot occupied exactly the space the
# NEXT deploy's backup preflight required, and the sweep that would reclaim it
# ran only in `backup` mode, after the check it needed to satisfy.
# ---------------------------------------------------------------------------


def test_backup_preflight_sweeps_aged_snapshots_before_measuring_space(
    tmp_path: Path,
) -> None:
    """The sweep must run in preflight too, and before the free-space check.

    Previously it lived at the very end of `backup` mode, so a preflight could
    never reclaim anything and a same-week deploy blocked its own successor.
    """

    db_path = tmp_path / "paper.db"
    sqlite3.connect(db_path).close()
    backups = tmp_path / "backups"
    backups.mkdir()
    aged = backups / "paper_trading-20200101T000000Z.sqlite3"
    aged.write_bytes(b"stale")
    aged_sum = backups / "paper_trading-20200101T000000Z.sqlite3.sha256"
    aged_sum.write_text("deadbeef  aged\n")
    old = time.time() - 40 * 86400
    os.utime(aged, (old, old))
    os.utime(aged_sum, (old, old))

    fresh = backups / "paper_trading-29991231T235959Z.sqlite3"
    fresh.write_bytes(b"current")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_aws = fake_bin / "aws"
    _write_executable(fake_aws, "#!/bin/sh\nexit 0\n")

    result = subprocess.run(
        ["bash", str(AWS_DIR / "backup_paper_db.sh"), "preflight", str(db_path)],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SFO_WEATHEREDGE_ENV_FILE": str(tmp_path / "missing.env"),
            "SFO_ARCHIVE_S3_BUCKET": "weatheredge-test",
            "SFO_ARCHIVE_AWS_CLI": str(fake_aws),
            "SFO_DATABASE_BACKUP_DIR": str(backups),
            "SFO_DATABASE_BACKUP_KEEP_DAYS": "1",
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not aged.exists(), "aged snapshot should be reclaimed during preflight"
    assert not aged_sum.exists(), "aged checksum should be reclaimed too"
    assert fresh.exists(), "a snapshot inside the retention window must survive"


def test_backup_gate_requires_one_database_copy_not_two(tmp_path: Path) -> None:
    """The gate's arithmetic is the thing that matters, so pin it directly.

    Both sides of `available >= required` move with the database, so a 2x
    requirement tightens three times as fast as the file grows. On the live
    volume that produced a hard ceiling of ~10.3 GB against a journal growing
    ~690 MB/day -- about one deployable day per compaction. The snapshot is
    deleted once S3 holds it and before the restore copy is pulled, so only one
    copy is ever on disk and the requirement is db + 1 GiB.
    """

    db_path = tmp_path / "paper.db"
    sqlite3.connect(db_path).close()
    payload = b"x" * (5 * 1024 * 1024)
    with open(db_path, "ab") as handle:
        handle.write(payload)
    database_bytes = db_path.stat().st_size

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_aws = fake_bin / "aws"
    _write_executable(fake_aws, "#!/bin/sh\nexit 0\n")
    # Report exactly one byte less than a single copy plus 1 GiB needs.
    short_kib = (database_bytes + 1073741824 - 1) // 1024
    _write_executable(
        fake_bin / "df",
        "#!/bin/sh\n"
        "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'\n"
        f"printf 'fake 1 1 {short_kib} 100%% /\\n'\n",
    )

    result = subprocess.run(
        ["bash", str(AWS_DIR / "backup_paper_db.sh"), "preflight", str(db_path)],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SFO_WEATHEREDGE_ENV_FILE": str(tmp_path / "missing.env"),
            "SFO_ARCHIVE_S3_BUCKET": "weatheredge-test",
            "SFO_ARCHIVE_AWS_CLI": str(fake_aws),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, "one byte short must still fail"
    assert f"required={database_bytes + 1073741824}" in result.stderr, (
        "the gate must ask for ONE database copy plus 1 GiB, not two copies; "
        f"got: {result.stderr}"
    )


def test_backup_drops_the_local_snapshot_before_pulling_the_restore_copy() -> None:
    """Ordering is what makes the single-copy requirement true."""

    helper = _read_backup_helper()
    upload_idx = helper.index('s3 cp "$snapshot"')
    drop_idx = helper.index('rm -f -- "$snapshot"')
    download_idx = helper.index('s3 cp "s3://$BUCKET/$object_key" "$restore_copy"')
    assert upload_idx < drop_idx < download_idx, (
        "the local snapshot must be uploaded, then dropped, and only then may "
        "the restore copy be pulled -- otherwise two copies coexist and the "
        "gate genuinely does need 2x the database"
    )


def test_backup_hands_back_the_copy_that_survived_the_round_trip() -> None:
    helper = _read_backup_helper()
    verify_idx = helper.index("downloaded backup failed integrity_check")
    promote_idx = helper.index('mv -f -- "$restore_copy" "$snapshot"')
    report_idx = helper.index("WEATHEREDGE_BACKUP_SNAPSHOT=$snapshot")
    assert verify_idx < promote_idx < report_idx


def _read_backup_helper() -> str:
    return (AWS_DIR / "backup_paper_db.sh").read_text()


def _extract_shell_version_constant(source_file: Path, constant_name: str) -> str:
    """Run the deploy script's own extraction helper against a source file."""

    script = f"""
set -euo pipefail
{_READ_VERSION_HELPER}
read_source_version_constant "$1" "$2"
"""
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(source_file), constant_name],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_deploy_provenance_versions_match_imported_constants() -> None:
    """Audit F-07: the literal read must equal what importing would have given.

    sync_to_box.sh stamps build provenance by reading these constants out of the
    source files with sed rather than importing them, because importing needed a
    local interpreter and macOS resolves `python3` to a 3.9 build that cannot
    import this package at all. That trade is only safe while the literal read
    and the real constant agree, so pin them together here: if someone reformats
    either assignment (single quotes, a type annotation, a computed value), this
    fails instead of silently stamping an empty or stale version.
    """

    sys.path.insert(0, str(ROOT / "trading"))
    try:
        from sfo_kalshi_quant.account import ACCOUNTING_POLICY_VERSION
        from sfo_kalshi_quant.maker_fills import EXECUTION_MODEL_VERSION
    finally:
        sys.path.remove(str(ROOT / "trading"))

    execution = _extract_shell_version_constant(
        ROOT / "trading" / "sfo_kalshi_quant" / "maker_fills.py",
        "EXECUTION_MODEL_VERSION",
    )
    accounting = _extract_shell_version_constant(
        ROOT / "trading" / "sfo_kalshi_quant" / "account.py",
        "ACCOUNTING_POLICY_VERSION",
    )

    assert execution == EXECUTION_MODEL_VERSION
    assert accounting == ACCOUNTING_POLICY_VERSION
    assert execution, "execution model version must never stamp empty"
    assert accounting, "accounting policy version must never stamp empty"


def test_deploy_provenance_extraction_fails_loudly_on_missing_constant(
    tmp_path: Path,
) -> None:
    """A renamed or reformatted constant must abort the deploy, not stamp empty."""

    source = tmp_path / "module.py"
    source.write_text("SOMETHING_ELSE = \"x\"\n", encoding="utf-8")
    script = f"""
set -euo pipefail
{_READ_VERSION_HELPER}
read_source_version_constant "$1" "$2"
"""
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(source), "EXECUTION_MODEL_VERSION"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "could not read EXECUTION_MODEL_VERSION" in result.stderr
