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
from sfo_kalshi_quant import research_m1_store as engine
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.research_m1_store import m1_trial_payload
from sfo_kalshi_quant.strategy_lab.status_alerts import _strategy_alerts

from test_nov01_scan_age import (
    CHECK_BLOCKED,
    SCRIPT,
    _every_tick,
    _paper_db,
    _run,
    _stamp,
)
from test_research_m1_trial import NOW, _store

M1_FAILED = "scheduler health failed: M1 paper trial is stale or its admissions keep failing"
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


def test_overdue_reconciliation_is_visible_until_maintenance_recovers(tmp_path, monkeypatch) -> None:
    db = _paper_db(tmp_path, _every_tick("live", "research"))
    store = PaperStore(db)

    def watchdog(label):
        check_dir = tmp_path / label
        check_dir.mkdir()
        return _run(check_dir, db)

    for profile in ("live", "research"):
        _stamp(db, profile, 1.0)
    assert m1_trial_payload(store, now=_at(11), resamples=10)["stop_rules"][0]["status"] == "ok"
    _evaluation(db, _at(0) - timedelta(hours=1), m1.DECISION_SHADOW)
    _evaluation(db, _at(0, 30), m1.DECISION_SHADOW)

    for now, overdue in (
        (_at(0, 42), False), (_at(1, 11), False), (_at(1, 12), True), (_at(11), True),
    ):
        trial = m1_trial_payload(store, now=now, resamples=10)
        fidelity = trial["stop_rules"][0]
        assert fidelity["status"] == ("overdue" if overdue else "ok")
        assert fidelity["last_reconciled_utc_day"] is None
        assert trial["entries_enabled_by_stop_rules"] is True and trial["halts"] == []
        alerts = _strategy_alerts(
            paper={"available": True, "summary": {}}, entry_block_reason=None, now=now,
            accounting={"accounts": {"research_m1": {"status": "ACTIVE", "trial": trial}}},
        )
        critical = [a for a in alerts if a["code"] == "m1-reconcile-overdue"]
        assert bool(critical) is overdue
        if critical:
            assert critical[0]["level"] == "critical"
        monkeypatch.setenv("SFO_SCHEDULER_M1_NOW", now.isoformat())
        result = watchdog(now.strftime("%H%M"))
        assert result.returncode == (1 if overdue else 0), result.stderr
        if overdue:
            assert "M1 trial reconciliation overdue" in result.stderr

    with store.connect() as conn:
        conn.execute("UPDATE paper_accounts SET status='ARCHIVED' WHERE account_id=?", (m1.M1_ACCOUNT_ID,))
    assert watchdog("archived").returncode == 0
    assert "m1-reconcile-overdue" not in {a["code"] for a in _strategy_alerts(
        paper={"available": True, "summary": {}}, entry_block_reason=None,
        accounting={"accounts": {"research_m1": {"status": "ARCHIVED", "trial": trial}}},
    )}
    with store.connect() as conn:
        conn.execute("UPDATE paper_accounts SET status='ACTIVE' WHERE account_id=?", (m1.M1_ACCOUNT_ID,))
    engine.run_m1_settle_maintenance(store, now=_at(11))
    recovered = m1_trial_payload(store, now=_at(11), resamples=10)
    assert recovered["stop_rules"][0]["status"] == "ok"
    assert recovered["stop_rules"][0]["last_reconciled_utc_day"] == "2026-11-02"
    assert recovered["entries_enabled_by_stop_rules"] is True and recovered["halts"] == []
    assert "m1-reconcile-overdue" not in {a["code"] for a in _strategy_alerts(
        paper={"available": True, "summary": {}}, entry_block_reason=None,
        accounting={"accounts": {"research_m1": {"status": "ACTIVE", "trial": recovered}}},
    )}
    _evaluation(db, _at(6, 50), m1.DECISION_SHADOW)
    assert watchdog("recovered").returncode == 0


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


@pytest.mark.parametrize("missing_table", [False, True])
@pytest.mark.parametrize(
    ("now", "age_hours", "healthy"),
    [(_at(20), 23, True), (_at(20), 25, False), (_at(11), 25, True)],
    ids=["new-account-grace", "never-started", "listing-gap"],
)
def test_empty_m1_journal_grace_expires_despite_fresh_research_scans(
    tmp_path, monkeypatch, missing_table, now, age_hours, healthy
) -> None:
    db = _paper_db(tmp_path, _every_tick("live", "research"))
    for profile in ("live", "research"):
        _stamp(db, profile, 1.0)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute(
            "UPDATE paper_accounts SET created_at=?, status='ACTIVE' WHERE account_id=?",
            ((now - timedelta(hours=age_hours)).isoformat(), m1.M1_ACCOUNT_ID),
        ).rowcount == 1
        if missing_table:
            conn.execute("DROP TABLE m1_evaluations")
        conn.commit()
    monkeypatch.setenv("SFO_SCHEDULER_M1_NOW", now.isoformat())
    result = _run(tmp_path, db)
    assert (result.returncode == 0) is healthy, result.stderr
    if not healthy:
        assert M1_FAILED in result.stderr
        assert "no evaluation since account creation" in result.stderr


def test_watchdog_alerts_on_repeated_admission_failures_even_inside_the_gap(
    tmp_path, monkeypatch
) -> None:
    # Failures in two UTC hours (an error at 10:50, a PENDING row orphaned at
    # 09:30) alert even in the listing gap.
    result = _watchdog(
        tmp_path, monkeypatch, _at(11), [(10, m1.DECISION_ADMISSION_ERROR), (90, m1.DECISION_PENDING)]
    )
    assert result.returncode == 1
    assert M1_FAILED in result.stderr and "admissions failed in 2 UTC hours" in result.stderr

    # One crash-orphaned PENDING row, one transient lock, or failures older than
    # three hours never alarm (and never alarm forever).
    for name, evaluations in {
        "orphan": [(90, m1.DECISION_PENDING)],
        "one-lock-hour": [(10, m1.DECISION_ADMISSION_ERROR), (15, m1.DECISION_ADMISSION_ERROR)],
        "old": [(300, m1.DECISION_ADMISSION_ERROR), (400, m1.DECISION_PENDING), (240, m1.DECISION_SHADOW)],
        "in-flight": [(90, m1.DECISION_ADMISSION_ERROR), (5, m1.DECISION_PENDING)],
    }.items():
        quiet = _watchdog(tmp_path / name, monkeypatch, _at(11), evaluations)
        assert quiet.returncode == 0, (name, quiet.stderr)


def test_the_m1_check_runs_after_the_live_and_v7_publication_repair() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    m1_check = script.index("# The M1 paper trial runs inside every research scan")
    assert script.index("OK: scheduler repaired bounded publication freshness") < m1_check
    assert m1_check < script.index('echo "OK: scheduler and publication health verified"')


def test_watchdog_skips_m1_when_research_is_not_scanned(tmp_path, monkeypatch) -> None:
    db = _paper_db(tmp_path, _every_tick("live"))
    _stamp(db, "live", 1.0)
    _evaluation(db, _at(20) - timedelta(hours=5), m1.DECISION_PENDING)
    monkeypatch.setenv("SFO_SCHEDULER_M1_NOW", _at(20).isoformat())
    result = _run(tmp_path, db, profiles="live")
    assert result.returncode == 0, result.stderr
    assert CHECK_BLOCKED not in result.stderr


def test_m1_failures_reach_public_status_after_a_clean_reconcile(tmp_path) -> None:
    """A contained M1 exception keeps the scan green, so publish the journal's verdict."""

    store, _clock = _store(tmp_path)
    db = tmp_path / "paper.db"

    def public(now):
        trial = m1_trial_payload(store, now=now, resamples=10)
        def alerts(status):
            return _strategy_alerts(
                paper={"available": True, "summary": {}}, entry_block_reason=None, now=now,
                accounting={"accounts": {"research_m1": {"status": status, "trial": trial}}},
            )
        return trial, [a for a in alerts("ACTIVE") if a["code"] == "m1-health"], alerts("ARCHIVED")

    _evaluation(db, _at(20), m1.DECISION_SHADOW)
    engine.run_m1_settle_maintenance(store, now=_at(20, 30))
    assert public(_at(21))[1] == []

    trial, critical, archived = public(_at(22))
    assert trial["stop_rules"][0]["status"] == "ok"
    assert [a["level"] for a in critical] == ["critical"]
    assert "no evaluation in the last 90 minutes" in critical[0]["detail"]
    assert "m1-health" not in {a["code"] for a in archived}
    # Dead before the listing gap stays failing through it; a normal gap stays quiet.
    assert public(_at(20) + timedelta(hours=16))[1]
    _evaluation(db, _at(20) + timedelta(hours=11), m1.DECISION_SHADOW)
    assert public(_at(20) + timedelta(hours=16))[1] == []

    _evaluation(db, _at(23), m1.DECISION_ADMISSION_ERROR, "boom")
    _evaluation(db, _at(23) + timedelta(hours=1, minutes=10), m1.DECISION_ADMISSION_ERROR, "boom")
    failed = public(_at(23) + timedelta(hours=1, minutes=20))[1]
    assert "admissions failed in 2 UTC hours" in failed[0]["detail"]
    _evaluation(db, _at(23) + timedelta(hours=4), m1.DECISION_SHADOW)
    assert public(_at(23) + timedelta(hours=4, minutes=5))[1] == []


def test_stop_rule_lookup_uses_the_order_index(tmp_path) -> None:
    store, _clock = _store(tmp_path)
    with store.connect() as conn:
        plan = " ".join(
            str(row[3]) for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT (SELECT e.agree FROM m1_evaluations e "
                "WHERE e.order_id = o.id ORDER BY e.id LIMIT 1) FROM paper_orders o"
            )
        )
    assert "idx_m1_evaluations_order" in plan
