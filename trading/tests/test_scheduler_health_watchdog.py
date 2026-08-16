from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sfo_kalshi_quant.publication import build_manifest


ROOT = Path(__file__).resolve().parents[2]
AWS_DIR = ROOT / "trading" / "deploy" / "aws"
SCRIPT = AWS_DIR / "check_scheduler_health.sh"
CANONICAL_TIMERS = (
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
)


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _fresh_root(
    parent: Path,
    *,
    operational_minutes: int = 1,
    strategy_minutes: int = 1,
    strategy_analysis_minutes: int | None = None,
) -> tuple[Path, Path]:
    now = datetime.now(timezone.utc)
    root = parent / "forecaster"
    root.mkdir()
    (root / "weather.db").write_bytes(b"sqlite-placeholder")
    _write_json(
        root / "build_info.json",
        {
            "source_sha": "a" * 40,
            "source_dirty": False,
            "synced_at_utc": _iso(now - timedelta(minutes=2)),
        },
    )
    _write_json(
        root / "trading_signal.json",
        {"generated_at": _iso(now - timedelta(minutes=operational_minutes))},
    )
    _write_json(
        root / "cities_data.json",
        {"generated_at": _iso(now - timedelta(minutes=operational_minutes))},
    )
    _write_json(root / "forecast_data.json", {"table": []})
    _write_json(root / "weather_story_data.json", {"temperature_histogram": {}})
    _write_json(
        root / "strategy_research.json",
        {
            "available": True,
            "generated_at": _iso(now - timedelta(minutes=strategy_minutes)),
            "analysis_generated_at": _iso(
                now
                - timedelta(
                    minutes=(
                        strategy_minutes
                        if strategy_analysis_minutes is None
                        else strategy_analysis_minutes
                    )
                )
            ),
        },
    )
    build_manifest(root, now=now)
    public_manifest = parent / "public-manifest.json"
    shutil.copy2(root / "publication_manifest.json", public_manifest)
    return root, public_manifest


def _run(
    tmp_path: Path,
    *,
    marker: Path | None = None,
    disabled_timer: str = "",
    integrity_fails: bool = False,
    root: Path | None = None,
    public_manifest: Path | None = None,
    failed_start: str = "",
    artifact_lock: Path | None = None,
    propagation_fails: bool = False,
) -> subprocess.CompletedProcess[str]:
    command_log = tmp_path / "systemctl.log"
    systemctl = tmp_path / "systemctl"
    _write_executable(
        systemctl,
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {command_log!s}
if [[ "$1" == "is-enabled" && "${{@: -1}}" == "{disabled_timer}" ]]; then
  exit 1
fi
if [[ "$1" == "start" && "${{@: -1}}" == "{failed_start}" ]]; then
  exit 42
fi
exit 0
""",
    )
    integrity = tmp_path / "integrity"
    _write_executable(
        integrity,
        f"#!/usr/bin/env bash\nexit {1 if integrity_fails else 0}\n",
    )
    final_health = tmp_path / "final-health"
    _write_executable(final_health, "#!/usr/bin/env bash\nexit 0\n")
    propagation_waiter = tmp_path / "propagation-waiter"
    _write_executable(
        propagation_waiter,
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "${SFO_PUBLICATION_PROPAGATION_TIMEOUT_SECONDS:-}" '
        '>"$PROPAGATION_TIMEOUT_LOG"\n'
        f"exit {1 if propagation_fails else 0}\n",
    )
    flock = tmp_path / "flock"
    _write_executable(
        flock,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$SFO_FLOCK_LOG"
while (( $# > 0 )); do
  case "$1" in
    -w|-E)
      shift 2
      ;;
    -n)
      shift
      ;;
    -u)
      exit 0
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done
(( $# > 0 )) || exit 0
shift
(( $# > 0 )) || exit 0
exec "$@"
""",
    )
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "SFO_SCHEDULER_TEST_MODE": "1",
        "SYSTEMCTL_BIN": str(systemctl),
        "SFO_SCHEDULER_INTEGRITY_HELPER": str(integrity),
        "SFO_DEPLOY_MAINTENANCE_MARKER": str(
            marker or tmp_path / "deploy-maintenance"
        ),
        "SFO_SCHEDULER_REPAIR_STATE_DIR": str(tmp_path / "repair-state"),
        "SFO_SCHEDULER_FINAL_HEALTH_CHECK": str(final_health),
        "SFO_SCHEDULER_PROPAGATION_WAITER": str(propagation_waiter),
        "PROPAGATION_TIMEOUT_LOG": str(tmp_path / "propagation-timeout.log"),
        "SFO_SCHEDULER_FLOCK_BIN": str(flock),
        "SFO_FLOCK_LOG": str(tmp_path / "flock.log"),
    }
    if root is not None:
        env.update(
            {
                "SFO_BASE_DIR": str(root.parent),
                "SFO_FORECASTER_ROOT": str(root),
                "SFO_TRADING_ROOT": str(ROOT / "trading"),
                "SFO_TRADING_PYTHON": sys.executable,
                "SFO_FORECAST_DB": str(root / "weather.db"),
                "SFO_PUBLICATION_MANIFEST_PATH": str(
                    root / "publication_manifest.json"
                ),
                "SFO_PUBLICATION_MAX_OPERATIONAL_AGE_MINUTES": "10",
                "SFO_PUBLICATION_MAX_PUBLIC_OPERATIONAL_AGE_MINUTES": "10",
                "SFO_PUBLICATION_MAX_STRATEGY_AGE_MINUTES": "20",
                "SFO_PUBLICATION_MAX_STRATEGY_ANALYSIS_AGE_MINUTES": "2160",
                "SFO_PUBLISH_PAGES": "1",
                "SFO_PUBLICATION_MANIFEST_URL": (
                    public_manifest.as_uri() if public_manifest else ""
                ),
                "SFO_ARTIFACT_GENERATION_LOCK": str(
                    artifact_lock or tmp_path / "artifact-generation.lock"
                ),
            }
        )
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_scheduler_health_skips_all_checks_and_repairs_during_deploy(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "deploy-maintenance"
    marker.touch()

    result = _run(tmp_path, marker=marker)

    assert result.returncode == 0, result.stderr
    assert "deployment maintenance active" in result.stdout
    assert not (tmp_path / "systemctl.log").exists()


def test_scheduler_health_rejects_disabled_canonical_timer_without_repair(
    tmp_path: Path,
) -> None:
    disabled = "sfo-kalshi-paper-scan.timer"

    result = _run(tmp_path, disabled_timer=disabled)

    assert result.returncode == 1
    assert f"timer is not enabled: {disabled}" in result.stderr
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8")
    assert f"is-enabled --quiet {disabled}" in calls
    assert " start " not in f" {calls}"


def test_scheduler_health_rejects_unit_drift_without_repair(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, integrity_fails=True)

    assert result.returncode == 1
    assert "canonical systemd unit integrity failed" in result.stderr
    assert not (tmp_path / "systemctl.log").exists()


def test_scheduler_health_accepts_fresh_local_and_public_artifacts(
    tmp_path: Path,
) -> None:
    root, public_manifest = _fresh_root(tmp_path)

    result = _run(
        tmp_path,
        root=root,
        public_manifest=public_manifest,
    )

    assert result.returncode == 0, result.stderr
    assert "scheduler and publication health verified" in result.stdout
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8")
    for timer in CANONICAL_TIMERS:
        assert f"is-enabled --quiet {timer}" in calls
        assert f"is-active --quiet {timer}" in calls
    assert " start " not in f" {calls}"


def test_scheduler_does_not_loop_fast_repair_for_stale_offline_analysis(
    tmp_path: Path,
) -> None:
    root, public_manifest = _fresh_root(
        tmp_path,
        strategy_minutes=1,
        strategy_analysis_minutes=2161,
    )

    result = _run(
        tmp_path,
        root=root,
        public_manifest=public_manifest,
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8")
    assert "start sfo-strategy-lab-refresh.service" not in calls
    assert "start sfo-operational-publish.service" not in calls


def test_scheduler_health_does_not_truncate_app_controlled_lock_symlink(
    tmp_path: Path,
) -> None:
    root, public_manifest = _fresh_root(tmp_path)
    sentinel = tmp_path / "root-owned-sentinel"
    sentinel.write_text("must-not-be-truncated\n", encoding="utf-8")
    artifact_lock = tmp_path / "artifact-generation.lock"
    artifact_lock.symlink_to(sentinel)

    result = _run(
        tmp_path,
        root=root,
        public_manifest=public_manifest,
        artifact_lock=artifact_lock,
    )

    assert result.returncode == 0, result.stderr
    assert sentinel.read_text(encoding="utf-8") == "must-not-be-truncated\n"
    flock_calls = (tmp_path / "flock.log").read_text(encoding="utf-8")
    assert str(artifact_lock) in flock_calls
    assert "__validate_publication_under_app_lock" in flock_calls
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'exec 8>"$ARTIFACT_LOCK"' not in script
    assert 'run_as_app "$FLOCK_BIN"' in script
    assert "application user must be unprivileged" in script


def test_scheduler_health_refuses_checksum_repair(
    tmp_path: Path,
) -> None:
    root, public_manifest = _fresh_root(tmp_path)
    _write_json(
        root / "strategy_research.json",
        {"available": True, "generated_at": _iso(datetime.now(timezone.utc))},
    )

    result = _run(
        tmp_path,
        root=root,
        public_manifest=public_manifest,
    )

    assert result.returncode == 1
    assert "checksum or manifest validation failed" in result.stderr
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8")
    assert " start " not in f" {calls}"


def test_scheduler_health_refuses_provenance_repair(
    tmp_path: Path,
) -> None:
    root, public_manifest = _fresh_root(tmp_path)
    payload = json.loads(public_manifest.read_text(encoding="utf-8"))
    payload["provenance"]["source_sha"] = "b" * 40
    _write_json(public_manifest, payload)

    result = _run(
        tmp_path,
        root=root,
        public_manifest=public_manifest,
    )

    assert result.returncode == 1
    assert "provenance mismatch" in result.stderr
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8")
    assert " start " not in f" {calls}"


def test_scheduler_health_refuses_database_staleness_repair(
    tmp_path: Path,
) -> None:
    root, public_manifest = _fresh_root(tmp_path)
    stale_epoch = (
        datetime.now(timezone.utc) - timedelta(hours=7)
    ).timestamp()
    os.utime(root / "weather.db", (stale_epoch, stale_epoch))

    result = _run(
        tmp_path,
        root=root,
        public_manifest=public_manifest,
    )

    assert result.returncode == 1
    assert "forecast DB is stale" in result.stderr
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8")
    assert " start " not in f" {calls}"


def test_scheduler_health_repairs_stale_strategy_then_publishes(
    tmp_path: Path,
) -> None:
    root, public_manifest = _fresh_root(tmp_path, strategy_minutes=25)

    result = _run(
        tmp_path,
        root=root,
        public_manifest=public_manifest,
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8").splitlines()
    strategy_start = calls.index("start sfo-strategy-lab-refresh.service")
    publication_start = calls.index("start sfo-operational-publish.service")
    assert strategy_start < publication_start
    assert not any(
        line.startswith("start sfo-kalshi-paper-") for line in calls
    )
    assert (tmp_path / "repair-state" / "last-repair-epoch").is_file()


def test_scheduler_health_repairs_public_staleness_with_publication_only(
    tmp_path: Path,
) -> None:
    root, public_manifest = _fresh_root(tmp_path)
    payload = json.loads(public_manifest.read_text(encoding="utf-8"))
    stale = _iso(datetime.now(timezone.utc) - timedelta(minutes=30))
    payload["published_at"] = stale
    payload["artifacts"]["trading_signal.json"]["generated_at"] = stale
    payload["artifacts"]["cities_data.json"]["generated_at"] = stale
    _write_json(public_manifest, payload)

    result = _run(
        tmp_path,
        root=root,
        public_manifest=public_manifest,
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8").splitlines()
    assert "start sfo-operational-publish.service" in calls
    assert "start sfo-strategy-lab-refresh.service" not in calls
    assert not any(
        line.startswith("start sfo-kalshi-paper-") for line in calls
    )
    assert (tmp_path / "propagation-timeout.log").read_text().strip() == "420"


def test_scheduler_health_cooldown_blocks_repeated_repair_without_failing(
    tmp_path: Path,
) -> None:
    root, public_manifest = _fresh_root(tmp_path, strategy_minutes=25)
    state = tmp_path / "repair-state"
    state.mkdir()
    (state / "last-repair-epoch").write_text(
        f"{int(datetime.now(timezone.utc).timestamp())}\n",
        encoding="utf-8",
    )

    result = _run(
        tmp_path,
        root=root,
        public_manifest=public_manifest,
    )

    # The repair that set the cooldown already owns the alerting decision, so
    # re-reporting the same incident here would multiply one lag into several
    # failed runs.
    assert result.returncode == 0, result.stderr
    assert "inside the repair cooldown" in result.stdout
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("start ") for line in calls)


def test_scheduler_health_tolerates_a_single_propagation_miss(
    tmp_path: Path,
) -> None:
    root, public_manifest = _fresh_root(tmp_path, strategy_minutes=25)

    result = _run(
        tmp_path,
        root=root,
        public_manifest=public_manifest,
        propagation_fails=True,
    )

    assert result.returncode == 0, result.stderr
    assert "miss 1/2" in result.stdout
    misses = tmp_path / "repair-state" / "propagation-miss-count"
    assert misses.read_text(encoding="utf-8").strip() == "1"


def test_scheduler_health_fails_after_consecutive_propagation_misses(
    tmp_path: Path,
) -> None:
    root, public_manifest = _fresh_root(tmp_path, strategy_minutes=25)
    state = tmp_path / "repair-state"
    state.mkdir()
    (state / "propagation-miss-count").write_text("1\n", encoding="utf-8")

    result = _run(
        tmp_path,
        root=root,
        public_manifest=public_manifest,
        propagation_fails=True,
    )

    assert result.returncode == 1
    assert "did not converge across 2 consecutive repair(s)" in result.stderr
    assert (state / "propagation-miss-count").read_text(encoding="utf-8").strip() == "2"


def test_scheduler_health_clears_propagation_misses_once_healthy(
    tmp_path: Path,
) -> None:
    root, public_manifest = _fresh_root(tmp_path)
    state = tmp_path / "repair-state"
    state.mkdir()
    misses = state / "propagation-miss-count"
    misses.write_text("1\n", encoding="utf-8")

    result = _run(
        tmp_path,
        root=root,
        public_manifest=public_manifest,
    )

    assert result.returncode == 0, result.stderr
    assert "scheduler and publication health verified" in result.stdout
    assert not misses.exists()


def test_scheduler_health_stops_when_strategy_repair_fails(
    tmp_path: Path,
) -> None:
    root, public_manifest = _fresh_root(tmp_path, strategy_minutes=25)

    result = _run(
        tmp_path,
        root=root,
        public_manifest=public_manifest,
        failed_start="sfo-strategy-lab-refresh.service",
    )

    assert result.returncode == 1
    assert "Strategy Lab refresh failed" in result.stderr
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8").splitlines()
    assert "start sfo-strategy-lab-refresh.service" in calls
    assert "start sfo-operational-publish.service" not in calls
