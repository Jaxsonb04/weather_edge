from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
import sqlite3
from pathlib import Path
from threading import Barrier

import pytest

from sfo_kalshi_quant.account import (
    account_for_profile,
    account_for_research_sleeve,
    strategy_fingerprint,
)
from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.models import TradeDecision
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


def _atomic_decision(
    ticker: str,
    *,
    contracts: float = 1.0,
    resting: bool = True,
) -> TradeDecision:
    limit_price = 0.80 if resting else None
    return TradeDecision(
        ticker=ticker,
        label="80° to 81°",
        action="BUY_NO",
        approved=True,
        probability=0.90,
        probability_lcb=0.88,
        yes_bid=0.17,
        yes_ask=0.18,
        spread=0.02,
        fee_per_contract=0.0,
        cost_per_contract=0.82,
        edge=0.08,
        edge_lcb=0.06,
        kelly_fraction=0.03,
        recommended_contracts=contracts,
        expected_profit=0.08 * contracts,
        reasons=[],
        side="NO",
        entry_bid=0.79,
        entry_ask=0.82,
        entry_bid_size=0.0,
        entry_ask_size=100.0,
        strike_type="between",
        floor_strike=80.0,
        cap_strike=81.0,
        trade_quality_score=75.0,
        limit_price=limit_price,
        limit_fee_per_contract=0.0 if resting else None,
        limit_cost_per_contract=0.80 if resting else None,
        limit_edge=0.10 if resting else None,
        limit_edge_lcb=0.08 if resting else None,
    )


def _fixed_research_clock() -> datetime:
    return datetime(2026, 7, 18, 20, tzinfo=UTC)


def _admission(policy, suffix: str):
    from sfo_kalshi_quant.db import ResearchAdmission

    return ResearchAdmission(
        account_id=policy.account_id,
        sleeve=policy.sleeve,
        policy_version=policy.policy_version,
        policy_fingerprint=policy.policy_fingerprint,
        objective_day="2026-07-18",
        scan_run_id=f"scan-{suffix}",
        reentry_fingerprint=f"reentry-{suffix}",
        lead_bucket="day-ahead",
        entry_decision_id=1,
    )


def _insert_research_decision_evidence(
    store,
    policy,
    suffix: str,
    decision: TradeDecision,
    *,
    objective_day: str = "2026-07-18",
    decision_overrides: dict[str, object] | None = None,
    context_overrides: dict[str, object] | None = None,
) -> int:
    identity = {
        "research_sleeve": policy.sleeve.value,
        "research_policy_version": policy.policy_version,
        "policy_fingerprint": policy.policy_fingerprint,
        "objective_day": objective_day,
        "lead_bucket": "day-ahead",
        "scan_run_id": f"scan-{suffix}",
        "reentry_fingerprint": f"reentry-{suffix}",
    }
    context = {
        **identity,
        "target_date": "2026-07-19",
        **(context_overrides or {}),
    }
    snapshot = {
        **identity,
        "target_date": "2026-07-19",
        "market_ticker": decision.ticker,
        "side": decision.side,
        **(decision_overrides or {}),
    }
    with store.connect() as conn:
        context_cursor = conn.execute(
            """
            INSERT INTO scan_context_snapshots (
                created_at, target_date, risk_profile,
                prediction_features_json, schema_version, research_sleeve,
                research_policy_version, policy_fingerprint, objective_day,
                lead_bucket, scan_run_id, reentry_fingerprint
            ) VALUES (
                '2026-07-18T12:00:00+00:00', ?, 'research', '{}', 1,
                ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                context["target_date"],
                context["research_sleeve"],
                context["research_policy_version"],
                context["policy_fingerprint"],
                context["objective_day"],
                context["lead_bucket"],
                context["scan_run_id"],
                context["reentry_fingerprint"],
            ),
        )
        decision_cursor = conn.execute(
            """
            INSERT INTO decision_snapshots (
                scan_context_id, created_at, target_date, market_ticker, label,
                action, side, approved, probability, probability_lcb, yes_bid,
                yes_ask, entry_bid, entry_ask, entry_bid_size, entry_ask_size,
                spread, fee_per_contract, cost_per_contract, edge, edge_lcb,
                kelly_fraction, recommended_contracts, recommended_spend,
                expected_profit, trade_quality_score, reasons_json, risk_profile,
                research_sleeve, research_policy_version, policy_fingerprint,
                objective_day, lead_bucket, scan_run_id, reentry_fingerprint
            ) VALUES (
                ?, '2026-07-18T12:00:01+00:00', ?, ?, ?, ?, ?, 1,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]',
                'research', ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                int(context_cursor.lastrowid),
                snapshot["target_date"],
                snapshot["market_ticker"],
                decision.label,
                decision.action,
                snapshot["side"],
                decision.probability,
                decision.probability_lcb,
                decision.yes_bid,
                decision.yes_ask,
                decision.bid,
                decision.ask,
                decision.bid_size,
                decision.ask_size,
                decision.spread,
                decision.fee_per_contract,
                decision.cost_per_contract,
                decision.edge,
                decision.edge_lcb,
                decision.kelly_fraction,
                decision.recommended_contracts,
                decision.recommended_contracts * decision.cost_per_contract,
                decision.expected_profit,
                decision.trade_quality_score,
                snapshot["research_sleeve"],
                snapshot["research_policy_version"],
                snapshot["policy_fingerprint"],
                snapshot["objective_day"],
                snapshot["lead_bucket"],
                snapshot["scan_run_id"],
                snapshot["reentry_fingerprint"],
            ),
        )
        return int(decision_cursor.lastrowid)


def _linked_admission(
    store,
    policy,
    suffix: str,
    decision: TradeDecision,
    *,
    objective_day: str = "2026-07-18",
    decision_overrides: dict[str, object] | None = None,
    context_overrides: dict[str, object] | None = None,
):
    entry_decision_id = _insert_research_decision_evidence(
        store,
        policy,
        suffix,
        decision,
        objective_day=objective_day,
        decision_overrides=decision_overrides,
        context_overrides=context_overrides,
    )
    return replace(
        _admission(policy, suffix),
        objective_day=objective_day,
        entry_decision_id=entry_decision_id,
    )


def test_research_admission_is_immutable() -> None:
    admission = _admission(TARGET_POLICY, "frozen")

    with pytest.raises(FrozenInstanceError):
        admission.account_id = MOTION_POLICY.account_id  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("account_id", MOTION_POLICY.account_id),
        ("sleeve", ResearchSleeve.MOTION),
        ("policy_version", "research-target-v999"),
        ("policy_fingerprint", MOTION_POLICY.policy_fingerprint),
    ),
)
def test_atomic_admission_requires_exact_policy_identity(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(tmp_path / f"identity-{field}.db")
    admission = replace(_admission(TARGET_POLICY, field), **{field: value})

    with pytest.raises(ValueError, match="research admission identity"):
        store.record_research_order_atomic(
            "2026-07-19",
            _atomic_decision(f"KXHIGHTSFO-IDENTITY-{field}"),
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )

    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE scan_run_id=?",
            (admission.scan_run_id,),
        ).fetchone()[0] == 0


def test_atomic_admission_requires_nonblank_lead_bucket(tmp_path: Path) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(tmp_path / "lead-bucket.db")
    admission = replace(_admission(TARGET_POLICY, "missing-lead"), lead_bucket=None)

    with pytest.raises(ValueError, match="research admission lead bucket"):
        store.record_research_order_atomic(
            "2026-07-19",
            _atomic_decision("KXHIGHTSFO-MISSING-LEAD"),
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )


def test_atomic_admission_fails_closed_on_tampered_account_policy(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "tampered-account.db", research_clock=_fixed_research_clock
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_accounts SET initial_capital=999 WHERE account_id=?",
            (TARGET_POLICY.account_id,),
        )

    decision = _atomic_decision("KXHIGHTSFO-TAMPERED-ACCOUNT")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        "tampered-account",
        decision,
    )
    order_id = store.record_research_order_atomic(
        "2026-07-19",
        decision,
        admission=admission,
        strategy_config=strategy_config_for_profile("research"),
    )

    assert order_id is None
    assert store.entries_for_market_side(
        "2026-07-19",
        "KXHIGHTSFO-TAMPERED-ACCOUNT",
        "NO",
        risk_profile="research",
        account_id=TARGET_POLICY.account_id,
    ) == 0


def test_concurrent_atomic_admissions_cannot_overreserve_account_cash(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    db_path = tmp_path / "concurrent.db"
    setup = PaperStore(db_path, research_clock=_fixed_research_clock)
    # Leave $30 available while preserving the fixed $1,000 policy reference.
    # Each candidate needs $20; a check-then-write race would admit both.
    with setup.connect() as conn:
        conn.execute(
            """
            INSERT INTO paper_account_ledger (
                created_at, account_id, order_id, event_type, amount,
                idempotency_key, details_json
            ) VALUES (
                '2026-07-18T08:00:00+00:00', ?, NULL, 'TEST_WITHDRAWAL',
                -970, 'test:target:withdrawal', '{}'
            )
            """,
            (TARGET_POLICY.account_id,),
        )

    stores = (
        PaperStore(db_path, research_clock=_fixed_research_clock),
        PaperStore(db_path, research_clock=_fixed_research_clock),
    )
    decisions = tuple(
        _atomic_decision(f"KXHIGHTSFO-CONCURRENT-{index}", contracts=25.0)
        for index in range(2)
    )
    admissions = tuple(
        _linked_admission(
            setup,
            TARGET_POLICY,
            f"concurrent-{index}",
            decisions[index],
        )
        for index in range(2)
    )
    barrier = Barrier(2)

    def admit(index: int) -> int | None:
        barrier.wait(timeout=5)
        return stores[index].record_research_order_atomic(
            "2026-07-19",
            decisions[index],
            admission=admissions[index],
            strategy_config=strategy_config_for_profile("research"),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, range(2)))

    assert sum(order_id is not None for order_id in results) == 1
    state = setup.research_account_state(account_id=TARGET_POLICY.account_id)
    assert state is not None
    assert state["available_cash"] == pytest.approx(10.0)
    assert state["reservations"] == pytest.approx(20.0)
    assert state["available_cash"] >= 0.0


def test_research_reservations_are_isolated_by_account(tmp_path: Path) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "reservations.db", research_clock=_fixed_research_clock
    )
    decision = _atomic_decision("KXHIGHTSFO-RESERVE-TARGET", contracts=10.0)
    target_id = store.record_research_order_atomic(
        "2026-07-19",
        decision,
        admission=_linked_admission(
            store,
            TARGET_POLICY,
            "reserve-target",
            decision,
        ),
        strategy_config=strategy_config_for_profile("research"),
    )

    assert target_id is not None
    target = store.research_account_state(account_id=TARGET_POLICY.account_id)
    motion = store.research_account_state(account_id=MOTION_POLICY.account_id)
    assert target is not None and motion is not None
    assert target["reservations"] == pytest.approx(8.0)
    assert target["available_cash"] == pytest.approx(992.0)
    assert motion["reservations"] == 0.0
    assert motion["available_cash"] == 1000.0


def test_active_research_exposure_releases_after_close(tmp_path: Path) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "release.db", research_clock=_fixed_research_clock
    )
    decision = _atomic_decision(
        "KXHIGHTSFO-RELEASE", contracts=20.0, resting=False
    )
    order_id = store.record_research_order_atomic(
        "2026-07-19",
        decision,
        admission=_linked_admission(
            store,
            TARGET_POLICY,
            "release",
            decision,
        ),
        strategy_config=strategy_config_for_profile("research"),
    )
    assert order_id is not None
    order = store.paper_order(order_id)
    assert order is not None
    assert store.research_open_risk(account_id=TARGET_POLICY.account_id) == pytest.approx(
        float(order["contracts"]) * float(order["cost_per_contract"])
    )

    store.close_paper_order(order_id, 0.90)

    assert store.research_open_risk(account_id=TARGET_POLICY.account_id) == 0.0
    capacity = store.account_policy_capacity(
        target_date="2026-07-19",
        market_ticker="KXHIGHTSFO-RELEASE-NEW",
        risk_profile="research",
        account_id=TARGET_POLICY.account_id,
        requested_spend=20.0,
    )
    assert capacity["allowed_spend"] == pytest.approx(20.0)


def test_same_market_allowed_across_sleeves_but_duplicate_account_rejected(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "duplicates.db", research_clock=_fixed_research_clock
    )
    ticker = "KXHIGHTSFO-SAME-MARKET"
    decision = _atomic_decision(ticker)
    target_admission = _linked_admission(
        store, TARGET_POLICY, "target-first", decision
    )
    motion_admission = _linked_admission(
        store, MOTION_POLICY, "motion-first", decision
    )
    duplicate_admission = _linked_admission(
        store, TARGET_POLICY, "target-duplicate", decision
    )
    target = store.record_research_order_atomic(
        "2026-07-19",
        decision,
        admission=target_admission,
        strategy_config=strategy_config_for_profile("research"),
    )
    motion = store.record_research_order_atomic(
        "2026-07-19",
        decision,
        admission=motion_admission,
        strategy_config=strategy_config_for_profile("research"),
    )
    duplicate = store.record_research_order_atomic(
        "2026-07-19",
        decision,
        admission=duplicate_admission,
        strategy_config=strategy_config_for_profile("research"),
    )

    assert target is not None
    assert motion is not None
    assert duplicate is None
    assert store.entries_for_market_side(
        "2026-07-19",
        ticker,
        "NO",
        risk_profile="research",
        account_id=TARGET_POLICY.account_id,
    ) == 1
    assert store.entries_for_market_side(
        "2026-07-19",
        ticker,
        "NO",
        risk_profile="research",
        account_id=MOTION_POLICY.account_id,
    ) == 1


def test_atomic_failure_rolls_back_order_and_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "rollback.db", research_clock=_fixed_research_clock
    )

    def fail_ledger(**_kwargs: object) -> None:
        raise RuntimeError("injected ledger failure")

    monkeypatch.setattr(store, "_record_research_reservation_or_fill", fail_ledger)
    decision = _atomic_decision("KXHIGHTSFO-ROLLBACK")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        "rollback",
        decision,
    )
    with pytest.raises(RuntimeError, match="injected ledger failure"):
        store.record_research_order_atomic(
            "2026-07-19",
            decision,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )

    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM paper_orders WHERE scan_run_id=?",
            (admission.scan_run_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM paper_account_ledger "
            "WHERE idempotency_key LIKE 'order:%'"
        ).fetchone()[0] == 0


def test_legacy_live_recording_api_and_fingerprints_remain_unchanged(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(tmp_path / "legacy-live.db")
    config = strategy_config_for_profile("live")
    order_id = store.record_paper_order(
        "2026-07-19",
        _atomic_decision("KXHIGHTSFO-LIVE", resting=False),
        risk_profile="live",
        strategy_config=config,
    )

    assert order_id is not None
    row = store.paper_order(order_id)
    assert row is not None
    assert row["account_id"] == "paper-shared"
    assert row["research_sleeve"] is None
    assert row["research_policy_version"] is None
    assert row["policy_fingerprint"] is None
    assert row["strategy_fingerprint"] == "73b10240c1c00a8937b5314f"


def test_atomic_admission_rejects_objective_day_pause_bypass(tmp_path: Path) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "objective-bypass.db", research_clock=_fixed_research_clock
    )
    decision = _atomic_decision("KXHIGHTSFO-OBJECTIVE-BYPASS")
    with store.connect() as conn:
        _insert_research_order(
            conn,
            ticker="KXHIGHTSFO-TODAY-LOSS",
            account_id=MOTION_POLICY.account_id,
            sleeve=MOTION_POLICY.sleeve.value,
            policy_version=MOTION_POLICY.policy_version,
            policy_fingerprint=MOTION_POLICY.policy_fingerprint,
        )
        conn.execute(
            "UPDATE paper_orders SET status='PAPER_CLOSED', "
            "closed_at='2026-07-18T19:00:00+00:00', realized_pnl=-50 "
            "WHERE market_ticker='KXHIGHTSFO-TODAY-LOSS'"
        )
    admission = _linked_admission(
        store,
        MOTION_POLICY,
        "objective-bypass",
        decision,
        objective_day="2026-07-19",
    )

    with pytest.raises(ValueError, match="current Pacific civil day"):
        store.record_research_order_atomic(
            "2026-07-19",
            decision,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )


@pytest.mark.parametrize(
    ("now_utc", "valid_day", "invalid_day"),
    (
        (datetime(2026, 3, 8, 7, 59, tzinfo=UTC), "2026-03-07", "2026-03-08"),
        (datetime(2026, 3, 8, 8, 0, tzinfo=UTC), "2026-03-08", "2026-03-07"),
        (datetime(2026, 11, 1, 6, 59, tzinfo=UTC), "2026-10-31", "2026-11-01"),
        (datetime(2026, 11, 1, 7, 0, tzinfo=UTC), "2026-11-01", "2026-10-31"),
    ),
)
def test_atomic_admission_uses_current_pacific_day_at_dst_boundaries(
    tmp_path: Path,
    now_utc: datetime,
    valid_day: str,
    invalid_day: str,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / f"dst-{now_utc.timestamp()}.db",
        research_clock=lambda: now_utc,
    )
    invalid_decision = _atomic_decision(
        f"KXHIGHTSFO-DST-INVALID-{int(now_utc.timestamp())}"
    )
    invalid = _linked_admission(
        store,
        TARGET_POLICY,
        f"dst-invalid-{int(now_utc.timestamp())}",
        invalid_decision,
        objective_day=invalid_day,
    )
    with pytest.raises(ValueError, match="current Pacific civil day"):
        store.record_research_order_atomic(
            "2026-07-19",
            invalid_decision,
            admission=invalid,
            strategy_config=strategy_config_for_profile("research"),
        )

    valid_decision = _atomic_decision(
        f"KXHIGHTSFO-DST-VALID-{int(now_utc.timestamp())}"
    )
    valid = _linked_admission(
        store,
        TARGET_POLICY,
        f"dst-valid-{int(now_utc.timestamp())}",
        valid_decision,
        objective_day=valid_day,
    )
    assert store.record_research_order_atomic(
        "2026-07-19",
        valid_decision,
        admission=valid,
        strategy_config=strategy_config_for_profile("research"),
    ) is not None


@pytest.mark.parametrize("resting", (True, False))
def test_atomic_admission_rolls_back_on_ledger_key_collision(
    tmp_path: Path,
    resting: bool,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / f"ledger-collision-{resting}.db",
        research_clock=_fixed_research_clock,
    )
    decision = _atomic_decision("KXHIGHTSFO-LEDGER-COLLISION", resting=resting)
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        f"ledger-collision-{resting}",
        decision,
    )
    event = "reserve" if resting else "entry-fill"
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO paper_account_ledger (
                created_at, account_id, order_id, event_type, amount,
                idempotency_key, details_json
            ) VALUES (
                '2026-07-18T12:00:00+00:00', ?, NULL, 'COLLISION', 0, ?, '{}'
            )
            """,
            (TARGET_POLICY.account_id, f"order:1:{event}"),
        )

    with pytest.raises(sqlite3.IntegrityError):
        store.record_research_order_atomic(
            "2026-07-19",
            decision,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM paper_account_ledger "
            "WHERE order_id IS NOT NULL"
        ).fetchone()[0] == 0


def test_atomic_admission_writes_one_exact_fill_debit_and_reconciles(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "strict-fill-ledger.db", research_clock=_fixed_research_clock
    )
    decision = _atomic_decision(
        "KXHIGHTSFO-STRICT-FILL", contracts=20, resting=False
    )
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        "strict-fill",
        decision,
    )
    order_id = store.record_research_order_atomic(
        "2026-07-19",
        decision,
        admission=admission,
        strategy_config=strategy_config_for_profile("research"),
    )
    assert order_id is not None
    order = store.paper_order(order_id)
    assert order is not None
    ledger = [
        row
        for row in store.account_ledger(account_id=TARGET_POLICY.account_id)
        if row["order_id"] == order_id
    ]
    expected_cost = float(order["contracts"]) * float(order["cost_per_contract"])
    assert len(ledger) == 1
    assert ledger[0]["event_type"] == "ENTRY_FILL"
    assert ledger[0]["amount"] == pytest.approx(-expected_cost)
    state = store.research_account_state(account_id=TARGET_POLICY.account_id)
    assert state is not None
    assert state["available_cash"] == pytest.approx(1000.0 - expected_cost)
    assert state["open_cost_basis"] == pytest.approx(expected_cost)
    assert state["realized_equity"] == pytest.approx(1000.0)


def test_atomic_admission_links_supplied_target_evidence_not_newer_motion(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "exact-entry-link.db", research_clock=_fixed_research_clock
    )
    decision = _atomic_decision("KXHIGHTSFO-EXACT-LINK")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        "exact-link-target",
        decision,
    )
    _insert_research_decision_evidence(
        store,
        MOTION_POLICY,
        "exact-link-motion-newer",
        decision,
    )

    order_id = store.record_research_order_atomic(
        "2026-07-19",
        decision,
        admission=admission,
        strategy_config=strategy_config_for_profile("research"),
    )

    assert order_id is not None
    row = store.paper_order(order_id)
    assert row is not None
    assert row["entry_decision_snapshot_id"] == admission.entry_decision_id


@pytest.mark.parametrize(
    ("location", "field", "bad_value"),
    (
        ("decision", "research_sleeve", "motion"),
        ("decision", "research_policy_version", "wrong-version"),
        ("decision", "policy_fingerprint", "wrong-fingerprint"),
        ("decision", "objective_day", "2026-07-17"),
        ("decision", "lead_bucket", "same-day"),
        ("decision", "scan_run_id", "wrong-scan"),
        ("decision", "reentry_fingerprint", "wrong-reentry"),
        ("decision", "target_date", "2026-07-20"),
        ("decision", "market_ticker", "KXHIGHTSFO-WRONG"),
        ("decision", "side", "YES"),
        ("context", "research_sleeve", "motion"),
        ("context", "research_policy_version", "wrong-version"),
        ("context", "policy_fingerprint", "wrong-fingerprint"),
        ("context", "objective_day", "2026-07-17"),
        ("context", "lead_bucket", "same-day"),
        ("context", "scan_run_id", "wrong-scan"),
        ("context", "reentry_fingerprint", "wrong-reentry"),
        ("context", "target_date", "2026-07-20"),
    ),
)
def test_atomic_admission_rejects_each_entry_evidence_identity_mismatch(
    tmp_path: Path,
    location: str,
    field: str,
    bad_value: object,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / f"evidence-{location}-{field}.db",
        research_clock=_fixed_research_clock,
    )
    decision = _atomic_decision(f"KXHIGHTSFO-EVIDENCE-{location}-{field}")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        f"evidence-{location}-{field}",
        decision,
        decision_overrides={field: bad_value} if location == "decision" else None,
        context_overrides={field: bad_value} if location == "context" else None,
    )

    with pytest.raises(ValueError, match="entry decision evidence"):
        store.record_research_order_atomic(
            "2026-07-19",
            decision,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("table", "assignment"),
    (
        ("decision_snapshots", "action='BUY_YES'"),
        ("decision_snapshots", "probability=0.50"),
        ("decision_snapshots", "risk_profile='live'"),
        ("scan_context_snapshots", "risk_profile='live'"),
    ),
)
def test_atomic_admission_rejects_decision_or_context_content_mismatch(
    tmp_path: Path,
    table: str,
    assignment: str,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / f"content-{table}-{assignment.split('=')[0]}.db",
        research_clock=_fixed_research_clock,
    )
    decision = _atomic_decision(f"KXHIGHTSFO-CONTENT-{table}")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        f"content-{table}-{assignment.split('=')[0]}",
        decision,
    )
    with store.connect() as conn:
        if table == "decision_snapshots":
            conn.execute(
                f"UPDATE decision_snapshots SET {assignment} WHERE id=?",
                (admission.entry_decision_id,),
            )
        else:
            conn.execute(
                f"UPDATE scan_context_snapshots SET {assignment} WHERE id=("
                "SELECT scan_context_id FROM decision_snapshots WHERE id=?)",
                (admission.entry_decision_id,),
            )

    with pytest.raises(ValueError, match="entry decision evidence"):
        store.record_research_order_atomic(
            "2026-07-19",
            decision,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )


def test_atomic_admission_rejects_missing_entry_decision_evidence(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "missing-entry-evidence.db",
        research_clock=_fixed_research_clock,
    )
    decision = _atomic_decision("KXHIGHTSFO-MISSING-EVIDENCE")
    admission = replace(
        _admission(TARGET_POLICY, "missing-evidence"),
        entry_decision_id=999,
    )

    with pytest.raises(ValueError, match="entry decision evidence is missing"):
        store.record_research_order_atomic(
            "2026-07-19",
            decision,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )
