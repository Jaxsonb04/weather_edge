"""Scheduler health must notice paper scans that stop recording decisions.

Audit F5 (2026-11-01): from Oct 27 to Nov 1 every sfo-kalshi-paper-scan run
failed, about 1,300 in a row, while check_scheduler_health.sh kept printing
"OK". A Type=oneshot service that fails or hangs leaves its timer "active", and
the watchdog only asked whether timers were enabled and active.

The fix requires every configured paper profile (each is a separate $1,000
paper account) to have journaled a decision recently. The scan timer's own
activation opens a start-up grace, so a deploy that quiesced scans for an hour
or a reboot does not fail on pre-quiesce rows.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.research_policy import TARGET_POLICY
from test_scheduler_health_watchdog import ROOT, SCRIPT, _fresh_root, _write_executable

# The newest paper activity the Nov 1 capture of the live site still showed.
INCIDENT_LAST_DECISION = "2026-10-27T15:45:38+00:00"
TIMER_ACTIVE_FOR_DAYS = 6 * 24 * 60.0
SCANS_FAILED = "scheduler health failed: paper scans stopped recording decisions"
CHECK_BLOCKED = "scheduler health blocked: paper scan freshness check failed"

_FLOCK_SHIM = """#!/usr/bin/env bash
set -euo pipefail
while (( $# > 0 )); do
  case "$1" in
    -w|-E) shift 2 ;;
    -n) shift ;;
    -u) exit 0 ;;
    --) shift; break ;;
    *) break ;;
  esac
done
(( $# > 0 )) || exit 0
shift
(( $# > 0 )) || exit 0
exec "$@"
"""


def _ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _paper_db(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    """A real PaperStore journal holding ``(created_at, risk_profile)`` rows.

    Research rows carry the Research ROI target identity the allocator writes.
    """

    db = tmp_path / "trading-data" / "paper_trading.db"
    PaperStore(db)
    research_identity = (
        TARGET_POLICY.account_id,
        TARGET_POLICY.sleeve.value,
        TARGET_POLICY.policy_version,
        TARGET_POLICY.policy_fingerprint,
    )
    with closing(sqlite3.connect(db)) as conn:
        for created_at, profile in sorted(rows):
            conn.execute(
                """
                INSERT INTO decision_snapshots (
                    created_at, target_date, market_ticker, label, action, side,
                    approved, probability, probability_lcb, yes_bid, yes_ask,
                    spread, fee_per_contract, cost_per_contract, edge, edge_lcb,
                    kelly_fraction, recommended_contracts, recommended_spend,
                    expected_profit, trade_quality_score, reasons_json,
                    risk_profile, account_id, research_sleeve,
                    research_policy_version, policy_fingerprint
                ) VALUES (
                    ?, '2026-10-27', 'KXHIGHLAX-26OCT27-B70.5', '70° to 71°',
                    'skip', 'NO', 0, 0.5, 0.4, 0.5, 0.52, 0.02, 0.01, 0.5,
                    0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0, '[]', ?, ?, ?, ?, ?
                )
                """,
                (
                    created_at,
                    profile,
                    *(research_identity if profile == "research" else (None,) * 4),
                ),
            )
        conn.commit()
    return db


def _run(
    tmp_path: Path,
    paper_db: Path,
    *,
    profiles: str = "live,research",
    timer_active_minutes: float = TIMER_ACTIVE_FOR_DAYS,
    scan_service: str = "failed",
    marker: bool = False,
    max_age_minutes: str | None = None,
    strategy_minutes: int = 1,
) -> subprocess.CompletedProcess[str]:
    """Run the watchdog against fresh publications and the given paper journal.

    ``scan_service`` is what systemd itself would report for the scan unit:
    "failed" (Result=exit-code) or "hung" (still activating, where systemd has
    already reset Result to success). Neither may decide the verdict.
    """

    root, public_manifest = _fresh_root(tmp_path, strategy_minutes=strategy_minutes)
    command_log = tmp_path / "systemctl.log"
    activated_us = time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1_000 - int(
        timer_active_minutes * 60_000_000
    )
    service_state, service_result = {
        "failed": ("failed", "exit-code"),
        "hung": ("activating", "success"),
    }[scan_service]
    systemctl = tmp_path / "systemctl"
    _write_executable(
        systemctl,
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {command_log!s}
unit="${{@: -1}}"
case "$1:$unit" in
  show:sfo-kalshi-paper-scan.timer)
    printf '%s\\n' {activated_us}
    ;;
  show:sfo-kalshi-paper-scan.service)
    printf 'ActiveState={service_state}\\nResult={service_result}\\n'
    ;;
  is-active:sfo-kalshi-paper-scan.service)
    printf '%s\\n' {service_state}
    exit 3
    ;;
esac
exit 0
""",
    )
    for name in ("integrity", "final-health", "propagation-waiter"):
        _write_executable(tmp_path / name, "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(tmp_path / "flock", _FLOCK_SHIM)
    maintenance = tmp_path / "deploy-maintenance"
    if marker:
        maintenance.touch()

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PAPER_RISK_PROFILE", "PAPER_RISK_PROFILES", "SFO_KALSHI_DB"}
    }
    env.update(
        {
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "SFO_SCHEDULER_TEST_MODE": "1",
            "SYSTEMCTL_BIN": str(systemctl),
            "SFO_SCHEDULER_INTEGRITY_HELPER": str(tmp_path / "integrity"),
            "SFO_DEPLOY_MAINTENANCE_MARKER": str(maintenance),
            "SFO_SCHEDULER_REPAIR_STATE_DIR": str(tmp_path / "repair-state"),
            "SFO_SCHEDULER_FINAL_HEALTH_CHECK": str(tmp_path / "final-health"),
            "SFO_SCHEDULER_PROPAGATION_WAITER": str(tmp_path / "propagation-waiter"),
            "SFO_SCHEDULER_FLOCK_BIN": str(tmp_path / "flock"),
            "SFO_BASE_DIR": str(root.parent),
            "SFO_FORECASTER_ROOT": str(root),
            "SFO_TRADING_ROOT": str(ROOT / "trading"),
            "SFO_TRADING_PYTHON": sys.executable,
            "SFO_FORECAST_DB": str(root / "weather.db"),
            "SFO_PUBLICATION_MANIFEST_PATH": str(root / "publication_manifest.json"),
            "SFO_PUBLICATION_MAX_OPERATIONAL_AGE_MINUTES": "10",
            "SFO_PUBLICATION_MAX_PUBLIC_OPERATIONAL_AGE_MINUTES": "10",
            "SFO_PUBLICATION_MAX_STRATEGY_AGE_MINUTES": "20",
            "SFO_PUBLISH_PAGES": "1",
            "SFO_PUBLICATION_MANIFEST_URL": public_manifest.as_uri(),
            "SFO_ARTIFACT_GENERATION_LOCK": str(tmp_path / "artifact-generation.lock"),
            "SFO_SCHEDULER_PAPER_DB": str(paper_db),
            "PAPER_RISK_PROFILES": profiles,
        }
    )
    if max_age_minutes is not None:
        env["SFO_SCHEDULER_SCAN_MAX_AGE_MINUTES"] = max_age_minutes
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "systemctl.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@pytest.mark.parametrize("scan_service", ["failed", "hung"])
def test_scheduler_health_fails_when_every_paper_profile_stopped_recording(
    tmp_path: Path, scan_service: str
) -> None:
    """The Oct 27 - Nov 1 state: timers active, publications fresh, scans dead."""

    paper_db = _paper_db(
        tmp_path,
        [(INCIDENT_LAST_DECISION, "live"), (INCIDENT_LAST_DECISION, "research")],
    )

    result = _run(tmp_path, paper_db, scan_service=scan_service)

    assert result.returncode == 1, result.stdout
    assert "scheduler and publication health verified" not in result.stdout
    for profile in ("live", "research"):
        assert (
            f"paper scan profile={profile} recorded no decision in the last 20 minutes"
        ) in result.stderr
    assert SCANS_FAILED in result.stderr
    # Detection only: the watchdog never starts a paper unit.
    assert not any(line.startswith("start ") for line in _calls(tmp_path))


def test_scheduler_health_names_the_one_profile_that_stopped(tmp_path: Path) -> None:
    """Since PR #156 one profile can fail while the other keeps scanning, and
    the unit's single Result cannot say which account went quiet."""

    paper_db = _paper_db(
        tmp_path,
        [(_ago(45), "research"), (_ago(46), "live"), (_ago(2), "live")],
    )

    result = _run(tmp_path, paper_db)

    assert result.returncode == 1, result.stdout
    assert "paper scan profile=research recorded no decision" in result.stderr
    assert "profile=live" not in result.stderr


def test_scheduler_health_accepts_recent_decisions_from_every_profile(
    tmp_path: Path,
) -> None:
    paper_db = _paper_db(
        tmp_path,
        [(INCIDENT_LAST_DECISION, "live"), (_ago(3), "live"), (_ago(2), "research")],
    )

    result = _run(tmp_path, paper_db)

    assert result.returncode == 0, result.stderr
    assert "scheduler and publication health verified" in result.stdout
    assert not any(line.startswith("start ") for line in _calls(tmp_path))


def test_scheduler_health_tolerates_a_couple_of_failed_scan_ticks(
    tmp_path: Path,
) -> None:
    """A Kalshi blip or a SQLite-lock retry that gave up fails one or two
    five-minute ticks; systemd reports Result=exit-code, but nothing is stale."""

    paper_db = _paper_db(tmp_path, [(_ago(12), "live"), (_ago(11), "research")])

    result = _run(tmp_path, paper_db, scan_service="failed")

    assert result.returncode == 0, result.stderr
    assert "scheduler and publication health verified" in result.stdout


@pytest.mark.parametrize(
    ("timer_active_minutes", "healthy"),
    [(3.0, True), (25.0, False)],
)
def test_scheduler_health_grants_start_up_grace_after_the_scan_timer_restarts(
    tmp_path: Path, timer_active_minutes: float, healthy: bool
) -> None:
    """A deploy quiesces every timer for about an hour, restores the scan timer,
    and starts the watchdog minutes later. Pre-quiesce rows are not a failure
    until the restored timer has had a full window to produce new ones."""

    paper_db = _paper_db(tmp_path, [(_ago(70), "live"), (_ago(70), "research")])

    result = _run(tmp_path, paper_db, timer_active_minutes=timer_active_minutes)

    assert ("show --property=ActiveEnterTimestampMonotonic --value "
            "sfo-kalshi-paper-scan.timer") in _calls(tmp_path)
    if healthy:
        assert result.returncode == 0, result.stderr
        assert "start-up grace" in result.stdout
        assert "scheduler and publication health verified" in result.stdout
    else:
        assert result.returncode == 1, result.stdout
        assert "paper scan profile=live recorded no decision" in result.stderr


@pytest.mark.parametrize(
    ("timer_active_minutes", "healthy"),
    [(2.0, True), (30.0, False)],
)
def test_scheduler_health_treats_a_missing_paper_journal_as_no_scans(
    tmp_path: Path, timer_active_minutes: float, healthy: bool
) -> None:
    """A fresh install has no journal until the first scan creates it."""

    result = _run(
        tmp_path,
        tmp_path / "trading-data" / "paper_trading.db",
        timer_active_minutes=timer_active_minutes,
    )

    if healthy:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1, result.stdout
        assert "paper scan profile=live recorded no decision" in result.stderr


def test_scheduler_health_checks_only_the_configured_profiles(tmp_path: Path) -> None:
    """The runner scans PAPER_RISK_PROFILES; aliases normalize the same way."""

    paper_db = _paper_db(
        tmp_path, [(INCIDENT_LAST_DECISION, "research"), (_ago(4), "live")]
    )

    result = _run(tmp_path, paper_db, profiles=" balanced ,")

    assert result.returncode == 0, result.stderr
    assert "scheduler and publication health verified" in result.stdout


def test_scheduler_health_scan_age_threshold_is_configurable(tmp_path: Path) -> None:
    paper_db = _paper_db(tmp_path, [(_ago(12), "live"), (_ago(12), "research")])

    result = _run(tmp_path, paper_db, max_age_minutes="10")

    assert result.returncode == 1, result.stdout
    assert "recorded no decision in the last 10 minutes" in result.stderr


def test_scheduler_health_rejects_an_invalid_scan_age_threshold(tmp_path: Path) -> None:
    paper_db = _paper_db(tmp_path, [(_ago(2), "live"), (_ago(2), "research")])

    result = _run(tmp_path, paper_db, max_age_minutes="twenty")

    assert result.returncode == 1
    assert (
        "scheduler health configuration is invalid: "
        "SFO_SCHEDULER_SCAN_MAX_AGE_MINUTES=twenty"
    ) in result.stderr


def test_scheduler_health_blocks_on_an_invalid_scan_profile(tmp_path: Path) -> None:
    """run_paper_scan_profiles.sh exits 2 on this, so no scan runs at all."""

    paper_db = _paper_db(tmp_path, [(_ago(2), "live")])

    result = _run(tmp_path, paper_db, profiles="live,bogus")

    assert result.returncode == 1
    assert "invalid paper risk profile: bogus" in result.stderr
    assert CHECK_BLOCKED in result.stderr


def test_scheduler_health_blocks_on_an_unreadable_paper_journal(tmp_path: Path) -> None:
    paper_db = tmp_path / "trading-data" / "paper_trading.db"
    paper_db.parent.mkdir()
    paper_db.write_bytes(b"not a sqlite database" * 64)

    result = _run(tmp_path, paper_db)

    assert result.returncode == 1
    assert "paper decision journal is unreadable" in result.stderr
    assert CHECK_BLOCKED in result.stderr


def test_stale_scans_fail_before_any_publication_repair(tmp_path: Path) -> None:
    """Like a timer error, dead scans fail the run instead of being masked by
    the bounded publication repair, which exits OK on its own paths."""

    paper_db = _paper_db(
        tmp_path, [(INCIDENT_LAST_DECISION, "live"), (_ago(2), "research")]
    )

    result = _run(tmp_path, paper_db, strategy_minutes=25)

    assert result.returncode == 1, result.stdout
    assert "paper scan profile=live recorded no decision" in result.stderr
    assert not any(line.startswith("start ") for line in _calls(tmp_path))


def test_scheduler_health_scan_check_respects_deploy_maintenance(
    tmp_path: Path,
) -> None:
    paper_db = _paper_db(tmp_path, [(INCIDENT_LAST_DECISION, "live")])

    result = _run(tmp_path, paper_db, marker=True)

    assert result.returncode == 0, result.stderr
    assert "deployment maintenance active" in result.stdout
    assert _calls(tmp_path) == []
