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


_IDENTITY_TABLES = (
    "paper_orders",
    "decision_snapshots",
    "scan_context_snapshots",
    "paper_monitor_snapshots",
    "research_shadow_monitor_snapshots",
)


def _insert_identity_row(
    conn: sqlite3.Connection,
    table: str,
    *,
    established: bool,
) -> int:
    identity = (
        (
            TARGET_POLICY.sleeve.value,
            TARGET_POLICY.policy_version,
            TARGET_POLICY.policy_fingerprint,
        )
        if established
        else (None, None, None)
    )
    if table == "paper_orders":
        return _insert_research_order(
            conn,
            ticker=(
                "KXHIGHTSFO-IDENTITY-ESTABLISHED"
                if established
                else "KXHIGHTSFO-IDENTITY-LEGACY"
            ),
            account_id=TARGET_POLICY.account_id if established else "paper-shared",
            sleeve=identity[0],
            policy_version=identity[1],
            policy_fingerprint=identity[2],
            risk_profile="research" if established else "live",
        )
    if table == "decision_snapshots":
        cursor = conn.execute(
            """
            INSERT INTO decision_snapshots (
                created_at, target_date, market_ticker, label, action, side,
                approved, probability, probability_lcb, yes_bid, yes_ask,
                spread, fee_per_contract, cost_per_contract, edge, edge_lcb,
                kelly_fraction, recommended_contracts, recommended_spend,
                expected_profit, trade_quality_score, reasons_json,
                research_sleeve, research_policy_version, policy_fingerprint
            ) VALUES (
                '2026-07-18T12:00:00+00:00', '2026-07-19',
                'KXHIGHTSFO-IDENTITY', '80 to 81', 'BUY_NO', 'NO', 1,
                0.70, 0.65, 0.20, 0.21, 0.01, 0.01, 0.81, 0.10, 0.05,
                0.01, 1, 0.81, 0.10, 50, '[]', ?, ?, ?
            )
            """,
            identity,
        )
        return int(cursor.lastrowid)
    if table == "scan_context_snapshots":
        cursor = conn.execute(
            """
            INSERT INTO scan_context_snapshots (
                created_at, target_date, prediction_features_json,
                schema_version, research_sleeve, research_policy_version,
                policy_fingerprint
            ) VALUES (
                '2026-07-18T12:00:00+00:00', '2026-07-19', '{}', 1, ?, ?, ?
            )
            """,
            identity,
        )
        return int(cursor.lastrowid)
    if table == "paper_monitor_snapshots":
        cursor = conn.execute(
            """
            INSERT INTO paper_monitor_snapshots (
                created_at, order_id, target_date, market_ticker, side, action,
                research_sleeve, research_policy_version, policy_fingerprint
            ) VALUES (
                '2026-07-18T12:00:00+00:00', 999, '2026-07-19',
                'KXHIGHTSFO-IDENTITY', 'NO', 'HOLD', ?, ?, ?
            )
            """,
            identity,
        )
        return int(cursor.lastrowid)
    if table == "research_shadow_monitor_snapshots":
        cursor = conn.execute(
            """
            INSERT INTO research_shadow_monitor_snapshots (
                created_at, shadow_order_id, target_date, market_ticker, side,
                action, research_sleeve, research_policy_version,
                policy_fingerprint
            ) VALUES (
                '2026-07-18T12:00:00+00:00', 999, '2026-07-19',
                'KXHIGHTSFO-IDENTITY', 'NO', 'HOLD', ?, ?, ?
            )
            """,
            identity,
        )
        return int(cursor.lastrowid)
    raise AssertionError(f"unsupported identity table {table}")


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


def test_target_order_cannot_atomically_move_account_and_erase_identity(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(tmp_path / "paper.db")
    with store.connect() as conn:
        order_id = _insert_identity_row(conn, "paper_orders", established=True)
        with pytest.raises(sqlite3.IntegrityError, match="research identity"):
            conn.execute(
                """
                UPDATE paper_orders
                SET account_id='paper-shared', research_sleeve=NULL,
                    research_policy_version=NULL, policy_fingerprint=NULL
                WHERE id=?
                """,
                (order_id,),
            )


@pytest.mark.parametrize("table", _IDENTITY_TABLES)
def test_established_research_identity_cannot_be_erased(
    tmp_path: Path,
    table: str,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(tmp_path / f"{table}.db")
    with store.connect() as conn:
        row_id = _insert_identity_row(conn, table, established=True)
        with pytest.raises(sqlite3.IntegrityError, match="research identity"):
            conn.execute(
                f"""
                UPDATE {table}
                SET research_sleeve=NULL, research_policy_version=NULL,
                    policy_fingerprint=NULL
                WHERE id=?
                """,
                (row_id,),
            )


@pytest.mark.parametrize("table", _IDENTITY_TABLES)
def test_identity_preserving_and_legacy_updates_remain_allowed(
    tmp_path: Path,
    table: str,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(tmp_path / f"{table}.db")
    with store.connect() as conn:
        research_id = _insert_identity_row(conn, table, established=True)
        legacy_id = _insert_identity_row(conn, table, established=False)
        conn.execute(
            f"UPDATE {table} SET objective_day='2026-07-18' WHERE id=?",
            (research_id,),
        )
        conn.execute(
            f"UPDATE {table} SET created_at='2026-07-18T13:00:00+00:00' WHERE id=?",
            (legacy_id,),
        )

        rows = conn.execute(
            f"SELECT id, research_sleeve, research_policy_version, "
            f"policy_fingerprint FROM {table} WHERE id IN (?, ?) ORDER BY id",
            (research_id, legacy_id),
        ).fetchall()

    assert rows == [
        (
            research_id,
            TARGET_POLICY.sleeve.value,
            TARGET_POLICY.policy_version,
            TARGET_POLICY.policy_fingerprint,
        ),
        (legacy_id, None, None, None),
    ]


def test_init_replaces_permissive_same_name_identity_trigger(tmp_path: Path) -> None:
    from sfo_kalshi_quant.db import PaperStore

    db_path = tmp_path / "permissive-trigger.db"
    store = PaperStore(db_path)
    trigger_name = "trg_paper_orders_research_identity_insert"
    with store.connect() as conn:
        conn.execute(f"DROP TRIGGER {trigger_name}")
        conn.execute(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE INSERT ON paper_orders
            BEGIN
                SELECT 1;
            END
            """
        )

    PaperStore(db_path)

    with sqlite3.connect(db_path) as conn:
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger_name,),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="research identity"):
            _insert_research_order(
                conn,
                ticker="KXHIGHTSFO-PERMISSIVE",
                account_id=TARGET_POLICY.account_id,
                sleeve=None,
                policy_version=TARGET_POLICY.policy_version,
                policy_fingerprint=TARGET_POLICY.policy_fingerprint,
            )
    assert "research identity requires" in trigger_sql


def test_identity_trigger_recreate_failure_rolls_back_old_definition(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    db_path = tmp_path / "trigger-recreate-failure.db"
    store = PaperStore(db_path)
    trigger_name = "trg_paper_orders_research_identity_insert"
    permissive_sql = f"""
        CREATE TRIGGER {trigger_name}
        BEFORE INSERT ON paper_orders
        BEGIN
            SELECT 1;
        END
    """
    with store.connect() as conn:
        conn.execute(f"DROP TRIGGER {trigger_name}")
        conn.execute(permissive_sql)

    denied = PaperStore(db_path, init=False)
    normal_connect = denied.connect

    def connect_denying_trigger_create() -> sqlite3.Connection:
        conn = normal_connect()

        def authorizer(
            action: int,
            arg1: str | None,
            _arg2: str | None,
            _db_name: str | None,
            _trigger_name: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_CREATE_TRIGGER and arg1 == trigger_name:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        return conn

    denied.connect = connect_denying_trigger_create  # type: ignore[method-assign]
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        denied.init()

    with sqlite3.connect(db_path) as conn:
        stored_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger_name,),
        ).fetchone()[0]
    assert "SELECT 1" in stored_sql
    assert "research identity requires" not in stored_sql


def test_clean_reinit_leaves_identity_trigger_schema_unchanged(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    db_path = tmp_path / "clean-trigger-reinit.db"
    store = PaperStore(db_path)

    def trigger_state() -> tuple[int, list[tuple[str, str, int]]]:
        with sqlite3.connect(db_path) as conn:
            schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
            triggers = conn.execute(
                """
                SELECT name, sql, rootpage
                FROM sqlite_master
                WHERE type='trigger' AND name LIKE 'trg_%_research_identity_%'
                ORDER BY name
                """
            ).fetchall()
        return schema_version, triggers

    before = trigger_state()
    PaperStore(db_path)
    after = trigger_state()

    assert len(before[1]) == 10
    assert after == before
