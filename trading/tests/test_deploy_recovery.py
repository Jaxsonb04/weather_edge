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


def _recovery_block() -> str:
    source = (AWS_DIR / "sync_to_box.sh").read_text(encoding="utf-8")
    start = source.index("RUNTIME_RECOVERY_REQUIRED=1\n")
    end = source.index("\n# Historical analysis is diagnostic", start)
    return source[start:end]


@pytest.mark.parametrize("watchdog_enabled", [False, True])
@pytest.mark.parametrize(
    ("interruption", "expected_status"), [("kill -TERM \"$$\"", 143), ("exit 37", 37)]
)
def test_analysis_interruption_restores_captured_policy_and_original_status(
    tmp_path: Path, watchdog_enabled: bool, interruption: str, expected_status: int
) -> None:
    calls_path = tmp_path / "ssh-calls.jsonl"
    ssh_stub = tmp_path / "ssh"
    ssh_stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['DEPLOY_RECOVERY_CALLS'], 'a') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    ssh_stub.chmod(0o755)
    captured = ["sfo-forecaster-refresh.timer", "sfo-kalshi-paper-scan.timer"]
    if watchdog_enabled:
        captured.append("sfo-scheduler-health.timer")
    timer_arguments = " ".join(shlex.quote(timer) for timer in captured)
    script = f"""set -euo pipefail
SSH_OPTS=(-o BatchMode=yes)
REMOTE_USER=operator
HOST_IP=example.invalid
QUIESCE_HELPER={shlex.quote(str(AWS_DIR / 'disable_systemd_timers.sh'))}
DEPLOY_MAINTENANCE_MARKER=/run/weatheredge-deploy-maintenance
ENABLED_TIMERS=({timer_arguments})
unset SCHEDULER_WATCHDOG_ENABLED
{_recovery_block()}
# Interruption during the analysis stage, before later timer classification.
ANALYSIS_CACHE_REFRESHED=0
{interruption}
"""
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "DEPLOY_RECOVERY_CALLS": str(calls_path),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == expected_status, result.stderr
    assert "unbound variable" not in result.stderr
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    prefix = ["-o", "BatchMode=yes", "operator@example.invalid"]
    expected_calls = [
        [*prefix, "bash", "-s", "restore", *captured],
        [*prefix, "sudo rm -f -- '/run/weatheredge-deploy-maintenance'"],
    ]
    if watchdog_enabled:
        expected_calls.append(
            [*prefix, "sudo systemctl start sfo-scheduler-health.service"]
        )
    assert calls == expected_calls
