from __future__ import annotations

import json
import importlib.util
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
SERVICES = tuple(timer.removesuffix(".timer") + ".service" for timer in TIMERS)


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def test_full_sync_transfers_root_install_inputs_from_arbitrary_cwd(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake bin"
    fake_bin.mkdir()
    calls = tmp_path / "rsync-calls.jsonl"
    ssh_calls = tmp_path / "ssh-calls.jsonl"
    _write_executable(
        fake_bin / "ssh",
        f"""#!{sys.executable}
import json, os, sys
with open(os.environ['SSH_CALLS'], 'a', encoding='utf-8') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
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
    assert "Restored 0 previously enabled WeatherEdge timer(s)." in result.stdout
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    # Three source syncs plus the PR-01 build_info.json provenance stamp.
    assert len(invocations) == 4
    assert invocations[-1][-1].endswith("/forecaster/build_info.json")
    packaging = next(call for call in invocations if str(ROOT / "pyproject.toml") in call)
    assert str(ROOT / "README.md") in packaging
    assert packaging[-1] == "ubuntu@ec2.example:/opt/weatheredge/"
    assert str(key) in " ".join(packaging)
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
    transfer_count = tmp_path / "transfer-count"
    ssh_log = tmp_path / "ssh.log"
    _write_executable(
        fake_bin / "ssh",
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SSH_LOG\"\n",
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
    action_log = tmp_path / "actions.log"
    transfer_count = tmp_path / "transfer-count"
    _write_executable(
        fake_bin / "ssh",
        "#!/bin/sh\nprintf 'ssh|%s\\n' \"$*\" >> \"$ACTION_LOG\"\n",
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
    assert actions[0].endswith("bash -s capture")
    assert actions[1].endswith("bash -s quiesce")
    assert "mkdir -p" in actions[2] and "chown" in actions[2]
    assert actions[3].startswith("rsync|")
    assert not any("enable" in action or "start" in action for action in actions)


def test_full_sync_reinstalls_units_and_restores_exact_enabled_timers_after_success(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
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

if args[-3:] == ['bash', '-s', 'capture']:
    print('sfo-operational-publish.timer')
    print('sfo-strategy-lab-refresh.timer')
    print('sfo-forecast-freshness.timer')
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
    capture_idx = next(i for i, line in enumerate(actions) if line.endswith("bash -s capture"))
    quiesce_idx = next(i for i, line in enumerate(actions) if line.endswith("bash -s quiesce"))
    first_rsync_idx = next(i for i, line in enumerate(actions) if line.startswith("rsync|"))
    install_idx = next(
        i for i, line in enumerate(actions) if "install_systemd_notimers.sh" in line
    )
    restore_idx = next(i for i, line in enumerate(actions) if line.startswith("restore|"))
    assert capture_idx < quiesce_idx < first_rsync_idx < install_idx < restore_idx
    assert actions[restore_idx] == (
        "restore|sfo-operational-publish.timer "
        "sfo-strategy-lab-refresh.timer sfo-forecast-freshness.timer"
    )
    assert "restored 3 previously enabled weatheredge timer(s)" in result.stdout.lower()


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
