from dataclasses import FrozenInstanceError
import sqlite3
from pathlib import Path

import pytest

from sfo_kalshi_quant.account import (
    account_for_profile,
    account_for_research_sleeve,
    strategy_fingerprint,
)
from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.research_policy import (
    MOTION_POLICY,
    TARGET_POLICY,
    ResearchSleeve,
)


_RESEARCH_IDENTITY_COLUMNS = {
    "research_sleeve",
    "research_policy_version",
    "policy_fingerprint",
    "objective_day",
    "lead_bucket",
    "scan_run_id",
    "reentry_fingerprint",
}


def _insert_research_order(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    account_id: str,
    sleeve: str | None,
    policy_version: str | None,
    policy_fingerprint: str | None,
    risk_profile: str = "research",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO paper_orders (
            created_at, target_date, market_ticker, label, action, risk_profile,
            side, contracts, yes_ask, fee_per_contract, cost_per_contract,
            probability, probability_lcb, edge, edge_lcb,
            trade_quality_score, expected_profit, status, reasons_json,
            account_id, research_sleeve, research_policy_version,
            policy_fingerprint
        ) VALUES (
            '2026-07-18T12:00:00+00:00', '2026-07-19', ?, '80 to 81',
            'BUY_NO', ?, 'NO', 1, 0.80, 0.01, 0.81, 0.70, 0.65,
            0.10, 0.05, 50, 0.10, 'PAPER_FILLED', '[]', ?, ?, ?, ?
        )
        """,
        (
            ticker,
            risk_profile,
            account_id,
            sleeve,
            policy_version,
            policy_fingerprint,
        ),
    )
    return int(cursor.lastrowid)


def test_research_policy_constants_are_fixed() -> None:
    assert TARGET_POLICY.sleeve is ResearchSleeve.TARGET
    assert TARGET_POLICY.account_id == "paper-research-target-v1"
    assert TARGET_POLICY.policy_version == "research-target-v1"
    assert TARGET_POLICY.reference_equity == 1000.0
    assert TARGET_POLICY.target_return == 0.05
    assert TARGET_POLICY.target_pnl == 50.0
    assert TARGET_POLICY.max_position_risk_pct == 0.03
    assert TARGET_POLICY.max_city_target_risk_pct == 0.06
    assert TARGET_POLICY.max_region_day_risk_pct == 0.12
    assert TARGET_POLICY.max_aggregate_risk_pct == 0.25
    assert TARGET_POLICY.daily_loss_pause_pct == 0.10
    assert TARGET_POLICY.min_lead_days == 1
    assert TARGET_POLICY.one_contract is False


def test_motion_policy_constants_are_fixed() -> None:
    assert MOTION_POLICY.sleeve is ResearchSleeve.MOTION
    assert MOTION_POLICY.account_id == "paper-research-motion-v1"
    assert MOTION_POLICY.policy_version == "research-motion-v1"
    assert MOTION_POLICY.reference_equity == 1000.0
    assert MOTION_POLICY.target_return == 0.0
    assert MOTION_POLICY.target_pnl == 0.0
    # Motion's per-position control is exactly one contract. The percentage
    # field is deliberately non-binding rather than inventing a fifth cap.
    assert MOTION_POLICY.max_position_risk_pct == 1.0
    assert MOTION_POLICY.max_city_target_risk_pct == 0.02
    assert MOTION_POLICY.max_region_day_risk_pct == 0.04
    assert MOTION_POLICY.max_aggregate_risk_pct == 0.10
    assert MOTION_POLICY.daily_loss_pause_pct == 0.05
    assert MOTION_POLICY.min_lead_days == 0
    assert MOTION_POLICY.one_contract is True


def test_research_sleeve_policies_are_immutable_and_fingerprinted() -> None:
    with pytest.raises(FrozenInstanceError):
        TARGET_POLICY.reference_equity = 2000.0  # type: ignore[misc]

    assert TARGET_POLICY.policy_fingerprint == "dea759010dc85ca5f4f610e2"
    assert MOTION_POLICY.policy_fingerprint == "1c50d872ce278b403a6ad80e"


def test_research_sleeves_route_to_isolated_accounts() -> None:
    assert account_for_research_sleeve(ResearchSleeve.TARGET) == TARGET_POLICY.account_id
    assert account_for_research_sleeve(ResearchSleeve.MOTION) == MOTION_POLICY.account_id


def test_live_account_and_fingerprint_are_unchanged() -> None:
    assert account_for_profile("live") == "paper-shared"
    config = strategy_config_for_profile("live")
    assert strategy_fingerprint(config, entry_mode="limit") == "a965c8280aca2b3621f0c312"
    assert strategy_fingerprint(config, entry_mode="market") == "73b10240c1c00a8937b5314f"


def test_init_bootstraps_both_research_accounts_without_rewriting_legacy(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    db_path = tmp_path / "paper.db"
    store = PaperStore(db_path)
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_accounts SET created_at=?, cutover_note=? WHERE account_id=?",
            (
                "2020-01-02T03:04:05+00:00",
                "legacy shared row: preserve bytes exactly",
                "paper-shared",
            ),
        )
        conn.execute(
            "UPDATE paper_accounts SET created_at=?, cutover_note=? WHERE account_id=?",
            (
                "2020-01-03T03:04:05+00:00",
                "legacy shadow row: preserve bytes exactly",
                "paper-research-shadow",
            ),
        )
        legacy_before = {
            row[0]: tuple(row)
            for row in conn.execute(
                "SELECT * FROM paper_accounts WHERE account_id IN (?, ?)",
                ("paper-shared", "paper-research-shadow"),
            ).fetchall()
        }

    # Exercise the idempotent legacy migration path twice.
    PaperStore(db_path)
    reopened = PaperStore(db_path)
    with reopened.connect() as conn:
        legacy_after = {
            row[0]: tuple(row)
            for row in conn.execute(
                "SELECT * FROM paper_accounts WHERE account_id IN (?, ?)",
                ("paper-shared", "paper-research-shadow"),
            ).fetchall()
        }
        research_accounts = conn.execute(
            """
            SELECT account_id, initial_capital, opening_cash, high_water_equity,
                   status
            FROM paper_accounts
            WHERE account_id IN (?, ?)
            ORDER BY account_id
            """,
            (TARGET_POLICY.account_id, MOTION_POLICY.account_id),
        ).fetchall()
        openings = conn.execute(
            """
            SELECT account_id, COUNT(*), SUM(amount)
            FROM paper_account_ledger
            WHERE account_id IN (?, ?) AND event_type='OPENING_CASH'
            GROUP BY account_id
            ORDER BY account_id
            """,
            (TARGET_POLICY.account_id, MOTION_POLICY.account_id),
        ).fetchall()

        goal_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='research_daily_goals'"
        ).fetchone()
        for table in (
            "paper_orders",
            "decision_snapshots",
            "scan_context_snapshots",
            "paper_monitor_snapshots",
            "research_shadow_monitor_snapshots",
        ):
            columns = {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            assert _RESEARCH_IDENTITY_COLUMNS <= columns

    assert legacy_after == legacy_before
    assert research_accounts == [
        (MOTION_POLICY.account_id, 1000.0, 1000.0, 1000.0, "ACTIVE"),
        (TARGET_POLICY.account_id, 1000.0, 1000.0, 1000.0, "ACTIVE"),
    ]
    assert openings == [
        (MOTION_POLICY.account_id, 1, 1000.0),
        (TARGET_POLICY.account_id, 1, 1000.0),
    ]
    assert goal_sql is not None
    normalized_goal_sql = " ".join(str(goal_sql[0]).split()).upper()
    assert "PRIMARY KEY(OBJECTIVE_DAY, ACCOUNT_ID, POLICY_VERSION)" in normalized_goal_sql
    assert "CHECK(REFERENCE_EQUITY > 0)" in normalized_goal_sql
    assert "CHECK(TARGET_RETURN > 0)" in normalized_goal_sql
    assert "CHECK(TARGET_PNL > 0)" in normalized_goal_sql


def test_new_research_write_requires_sleeve_policy_identity(tmp_path: Path) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(tmp_path / "paper.db")
    valid = {
        "sleeve": TARGET_POLICY.sleeve.value,
        "policy_version": TARGET_POLICY.policy_version,
        "policy_fingerprint": TARGET_POLICY.policy_fingerprint,
    }
    with store.connect() as conn:
        for missing in tuple(valid):
            identity = {**valid, missing: None}
            with pytest.raises(sqlite3.IntegrityError, match="research identity"):
                _insert_research_order(
                    conn,
                    ticker=f"KXHIGHTSFO-MISSING-{missing}",
                    account_id=TARGET_POLICY.account_id,
                    **identity,
                )

        with pytest.raises(sqlite3.IntegrityError, match="research identity"):
            _insert_research_order(
                conn,
                ticker="KXHIGHTSFO-BLANK",
                account_id=MOTION_POLICY.account_id,
                sleeve=" ",
                policy_version=MOTION_POLICY.policy_version,
                policy_fingerprint=MOTION_POLICY.policy_fingerprint,
            )

        # Legacy/live rows with no research identity remain valid.
        legacy_id = _insert_research_order(
            conn,
            ticker="KXHIGHTSFO-LEGACY",
            account_id="paper-shared",
            sleeve=None,
            policy_version=None,
            policy_fingerprint=None,
            risk_profile="live",
        )
        assert legacy_id > 0
