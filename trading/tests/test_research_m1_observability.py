"""M1 trial failures must be visible: a published health block and a watchdog check.

The research scan contains M1 exceptions (stderr, exit 0, success stamp), so the
trial's own journal is the evidence. The watchdog tolerates the normal daily gap
of about 07:00-15:00 UTC when no next-day market is listed and M1 writes nothing.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sfo_kalshi_quant import research_m1 as m1
from sfo_kalshi_quant.research_m1_store import m1_trial_payload

from test_nov01_scan_age import (
    CHECK_BLOCKED,
    _every_tick,
    _paper_db,
    _run,
    _stamp,
)
from test_research_m1_trial import NOW, _store

M1_FAILED = "scheduler health failed: M1 paper trial is stale or has stuck admissions"
INSERT_EVALUATION = """
INSERT INTO m1_evaluations (
    created_at, utc_hour, scan_id, policy_version, policy_fingerprint,
    coefficient_set_id, target_date, market_ticker, event_ticker, series_ticker,
    side, civil_tz, local_hour, lead_days, listing_hour, bucket, leg_passes,
    contracts, stake, placement_enabled, decision, reason
) VALUES (?, ?, 'scan', 'v', 'f', 'c', '2026-10-21', ?, 'E', 'KXHIGHLAX', 'YES',
          'America/Los_Angeles', 12.0, 1, 7, 'L1b', 1, 1, 1.0, 1, ?, ?)
"""


def _evaluation(db: Path, created_at: datetime, decision: str, reason: str | None = None) -> None:
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            INSERT_EVALUATION,
            (
                created_at.isoformat(),
                created_at.strftime("%Y-%m-%dT%H"),
                f"T-{created_at.isoformat()}-{decision}",
                decision,
                reason,
            ),
        )
        conn.commit()


def test_trial_payload_publishes_m1_health(tmp_path) -> None:
    store, _ = _store(tmp_path)
    db = tmp_path / "paper.db"
    _evaluation(db, NOW - timedelta(hours=3), m1.DECISION_PENDING)
    _evaluation(db, NOW - timedelta(minutes=10), m1.DECISION_PENDING)  # still fresh
    _evaluation(db, NOW - timedelta(minutes=5), m1.DECISION_ADMISSION_ERROR, "admission error: boom")
    health = m1_trial_payload(store, now=NOW, resamples=10)["health"]
    assert health["last_evaluation_at"] == (NOW - timedelta(minutes=5)).isoformat()
    assert health["stale_pending_admissions"] == 1
    assert health["last_error"] == {
        "at": (NOW - timedelta(minutes=5)).isoformat(),
        "reason": "admission error: boom",
    }
    assert health["missed_entries"] is None  # nothing reconciled yet


def _watchdog(tmp_path, monkeypatch, now: datetime, evaluations):
    """Run the real watchdog with fresh scans and the given M1 journal at ``now``."""

    db = _paper_db(tmp_path, _every_tick("live", "research"))
    for profile in ("live", "research"):
        _stamp(db, profile, 1.0)
    for age_minutes, decision in evaluations:
        _evaluation(db, now - timedelta(minutes=age_minutes), decision)
    monkeypatch.setenv("SFO_SCHEDULER_M1_NOW", now.isoformat())
    return _run(tmp_path, db)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 3, hour, minute, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "newest_age", "healthy"),
    [
        pytest.param(_at(20), 30, True, id="evaluating-in-the-listed-window"),
        pytest.param(_at(20), 120, False, id="silent-for-two-hours-while-listed"),
        pytest.param(_at(11), 240, True, id="normal-07-to-15-utc-gap"),
        pytest.param(_at(16, 15), 540, True, id="first-90-minutes-after-listing"),
        pytest.param(_at(17), 600, False, id="still-silent-after-the-grace"),
    ],
)
def test_watchdog_alerts_on_stale_m1_evaluations_but_not_the_daily_gap(
    tmp_path, monkeypatch, now, newest_age, healthy
) -> None:
    result = _watchdog(tmp_path, monkeypatch, now, [(newest_age, m1.DECISION_SHADOW)])
    assert (result.returncode == 0) is healthy, result.stderr
    if not healthy:
        assert M1_FAILED in result.stderr
        assert "no evaluation in the last 90 minutes" in result.stderr


def test_watchdog_alerts_on_stuck_pending_admissions_even_inside_the_gap(
    tmp_path, monkeypatch
) -> None:
    result = _watchdog(tmp_path, monkeypatch, _at(11), [(90, m1.DECISION_PENDING)])
    assert result.returncode == 1
    assert M1_FAILED in result.stderr and "1 PENDING_ADMISSION row" in result.stderr
    fresh = _watchdog(tmp_path / "fresh", monkeypatch, _at(11), [(20, m1.DECISION_PENDING)])
    assert fresh.returncode == 0, fresh.stderr


def test_watchdog_skips_m1_when_research_is_not_scanned(tmp_path, monkeypatch) -> None:
    db = _paper_db(tmp_path, _every_tick("live"))
    _stamp(db, "live", 1.0)
    _evaluation(db, _at(20) - timedelta(hours=5), m1.DECISION_PENDING)
    monkeypatch.setenv("SFO_SCHEDULER_M1_NOW", _at(20).isoformat())
    result = _run(tmp_path, db, profiles="live")
    assert result.returncode == 0, result.stderr
    assert CHECK_BLOCKED not in result.stderr
