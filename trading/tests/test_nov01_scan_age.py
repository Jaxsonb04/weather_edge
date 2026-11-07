"""Scheduler health must notice paper scans that keep failing.

Audit F5 (2026-11-01): from Oct 27 to Nov 1 every sfo-kalshi-paper-scan run
failed, about 1,300 in a row, while check_scheduler_health.sh kept printing
"OK". A Type=oneshot service that fails or hangs leaves its timer "active", and
the watchdog only asked whether timers were enabled and active.

The watchdog now requires every configured paper profile (each is a separate
$1,000 paper account) to have finished a successful scan recently.
run_paper_scan_profiles.sh records that per profile, and only when the profile
exits 0. Fresh decision rows cannot stand in for it. The scan journals one city
and target at a time, and a later city's target lookup, an arbitrage
containment error, or a placement or admission error still fails the run after
earlier rows are saved, so a scan that fails that way on every tick keeps its
rows fresh for days. The scan timer's own activation opens a start-up grace, so
a deploy that quiesced scans for an hour, or a reboot, does not fail on old
stamps.
"""

from __future__ import annotations

import fcntl
import os
import re
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
from test_scheduler_health_watchdog import (
    AWS_DIR,
    ROOT,
    SCRIPT,
    _fresh_root,
    _write_executable,
)

RUNNER = AWS_DIR / "run_paper_scan_profiles.sh"
PROFILES = ("live", "research")
# The scans began failing on Oct 27. The newest paper order the Nov 1 capture
# of the live site showed is 2026-10-27T15:45:38Z, and these tests use it as
# the last successful scan. Any moment that day is days outside the window.
LAST_SUCCESS_BEFORE_CRASH = "2026-10-27T15:45:38+00:00"
# Every scan tick journals the cities ahead of the one that fails it.
EVERY_TICK_MINUTES = (1.0, 6.0, 11.0, 16.0)
TIMER_ACTIVE_FOR_DAYS = 6 * 24 * 60.0
NOT_SUCCEEDING = "scheduler health failed: paper scans have not succeeded recently"
NOT_JOURNALING = "scheduler health failed: paper scans succeed but journal no decisions"
CHECK_BLOCKED = "scheduler health blocked: paper scan freshness check failed"
STILL_JOURNALING = "its runs still journal decisions but none has succeeded"

RESEARCH_IDENTITY = (
    TARGET_POLICY.account_id,
    TARGET_POLICY.sleeve.value,
    TARGET_POLICY.policy_version,
    TARGET_POLICY.policy_fingerprint,
)
# Research rows carry the Research ROI target identity the allocator writes.
INSERT_DECISION = """
INSERT INTO decision_snapshots (
    created_at, target_date, market_ticker, label, action, side, approved,
    probability, probability_lcb, yes_bid, yes_ask, spread, fee_per_contract,
    cost_per_contract, edge, edge_lcb, kelly_fraction, recommended_contracts,
    recommended_spend, expected_profit, trade_quality_score, reasons_json,
    risk_profile, account_id, research_sleeve, research_policy_version,
    policy_fingerprint
) VALUES (
    ?, '2026-10-27', 'KXHIGHLAX-26OCT27-B70.5', '70° to 71°', 'skip', 'NO', 0,
    0.5, 0.4, 0.5, 0.52, 0.02, 0.01, 0.5, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0,
    '[]', ?, ?, ?, ?, ?
)
"""

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

# A real advisory lock for the runner's `flock -n 9` (macOS ships no flock).
_FCNTL_FLOCK = """#!{python}
import fcntl
import sys

try:
    fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(1)
"""

# Stands in for `python -m sfo_kalshi_quant.cli ... portfolio-scan`: journals a
# decision for its profile, as the scan does for each city before anything can
# fail the run, then exits with PAPER_SCAN_STATUS_<PROFILE> (default 0).
_SCAN_STUB = """#!{python}
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone

args = sys.argv[1:]
profile = args[args.index("--risk-profile") + 1]
db_path = args[args.index("--db-path") + 1]
identity = {identity!r} if profile == "research" else (None,) * 4
with closing(sqlite3.connect(db_path)) as conn:
    conn.execute(
        {insert!r}, (datetime.now(timezone.utc).isoformat(), profile, *identity)
    )
    conn.commit()
raise SystemExit(int(os.environ.get("PAPER_SCAN_STATUS_" + profile.upper(), "0")))
"""


def _ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _every_tick(*profiles: str) -> list[tuple[str, str]]:
    return [(_ago(minutes), profile) for minutes in EVERY_TICK_MINUTES for profile in profiles]


def _paper_db(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    """A real PaperStore journal holding ``(created_at, risk_profile)`` rows."""

    db = tmp_path / "trading-data" / "paper_trading.db"
    PaperStore(db)
    with closing(sqlite3.connect(db)) as conn:
        for created_at, profile in sorted(rows):
            conn.execute(
                INSERT_DECISION,
                (
                    created_at,
                    profile,
                    *(RESEARCH_IDENTITY if profile == "research" else (None,) * 4),
                ),
            )
        conn.commit()
    return db


def _stamp_path(paper_db: Path, profile: str) -> Path:
    return paper_db.parent / ".paper-scan-state" / f"last-success-{profile}"


def _stamp(paper_db: Path, profile: str, when: float | str) -> None:
    """Write the runner's success stamp, ``when`` minutes ago or at an ISO time."""

    moment = (
        datetime.fromisoformat(when)
        if isinstance(when, str)
        else datetime.now(timezone.utc) - timedelta(minutes=when)
    )
    path = _stamp_path(paper_db, profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{int(moment.timestamp())}\n", encoding="ascii")


def _run(
    tmp_path: Path,
    paper_db: Path,
    *,
    profiles: str = "live,research",
    timer_active_minutes: float = TIMER_ACTIVE_FOR_DAYS,
    marker: bool = False,
    max_age_minutes: str | None = None,
    strategy_minutes: int = 1,
) -> subprocess.CompletedProcess[str]:
    """Run the watchdog against fresh publications and the given paper journal.

    systemctl reports only the scan timer's activation. The verdict must not
    depend on anything else systemd knows about the scan unit.
    """

    root, public_manifest = _fresh_root(tmp_path, strategy_minutes=strategy_minutes)
    command_log = tmp_path / "systemctl.log"
    # Both ends of the monotonic subtraction share a synthetic boot clock.
    # A runner booted less than timer_active_minutes ago cannot represent the
    # simulated activation with its real clock (systemd rejects <= 0 stamps).
    monotonic_ns = 7 * 24 * 60 * 60 * 1_000_000_000
    activated_us = monotonic_ns // 1_000 - int(timer_active_minutes * 60_000_000)
    python = tmp_path / "scheduler-python"
    _write_executable(
        python,
        f"""#!{sys.executable}
import os, sys, time
if sys.argv[1] == "-":
    sys.argv = sys.argv[1:]
    time.clock_gettime_ns = lambda clock: {monotonic_ns}
    exec(compile(sys.stdin.read(), "<stdin>", "exec"), {{"__name__": "__main__"}})
else:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
""",
    )
    systemctl = tmp_path / "systemctl"
    _write_executable(
        systemctl,
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {command_log!s}
if [[ "$1" == show && "${{@: -1}}" == sfo-kalshi-paper-scan.timer ]]; then
  printf '%s\\n' {activated_us}
fi
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
            "SFO_TRADING_PYTHON": str(python),
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


def _run_scan_runner(
    tmp_path: Path,
    paper_db: Path,
    extra_path: Path | None = None,
    profiles: str = ",".join(PROFILES),
    **statuses: int,
) -> subprocess.CompletedProcess[str]:
    """Run the real run_paper_scan_profiles.sh over ``profiles``, with a stub
    CLI that journals a decision and exits with ``statuses[profile]``."""

    stub = tmp_path / "scan-python"
    _write_executable(
        stub,
        _SCAN_STUB.format(
            python=sys.executable, identity=RESEARCH_IDENTITY, insert=INSERT_DECISION
        ),
    )
    trading = tmp_path / "runner-trading"
    trading.mkdir(exist_ok=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PAPER_", "SFO_"))
    }
    if extra_path is not None:
        env["PATH"] = f"{extra_path}:{os.environ['PATH']}"
    env.update(
        {
            "SFO_TRADING_ROOT": str(trading),
            "SFO_FORECASTER_ROOT": str(tmp_path / "runner-forecaster"),
            "SFO_TRADING_PYTHON": str(stub),
            "SFO_KALSHI_DB": str(paper_db),
            "SFO_PAPER_SCAN_LOCK": str(tmp_path / "paper-scan.lock"),
            "PAPER_RISK_PROFILES": profiles,
            **{
                f"PAPER_SCAN_STATUS_{profile.upper()}": str(status)
                for profile, status in statuses.items()
            },
        }
    )
    return subprocess.run(
        ["bash", str(RUNNER)], env=env, text=True, capture_output=True, check=False
    )


def _journaled_profiles(paper_db: Path) -> list[str]:
    with closing(sqlite3.connect(paper_db)) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT risk_profile FROM decision_snapshots ORDER BY id"
            )
        ]


# ---------------------------------------------------------------------------
# What the watchdog accepts and rejects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("last_success", "healthy"),
    [
        pytest.param(12.0, True, id="two-failed-ticks"),
        pytest.param(25.0, False, id="four-failed-ticks-or-a-hang"),
        pytest.param(LAST_SUCCESS_BEFORE_CRASH, False, id="failing-for-days"),
        pytest.param(None, False, id="never-succeeded"),
    ],
)
def test_only_the_last_successful_run_separates_failed_ticks_from_a_failing_scan(
    tmp_path: Path, last_success: float | str | None, healthy: bool
) -> None:
    """Had the PR #156 label crash hit only the Boston event, live (registry
    order mia, lax, ..., bos) would journal 13 cities and research
    (RESEARCH_SCAN_CITY_ORDER) 12 before crashing, on every tick, for days. The
    journal looks the same after a Kalshi blip or a SQLite-lock retry that gave
    up on a tick or two. Only the time of the last successful run differs."""

    paper_db = _paper_db(tmp_path, _every_tick(*PROFILES))
    if last_success is not None:
        for profile in PROFILES:
            _stamp(paper_db, profile, last_success)

    result = _run(tmp_path, paper_db)

    if healthy:
        assert result.returncode == 0, result.stderr
        assert "scheduler and publication health verified" in result.stdout
        return
    assert result.returncode == 1, result.stdout
    assert "scheduler and publication health verified" not in result.stdout
    for profile in PROFILES:
        assert (
            f"paper scan profile={profile} has no successful run in the last 20 minutes"
        ) in result.stderr
    assert result.stderr.count(STILL_JOURNALING) == 2
    assert NOT_SUCCEEDING in result.stderr
    # Detection only: the watchdog never starts a paper unit.
    assert not any(line.startswith("start ") for line in _calls(tmp_path))


def test_scheduler_health_catches_the_oct27_label_crash_as_main_runs_it(
    tmp_path: Path,
) -> None:
    """With main's runner, which keeps scanning after a profile fails, the
    Oct 27 LA crash leaves live journaling Miami before it reaches LA on every
    tick, while research scans LA first and journals nothing. Both accounts
    stopped trading, and the live journal alone still looks current."""

    paper_db = _paper_db(
        tmp_path,
        [(LAST_SUCCESS_BEFORE_CRASH, "research"), *_every_tick("live")],
    )
    for profile in PROFILES:
        _stamp(paper_db, profile, LAST_SUCCESS_BEFORE_CRASH)

    result = _run(tmp_path, paper_db)

    assert result.returncode == 1, result.stdout
    assert (
        "paper scan profile=live has no successful run in the last 20 minutes "
        f"(last success 2026-10-27T15:45:38Z); {STILL_JOURNALING}\n"
    ) in result.stderr
    assert (
        "paper scan profile=research has no successful run in the last 20 minutes "
        "(last success 2026-10-27T15:45:38Z)\n"
    ) in result.stderr
    assert NOT_SUCCEEDING in result.stderr


def test_scheduler_health_names_the_one_profile_that_stopped(tmp_path: Path) -> None:
    """Since PR #156 one profile can fail while the other keeps scanning, and
    the unit's single Result cannot say which account went quiet."""

    paper_db = _paper_db(
        tmp_path,
        [(_ago(45), "research"), (_ago(46), "live"), (_ago(2), "live")],
    )
    _stamp(paper_db, "live", 2.0)
    _stamp(paper_db, "research", 45.0)

    result = _run(tmp_path, paper_db)

    assert result.returncode == 1, result.stdout
    assert "paper scan profile=research has no successful run" in result.stderr
    assert "profile=live" not in result.stderr


def test_scheduler_health_accepts_a_recent_successful_run_from_every_profile(
    tmp_path: Path,
) -> None:
    paper_db = _paper_db(
        tmp_path,
        [(LAST_SUCCESS_BEFORE_CRASH, "live"), (_ago(3), "live"), (_ago(2), "research")],
    )
    _stamp(paper_db, "live", 2.5)
    _stamp(paper_db, "research", 1.5)

    result = _run(tmp_path, paper_db)

    assert result.returncode == 0, result.stderr
    assert "scheduler and publication health verified" in result.stdout
    assert not any(line.startswith("start ") for line in _calls(tmp_path))
    # The verdict never asks systemd about the scan service itself.
    assert not any("sfo-kalshi-paper-scan.service" in line for line in _calls(tmp_path))


def test_scheduler_health_fails_a_scan_that_succeeds_without_journaling(
    tmp_path: Path,
) -> None:
    """The secondary check. A profile can exit 0 and still journal nothing, for
    example when every target it reaches yields an empty decision list. Paper
    activity stops all the same, so a fresh stamp alone is not healthy."""

    paper_db = _paper_db(tmp_path, [(_ago(40), "live"), (_ago(2), "research")])
    _stamp(paper_db, "live", 3.0)
    _stamp(paper_db, "research", 2.0)

    result = _run(tmp_path, paper_db)

    assert result.returncode == 1, result.stdout
    assert re.search(
        r"paper scan profile=live last succeeded 3\.\d minutes ago but journaled "
        r"no decision in the last 20 minutes",
        result.stderr,
    ), result.stderr
    assert "profile=research" not in result.stderr
    assert NOT_JOURNALING in result.stderr


@pytest.mark.parametrize(
    ("timer_active_minutes", "healthy"),
    [(3.0, True), (25.0, False)],
)
def test_scheduler_health_grants_start_up_grace_after_the_scan_timer_restarts(
    tmp_path: Path, timer_active_minutes: float, healthy: bool
) -> None:
    """A deploy quiesces every timer for about an hour, restores the scan timer,
    and starts the watchdog minutes later. Pre-quiesce stamps and rows are not
    a failure until the restored timer has had a full window to replace them."""

    paper_db = _paper_db(tmp_path, [(_ago(70), "live"), (_ago(70), "research")])
    for profile in PROFILES:
        _stamp(paper_db, profile, 70.0)

    result = _run(tmp_path, paper_db, timer_active_minutes=timer_active_minutes)

    assert (
        "show --property=ActiveEnterTimestampMonotonic --value "
        "sfo-kalshi-paper-scan.timer"
    ) in _calls(tmp_path)
    if healthy:
        assert result.returncode == 0, result.stderr
        assert "start-up grace" in result.stdout
        assert "not yet healthy: live, research" in result.stdout
        assert "scheduler and publication health verified" in result.stdout
    else:
        assert result.returncode == 1, result.stdout
        assert "paper scan profile=live has no successful run" in result.stderr


@pytest.mark.parametrize(
    ("timer_active_minutes", "healthy"),
    [(2.0, True), (30.0, False)],
)
def test_scheduler_health_treats_missing_stamps_and_journal_as_no_scans(
    tmp_path: Path, timer_active_minutes: float, healthy: bool
) -> None:
    """A fresh install has neither until the first scan; the first deploy of
    this check has no stamps until the restarted timer's first good run."""

    result = _run(
        tmp_path,
        tmp_path / "trading-data" / "paper_trading.db",
        timer_active_minutes=timer_active_minutes,
    )

    if healthy:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1, result.stdout
        assert (
            "paper scan profile=live has no successful run in the last 20 minutes "
            "(no valid success stamp)"
        ) in result.stderr


@pytest.mark.parametrize(("timer_active_minutes", "healthy"), [(3.0, True), (25.0, False)])
def test_start_up_grace_is_independent_of_host_uptime(
    tmp_path: Path, monkeypatch, timer_active_minutes: float, healthy: bool
) -> None:
    # Simulate a host booted just 30 seconds ago, younger than either timer
    # activation. The helper must still model both sides of the grace window.
    monkeypatch.setattr(time, "clock_gettime_ns", lambda clock: 30_000_000_000)
    result = _run(
        tmp_path,
        tmp_path / "trading-data" / "paper_trading.db",
        timer_active_minutes=timer_active_minutes,
    )
    assert (result.returncode == 0) is healthy, result.stderr
    assert ("start-up grace" in result.stdout) is healthy
    if not healthy:
        assert NOT_SUCCEEDING in result.stderr


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param("soon\n", id="not-a-number"),
        pytest.param("1793632712.5\n", id="fractional"),
        pytest.param("-60\n", id="negative"),
        pytest.param(f"{int(time.time()) + 86_400}\n", id="a-day-in-the-future"),
    ],
)
def test_scheduler_health_does_not_count_a_malformed_or_future_stamp(
    tmp_path: Path, text: str
) -> None:
    """A future stamp would otherwise read as fresh until the clock caught up."""

    paper_db = _paper_db(tmp_path, _every_tick(*PROFILES))
    _stamp(paper_db, "research", 1.0)
    _stamp_path(paper_db, "live").write_text(text, encoding="ascii")

    result = _run(tmp_path, paper_db)

    assert result.returncode == 1, result.stdout
    assert "paper scan profile=live has no successful run" in result.stderr
    assert "profile=research" not in result.stderr


def test_scheduler_health_checks_only_the_configured_profiles(tmp_path: Path) -> None:
    """The runner scans PAPER_RISK_PROFILES; aliases normalize the same way."""

    paper_db = _paper_db(
        tmp_path, [(LAST_SUCCESS_BEFORE_CRASH, "research"), (_ago(4), "live")]
    )
    _stamp(paper_db, "live", 4.0)
    _stamp(paper_db, "research", LAST_SUCCESS_BEFORE_CRASH)

    result = _run(tmp_path, paper_db, profiles=" balanced ,")

    assert result.returncode == 0, result.stderr
    assert "scheduler and publication health verified" in result.stdout


def test_scheduler_health_scan_age_threshold_is_configurable(tmp_path: Path) -> None:
    paper_db = _paper_db(tmp_path, _every_tick(*PROFILES))
    for profile in PROFILES:
        _stamp(paper_db, profile, 12.0)

    result = _run(tmp_path, paper_db, max_age_minutes="10")

    assert result.returncode == 1, result.stdout
    assert "has no successful run in the last 10 minutes" in result.stderr


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
    _stamp(paper_db, "live", 2.0)

    result = _run(tmp_path, paper_db, profiles="live,bogus")

    assert result.returncode == 1
    assert "invalid paper risk profile: bogus" in result.stderr
    assert CHECK_BLOCKED in result.stderr


def test_scheduler_health_blocks_on_an_unreadable_paper_journal(tmp_path: Path) -> None:
    paper_db = tmp_path / "trading-data" / "paper_trading.db"
    paper_db.parent.mkdir()
    paper_db.write_bytes(b"not a sqlite database" * 64)
    for profile in PROFILES:
        _stamp(paper_db, profile, 2.0)

    result = _run(tmp_path, paper_db)

    assert result.returncode == 1
    assert "paper decision journal is unreadable" in result.stderr
    assert CHECK_BLOCKED in result.stderr


def test_scheduler_health_blocks_on_an_unreadable_success_stamp(tmp_path: Path) -> None:
    paper_db = _paper_db(tmp_path, [(_ago(1), "live")])
    _stamp_path(paper_db, "live").mkdir(parents=True)

    result = _run(tmp_path, paper_db, profiles="live")

    assert result.returncode == 1
    assert "paper scan success stamp is unreadable" in result.stderr
    assert CHECK_BLOCKED in result.stderr


def test_stale_scans_fail_before_any_publication_repair(tmp_path: Path) -> None:
    """Like a timer error, failing scans fail the run instead of being masked
    by the bounded publication repair, which exits OK on its own paths."""

    paper_db = _paper_db(tmp_path, _every_tick(*PROFILES))
    _stamp(paper_db, "live", LAST_SUCCESS_BEFORE_CRASH)
    _stamp(paper_db, "research", 2.0)

    result = _run(tmp_path, paper_db, strategy_minutes=25)

    assert result.returncode == 1, result.stdout
    assert "paper scan profile=live has no successful run" in result.stderr
    assert not any(line.startswith("start ") for line in _calls(tmp_path))


def test_scheduler_health_scan_check_respects_deploy_maintenance(
    tmp_path: Path,
) -> None:
    paper_db = _paper_db(tmp_path, _every_tick("live"))

    result = _run(tmp_path, paper_db, marker=True)

    assert result.returncode == 0, result.stderr
    assert "deployment maintenance active" in result.stdout
    assert _calls(tmp_path) == []


# ---------------------------------------------------------------------------
# What run_paper_scan_profiles.sh records
# ---------------------------------------------------------------------------


def test_runner_stamps_only_the_profiles_that_exit_zero(tmp_path: Path) -> None:
    paper_db = _paper_db(tmp_path, [])
    before = int(time.time())

    result = _run_scan_runner(tmp_path, paper_db, research=1)

    after = int(time.time())
    assert result.returncode == 1, result.stderr
    # Research journaled before it failed, exactly the case a row check misses.
    assert _journaled_profiles(paper_db) == ["live", "research"]
    state_dir = paper_db.parent / ".paper-scan-state"
    # One stamp, renamed into place, with no temporary file left behind.
    assert sorted(path.name for path in state_dir.iterdir()) == ["last-success-live"]
    finished = int(_stamp_path(paper_db, "live").read_text(encoding="ascii"))
    assert before <= finished <= after


def test_runner_and_watchdog_name_stamps_by_the_canonical_profile(
    tmp_path: Path,
) -> None:
    """Aliases in PAPER_RISK_PROFILES normalize the same way on both sides, so
    the stamp the runner writes is the one the watchdog looks for."""

    paper_db = _paper_db(tmp_path, [])
    check = tmp_path / "check"
    check.mkdir()

    runner = _run_scan_runner(tmp_path, paper_db, profiles=" Balanced ,fast_feedback")

    assert runner.returncode == 0, runner.stderr
    state_dir = paper_db.parent / ".paper-scan-state"
    assert sorted(path.name for path in state_dir.iterdir()) == [
        "last-success-live",
        "last-success-research",
    ]
    result = _run(check, paper_db, profiles="conservative, Exploratory")
    assert result.returncode == 0, result.stderr
    assert "scheduler and publication health verified" in result.stdout


def test_runner_leaves_a_failed_profiles_last_success_alone(tmp_path: Path) -> None:
    paper_db = _paper_db(tmp_path, [])
    _stamp(paper_db, "research", 30.0)
    previous = _stamp_path(paper_db, "research").read_text(encoding="ascii")

    result = _run_scan_runner(tmp_path, paper_db, research=1)

    assert result.returncode == 1, result.stderr
    assert _stamp_path(paper_db, "research").read_text(encoding="ascii") == previous


def test_an_unwritable_stamp_never_skips_a_profile_or_changes_the_exit(
    tmp_path: Path,
) -> None:
    """The stamp is written under set -e; a failure to write it must not skip
    the next account's scan, the way the Oct 27 live crash skipped research."""

    paper_db = _paper_db(tmp_path, [])
    (paper_db.parent / ".paper-scan-state").write_text("not a directory\n")

    result = _run_scan_runner(tmp_path, paper_db)

    assert result.returncode == 0, result.stderr
    assert _journaled_profiles(paper_db) == ["live", "research"]
    for profile in PROFILES:
        assert (
            f"warning: paper scan profile={profile} succeeded but its success stamp "
            "could not be written"
        ) in result.stderr


def test_a_tick_skipped_behind_the_scan_lock_records_no_success(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "flock", _FCNTL_FLOCK.format(python=sys.executable))
    paper_db = _paper_db(tmp_path, [])
    with open(tmp_path / "paper-scan.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = _run_scan_runner(tmp_path, paper_db, extra_path=fake_bin)

    assert result.returncode == 0, result.stderr
    assert "previous paper scan still running" in result.stdout
    assert _journaled_profiles(paper_db) == []
    assert not (paper_db.parent / ".paper-scan-state").exists()


def test_runner_and_watchdog_agree_on_a_profile_that_fails_partway(
    tmp_path: Path,
) -> None:
    """End to end: research journals and then fails, live succeeds. The real
    runner's stamps are what the real watchdog reads."""

    paper_db = _paper_db(tmp_path, [])
    first_check = tmp_path / "first-check"
    first_check.mkdir()
    second_check = tmp_path / "second-check"
    second_check.mkdir()

    assert _run_scan_runner(tmp_path, paper_db, research=1).returncode == 1
    failing = _run(first_check, paper_db)

    assert failing.returncode == 1, failing.stdout
    assert (
        "paper scan profile=research has no successful run in the last 20 minutes "
        f"(no valid success stamp); {STILL_JOURNALING}"
    ) in failing.stderr
    assert "profile=live" not in failing.stderr

    assert _run_scan_runner(tmp_path, paper_db).returncode == 0
    recovered = _run(second_check, paper_db)

    assert recovered.returncode == 0, recovered.stderr
    assert "scheduler and publication health verified" in recovered.stdout
