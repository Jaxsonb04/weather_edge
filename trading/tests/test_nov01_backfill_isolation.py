"""A failed dataset night must not skip the nightly forecaster maintenance.

sfo-dataset-backfill.service is a Type=oneshot unit, and systemd stops a oneshot
unit at its first failing command that lacks a "-" prefix (systemd.service(5)).
run_dataset_backfill.sh ran first without one, and it exits nonzero when a
source fails on consecutive runs or dataset research fails, so every such night
skipped the IEM truth backfill, the NWP archive and both EMOS rebuilds behind
it. The unit must still fail on those nights so OnFailure alerts.

These tests execute the real unit template's Exec lines in systemd's order
against the real dataset script with stub runtimes. Only systemd's documented
sequencing rule is emulated, because systemd cannot run here.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
TRADING_DIR = ROOT / "trading"
SCRIPT = TRADING_DIR / "deploy" / "aws" / "run_dataset_backfill.sh"
UNIT = TRADING_DIR / "deploy" / "aws" / "systemd" / "sfo-dataset-backfill.service.in"
DATASET_STEP = "/usr/bin/env bash __TRADING_DIR__/deploy/aws/run_dataset_backfill.sh"
# Truth and NWP rows must land before the EMOS rebuilds read them.
MAINTENANCE_ORDER = [
    "city_truth.py",
    "nwp_archive.py",
    "emos_forecast.py --lead 1",
    "emos_forecast.py --lead 2",
]
# systemd runs every ExecStart line and then every ExecStartPost line. Any
# other Exec directive would change that sequence, so the emulator refuses it.
MODELLED_DIRECTIVES = ("ExecStart", "ExecStartPost")
PLACEHOLDER = re.compile(r"__[A-Z_]+__")
EXIT_EXEC = 203  # systemd's status for a command it could not execute


DATASET_CLI_STUB = f"""#!{sys.executable}
# Stands in for `python -m sfo_kalshi_quant.cli` and the inline `python -c`
# helpers that run_dataset_backfill.sh calls.
import os, signal, sys
args = sys.argv[1:]
if args[:1] == ["-c"]:
    print(0 if "monotonic" in args[1] else "2026-11-01")
    raise SystemExit(0)
with open(os.environ["DATASET_CALL_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
if "dataset-backfill" in args:
    source = args[args.index("--source") + 1]
    if source in os.environ.get("FAIL_SOURCES", "").split(","):
        raise SystemExit(11)
if "dataset-research" in args:
    if os.environ.get("KILL_DATASET_RUN"):
        # End the dataset script the way an OOM kill (SIGKILL) or a stop
        # request (SIGTERM) would, before it can record how the run ended.
        os.kill(os.getppid(), getattr(signal, os.environ["KILL_DATASET_RUN"]))
    if os.environ.get("FAIL_RESEARCH") == "1":
        raise SystemExit(12)
"""

FORECASTER_PYTHON_STUB = f"""#!{sys.executable}
# Stands in for the forecaster venv's python and logs each maintenance step.
import os, sys
from pathlib import Path
script, *args = sys.argv[1:]
step = Path(script).name
if "--lead" in args:
    step += " --lead " + args[args.index("--lead") + 1]
with open(os.environ["MAINTENANCE_LOG"], "a") as log:
    log.write(step + "\\n")
raise SystemExit(1 if os.environ.get("FAIL_MAINTENANCE") == "1" else 0)
"""


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _exec_commands(unit_text: str) -> list[tuple[str, str]]:
    """Return (directive, command line) pairs in the order systemd runs them."""
    found: dict[str, list[str]] = {}
    for line in unit_text.splitlines():
        if line.startswith("Exec"):
            directive, command = line.split("=", 1)
            assert command, f"an empty {directive}= would reset the list"
            found.setdefault(directive, []).append(command)
    assert set(found) <= set(MODELLED_DIRECTIVES), sorted(found)
    return [
        (directive, command)
        for directive in MODELLED_DIRECTIVES
        for command in found.get(directive, [])
    ]


@dataclass
class UnitRun:
    failed: bool
    status: int  # exit status of the command that failed the unit, else 0
    maintenance: list[str]
    stderr: str


class NightlyRig:
    """The dataset backfill unit wired to stub runtimes in a temp tree."""

    def __init__(self, tmp_path: Path, **env: str) -> None:
        self.tmp_path = tmp_path
        self.forecaster = tmp_path / "forecaster"
        self.call_log = tmp_path / "dataset-calls.log"
        self.maintenance_log = tmp_path / "maintenance.log"
        self.state_dir = tmp_path / "data" / ".dataset-state"
        dataset_cli = tmp_path / "bin" / "python"
        _write_executable(dataset_cli, DATASET_CLI_STUB)
        _write_executable(
            self.forecaster / ".venv" / "bin" / "python", FORECASTER_PYTHON_STUB
        )
        (tmp_path / "trading").mkdir()
        inherited = {
            key: value for key, value in os.environ.items() if key != "INVOCATION_ID"
        }
        # What EnvironmentFile= supplies. The state directory stays at its
        # default beside the dataset DB, as in production.
        self.env = {
            **inherited,
            "SFO_TRADING_ROOT": str(tmp_path / "trading"),
            "SFO_TRADING_PYTHON": str(dataset_cli),
            "SFO_DATASET_DB": str(tmp_path / "data" / "paper_trading.db"),
            "SFO_DATASET_SOURCES": "iem-asos,hrrr",
            "SFO_DATASET_SOURCE_FAILURE_THRESHOLD": "2",
            "SFO_DATASET_START_DATE": "2026-10-22",
            "SFO_DATASET_END_DATE": "2026-11-01",
            "SFO_FORECASTER_ROOT": str(self.forecaster),
            "SFO_DATASET_RESEARCH_PATH": str(tmp_path / "dataset_research.json"),
            "DATASET_CALL_LOG": str(self.call_log),
            "MAINTENANCE_LOG": str(self.maintenance_log),
            **env,
        }
        self.placeholders = {
            "__TRADING_DIR__": str(TRADING_DIR),
            "__FORECASTER_DIR__": str(self.forecaster),
        }

    def _argv(self, command: str) -> list[str]:
        # These command lines use no quoting, %-specifiers or $VARIABLES, so
        # systemd's parsing reduces to splitting on whitespace. Splitting
        # before substitution keeps a checkout path with spaces one argument.
        assert not set(command) & set("'\"\\%$"), command
        argv = []
        for token in command.split():
            for placeholder in PLACEHOLDER.findall(token):
                token = token.replace(placeholder, self.placeholders[placeholder])
            argv.append(token)
        return argv

    def run_unit(self, *, dataset_step_starts: bool = True) -> UnitRun:
        """Activate the unit once under systemd's oneshot sequencing.

        A command that fails without a "-" prefix stops the sequence and fails
        the unit; a prefixed command's failure is ignored. Every process of one
        activation sees the same fresh INVOCATION_ID, as under systemd.
        """
        env = {**self.env, "INVOCATION_ID": uuid.uuid4().hex}
        self.maintenance_log.unlink(missing_ok=True)
        stderr = []
        for _directive, line in _exec_commands(UNIT.read_text(encoding="utf-8")):
            ignore_failure = line.startswith("-")
            command = line.removeprefix("-")
            assert not command.startswith(tuple("@:+!-")), f"unmodelled prefix: {line}"
            if command == DATASET_STEP and not dataset_step_starts:
                status = EXIT_EXEC
            else:
                result = subprocess.run(
                    self._argv(command),
                    env=env,
                    cwd=self.tmp_path,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                status = result.returncode
                stderr.append(result.stderr)
            if status != 0 and not ignore_failure:
                return UnitRun(True, status, self._maintenance(), "".join(stderr))
        return UnitRun(False, 0, self._maintenance(), "".join(stderr))

    def run_by_hand(self, *args: str) -> subprocess.CompletedProcess[str]:
        """An operator running the script directly, outside systemd."""
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=self.env,
            cwd=self.tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def _maintenance(self) -> list[str]:
        if not self.maintenance_log.exists():
            return []
        return self.maintenance_log.read_text(encoding="utf-8").splitlines()


def test_only_the_final_status_report_can_stop_the_unit() -> None:
    unit = UNIT.read_text(encoding="utf-8")
    lines = unit.splitlines()
    commands = _exec_commands(unit)

    # The dataset step still runs first: its lock-retry budget is measured from
    # the script's start against this unit's single start deadline.
    assert commands[0] == ("ExecStart", "-" + DATASET_STEP)
    assert "TimeoutStartSec=2700" in lines
    # Every other step is fail-soft, so no failure can skip a later step, and
    # the last command re-raises the dataset step's own result.
    unprefixed = [entry for entry in commands if not entry[1].startswith("-")]
    assert unprefixed == [("ExecStartPost", DATASET_STEP + " --report-status")]
    assert commands[-1] == unprefixed[0]
    assert "Type=oneshot" in lines
    assert "OnFailure=sfo-alert@%n.service" in lines


def test_healthy_night_runs_every_maintenance_step_in_order(tmp_path: Path) -> None:
    night = NightlyRig(tmp_path).run_unit()

    assert night.maintenance == MAINTENANCE_ORDER
    assert not night.failed, night.stderr


def test_source_failing_two_nights_running_still_gets_maintenance(tmp_path: Path) -> None:
    rig = NightlyRig(tmp_path, FAIL_SOURCES="hrrr")

    first = rig.run_unit()
    assert first.maintenance == MAINTENANCE_ORDER
    assert not first.failed, first.stderr  # one bad night per source is tolerated

    second = rig.run_unit()
    assert second.maintenance == MAINTENANCE_ORDER
    assert second.failed
    assert second.status == 1
    assert "hrrr(2)" in second.stderr


@pytest.mark.parametrize(
    ("env", "status", "message"),
    [
        ({"FAIL_RESEARCH": "1"}, 1, "dataset research failed"),
        ({"SFO_DATASET_LOCK_RETRY_ATTEMPTS": "11"}, 2, "SFO_DATASET_LOCK_RETRY_ATTEMPTS"),
    ],
    ids=["research-failure", "invalid-config"],
)
def test_failed_dataset_step_still_gets_maintenance_and_fails_with_its_status(
    tmp_path: Path, env: dict[str, str], status: int, message: str
) -> None:
    night = NightlyRig(tmp_path, **env).run_unit()

    assert night.maintenance == MAINTENANCE_ORDER
    assert night.failed
    assert night.status == status
    assert message in night.stderr


def test_failing_maintenance_does_not_hide_a_failed_dataset_night(tmp_path: Path) -> None:
    night = NightlyRig(tmp_path, FAIL_RESEARCH="1", FAIL_MAINTENANCE="1").run_unit()

    assert night.maintenance == MAINTENANCE_ORDER
    assert night.failed
    assert night.status == 1


@pytest.mark.parametrize("signal_name", ["SIGKILL", "SIGTERM"])
def test_dataset_run_killed_before_finishing_still_fails_the_unit(
    tmp_path: Path, signal_name: str
) -> None:
    # bash can run its EXIT trap with status 0 after a SIGTERM, so a run must
    # never be recorded as a success on its way out.
    night = NightlyRig(tmp_path, KILL_DATASET_RUN=signal_name).run_unit()

    assert night.maintenance == MAINTENANCE_ORDER
    assert night.failed


def test_status_left_by_an_earlier_night_never_decides_a_later_one(tmp_path: Path) -> None:
    rig = NightlyRig(tmp_path)
    assert not rig.run_unit().failed

    # A dataset step that cannot even start records nothing, so the previous
    # night's success must not pass this one.
    unstarted = rig.run_unit(dataset_step_starts=False)
    assert unstarted.maintenance == MAINTENANCE_ORDER
    assert unstarted.failed

    # Nor may a failed night fail the healthy night after it.
    rig.env["FAIL_RESEARCH"] = "1"
    assert rig.run_unit().failed
    del rig.env["FAIL_RESEARCH"]
    recovered = rig.run_unit()
    assert recovered.maintenance == MAINTENANCE_ORDER
    assert not recovered.failed, recovered.stderr


def test_manual_run_keeps_its_exit_status_and_records_nothing(tmp_path: Path) -> None:
    rig = NightlyRig(tmp_path, FAIL_RESEARCH="1")

    result = rig.run_by_hand()

    assert result.returncode == 1
    assert "dataset research failed" in result.stderr
    # Outside systemd there is no INVOCATION_ID and no report step to read one.
    assert list(rig.state_dir.iterdir()) == []


def test_report_and_mistyped_modes_never_start_a_backfill(tmp_path: Path) -> None:
    rig = NightlyRig(tmp_path)

    # Without systemd's INVOCATION_ID there is no run to report on.
    assert rig.run_by_hand("--report-status").returncode != 0
    assert rig.run_by_hand("--report-satus").returncode == 2
    assert not rig.call_log.exists()
