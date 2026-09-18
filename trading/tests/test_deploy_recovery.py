"""Exercise deployment interruption recovery before analysis has completed."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest


AWS_DIR = Path(__file__).resolve().parents[1] / "deploy" / "aws"

DEADMAN_BIN = "/usr/local/libexec/weatheredge/deploy_deadman.sh"
DEADMAN_DEPLOY_ID = "20260918T031500Z-48213-19274"


def _recovery_block() -> str:
    source = (AWS_DIR / "sync_to_box.sh").read_text(encoding="utf-8")
    start = source.index("RUNTIME_RECOVERY_REQUIRED=1\n")
    end = source.index("\n# --- end deployment runtime recovery", start)
    return source[start:end]


def _deadman_host_block() -> str:
    """The host side of the dead-man, verbatim from sync_to_box.sh.

    The recovery trap now consults it before it touches the box, so the block
    has to come along; extracting it keeps this test on the real code the same
    way ``_recovery_block`` does.
    """

    source = (AWS_DIR / "sync_to_box.sh").read_text(encoding="utf-8")
    start = source.index("# --- box-side deploy dead-man, host side")
    end = source.index("# --- end box-side deploy dead-man, host side", start)
    return source[start:end]


@pytest.mark.parametrize("watchdog_enabled", [False, True])
@pytest.mark.parametrize(
    ("interruption", "expected_status"), [("kill -TERM \"$$\"", 143), ("exit 37", 37)]
)
@pytest.mark.parametrize("beat_status", [0, 255])
def test_analysis_interruption_restores_captured_policy_and_original_status(
    tmp_path: Path,
    watchdog_enabled: bool,
    interruption: str,
    expected_status: int,
    beat_status: int,
) -> None:
    """A live (0) or merely unreachable (255) dead-man leaves recovery unchanged."""

    calls_path = tmp_path / "ssh-calls.jsonl"
    _write_ssh_stub(tmp_path)
    captured = ["sfo-forecaster-refresh.timer", "sfo-kalshi-paper-scan.timer"]
    if watchdog_enabled:
        captured.append("sfo-scheduler-health.timer")
    script = _recovery_script(tmp_path, captured, interruption)
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "DEPLOY_RECOVERY_CALLS": str(calls_path),
            "FAKE_BEAT_STATUS": str(beat_status),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == expected_status, result.stderr
    assert "unbound variable" not in result.stderr
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    prefix = ["-o", "BatchMode=yes", "operator@example.invalid"]
    beat_prefix = [
        "-o",
        "BatchMode=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "operator@example.invalid",
    ]
    expected_calls = [
        [*beat_prefix, f"sudo -n '{DEADMAN_BIN}' beat '{DEADMAN_DEPLOY_ID}'"],
        [*prefix, "bash", "-s", "restore", *captured],
        [*prefix, "sudo rm -f -- '/run/weatheredge-deploy-maintenance'"],
    ]
    if watchdog_enabled:
        expected_calls.append(
            [*prefix, "sudo systemctl start sfo-scheduler-health.service"]
        )
    # A recovery that put the runtime back owns the box, so it takes the
    # box-side dead-man down with it instead of leaving one armed to re-enable
    # timers it has already enabled.
    expected_calls.append(
        [*prefix, f"sudo '{DEADMAN_BIN}' disarm '{DEADMAN_DEPLOY_ID}'"]
    )
    assert calls == expected_calls


def test_analysis_interruption_defers_to_a_dead_man_that_already_acted(
    tmp_path: Path,
) -> None:
    """A beat that answers 14 means the box-side dead-man already restored (or
    deliberately did not). The trap must then change nothing remotely."""

    calls_path = tmp_path / "ssh-calls.jsonl"
    _write_ssh_stub(tmp_path)
    script = _recovery_script(
        tmp_path,
        ["sfo-forecaster-refresh.timer", "sfo-scheduler-health.timer"],
        "exit 37",
    )
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "DEPLOY_RECOVERY_CALLS": str(calls_path),
            "FAKE_BEAT_STATUS": "14",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 70, result.stderr
    assert "This deploy is revoked" in result.stderr
    assert f"sudo {DEADMAN_BIN} status" in result.stderr
    assert "last-action" in result.stderr
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert len(calls) == 1
    assert calls[0][-1] == f"sudo -n '{DEADMAN_BIN}' beat '{DEADMAN_DEPLOY_ID}'"


def test_analysis_interruption_holds_a_dead_man_it_deliberately_left_quiesced(
    tmp_path: Path,
) -> None:
    """When the restore fails the trap re-quiesces the box on purpose. A
    post-install dead-man would re-enable every one of those timers about a
    lease later, so it has to be moved to ATTENTION instead."""

    calls_path = tmp_path / "ssh-calls.jsonl"
    _write_ssh_stub(tmp_path, fail_restore=True)
    script = _recovery_script(
        tmp_path,
        ["sfo-forecaster-refresh.timer", "sfo-scheduler-health.timer"],
        "exit 37",
    )
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "DEPLOY_RECOVERY_CALLS": str(calls_path),
            "FAKE_BEAT_STATUS": "0",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 37, result.stderr
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    commands = [call[-1] for call in calls]
    # It re-quiesced deliberately, never released the marker, and held the
    # dead-man so it cannot undo that decision.
    assert any(call[-3:] == ["bash", "-s", "quiesce"] for call in calls)
    assert not any("rm -f -- '/run/weatheredge-deploy-maintenance'" in c for c in commands)
    assert commands[-1] == f"sudo '{DEADMAN_BIN}' hold '{DEADMAN_DEPLOY_ID}'"
    assert f"sudo '{DEADMAN_BIN}' disarm " not in " ".join(commands)
    assert "moved to ATTENTION" in result.stderr


@pytest.mark.parametrize("beat_status", [1, 2, 11, 12, 126])
def test_an_uninterpretable_beat_never_suppresses_the_host_side_recovery(
    tmp_path: Path, beat_status: int
) -> None:
    """Only 14 and 127 prove the dead-man acted. A payload that could not write
    a full /var exits 1, and reading that as "already restored" would switch the
    host recovery off and leave production quiesced and dark for good."""

    calls_path = tmp_path / "ssh-calls.jsonl"
    _write_ssh_stub(tmp_path)
    captured = ["sfo-forecaster-refresh.timer", "sfo-scheduler-health.timer"]
    script = _recovery_script(tmp_path, captured, "exit 37")
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "DEPLOY_RECOVERY_CALLS": str(calls_path),
            "FAKE_BEAT_STATUS": str(beat_status),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 37, result.stderr
    assert "This deploy is revoked" not in result.stderr
    assert f"unexpected box-side dead-man beat status={beat_status}" in result.stderr
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert any("restore" in call for call in calls)
    assert any(
        "sudo rm -f -- '/run/weatheredge-deploy-maintenance'" in call for call in calls
    )


def _write_ssh_stub(tmp_path: Path, *, fail_restore: bool = False) -> None:
    ssh_stub = tmp_path / "ssh"
    ssh_stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['DEPLOY_RECOVERY_CALLS'], 'a') as handle:\n"
        "    handle.write(json.dumps(args) + '\\n')\n"
        "if args and ' beat ' in args[-1]:\n"
        "    raise SystemExit(int(os.environ['FAKE_BEAT_STATUS']))\n"
        f"if {fail_restore!r} and 'restore' in args:\n"
        "    raise SystemExit(4)\n",
        encoding="utf-8",
    )
    ssh_stub.chmod(0o755)


def _recovery_script(tmp_path: Path, captured: list[str], interruption: str) -> str:
    timer_arguments = " ".join(shlex.quote(timer) for timer in captured)
    return f"""set -euo pipefail
SSH_OPTS=(-o BatchMode=yes)
REMOTE_USER=operator
HOST_IP=example.invalid
QUIESCE_HELPER={shlex.quote(str(AWS_DIR / 'disable_systemd_timers.sh'))}
DEPLOY_MAINTENANCE_MARKER=/run/weatheredge-deploy-maintenance
ENABLED_TIMERS=({timer_arguments})
DEADMAN_ENABLED=1
DEADMAN_BIN={shlex.quote(DEADMAN_BIN)}
DEADMAN_STATE_DIR=/var/lib/weatheredge/deploy-deadman
DEADMAN_FENCE={shlex.quote(str(tmp_path / 'deadman-fence'))}
DEADMAN_BEAT_PID=""
DEPLOY_ID={shlex.quote(DEADMAN_DEPLOY_ID)}
MAIN_PID=$$
unset SCHEDULER_WATCHDOG_ENABLED
{_deadman_host_block()}
{_recovery_block()}
# Interruption during the analysis stage, before later timer classification.
ANALYSIS_CACHE_REFRESHED=0
{interruption}
"""
