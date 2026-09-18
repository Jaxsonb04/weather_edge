"""Drive the box-side deploy dead-man payload directly.

Every path here runs the real ``trading/deploy/aws/deploy_deadman.sh`` against a
fake ``systemctl`` and a throwaway box layout, because the box it protects
(13.52.240.76) cannot be reached from anywhere we control and the very first
deploy that carries this change is the one it has to protect.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from test_deploy_shell_behavior import _write_executable


ROOT = Path(__file__).resolve().parents[2]
AWS_DIR = ROOT / "trading" / "deploy" / "aws"
DEADMAN = AWS_DIR / "deploy_deadman.sh"
QUIESCE_HELPER = AWS_DIR / "disable_systemd_timers.sh"

DEPLOY_ID = "20260918T031500Z-48213-19274"
SOURCE_SHA = "daee13d01f2ec0ffee0123456789abcdef012345"
BOOT_ID = "4f1a8c2e-0000-4000-8000-000000000001"

# A v2 host: the retired Apple refresh timer is still enabled there, and the old
# release's scheduler watchdog still requires it.
CAPTURED_TIMERS = (
    "sfo-kalshi-paper-scan.timer",
    "weatheredge-apple-refresh.timer",
    "sfo-scheduler-health.timer",
)

# systemctl verbs that change a WeatherEdge unit's state. The dead-man's own
# units are excluded: tearing down its trigger pair and raising its best-effort
# alert instance touch no production timer.
_MUTATING = ("enable", "disable", "start", "stop", "restart")
_OWN_UNITS = (
    "weatheredge-deploy-deadman.timer",
    "weatheredge-deploy-deadman.service",
    "sfo-alert@weatheredge-deploy-deadman.service",
)


class Box:
    """A throwaway box layout with every DEADMAN_* path redirected into tmp."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.state_dir = tmp_path / "var-lib" / "deploy-deadman"
        self.cron_file = tmp_path / "cron.d" / "weatheredge-deploy-deadman"
        self.unit_dir = tmp_path / "systemd-system"
        self.libexec = tmp_path / "libexec"
        self.self_path = self.libexec / "deploy_deadman.sh"
        self.marker = tmp_path / "run" / "weatheredge-deploy-maintenance"
        self.boot_id_file = tmp_path / "boot_id"
        self.remote_base = tmp_path / "opt" / "weatheredge"
        self.systemctl_log = tmp_path / "systemctl.log"
        self.enabled_state = tmp_path / "systemctl-enabled"
        self.flock = tmp_path / "bin" / "flock-missing"

        for directory in (
            self.state_dir.parent,
            self.cron_file.parent,
            self.unit_dir,
            self.libexec,
            self.marker.parent,
            self.remote_base / "forecaster",
            self.remote_base / "trading" / "sfo_kalshi_quant",
            tmp_path / "bin",
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.self_path.write_bytes(DEADMAN.read_bytes())
        self.self_path.chmod(0o755)
        self.boot_id_file.write_text(BOOT_ID + "\n", encoding="utf-8")
        _write_executable(
            tmp_path / "bin" / "systemctl",
            """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
verb="$1"
shift
case "$verb" in
  enable)
    if [ -n "$FAKE_RESTORE_STATUS" ]; then exit "$FAKE_RESTORE_STATUS"; fi
    for unit in "$@"; do
      case "$unit" in --*) continue ;; esac
      printf '%s\\n' "$unit" >> "$FAKE_ENABLED_STATE"
    done
    exit 0 ;;
  is-enabled|is-active)
    for unit in "$@"; do
      case "$unit" in --*) continue ;; esac
      if [ -f "$FAKE_ENABLED_STATE" ] && grep -qx "$unit" "$FAKE_ENABLED_STATE"; then
        exit 0
      fi
      exit 1
    done
    exit 1 ;;
  show)
    printf 'loaded\\n'
    exit 0 ;;
esac
exit 0
""",
        )
        self.systemctl = tmp_path / "bin" / "systemctl"

    # -- running -------------------------------------------------------------
    def env(self, **overrides: str) -> dict[str, str]:
        env = {
            **os.environ,
            "DEADMAN_STATE_DIR": str(self.state_dir),
            "DEADMAN_SELF": str(self.self_path),
            "DEADMAN_CRON_FILE": str(self.cron_file),
            "DEADMAN_UNIT_DIR": str(self.unit_dir),
            "DEADMAN_LOCK_FILE": str(self.root / "deadman.lock"),
            "DEADMAN_MARKER": str(self.marker),
            "DEADMAN_BOOT_ID_FILE": str(self.boot_id_file),
            "DEADMAN_REMOTE_BASE": str(self.remote_base),
            "SYSTEMCTL_BIN": str(self.systemctl),
            "FLOCK_BIN": str(self.flock),
            "FAKE_SYSTEMCTL_LOG": str(self.systemctl_log),
            "FAKE_ENABLED_STATE": str(self.enabled_state),
            "FAKE_RESTORE_STATUS": "",
        }
        env.update(overrides)
        return env

    def run(
        self, *args: str, stdin: bytes | None = None, **env_overrides: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.self_path), *args],
            input=stdin.decode("utf-8") if stdin is not None else "",
            env=self.env(**env_overrides),
            capture_output=True,
            text=True,
            timeout=60,
        )

    # -- arrangement ---------------------------------------------------------
    def arm(
        self,
        *timers: str,
        phase: str = "pre-transfer",
        deploy_id: str = DEPLOY_ID,
        pre_transfer_restore: str = "1",
        tick_wait: str = "0",
        extra: tuple[str, ...] = (),
        **env_overrides: str,
    ) -> subprocess.CompletedProcess[str]:
        return self.run(
            "arm",
            "--deploy-id",
            deploy_id,
            "--phase",
            phase,
            "--source-sha",
            SOURCE_SHA,
            "--deploy-host",
            "deploy-mac",
            "--lease-seconds",
            "600",
            "--tick-wait-seconds",
            tick_wait,
            "--pre-transfer-restore",
            pre_transfer_restore,
            "--install-helper",
            *extra,
            "--",
            *timers,
            stdin=QUIESCE_HELPER.read_bytes(),
            **env_overrides,
        )

    def expire_lease(self) -> None:
        (self.state_dir / "heartbeat").write_text("1 1970-01-01T00:00:01Z\n", encoding="utf-8")

    def hold_marker(self) -> None:
        self.marker.write_text("", encoding="utf-8")

    def stamp_release(self, source_sha: str = SOURCE_SHA) -> Path:
        build_info = self.remote_base / "forecaster" / "build_info.json"
        build_info.write_text(
            '{\n  "source_sha": "%s",\n  "source_dirty": false\n}\n' % source_sha,
            encoding="utf-8",
        )
        return build_info

    # -- inspection ----------------------------------------------------------
    def systemctl_calls(self) -> list[str]:
        if not self.systemctl_log.exists():
            return []
        return self.systemctl_log.read_text(encoding="utf-8").splitlines()

    def mutating_calls(self) -> list[str]:
        """Calls that change a production unit's state."""

        return [
            call
            for call in self.systemctl_calls()
            if call.split(" ", 1)[0] in _MUTATING
            and not any(unit in call for unit in _OWN_UNITS)
        ]

    def production_calls(self) -> list[str]:
        """Every call that names anything but the dead-man's own units."""

        return [
            call
            for call in self.systemctl_calls()
            if call != "daemon-reload"
            and not any(unit in call for unit in _OWN_UNITS)
        ]

    def state(self) -> dict[str, str]:
        text = (self.state_dir / "state").read_text(encoding="utf-8")
        return dict(
            line.split("=", 1) for line in text.splitlines() if line
        )

    def last_action(self) -> dict[str, str]:
        text = (self.state_dir / "last-action").read_text(encoding="utf-8")
        return dict(line.split("=", 1) for line in text.splitlines() if line)

    def triggers_present(self) -> tuple[bool, bool]:
        return (
            self.cron_file.exists(),
            (self.unit_dir / "weatheredge-deploy-deadman.timer").exists(),
        )


@pytest.fixture()
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


# ---------------------------------------------------------------- arming ----


def _arm_awaiting_ticks(
    box: Box, source_at, *, tick_wait: str = "8"
) -> subprocess.CompletedProcess[str]:
    """Run `arm --await-tick` while driving the triggers ``source_at`` chooses.

    ``source_at(elapsed_seconds)`` returns the trigger source to run, or None.
    Driving each source in its own window is what makes the assertion mean
    something: arm samples only the LAST line of last-tick, so back-to-back
    cron+systemd ticks would prove nothing about the cron half.
    """

    process = subprocess.Popen(
        [
            "bash",
            str(box.self_path),
            "arm",
            "--deploy-id",
            DEPLOY_ID,
            "--phase",
            "pre-transfer",
            "--source-sha",
            SOURCE_SHA,
            "--deploy-host",
            "deploy-mac",
            "--lease-seconds",
            "600",
            "--tick-wait-seconds",
            tick_wait,
            "--pre-transfer-restore",
            "1",
            "--install-helper",
            "--await-tick",
            "--",
            *CAPTURED_TIMERS,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=box.env(),
        text=True,
    )
    assert process.stdin is not None
    process.stdin.write(QUIESCE_HELPER.read_text(encoding="utf-8"))
    process.stdin.close()

    start = time.time()
    deadline = start + float(tick_wait) + 20
    while process.poll() is None and time.time() < deadline:
        if box.cron_file.exists():
            source = source_at(time.time() - start)
            if source is not None:
                box.run("tick", source)
        time.sleep(0.2)
    stdout, stderr = process.communicate(timeout=30)
    return subprocess.CompletedProcess(
        ["arm"], process.returncode, stdout=stdout, stderr=stderr
    )


def test_arm_installs_both_triggers_and_proves_both_actually_run_the_payload(
    box: Box,
) -> None:
    """The whole point of --await-tick: turn "we assume cron works on that box"
    into "the box just ran our payload", before anything is quiesced. Both
    sources get their own window, so the cron half of the redundancy -- the
    graft that justifies the cron-primary design -- is actually proven."""

    result = _arm_awaiting_ticks(
        box, lambda elapsed: "cron" if elapsed < 3.0 else "systemd"
    )

    assert result.returncode == 0, result.stderr
    version = re.search(r'^DEADMAN_VERSION="([^"]*)"$', DEADMAN.read_text(), re.M)
    assert version is not None
    assert f"DEADMAN_VERSION={version.group(1)}" in result.stdout
    assert f"DEADMAN_DEPLOY_ID={DEPLOY_ID}" in result.stdout
    assert "DEADMAN_TRIGGERS=cron,systemd" in result.stdout
    assert "never ticked" not in result.stderr
    state = box.state()
    assert state["PHASE"] == "pre-transfer"
    assert state["DEPLOY_ID"] == DEPLOY_ID
    assert state["PRE_TRANSFER_RESTORE"] == "1"
    assert state["TIMERS"] == " ".join(CAPTURED_TIMERS)
    assert state["ARMED_BOOT_ID"] == BOOT_ID
    assert box.triggers_present() == (True, True)
    assert (box.state_dir / "disable_systemd_timers.sh").read_text(
        encoding="utf-8"
    ) == QUIESCE_HELPER.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("driven", "silent"), [("cron", "systemd"), ("systemd", "cron")]
)
def test_arm_succeeds_on_one_trigger_but_says_which_one_never_ticked(
    box: Box, driven: str, silent: str
) -> None:
    """One live trigger is enough to arm; losing the other is a loud warning,
    not a refusal. This is the only thing that can catch a broken cron entry or
    a systemd pair the box will not run."""

    result = _arm_awaiting_ticks(box, lambda _elapsed: driven, tick_wait="4")

    assert result.returncode == 0, result.stderr
    assert f"the dead-man {silent} trigger never ticked" in result.stderr
    assert box.triggers_present() == (True, True)


def test_a_phase_change_keeps_the_deploy_s_own_provenance(box: Box) -> None:
    """Phase is data, not a re-arm: the boot the deploy started on must survive
    it, or a reboot between two phases would stop firing immediately.

    Asserted by consequence, not by comparing timestamps that happen to differ:
    the box reboots between the two phases and the very next tick must fire on a
    completely fresh lease.
    """

    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    first = box.state()
    box.hold_marker()
    box.run("tick", "cron")

    # A reboot between the two phase advances. `systemctl disable` is
    # persistent, the marker in /run is not, so waiting is never right.
    box.boot_id_file.write_text(
        "00000000-0000-4000-8000-000000000002\n", encoding="utf-8"
    )
    assert box.arm(phase="mixed").returncode == 0
    second = box.state()

    assert second["PHASE"] == "mixed"
    assert second["TIMERS"] == ""
    assert second["ARMED_AT_EPOCH"] == first["ARMED_AT_EPOCH"]
    assert second["ARMED_AT_UTC"] == first["ARMED_AT_UTC"]
    assert second["ARMED_BOOT_ID"] == first["ARMED_BOOT_ID"] == BOOT_ID
    assert int(second["PHASE_AT_EPOCH"]) >= int(first["PHASE_AT_EPOCH"])

    box.systemctl_log.unlink(missing_ok=True)
    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    # Fresh heartbeat, fired anyway, and `mixed` still enables nothing.
    assert box.mutating_calls() == []
    assert box.last_action()["outcome"] == "attention-mixed-tree"


def test_arm_refuses_a_second_deploy_while_a_lease_is_live(box: Box) -> None:
    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    before = (box.state_dir / "state").read_text(encoding="utf-8")
    cron_before = box.cron_file.read_text(encoding="utf-8")

    result = box.arm(*CAPTURED_TIMERS, deploy_id="20260919T000000Z-1-2")

    assert result.returncode == 11
    assert "another deploy already owns this box" in result.stderr
    assert DEPLOY_ID in result.stderr
    assert (box.state_dir / "state").read_text(encoding="utf-8") == before
    assert box.cron_file.read_text(encoding="utf-8") == cron_before


def test_arm_refuses_an_unacknowledged_incident_unless_told_to_clear_it(
    box: Box,
) -> None:
    assert box.arm(phase="mixed").returncode == 0
    box.hold_marker()
    box.expire_lease()
    box.run("tick", "cron")
    assert box.state()["PHASE"] == "attention"

    refused = box.arm(*CAPTURED_TIMERS, deploy_id="20260919T000000Z-1-2")
    cleared = box.arm(
        *CAPTURED_TIMERS,
        deploy_id="20260919T000000Z-1-2",
        extra=("--clear-incident",),
    )

    assert refused.returncode == 11
    assert "unacknowledged dead-man incident" in refused.stderr
    assert "clear --force" in refused.stderr
    assert cleared.returncode == 0, cleared.stderr
    assert box.state()["PHASE"] == "pre-transfer"


def test_arm_starts_a_new_deploy_with_no_history_from_the_last_one(box: Box) -> None:
    """arm runs before the maintenance marker is installed, so a marker-seen
    left by an earlier deploy would stand the dead-man down on its first tick.
    A failed-restore budget must not carry over either.

    The takeover path is the one that matters, so it is reached WITHOUT calling
    `clear`, which would delete those files by itself and make this a tautology.
    """

    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()
    box.run("tick", "cron", FAKE_RESTORE_STATUS="1")
    assert (box.state_dir / "marker-seen").exists()
    assert (box.state_dir / "restore-attempts").read_text().strip() == "1"
    box.marker.unlink()

    # A host whose heartbeat is older than its own lease is provably gone, so
    # SFO_DEPLOY_DEADMAN_CLEAR_INCIDENT=1 may take the box over from it.
    box.expire_lease()
    taken_over = box.arm(
        *CAPTURED_TIMERS,
        deploy_id="20260919T000000Z-1-2",
        extra=("--clear-incident",),
    )

    assert taken_over.returncode == 0, taken_over.stderr
    assert box.state()["DEPLOY_ID"] == "20260919T000000Z-1-2"
    assert not (box.state_dir / "marker-seen").exists()
    assert not (box.state_dir / "restore-attempts").exists()

    # The lease, not a stale marker-seen, governs this window, and the retry
    # budget starts again from zero rather than one failure from exhaustion.
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()
    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert box.last_action()["outcome"] == "restored-pre-transfer"
    assert any(call.startswith("enable ") for call in box.systemctl_calls())


def test_arm_never_steals_a_box_from_a_deploy_whose_lease_is_still_live(
    box: Box,
) -> None:
    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    before = (box.state_dir / "state").read_text(encoding="utf-8")

    result = box.arm(
        *CAPTURED_TIMERS,
        deploy_id="20260919T000000Z-1-2",
        extra=("--clear-incident",),
    )

    assert result.returncode == 11
    assert "another deploy already owns this box" in result.stderr
    assert "clear --force" in result.stderr
    assert "SFO_DEPLOY_DEADMAN_CLEAR_INCIDENT=1" in result.stderr
    assert (box.state_dir / "state").read_text(encoding="utf-8") == before


def test_arm_rolls_everything_back_when_no_trigger_ever_runs_the_payload(
    box: Box,
) -> None:
    # No cron directory and no systemctl that can actually fire the unit: the
    # triggers are written but nothing ticks.
    result = box.arm(
        *CAPTURED_TIMERS,
        extra=("--await-tick",),
        tick_wait="2",
        DEADMAN_CRON_FILE=str(box.root / "no-such-dir" / "weatheredge-deploy-deadman"),
    )

    assert result.returncode == 12
    assert "no dead-man trigger ran the payload" in result.stderr
    assert not (box.state_dir / "state").exists()
    assert not (box.state_dir / "heartbeat").exists()
    assert not (box.state_dir / "disable_systemd_timers.sh").exists()
    assert box.triggers_present() == (False, False)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"deploy_id": "short"}, "valid --deploy-id"),
        ({"phase": "whenever"}, "--phase pre-transfer|mixed|post-install"),
    ],
)
def test_arm_validates_before_it_writes_anything(
    box: Box, overrides: dict[str, str], message: str
) -> None:
    result = box.arm(*CAPTURED_TIMERS, **overrides)

    assert result.returncode == 2
    assert message in result.stderr
    assert not box.state_dir.exists() or not (box.state_dir / "state").exists()
    assert box.triggers_present() == (False, False)


def test_arm_rejects_a_timer_name_outside_the_unit_grammar(box: Box) -> None:
    result = box.arm("sfo-kalshi-paper-scan.timer", "rm -rf /")

    assert result.returncode == 2
    assert "unexpected timer name" in result.stderr
    assert box.triggers_present() == (False, False)


def test_arm_refuses_a_lease_below_the_documented_floor(box: Box) -> None:
    """A lease under ~300s would false-fire inside a stall the deploy itself is
    designed to survive: SSH_OPTS tolerate 180s of silence on their own."""

    result = box.run(
        "arm",
        "--deploy-id",
        DEPLOY_ID,
        "--phase",
        "pre-transfer",
        "--source-sha",
        SOURCE_SHA,
        "--deploy-host",
        "deploy-mac",
        "--lease-seconds",
        "30",
        "--tick-wait-seconds",
        "0",
        "--pre-transfer-restore",
        "1",
        "--install-helper",
        "--",
        *CAPTURED_TIMERS,
        stdin=QUIESCE_HELPER.read_bytes(),
    )

    assert result.returncode == 2
    assert "--lease-seconds of at least 60" in result.stderr
    assert box.triggers_present() == (False, False)


def test_arm_refuses_a_box_carrying_a_different_payload_version(box: Box) -> None:
    """The host reads DEADMAN_VERSION out of its own copy and hands it over. A
    failed tee+mv would otherwise leave an older payload arming happily against
    a state format it cannot parse."""

    result = box.arm(*CAPTURED_TIMERS, extra=("--expect-version", "999"))

    assert result.returncode == 2
    assert "version mismatch" in result.stderr
    assert box.triggers_present() == (False, False)
    assert not (box.state_dir / "state").exists()


# ----------------------------------------------------------------- ticking ---


def test_tick_with_a_fresh_heartbeat_touches_nothing(box: Box) -> None:
    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.hold_marker()
    box.systemctl_log.unlink(missing_ok=True)

    result = box.run("tick", "cron")

    assert result.returncode == 0
    assert box.production_calls() == []
    assert box.marker.exists()
    assert box.triggers_present() == (True, True)


def test_tick_restores_the_captured_runtime_and_removes_every_trace_of_itself(
    box: Box,
) -> None:
    """The case that caused both outages: the host died before any source was
    transferred, so the box still runs the old release and gets it back."""

    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()
    box.systemctl_log.unlink(missing_ok=True)

    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    calls = box.systemctl_calls()
    enable = [call for call in calls if call.startswith("enable ")]
    # One call, exactly the captured set, retired Apple refresh timer included:
    # before the first rsync the box still runs the release that requires it.
    assert enable == ["enable --now " + " ".join(CAPTURED_TIMERS)]
    assert "start sfo-scheduler-health.service" in calls
    assert not box.marker.exists()
    assert box.triggers_present() == (False, False)
    assert not (box.state_dir / "state").exists()
    assert not (box.state_dir / "disable_systemd_timers.sh").exists()
    assert not box.self_path.exists()
    action = box.last_action()
    assert action["outcome"] == "restored-pre-transfer"
    assert action["marker"] == "removed"
    assert "Release deploy and rollback" in action["note"]


def test_tick_never_restores_a_host_the_deploy_would_not_have_restored(
    box: Box,
) -> None:
    assert box.arm(*CAPTURED_TIMERS, pre_transfer_restore="0").returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()
    box.systemctl_log.unlink(missing_ok=True)

    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert box.mutating_calls() == []
    assert box.marker.exists()
    assert box.state()["PHASE"] == "attention"
    assert box.last_action()["outcome"] == "attention-not-restorable"


def test_tick_stays_dark_when_the_tree_looks_half_synced(box: Box) -> None:
    """The one path where the dead-man knowingly chooses dark over possibly
    wrong: enabling producers over a mixed tree risks the database."""

    build_info = box.stamp_release()
    stale = box.remote_base / "trading" / "sfo_kalshi_quant" / "maker_fills.py"
    stale.write_text("# newer than the release stamp\n", encoding="utf-8")
    newer = build_info.stat().st_mtime + 60
    os.utime(stale, (newer, newer))

    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()
    box.systemctl_log.unlink(missing_ok=True)

    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert box.mutating_calls() == []
    assert box.marker.exists()
    action = box.last_action()
    assert action["outcome"] == "attention-mixed-tree"
    assert action["probe"] == "mixed"
    assert action["probe_detail"] == str(stale)


def test_tick_sees_a_transfer_that_rsync_gave_an_old_mtime(box: Box) -> None:
    """rsync -a preserves the SOURCE mtime, so a transferred file can easily
    look older than the release stamp. It cannot preserve ctime -- rsync writes
    a temp file and renames it -- so the probe compares ctime against the stamp
    the dead-man drops when it arms."""

    build_info = box.stamp_release()
    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.hold_marker()
    box.run("tick", "cron")

    # Written after arming, but backdated the way rsync -a would leave it.
    transferred = box.remote_base / "trading" / "sfo_kalshi_quant" / "maker_fills.py"
    transferred.write_text("# arrived in a partial transfer\n", encoding="utf-8")
    older = build_info.stat().st_mtime - 600
    os.utime(transferred, (older, older))
    assert transferred.stat().st_mtime < build_info.stat().st_mtime

    box.expire_lease()
    box.systemctl_log.unlink(missing_ok=True)
    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert box.mutating_calls() == []
    assert box.marker.exists()
    action = box.last_action()
    assert action["outcome"] == "attention-mixed-tree"
    assert action["probe"] == "written-since-arm"
    assert action["probe_detail"] == str(transferred)


def test_tick_restores_when_the_release_stamp_is_absent(box: Box) -> None:
    """Fail-safe polarity. A missing build_info.json also means no rsync of this
    deploy landed, and the failure being eliminated is *dark*, so only a
    positive mixed-tree signal may block the restore. This is the case on the
    very first deploy that carries the dead-man."""

    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()

    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert any(call.startswith("enable ") for call in box.systemctl_calls())
    assert not box.marker.exists()
    action = box.last_action()
    assert action["outcome"] == "restored-pre-transfer"
    assert action["probe"] == "inconclusive"


def test_tick_mid_transfer_never_enables_a_timer(box: Box) -> None:
    assert box.arm(phase="mixed").returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()
    box.systemctl_log.unlink(missing_ok=True)

    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert box.mutating_calls() == []
    assert box.marker.exists()
    assert box.state()["PHASE"] == "attention"
    assert box.state()["TIMERS"] == ""
    assert box.triggers_present() == (False, False)
    assert box.last_action()["outcome"] == "attention-mixed-tree"
    # Best effort and nothing more: the alert unit may point into a mid-rsync
    # tree, so the durable signals are last-action and the retained marker.
    assert "start sfo-alert@weatheredge-deploy-deadman.service" in box.systemctl_calls()


def test_tick_post_install_restores_only_against_the_exact_release_stamp(
    box: Box,
) -> None:
    box.stamp_release()
    assert box.arm(*CAPTURED_TIMERS, phase="post-install").returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()

    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert any(call.startswith("enable ") for call in box.systemctl_calls())
    assert not box.marker.exists()
    assert box.last_action()["outcome"] == "restored-post-install"


def test_tick_post_install_refuses_a_tree_that_lost_its_release_stamp(
    box: Box,
) -> None:
    """build_info.json is rsynced before the install gates run, so a host that
    reached post-install provably stamped it. Missing means the tree changed
    afterwards, and nothing may be enabled over it."""

    assert box.arm(*CAPTURED_TIMERS, phase="post-install").returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()
    box.systemctl_log.unlink(missing_ok=True)

    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert box.mutating_calls() == []
    assert box.marker.exists()
    assert box.last_action()["outcome"] == "attention-missing-build-info"


def test_tick_post_install_refuses_a_tree_that_changed_after_the_install(
    box: Box,
) -> None:
    box.stamp_release(source_sha="0000000000000000000000000000000000000000")
    assert box.arm(*CAPTURED_TIMERS, phase="post-install").returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()
    box.systemctl_log.unlink(missing_ok=True)

    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert box.mutating_calls() == []
    assert box.marker.exists()
    assert box.last_action()["outcome"] == "attention-source-sha-mismatch"


def test_a_reboot_fires_the_dead_man_without_waiting_for_the_lease(box: Box) -> None:
    """`systemctl disable` is persistent and the marker in /run is not, so a
    reboot mid-deploy always strands the box: waiting is never right."""

    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.boot_id_file.write_text("00000000-0000-4000-8000-000000000002\n", encoding="utf-8")

    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert any(call.startswith("enable ") for call in box.systemctl_calls())
    assert box.last_action()["outcome"] == "restored-pre-transfer"


def test_an_unreadable_boot_id_degrades_to_the_lease_without_erroring(box: Box) -> None:
    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.hold_marker()
    box.boot_id_file.unlink()
    box.systemctl_log.unlink(missing_ok=True)

    fresh = box.run("tick", "cron")
    box.expire_lease()
    expired = box.run("tick", "cron")

    assert fresh.returncode == 0, fresh.stderr
    assert expired.returncode == 0, expired.stderr
    assert any(call.startswith("enable ") for call in box.systemctl_calls())


def test_a_released_marker_on_the_same_boot_stands_the_dead_man_down(box: Box) -> None:
    """A lost disarm is harmless, and a stale dead-man never re-enables a timer
    an operator deliberately paused."""

    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.marker.unlink()
    box.expire_lease()
    box.systemctl_log.unlink(missing_ok=True)

    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert box.mutating_calls() == []
    assert box.triggers_present() == (False, False)
    assert not (box.state_dir / "state").exists()
    assert not box.self_path.exists()
    assert box.last_action()["outcome"] == "stood-down"


def test_the_arm_to_quiesce_window_is_governed_by_the_lease_not_the_marker(
    box: Box,
) -> None:
    """arm runs before the marker is written, so "marker absent" only means
    "released" once the marker has actually been observed."""

    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.run("tick", "cron")
    assert not (box.state_dir / "marker-seen").exists()
    box.expire_lease()

    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert any(call.startswith("enable ") for call in box.systemctl_calls())
    assert box.last_action()["outcome"] == "restored-pre-transfer"


def test_a_failing_restore_keeps_the_box_watched_until_the_budget_runs_out(
    box: Box,
) -> None:
    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()

    for attempt in range(1, 6):
        result = box.run("tick", "cron", FAKE_RESTORE_STATUS="1")
        assert result.returncode == 0, result.stderr
        assert (box.state_dir / "restore-attempts").read_text().strip() == str(attempt)
        assert box.triggers_present() == (True, True)
        assert box.marker.exists()
        assert box.state()["PHASE"] == "pre-transfer"

    sixth = box.run("tick", "cron", FAKE_RESTORE_STATUS="1")

    assert sixth.returncode == 0, sixth.stderr
    assert box.state()["PHASE"] == "attention"
    assert box.last_action()["outcome"] == "attention-restore-exhausted"
    assert box.marker.exists()
    assert box.triggers_present() == (False, False)


def test_an_incident_acts_exactly_once_and_never_erases_its_own_record(
    box: Box,
) -> None:
    """After an incident the state stays for `status` and the next deploy is
    refused. A leftover trigger that fired again used to overwrite last-action
    with `stood-down`, delete the payload and quietly unblock the next deploy."""

    assert box.arm(phase="mixed").returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()
    box.run("tick", "cron")
    assert box.last_action()["outcome"] == "attention-mixed-tree"

    # An operator restores by hand and releases maintenance; a leftover trigger
    # (or a manual tick) then runs.
    box.marker.unlink()
    box.systemctl_log.unlink(missing_ok=True)
    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert box.production_calls() == []
    assert box.last_action()["outcome"] == "attention-mixed-tree"
    assert box.state()["PHASE"] == "attention"
    assert box.self_path.exists()
    refused = box.arm(*CAPTURED_TIMERS, deploy_id="20260919T000000Z-1-2")
    assert refused.returncode == 11


def test_an_orphan_trigger_with_nothing_to_watch_removes_itself(box: Box) -> None:
    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    (box.state_dir / "state").unlink()
    box.systemctl_log.unlink(missing_ok=True)

    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert box.production_calls() == []
    assert box.triggers_present() == (False, False)


def test_a_restore_without_the_watchdog_in_it_never_starts_the_watchdog(
    box: Box,
) -> None:
    """recover_pre_transfer_runtime only starts sfo-scheduler-health when the
    capture contained its timer. The dead-man must mirror that, or it would
    start a watchdog an operator deliberately disabled."""

    paused = ("sfo-kalshi-paper-scan.timer", "sfo-kalshi-paper-monitor.timer")
    assert box.arm(*paused).returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()
    box.systemctl_log.unlink(missing_ok=True)

    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert [call for call in box.systemctl_calls() if call.startswith("enable ")] == [
        "enable --now " + " ".join(paused)
    ]
    assert not any("sfo-scheduler-health" in call for call in box.systemctl_calls())
    assert box.last_action()["outcome"] == "restored-pre-transfer"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can remove a file anywhere")
def test_a_teardown_that_cannot_remove_its_trigger_downgrades_to_an_incident(
    box: Box,
) -> None:
    """Never leave a payload that can fire again with no trigger file to
    explain it."""

    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()
    box.cron_file.parent.chmod(0o500)
    try:
        result = box.run("tick", "cron")
    finally:
        box.cron_file.parent.chmod(0o755)

    assert result.returncode == 0, result.stderr
    # The restore itself still happened; only the self-teardown could not.
    assert any(call.startswith("enable ") for call in box.systemctl_calls())
    assert box.last_action()["outcome"] == "attention-trigger-removal-failed"
    assert box.state()["PHASE"] == "attention"
    assert box.self_path.exists()


def test_a_sibling_poll_that_already_holds_the_lock_makes_this_tick_a_no_op(
    box: Box,
) -> None:
    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()
    busy_flock = box.root / "bin" / "flock-busy"
    _write_executable(busy_flock, "#!/bin/sh\nexit 1\n")
    box.systemctl_log.unlink(missing_ok=True)

    result = box.run("tick", "cron", FLOCK_BIN=str(busy_flock))

    assert result.returncode == 0
    assert box.production_calls() == []
    assert box.marker.exists()
    assert box.state()["PHASE"] == "pre-transfer"


@pytest.mark.parametrize("damage_kind", ["missing-keys", "unknown-key", "not-key-value"])
def test_damaged_state_never_derives_a_unit_name_from_it(
    box: Box, damage_kind: str
) -> None:
    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.hold_marker()
    valid = (box.state_dir / "state").read_text(encoding="utf-8")
    if damage_kind == "missing-keys":
        damage = "PHASE=pre-transfer\n"
    elif damage_kind == "unknown-key":
        # Otherwise complete and valid, so the unknown key is the ONLY reason
        # this is rejected. A half-written or tampered state file must stand the
        # dead-man down, not send it enabling production timers.
        damage = valid + "SURPRISE=1\n"
    else:
        damage = "not a key value line\n"
    (box.state_dir / "state").write_text(damage, encoding="utf-8")
    box.systemctl_log.unlink(missing_ok=True)

    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert box.production_calls() == []
    assert box.marker.exists()
    assert box.last_action()["outcome"] == "attention-unparseable-state"


def test_too_many_timers_is_treated_as_damage(box: Box) -> None:
    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    state = (box.state_dir / "state").read_text(encoding="utf-8")
    flood = " ".join(f"sfo-flood-{index}.timer" for index in range(33))
    (box.state_dir / "state").write_text(
        re.sub(r"^TIMERS=.*$", f"TIMERS={flood}", state, flags=re.M), encoding="utf-8"
    )
    box.hold_marker()
    box.systemctl_log.unlink(missing_ok=True)

    result = box.run("tick", "cron")

    assert result.returncode == 0, result.stderr
    assert box.production_calls() == []
    assert box.last_action()["outcome"] == "attention-unparseable-state"


def test_an_unknown_timer_is_refused_twice_over(box: Box) -> None:
    """Once by the payload's own grammar, and once by the pinned quiesce helper,
    whose UNIT_PAIRS stays the only allowlist of unit names in the system."""

    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    state = (box.state_dir / "state").read_text(encoding="utf-8")
    (box.state_dir / "state").write_text(
        re.sub(r"^TIMERS=.*$", "TIMERS=not-a-timer", state, flags=re.M), encoding="utf-8"
    )
    box.hold_marker()
    box.systemctl_log.unlink(missing_ok=True)

    grammar = box.run("tick", "cron")

    assert grammar.returncode == 0
    # The payload's own grammar rejects it as damaged state, so no unit name is
    # ever derived from it.
    assert box.production_calls() == []
    assert box.last_action()["outcome"] == "attention-unparseable-state"

    (box.state_dir / "state").write_text(
        re.sub(r"^TIMERS=.*$", "TIMERS=totally-unknown.timer", state, flags=re.M),
        encoding="utf-8",
    )
    box.expire_lease()
    box.systemctl_log.unlink(missing_ok=True)

    allowlist = box.run("tick", "cron")

    assert allowlist.returncode == 0, allowlist.stderr
    # A name the grammar allows still has to exist in the pinned helper's
    # UNIT_PAIRS, which is the only allowlist of unit names in the system.
    assert not any(call.startswith("enable ") for call in box.systemctl_calls())
    assert (box.state_dir / "restore-attempts").read_text().strip() == "1"
    assert box.marker.exists()


# -------------------------------------------------------------- heartbeat ----


def test_beat_updates_the_lease_and_reports_who_owns_the_box(box: Box) -> None:
    missing = box.run("beat", DEPLOY_ID)
    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.run("tick", "cron")
    box.expire_lease()
    before = (box.state_dir / "heartbeat").read_text(encoding="utf-8")

    matching = box.run("beat", DEPLOY_ID)
    wrong = box.run("beat", "20260919T000000Z-1-2")

    assert missing.returncode == 14
    assert matching.returncode == 0, matching.stderr
    assert (box.state_dir / "heartbeat").read_text(encoding="utf-8") != before
    assert wrong.returncode == 14


def test_beat_reports_an_armed_but_unwatched_box(box: Box) -> None:
    assert box.arm(*CAPTURED_TIMERS).returncode == 0

    result = box.run("beat", DEPLOY_ID)

    assert result.returncode == 13
    assert "no dead-man trigger has ticked" in result.stderr


def test_beat_reports_an_armed_box_whose_triggers_went_quiet(box: Box) -> None:
    """Assumption #6: nothing stops configuration management from reaping an
    unknown cron entry and unit pair. The stale-tick arm is what notices within
    DEADMAN_TICK_STALE_SECONDS instead of at the end of the deploy."""

    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.run("tick", "cron")
    assert box.run("beat", DEPLOY_ID).returncode == 0
    (box.state_dir / "last-tick").write_text("1000 cron\n", encoding="utf-8")

    result = box.run("beat", DEPLOY_ID)

    assert result.returncode == 13
    assert "no dead-man trigger has ticked" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a read-only directory")
def test_beat_reports_an_unusable_box_rather_than_a_revocation(box: Box) -> None:
    """A payload that cannot write /var exits 12, never a status the host reads
    as "the dead-man already restored production". The box's backups land on the
    same filesystem as its state, and it has a documented history of filling."""

    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.run("tick", "cron")
    box.state_dir.chmod(0o500)
    try:
        result = box.run("beat", DEPLOY_ID)
    finally:
        box.state_dir.chmod(0o755)

    assert result.returncode == 12, result.stderr
    assert "could not refresh the dead-man heartbeat" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a read-only directory")
def test_a_tick_that_cannot_record_itself_still_fires(box: Box) -> None:
    """The write that proves a trigger is alive must never gate the restore: a
    full /var used to make `tick` exit before it evaluated the lease at all,
    while making `beat` fail too -- so neither side acted, ever."""

    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()
    box.systemctl_log.unlink(missing_ok=True)
    box.state_dir.chmod(0o500)
    try:
        result = box.run("tick", "cron")
    finally:
        box.state_dir.chmod(0o755)

    # Production is back. The only thing that failed is the record-keeping and
    # the payload's own final self-deletion, which nothing depends on.
    assert [call for call in box.systemctl_calls() if call.startswith("enable ")] == [
        "enable --now " + " ".join(CAPTURED_TIMERS)
    ]
    assert "start sfo-scheduler-health.service" in box.systemctl_calls()
    assert not box.marker.exists()
    assert box.triggers_present() == (False, False)


def test_beat_reports_revoked_once_the_dead_man_has_fired(box: Box) -> None:
    assert box.arm(phase="mixed").returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.expire_lease()
    box.run("tick", "cron")

    result = box.run("beat", DEPLOY_ID)

    assert result.returncode == 14


# ------------------------------------------------------- operator surface ----


def test_disarm_leaves_the_box_byte_identical_to_an_unarmed_one(box: Box) -> None:
    assert box.arm(*CAPTURED_TIMERS).returncode == 0

    first = box.run("disarm", DEPLOY_ID)
    second = box.run("disarm", DEPLOY_ID)

    assert first.returncode == 0, first.stderr
    assert box.triggers_present() == (False, False)
    assert not (box.state_dir / "state").exists()
    assert not (box.state_dir / "disable_systemd_timers.sh").exists()
    assert not box.self_path.exists()
    # The payload removed itself, so the second call cannot even run.
    assert second.returncode == 127


def test_disarm_refuses_to_take_a_box_from_the_deploy_that_owns_it(
    box: Box,
) -> None:
    assert box.arm(*CAPTURED_TIMERS).returncode == 0

    result = box.run("disarm", "20260919T000000Z-1-2")

    assert result.returncode == 11
    assert box.triggers_present() == (True, True)
    assert (box.state_dir / "state").exists()
    assert box.self_path.exists()


def test_hold_stops_the_dead_man_undoing_a_deliberate_host_quiesce(
    box: Box,
) -> None:
    """recover_deploy_runtime re-quiesces a box it could not safely restore. A
    post-install dead-man would re-enable all of it about a lease later."""

    box.stamp_release()
    assert box.arm(*CAPTURED_TIMERS, phase="post-install").returncode == 0
    box.hold_marker()
    box.run("tick", "cron")
    box.systemctl_log.unlink(missing_ok=True)

    result = box.run("hold", DEPLOY_ID)

    assert result.returncode == 0, result.stderr
    assert box.state()["PHASE"] == "attention"
    assert box.last_action()["outcome"] == "attention-host-recovery-held"
    assert box.triggers_present() == (False, False)
    assert box.marker.exists()
    assert box.self_path.exists()

    # It really is inert now, and the next deploy is refused until cleared.
    box.expire_lease()
    box.systemctl_log.unlink(missing_ok=True)
    assert box.run("tick", "cron").returncode == 0
    assert box.production_calls() == []
    assert box.arm(*CAPTURED_TIMERS, deploy_id="20260919T000000Z-1-2").returncode == 11
    assert box.run("hold", "20260919T000000Z-1-2").returncode == 0


def test_hold_refuses_a_deploy_that_does_not_own_the_box(box: Box) -> None:
    assert box.arm(*CAPTURED_TIMERS).returncode == 0

    result = box.run("hold", "20260919T000000Z-1-2")

    assert result.returncode == 11
    assert box.state()["PHASE"] == "pre-transfer"
    assert box.triggers_present() == (True, True)


def test_clear_refuses_a_live_lease_but_force_always_works(box: Box) -> None:
    assert box.arm(*CAPTURED_TIMERS).returncode == 0

    refused = box.run("clear")
    assert refused.returncode == 11
    assert "still holds a live lease" in refused.stderr
    assert (box.state_dir / "state").exists()

    forced = box.run("clear", "--force")

    assert forced.returncode == 0, forced.stderr
    assert not (box.state_dir / "state").exists()
    assert box.triggers_present() == (False, False)


def test_status_is_read_only(box: Box) -> None:
    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    box.run("tick", "cron")
    watched = [
        box.state_dir / "state",
        box.state_dir / "heartbeat",
        box.state_dir / "last-tick",
        box.cron_file,
    ]
    before = {path: path.stat().st_mtime_ns for path in watched}

    result = box.run("status")

    assert result.returncode == 0, result.stderr
    printed = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    assert printed["deadman"] == "armed"
    assert printed["phase"] == "pre-transfer"
    assert printed["deploy_id"] == DEPLOY_ID
    assert int(printed["heartbeat_age_seconds"]) >= 0
    assert int(printed["fires_in_seconds"]) <= 600
    assert printed["cron_trigger_present"] == "1"
    assert printed["systemd_trigger_present"] == "1"
    assert printed["last_tick_source"] == "cron"
    assert printed["timers"] == " ".join(CAPTURED_TIMERS)
    assert {path: path.stat().st_mtime_ns for path in watched} == before


# --------------------------------------------------------------- guards ------


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock is not installed here")
def test_every_verb_works_with_a_real_flock(box: Box) -> None:
    """Every other test runs with FLOCK_BIN pointing at nothing, so the locking
    path -- including release_lock's guard against closing a descriptor that was
    never opened, which is fatal for `exec` -- is otherwise never executed. The
    box has /usr/bin/flock, and ubuntu-latest runs this."""

    flock = str(shutil.which("flock"))
    assert box.arm(*CAPTURED_TIMERS, FLOCK_BIN=flock).returncode == 0
    assert box.run("tick", "cron", FLOCK_BIN=flock).returncode == 0
    assert box.run("beat", DEPLOY_ID, FLOCK_BIN=flock).returncode == 0
    assert box.run("status", FLOCK_BIN=flock).returncode == 0

    box.hold_marker()
    box.expire_lease()
    box.systemctl_log.unlink(missing_ok=True)
    assert box.run("tick", "cron", FLOCK_BIN=flock).returncode == 0

    assert any(call.startswith("enable ") for call in box.systemctl_calls())
    assert box.last_action()["outcome"] == "restored-pre-transfer"
    assert not box.self_path.exists()


def test_a_teardown_removes_the_systemd_enablement_symlink_too(box: Box) -> None:
    """`systemctl disable` is best effort here. A dangling
    timers.target.wants link survives reboots and makes every later
    daemon-reload on production complain about a unit that no longer exists."""

    assert box.arm(*CAPTURED_TIMERS).returncode == 0
    wants = box.unit_dir / "timers.target.wants"
    wants.mkdir(parents=True, exist_ok=True)
    link = wants / "weatheredge-deploy-deadman.timer"
    link.symlink_to(box.unit_dir / "weatheredge-deploy-deadman.timer")

    assert box.run("disarm", DEPLOY_ID).returncode == 0

    assert not link.is_symlink()
    assert box.triggers_present() == (False, False)


def test_a_box_without_logger_gets_a_cron_entry_that_still_runs(box: Box) -> None:
    """Nothing verifies logger exists on that box, and a missing one would reap
    the right-hand side of the pipeline and SIGPIPE the payload mid-teardown."""

    result = box.arm(
        *CAPTURED_TIMERS, DEADMAN_LOGGER=str(box.root / "bin" / "no-such-logger")
    )

    assert result.returncode == 0, result.stderr
    assert "is missing" in result.stderr
    line = [
        entry
        for entry in box.cron_file.read_text(encoding="utf-8").splitlines()
        if entry.startswith("*")
    ]
    assert line == [f"* * * * * root {box.self_path} tick cron >/dev/null 2>&1"]


def test_payload_uses_no_gnu_only_or_bsd_only_spelling() -> None:
    """It is authored on a BSD userland, tested on ubuntu-latest, and runs on a
    GNU box that nobody can log into to fix a portability mistake."""

    text = DEADMAN.read_text(encoding="utf-8")
    for spelling in (
        "stat -c",
        "date -d",
        "-newermt",
        "-printf",
        "-quit",
        "readlink -f",
        "--ignore-fail-on-non-empty",
        "rmdir --",
        "sed -i",
    ):
        assert spelling not in text, spelling


def test_payload_parses_and_emits_a_debian_legal_cron_entry(box: Box) -> None:
    assert subprocess.run(["bash", "-n", str(DEADMAN)]).returncode == 0
    assert box.arm(*CAPTURED_TIMERS).returncode == 0

    text = box.cron_file.read_text(encoding="utf-8")
    lines = text.splitlines()

    # Debian's run-parts rules: no dot in the name, a sixth user field, and a
    # trailing newline, or cron silently ignores the entry.
    assert "." not in box.cron_file.name
    assert text.endswith("\n")
    assert [line for line in lines if line.startswith("*")] == [
        f"* * * * * root {box.self_path} tick cron 2>&1"
        " | /usr/bin/logger -t weatheredge-deploy-deadman"
    ]
    assert "SHELL=/bin/bash" in lines
    assert any(line.startswith("PATH=") for line in lines)
    schedule = [line for line in lines if line.startswith("*")][0].split()
    assert schedule[:6] == ["*", "*", "*", "*", "*", "root"]
    assert box.cron_file.stat().st_mode & 0o022 == 0
