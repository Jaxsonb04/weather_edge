from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
AWS_DIR = ROOT / "trading" / "deploy" / "aws"
SYSTEMD_DIR = AWS_DIR / "systemd"
OPERATIONAL_SERVICES = {
    "sfo-forecaster-refresh.service.in",
    "sfo-operational-publish.service.in",
    "sfo-strategy-lab-refresh.service.in",
    "sfo-dataset-backfill.service.in",
    "sfo-kalshi-paper-scan.service.in",
    "sfo-kalshi-paper-monitor.service.in",
    "sfo-kalshi-paper-settle.service.in",
    "sfo-kalshi-paper-prune.service.in",
    "sfo-forecast-freshness.service.in",
}


def _system_rsync_is_macos_openrsync() -> bool:
    if sys.platform != "darwin" or not Path("/usr/bin/rsync").exists():
        return False
    result = subprocess.run(
        ["/usr/bin/rsync", "--version"], capture_output=True, text=True, check=False
    )
    return result.returncode == 0 and "openrsync" in result.stdout.lower()


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _write_fake_legacy_rsync(path: Path) -> None:
    _write_executable(
        path,
        """#!/bin/sh
if [ "$1" = --protect-args ] && [ "$2" = --version ]; then exit 1; fi
wrapper=
destination=
while [ "$#" -gt 0 ]; do
  if [ "$1" = -e ]; then
    wrapper="$2"
    shift 2
  else
    destination="$1"
    shift
  fi
done
remote_host=${destination%%:*}
remote_path=${destination#*:}
"$wrapper" "$remote_host" rsync --server "$remote_path"
""",
    )


def test_all_and_only_operational_services_alert_without_recursion() -> None:
    assert {path.name for path in SYSTEMD_DIR.glob("sfo-*.service.in")} == (
        OPERATIONAL_SERVICES | {"sfo-alert@.service.in"}
    )
    for name in OPERATIONAL_SERVICES:
        assert "OnFailure=sfo-alert@%n.service" in (SYSTEMD_DIR / name).read_text()
    alert = (SYSTEMD_DIR / "sfo-alert@.service.in").read_text()
    assert "OnFailure=" not in alert
    for installer_name in ("install_systemd.sh", "install_systemd_notimers.sh"):
        installer = (AWS_DIR / installer_name).read_text()
        assert "sfo-alert@.service.in" in installer
        assert "/etc/systemd/system/sfo-alert@.service" in installer


def test_alert_script_posts_json_without_url_in_arguments(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    args_log = tmp_path / "curl-args.json"
    stdin_log = tmp_path / "curl-stdin"
    _write_executable(
        fake_bin / "curl",
        f"""#!{sys.executable}
import json, os, sys
open(os.environ['ARGS_LOG'], 'w').write(json.dumps(sys.argv[1:]))
open(os.environ['STDIN_LOG'], 'w').write(sys.stdin.read())
""",
    )
    result = subprocess.run(
        ["bash", str(AWS_DIR / "send_systemd_failure_alert.sh"), "failed.service", "sfo-alert@failed.service.service"],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "ARGS_LOG": str(args_log),
            "STDIN_LOG": str(stdin_log),
            "SFO_FRESHNESS_ALERT_URL": "https://secret.example/topic-token",
            "SFO_ALERT_HOSTNAME": "weather host",
            "SFO_ALERT_TIMESTAMP": "2026-07-11T12:00:00Z",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "secret.example" not in " ".join(json.loads(args_log.read_text()))
    config = stdin_log.read_text()
    assert "secret.example" in config
    payload_line = next(line for line in config.splitlines() if line.startswith("data = "))
    payload = json.loads(json.loads(payload_line.removeprefix("data = ")))
    assert payload["message"] == "failed.service/sfo-alert@failed.service.service failed"
    assert payload["host"] == "weather host"
    assert payload["timestamp"] == "2026-07-11T12:00:00Z"


def test_alert_script_unset_url_warns_and_succeeds(tmp_path: Path) -> None:
    result = subprocess.run(
        ["/bin/bash", str(AWS_DIR / "send_systemd_failure_alert.sh"), "failed.service", "alert.service"],
        env={**os.environ, "SFO_FRESHNESS_ALERT_URL": "", "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "SFO_FRESHNESS_ALERT_URL is unset" in result.stderr


def test_alert_script_curl_failure_is_visible(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "curl", "#!/bin/sh\nexit 37\n")
    result = subprocess.run(
        ["bash", str(AWS_DIR / "send_systemd_failure_alert.sh"), "failed.service", "alert.service"],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SFO_FRESHNESS_ALERT_URL": "https://secret.example/token",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "alert POST failed" in result.stderr
    assert "secret.example" not in result.stderr


def _freshness_result(
    tmp_path: Path, df_output: str, *, alert_url: str = ""
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    db = tmp_path / "weather.db"
    db.touch()
    manifest = tmp_path / "publication_manifest.json"
    manifest.write_text("{}")
    python = fake_bin / "python"
    _write_executable(python, "#!/bin/sh\nexit 0\n")
    _write_executable(
        fake_bin / "df",
        f"#!{sys.executable}\nprint({df_output!r}.replace('\\\\n', '\\n'))\n",
    )
    _write_executable(
        fake_bin / "curl",
        "#!/bin/sh\necho called >> \"$CURL_LOG\"\nexit 0\n",
    )
    return subprocess.run(
        ["bash", str(AWS_DIR / "check_forecast_db_freshness.sh")],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "SFO_BASE_DIR": str(tmp_path),
            "SFO_FORECASTER_ROOT": str(tmp_path),
            "SFO_TRADING_ROOT": str(tmp_path),
            "SFO_TRADING_PYTHON": str(python),
            "SFO_FORECAST_DB": str(db),
            "SFO_PUBLICATION_MANIFEST_PATH": str(manifest),
            "SFO_FORECAST_STALE_MARKER": str(tmp_path / "STALE"),
            "SFO_DISK_USAGE_MAX_PERCENT": "85",
            "SFO_FRESHNESS_ALERT_URL": alert_url,
            "CURL_LOG": str(tmp_path / "curl.log"),
        },
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("used", "expected_ok"),
    [("84%", True), ("85%", False), ("bogus", False)],
)
def test_freshness_disk_threshold_and_malformed_df(
    tmp_path: Path, used: str, expected_ok: bool
) -> None:
    result = _freshness_result(
        tmp_path,
        f"Filesystem 1024-blocks Used Available Capacity Mounted on\\n/dev/fake 100 84 16 {used} /",
    )
    assert (result.returncode == 0) is expected_ok, result.stderr
    if expected_ok:
        assert "disk usage 84%" in result.stdout
    else:
        assert "disk usage" in result.stderr


def test_manual_freshness_failure_does_not_post_duplicate_webhook(tmp_path: Path) -> None:
    result = _freshness_result(
        tmp_path,
        "Filesystem 1024-blocks Used Available Capacity Mounted on\\n"
        "/dev/fake 100 85 15 85% /",
        alert_url="https://secret.example/topic",
    )
    assert result.returncode != 0
    assert "STALE:" in result.stderr
    assert not (tmp_path / "curl.log").exists()


def _fake_backfill_python(path: Path) -> None:
    _write_executable(
        path,
        f"""#!{sys.executable}
import os, sys
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as stream: stream.write(' '.join(args) + '\\n')
if 'dataset-backfill' in args:
    source = args[args.index('--source') + 1]
    if source in os.environ.get('FAIL_SOURCES', '').split(','): raise SystemExit(11)
if 'dataset-research' in args and os.environ.get('FAIL_RESEARCH') == '1': raise SystemExit(12)
if args[:1] == ['-c']:
    print(os.environ.get('FAKE_DATE', '2026-07-11'))
""",
    )


def _backfill_result(tmp_path: Path, sources: str, **extra: str) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    python = tmp_path / "python"
    _fake_backfill_python(python)
    trading = tmp_path / "trading"
    trading.mkdir()
    return subprocess.run(
        ["bash", str(AWS_DIR / "run_dataset_backfill.sh")],
        env={
            **os.environ,
            "SFO_TRADING_ROOT": str(trading),
            "SFO_TRADING_PYTHON": str(python),
            "SFO_DATASET_SOURCES": sources,
            "SFO_DATASET_START_DATE": "2026-07-01",
            "SFO_DATASET_END_DATE": "2026-07-11",
            "SFO_DATASET_DB": str(tmp_path / "data" / "paper.db"),
            "SFO_FORECASTER_ROOT": str(tmp_path / "forecaster"),
            "CALL_LOG": str(tmp_path / "calls"),
            **extra,
        },
        capture_output=True,
        text=True,
    )


def test_backfill_partial_failure_commits_success_then_exits_nonzero(tmp_path: Path) -> None:
    result = _backfill_result(tmp_path, "good,bad,also-good", FAIL_SOURCES="bad")
    assert result.returncode != 0
    assert "ERROR: 1 dataset source(s) failed: bad" in result.stderr
    calls = (tmp_path / "calls").read_text()
    assert calls.count("dataset-backfill") == 3
    assert "dataset-research" in calls


def test_backfill_success_exits_zero(tmp_path: Path) -> None:
    result = _backfill_result(tmp_path, "good,also-good")
    assert result.returncode == 0, result.stderr


def test_backfill_all_fail_and_research_failure_exit_nonzero(tmp_path: Path) -> None:
    all_fail = _backfill_result(tmp_path / "all", "bogus,invalid", FAIL_SOURCES="bogus,invalid")
    assert all_fail.returncode != 0
    assert "ERROR: 2 dataset source(s) failed: bogus,invalid" in all_fail.stderr
    research = _backfill_result(tmp_path / "research", "good", FAIL_RESEARCH="1")
    assert research.returncode != 0
    assert "dataset research failed" in research.stderr


def _fake_flock(path: Path) -> None:
    _write_executable(
        path,
        f"""#!{sys.executable}
import fcntl, os, sys
nonblocking = '-n' in sys.argv
fd = int(sys.argv[-1])
if '-u' in sys.argv:
    fcntl.flock(fd, fcntl.LOCK_UN)
    raise SystemExit(0)
try: fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
except BlockingIOError: raise SystemExit(1)
""",
    )


def test_paper_scan_persistent_lock_prevents_overlap(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_flock(fake_bin / "flock")
    python = tmp_path / "slow python"
    _write_executable(python, "#!/bin/sh\necho start >> \"$CALL_LOG\"\nsleep 1\necho done >> \"$CALL_LOG\"\n")
    trading = tmp_path / "base" / "trading"
    trading.mkdir(parents=True)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SFO_BASE_DIR": str(tmp_path / "base"),
        "SFO_TRADING_ROOT": str(trading),
        "SFO_TRADING_PYTHON": str(python),
        "CALL_LOG": str(tmp_path / "calls"),
    }
    first = subprocess.Popen(["bash", str(AWS_DIR / "run_paper_scan_profiles.sh")], env=env)
    time.sleep(0.2)
    second = subprocess.run(
        ["bash", str(AWS_DIR / "run_paper_scan_profiles.sh")], env=env, capture_output=True, text=True
    )
    assert first.wait() == 0
    assert second.returncode == 0
    assert "previous paper scan still running" in second.stdout
    assert (tmp_path / "calls").read_text().splitlines() == ["start", "done"]
    assert (tmp_path / "base" / ".locks" / "paper-scan.lock").exists()


def test_publish_and_scan_lock_defaults_are_persistent_and_documented() -> None:
    publisher = (AWS_DIR / "publish_forecaster_pages.sh").read_text()
    scan = (AWS_DIR / "run_paper_scan_profiles.sh").read_text()
    env = (AWS_DIR / "sfo-weather.env.example").read_text()
    assert "${SFO_PAGES_LOCK:-$BASE_DIR/.locks/pages-publish.lock}" in publisher
    assert "${SFO_PAPER_SCAN_LOCK:-$BASE_DIR/.locks/paper-scan.lock}" in scan
    assert 'mkdir -p "$(dirname "$PAGES_LOCK")"' in publisher
    assert 'mkdir -p "$(dirname "$SCAN_LOCK")"' in scan
    assert "/tmp/sfo-paper-scan.lock" not in env
    assert "SFO_PAPER_SCAN_LOCK=/opt/weatheredge/.locks/paper-scan.lock" in env
    assert "SFO_PAGES_LOCK=/opt/weatheredge/.locks/pages-publish.lock" in env


def test_pages_publish_persistent_lock_serializes_git_work(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_flock(fake_bin / "flock")
    _write_executable(
        fake_bin / "git",
        f"""#!{sys.executable}
import os, sys, time
if sys.argv[1:2] == ['init']:
    try: os.mkdir(os.environ['ACTIVE_DIR'])
    except FileExistsError: open(os.environ['OVERLAP_LOG'], 'a').write('overlap\\n')
    open(os.environ['GIT_LOG'], 'a').write('start\\n')
    time.sleep(0.4)
    if os.path.isdir(os.environ['ACTIVE_DIR']): os.rmdir(os.environ['ACTIVE_DIR'])
    open(os.environ['GIT_LOG'], 'a').write('done\\n')
    raise SystemExit(0)
if sys.argv[1:2] == ['fetch']: raise SystemExit(1)
if sys.argv[1:] == ['diff', '--cached', '--quiet']: raise SystemExit(0)
raise SystemExit(0)
""",
    )
    python = tmp_path / "python"
    artifacts = (
        "trading_signal.json", "forecast_data.json", "weather_story_data.json",
        "cities_data.json", "publication_manifest.json",
    )
    _write_executable(python, "#!/bin/sh\nprintf '%s\\n' " + " ".join(artifacts) + "\n")
    base = tmp_path / "base"
    forecaster = base / "forecaster"
    trading = base / "trading"
    webdist = base / "webdist"
    forecaster.mkdir(parents=True)
    trading.mkdir()
    webdist.mkdir()
    (webdist / "index.html").write_text("app")
    for artifact in artifacts:
        (forecaster / artifact).write_text("{}")
    key = tmp_path / "key"
    key.touch()
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SFO_PUBLISH_PAGES": "1",
        "SFO_BASE_DIR": str(base),
        "SFO_FORECASTER_ROOT": str(forecaster),
        "SFO_TRADING_ROOT": str(trading),
        "SFO_TRADING_PYTHON": str(python),
        "SFO_WEBDIST_DIR": str(webdist),
        "SFO_PAGES_DEPLOY_KEY": str(key),
        "SFO_ARTIFACT_GENERATION_LOCK": str(base / ".locks" / "artifact-generation.lock"),
        "GIT_LOG": str(tmp_path / "git.log"),
        "OVERLAP_LOG": str(tmp_path / "overlap.log"),
        "ACTIVE_DIR": str(tmp_path / "active"),
    }
    commands = [
        subprocess.Popen(["bash", str(AWS_DIR / "publish_forecaster_pages.sh")], env=env),
        subprocess.Popen(["bash", str(AWS_DIR / "publish_forecaster_pages.sh")], env=env),
    ]
    assert [command.wait() for command in commands] == [0, 0]
    assert not (tmp_path / "overlap.log").exists()
    assert (tmp_path / "git.log").read_text().splitlines() == ["start", "done", "start", "done"]
    assert (base / ".locks" / "pages-publish.lock").exists()


@pytest.mark.parametrize("installer", ["install_systemd.sh", "install_systemd_notimers.sh"])
def test_timezone_failure_cannot_be_ignored(installer: str) -> None:
    text = (AWS_DIR / installer).read_text()
    assert "timedatectl set-timezone America/Los_Angeles || true" not in text
    assert "timedatectl show -p Timezone --value" in text


def test_installer_timezone_failure_aborts_before_dependencies(tmp_path: Path) -> None:
    base = tmp_path / "base"
    (base / "trading" / "sfo_kalshi_quant").mkdir(parents=True)
    (base / "forecaster").mkdir()
    (base / "forecaster" / "google_weather_cache.py").touch()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    preflight_log = tmp_path / "timedatectl.log"
    mutation_log = tmp_path / "sudo.log"
    _write_executable(
        fake_bin / "timedatectl",
        "#!/bin/sh\necho \"$*\" >> \"$PREFLIGHT_LOG\"\nexit 42\n",
    )
    _write_executable(fake_bin / "sudo", "#!/bin/sh\necho \"$*\" >> \"$MUTATION_LOG\"\n")
    result = subprocess.run(
        ["bash", str(AWS_DIR / "install_systemd.sh")],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "BASE_DIR": str(base),
            "PREFLIGHT_LOG": str(preflight_log),
            "MUTATION_LOG": str(mutation_log),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 42
    assert preflight_log.read_text().splitlines() == ["show -p Timezone --value"]
    assert not mutation_log.exists()


def test_timerless_installer_timezone_failure_precedes_all_system_mutation(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base"
    (base / "trading" / "sfo_kalshi_quant").mkdir(parents=True)
    (base / "forecaster").mkdir()
    (base / "forecaster" / "google_weather_cache.py").touch()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    preflight_log = tmp_path / "timedatectl.log"
    sudo_log = tmp_path / "sudo.log"
    systemctl_log = tmp_path / "systemctl.log"
    _write_executable(
        fake_bin / "timedatectl",
        "#!/bin/sh\necho \"$*\" >> \"$PREFLIGHT_LOG\"\nexit 42\n",
    )
    _write_executable(fake_bin / "sudo", "#!/bin/sh\necho \"$*\" >> \"$SUDO_LOG\"\n")
    _write_executable(
        fake_bin / "systemctl",
        "#!/bin/sh\necho \"$*\" >> \"$SYSTEMCTL_LOG\"\nexit 99\n",
    )
    result = subprocess.run(
        ["bash", str(AWS_DIR / "install_systemd_notimers.sh")],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "BASE_DIR": str(base),
            "PREFLIGHT_LOG": str(preflight_log),
            "SUDO_LOG": str(sudo_log),
            "SYSTEMCTL_LOG": str(systemctl_log),
            "SYSTEMCTL_BIN": str(fake_bin / "systemctl"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 42
    assert preflight_log.read_text().splitlines() == ["show -p Timezone --value"]
    assert not sudo_log.exists()
    assert not systemctl_log.exists()


def test_regular_installer_refuses_timezone_mismatch_without_mutation(tmp_path: Path) -> None:
    base = tmp_path / "base"
    (base / "trading" / "sfo_kalshi_quant").mkdir(parents=True)
    (base / "forecaster").mkdir()
    (base / "forecaster" / "google_weather_cache.py").touch()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "timedatectl", "#!/bin/sh\nprintf 'UTC\\n'\n")
    mutation_log = tmp_path / "mutation.log"
    _write_executable(fake_bin / "sudo", "#!/bin/sh\necho \"$*\" >> \"$MUTATION_LOG\"\n")
    result = subprocess.run(
        ["bash", str(AWS_DIR / "install_systemd.sh")],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "BASE_DIR": str(base),
            "MUTATION_LOG": str(mutation_log),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "install_systemd_notimers.sh" in result.stderr
    assert not mutation_log.exists()


def test_timerless_timezone_mismatch_quiesces_before_set(tmp_path: Path) -> None:
    base = tmp_path / "base"
    (base / "trading" / "sfo_kalshi_quant").mkdir(parents=True)
    (base / "forecaster").mkdir()
    (base / "forecaster" / "google_weather_cache.py").touch()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    order_log = tmp_path / "order.log"
    _write_executable(
        fake_bin / "timedatectl",
        "#!/bin/sh\necho \"timedatectl $*\" >> \"$ORDER_LOG\"\nprintf 'UTC\\n'\n",
    )
    _write_executable(
        fake_bin / "systemctl",
        """#!/bin/sh
echo "systemctl $*" >> "$ORDER_LOG"
if [ "$1" = show ]; then echo loaded; exit 0; fi
if [ "$1" = is-active ]; then echo inactive; exit 3; fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "sudo",
        "#!/bin/sh\necho \"sudo $*\" >> \"$ORDER_LOG\"\nexit 55\n",
    )
    result = subprocess.run(
        ["bash", str(AWS_DIR / "install_systemd_notimers.sh")],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "BASE_DIR": str(base),
            "SYSTEMCTL_BIN": str(fake_bin / "systemctl"),
            "ORDER_LOG": str(order_log),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 55
    calls = order_log.read_text().splitlines()
    assert calls[0] == "timedatectl show -p Timezone --value"
    assert any(call.startswith("systemctl stop ") for call in calls[1:-1])
    assert calls[-1] == "sudo timedatectl set-timezone America/Los_Angeles"
    assert not any("apt-get" in call for call in calls)


def test_paper_scan_truthy_is_bash3_portable_and_case_insensitive(tmp_path: Path) -> None:
    text = (AWS_DIR / "run_paper_scan_profiles.sh").read_text()
    assert "${1,,}" not in text
    assert "tr '[:upper:]' '[:lower:]'" in text
    trading = tmp_path / "trading"
    trading.mkdir()
    python = tmp_path / "python"
    _write_executable(python, "#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$CALL_LOG\"\n")
    result = subprocess.run(
        ["/bin/bash", str(AWS_DIR / "run_paper_scan_profiles.sh")],
        env={
            **os.environ,
            "SFO_TRADING_ROOT": str(trading),
            "SFO_TRADING_PYTHON": str(python),
            "SFO_PAPER_SCAN_LOCK": str(tmp_path / "lock"),
            "SFO_PAPER_PLACE_ORDERS": "TRUE",
            "CALL_LOG": str(tmp_path / "call"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--place-paper" in (tmp_path / "call").read_text()


def test_deploy_web_app_works_from_arbitrary_cwd_and_space_paths(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"
    for command in ("bun", "ssh"):
        _write_executable(
            fake_bin / command,
            f"#!/bin/sh\nprintf '{command} cwd=%s args=' \"$PWD\" >> \"$CALL_LOG\"\nprintf '<%s>' \"$@\" >> \"$CALL_LOG\"\nprintf '\\n' >> \"$CALL_LOG\"\n[ \"${{FAIL_COMMAND:-}}\" != {command} ]\n",
        )
    _write_executable(
        fake_bin / "rsync",
        """#!/bin/sh
if [ "$1" = --protect-args ] && [ "$2" = --version ]; then exit 0; fi
printf 'rsync cwd=%s args=' "$PWD" >> "$CALL_LOG"
printf '<%s>' "$@" >> "$CALL_LOG"
printf '\n' >> "$CALL_LOG"
[ "${FAIL_COMMAND:-}" != rsync ]
""",
    )
    key = tmp_path / "operator key.pem"
    key.write_text("key")
    env_file = tmp_path / "target env"
    env_file.write_text(
        f"EC2_IP=host.example\nEC2_KEY='{key}'\nREMOTE_USER=deploy\nREMOTE_BASE='/srv/weather edge'\n"
    )
    result = subprocess.run(
        ["bash", str(AWS_DIR / "deploy_web_app.sh"), str(env_file)],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "CALL_LOG": str(log)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert calls[0].startswith(f"bun cwd={ROOT} args=<run><build>")
    assert calls[1].startswith("ssh ")
    assert "operator key.pem" not in calls[2]
    assert "<--protect-args>" in calls[2]
    assert "<deploy@host.example:/srv/weather edge/webdist/>" in calls[2]
    wrapper_path = calls[2].split("<-e><", 1)[1].split(">", 1)[0]
    assert " " not in wrapper_path
    assert not Path(wrapper_path).exists()
    assert calls[-1].startswith("ssh ")
    assert "Done" in result.stdout


def test_legacy_rsync_rejects_spaced_remote_path_before_build(tmp_path: Path) -> None:
    key = tmp_path / "operator key.pem"
    key.touch()
    env_file = tmp_path / "target env"
    env_file.write_text(
        f"EC2_IP=host.example\nEC2_KEY='{key}'\nREMOTE_BASE='/srv/weather edge'\n"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_legacy_rsync(fake_bin / "legacy-rsync")
    build_log = tmp_path / "build.log"
    _write_executable(fake_bin / "bun", "#!/bin/sh\necho built > \"$BUILD_LOG\"\n")
    result = subprocess.run(
        ["bash", str(AWS_DIR / "deploy_web_app.sh"), str(env_file)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "RSYNC_BIN": str(fake_bin / "legacy-rsync"),
            "BUILD_LOG": str(build_log),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "does not support --protect-args" in result.stderr
    assert "REMOTE_BASE must match" in result.stderr
    assert not build_log.exists()


def test_legacy_rsync_safe_remote_path_proceeds_with_spaced_key(tmp_path: Path) -> None:
    key = tmp_path / "operator key.pem"
    key.touch()
    env_file = tmp_path / "target env"
    env_file.write_text(f"EC2_IP=host.example\nEC2_KEY='{key}'\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_legacy_rsync(fake_bin / "legacy-rsync")
    build_log = tmp_path / "build.log"
    ssh_log = tmp_path / "ssh.jsonl"
    _write_executable(fake_bin / "bun", "#!/bin/sh\necho built > \"$BUILD_LOG\"\n")
    _write_executable(
        fake_bin / "ssh",
        f"""#!{sys.executable}
import json, os, sys
with open(os.environ['SSH_LOG'], 'a') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')
raise SystemExit(19 if 'rsync' in sys.argv[1:] else 0)
""",
    )
    result = subprocess.run(
        ["bash", str(AWS_DIR / "deploy_web_app.sh"), str(env_file)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "RSYNC_BIN": str(fake_bin / "legacy-rsync"),
            "BUILD_LOG": str(build_log),
            "SSH_LOG": str(ssh_log),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 19
    assert build_log.exists()
    calls = [json.loads(line) for line in ssh_log.read_text().splitlines()]
    rsync_call = next(call for call in calls if "rsync" in call)
    assert rsync_call[rsync_call.index("-i") + 1] == str(key)
    assert rsync_call[-1] == "/opt/weatheredge/webdist/"


@pytest.mark.skipif(
    not _system_rsync_is_macos_openrsync(),
    reason="requires macOS /usr/bin/openrsync",
)
def test_macos_rsync_parser_preserves_spaced_key_and_remote_path(tmp_path: Path) -> None:
    probe_dir = Path(tempfile.mkdtemp(prefix="weatheredge-rsync-probe-", dir="/tmp"))
    try:
        source = tmp_path / "source dir"
        source.mkdir()
        (source / "index.html").touch()
        key = tmp_path / "operator key.pem"
        key.touch()
        log = tmp_path / "ssh-args.json"
        fake_ssh = probe_dir / "ssh-probe"
        wrapper = probe_dir / "ssh-wrapper"
        _write_executable(
            fake_ssh,
            f"#!{sys.executable}\nimport json, os, sys\nopen(os.environ['SSH_ARGS_LOG'], 'w').write(json.dumps(sys.argv[1:]))\nraise SystemExit(19)\n",
        )
        _write_executable(
            wrapper,
            "#!/bin/sh\nexec \"$FAKE_SSH\" -i \"$WEATHEREDGE_RSYNC_SSH_KEY\" -o StrictHostKeyChecking=accept-new \"$@\"\n",
        )
        remote_path = "/srv/weather edge/webdist/"
        result = subprocess.run(
            [
                "/usr/bin/rsync", "-a", "-e", str(wrapper),
                f"{source}/", f"deploy@host:{remote_path}",
            ],
            env={
                **os.environ,
                "FAKE_SSH": str(fake_ssh),
                "WEATHEREDGE_RSYNC_SSH_KEY": str(key),
                "SSH_ARGS_LOG": str(log),
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode == 19
        args = json.loads(log.read_text())
        assert args[args.index("-i") + 1] == str(key)
        assert args[-1] == remote_path
        assert " " not in str(wrapper)
    finally:
        shutil.rmtree(probe_dir)


@pytest.mark.parametrize(
    "remote_base",
    [
        "/opt/weatheredge;touch",
        "/opt/$HOME",
        "/opt/`id`",
        "/opt/weather*",
        "/opt/weather?",
        "/opt/weather|edge",
        "/opt/weather<edge",
        "/opt/weather>edge",
        "/opt/weather edge",
        '/opt/weather"edge',
        "/opt/weather'edge",
        "/opt/weather\\edge",
        "relative/path",
        "/opt/../weatheredge",
        "/../opt/weatheredge",
        "/opt/weatheredge/..",
    ],
)
def test_legacy_rsync_rejects_unsafe_remote_base_before_any_action(
    tmp_path: Path, remote_base: str
) -> None:
    key = tmp_path / "operator key.pem"
    key.touch()
    env_file = tmp_path / "target env"
    env_file.write_text(f"EC2_IP=host.example\nEC2_KEY='{key}'\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    legacy_rsync = fake_bin / "legacy-rsync"
    _write_fake_legacy_rsync(legacy_rsync)
    action_log = tmp_path / "actions.log"
    for command in ("bun", "ssh"):
        _write_executable(
            fake_bin / command,
            f"#!/bin/sh\necho {command} >> \"$ACTION_LOG\"\nexit 77\n",
        )
    result = subprocess.run(
        ["bash", str(AWS_DIR / "deploy_web_app.sh"), str(env_file)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "RSYNC_BIN": str(legacy_rsync),
            "REMOTE_BASE": remote_base,
            "ACTION_LOG": str(action_log),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "REMOTE_BASE" in result.stderr
    assert not action_log.exists()


def test_deploy_uses_ephemeral_keyless_ssh_wrapper() -> None:
    deployer = (AWS_DIR / "deploy_web_app.sh").read_text()
    assert "--protect-args" in deployer
    assert "printf -v RSYNC_RSH" not in deployer
    assert "WEATHEREDGE_RSYNC_SSH_KEY" in deployer
    assert "trap" in deployer


def test_deploy_web_app_failure_never_reports_success(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "bun", "#!/bin/sh\nexit 23\n")
    _write_executable(
        fake_bin / "rsync",
        "#!/bin/sh\n[ \"$1\" = --protect-args ] && [ \"$2\" = --version ]\n",
    )
    key = tmp_path / "key"
    key.touch()
    env_file = tmp_path / "env"
    env_file.write_text(f"EC2_IP=host\nEC2_KEY='{key}'\n")
    result = subprocess.run(
        ["bash", str(AWS_DIR / "deploy_web_app.sh"), str(env_file)],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 23
    assert "Done" not in result.stdout


def test_forecaster_policy_syncs_committed_inputs_but_protects_runtime() -> None:
    policy = (AWS_DIR / "forecaster-runtime.rsync-filter").read_text()
    for script_name in ("sync_to_box.sh", "sync_forecaster_source.sh"):
        script = (AWS_DIR / script_name).read_text()
        assert '--exclude "forecast_data.json"' not in script
        assert '--exclude "weather_story_data.json"' not in script
    assert "forecast_data.json" not in policy
    assert "weather_story_data.json" not in policy
    for protected in (
        "google_weather_cache.json",
        "trading_signal.json",
        "strategy_research.json",
        "cities_data.json",
        "weather.db",
        "*-wal",
        "*-shm",
        "models/",
        "STALE_FORECAST",
    ):
        assert protected in policy


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_forecaster_filter_behavior_copies_inputs_and_preserves_runtime(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "forecast_data.json").write_text("committed forecast")
    (source / "weather_story_data.json").write_text("committed story")
    for name in ("trading_signal.json", "weather.db", "STALE_FORECAST"):
        (source / name).write_text("source runtime")
        (target / name).write_text("production runtime")
    result = subprocess.run(
        [
            "rsync", "-a", "--delete",
            f"--exclude-from={AWS_DIR / 'forecaster-runtime.rsync-filter'}",
            f"{source}/", f"{target}/",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (target / "forecast_data.json").read_text() == "committed forecast"
    assert (target / "weather_story_data.json").read_text() == "committed story"
    for name in ("trading_signal.json", "weather.db", "STALE_FORECAST"):
        assert (target / name).read_text() == "production runtime"


def test_verify_runner_uses_pytest_and_not_obsolete_direct_runner() -> None:
    runner = (ROOT / "scripts" / "run_tests.sh").read_text()
    assert "pytest" in runner
