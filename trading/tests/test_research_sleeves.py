from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
import json
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
from sfo_kalshi_quant.execution import with_buy_limit
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.research_policy import (
    MOTION_POLICY,
    TARGET_POLICY,
    ResearchSleeve,
)
from sfo_kalshi_quant.research_portfolio import (
    ResearchOpportunity,
    allocate_research_plans,
)
from sfo_kalshi_quant.store.diagnostics import _strategy_config_snapshot


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


def test_target_attainment_locks_only_target_allocation_while_motion_continues() -> None:
    opportunity = ResearchOpportunity(
        _atomic_decision("KXHIGHTSFO-26JUL20-B80.5", contracts=2.0),
        target_date="2026-07-20",
        lead_days=2,
    )

    plans = allocate_research_plans([opportunity], realized_today=50.0)

    assert plans.target.legs == []
    assert plans.target.dispositions[0].status == "capacity_blocked"
    assert "target attained" in (plans.target.dispositions[0].reason or "")
    assert len(plans.motion.legs) == 1
    assert plans.motion.legs[0].decision.recommended_contracts == 1


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
    decision = TradeDecision(
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
        entry_bid=0.79 if resting else 0.81,
        entry_ask=0.82,
        entry_bid_size=0.0,
        entry_ask_size=100.0,
        strike_type="between",
        floor_strike=80.0,
        cap_strike=81.0,
        trade_quality_score=75.0,
    )
    limited = with_buy_limit(decision, strategy_config_for_profile("research"))
    assert limited.approved
    assert limited.limit_price is not None
    return limited


def _motion_atomic_decision(
    ticker: str,
    *,
    contracts: float = 1.0,
) -> TradeDecision:
    from sfo_kalshi_quant.paper import with_motion_taker_execution

    raw = replace(
        _atomic_decision(ticker, contracts=contracts),
        limit_price=None,
        limit_fee_per_contract=None,
        limit_cost_per_contract=None,
        limit_edge=None,
        limit_edge_lcb=None,
    )
    prepared = with_motion_taker_execution(
        raw,
        strategy_config_for_profile("research"),
    )
    assert prepared is not None
    return prepared


def _fixed_research_clock() -> datetime:
    return datetime(2026, 7, 18, 20, tzinfo=UTC)


def _admission(policy, suffix: str, *, lead_bucket: str = "day-ahead"):
    from sfo_kalshi_quant.db import ResearchAdmission

    return ResearchAdmission(
        account_id=policy.account_id,
        sleeve=policy.sleeve,
        policy_version=policy.policy_version,
        policy_fingerprint=policy.policy_fingerprint,
        objective_day="2026-07-18",
        scan_run_id=f"scan-{suffix}",
        reentry_fingerprint=f"reentry-{suffix}",
        lead_bucket=lead_bucket,
        entry_decision_id=1,
    )


def _insert_research_decision_evidence(
    store,
    policy,
    suffix: str,
    decision: TradeDecision,
    *,
    objective_day: str = "2026-07-18",
    target_date: str = "2026-07-19",
    lead_bucket: str = "day-ahead",
    strategy_config_json: str | None = None,
    decision_overrides: dict[str, object] | None = None,
    context_overrides: dict[str, object] | None = None,
) -> int:
    identity = {
        "research_sleeve": policy.sleeve.value,
        "research_policy_version": policy.policy_version,
        "policy_fingerprint": policy.policy_fingerprint,
        "objective_day": objective_day,
        "lead_bucket": lead_bucket,
        "scan_run_id": f"scan-{suffix}",
        "reentry_fingerprint": f"reentry-{suffix}",
    }
    context = {
        **identity,
        "target_date": target_date,
        "strategy_config_json": (
            strategy_config_json
            if strategy_config_json is not None
            else json.dumps(
                _strategy_config_snapshot(strategy_config_for_profile("research")),
                sort_keys=True,
            )
        ),
        **(context_overrides or {}),
    }
    snapshot = {
        **identity,
        "target_date": target_date,
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
                lead_bucket, scan_run_id, reentry_fingerprint,
                strategy_config_json
            ) VALUES (
                '2026-07-18T12:00:00+00:00', ?, 'research', '{}', 1,
                ?, ?, ?, ?, ?, ?, ?, ?
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
                context["strategy_config_json"],
            ),
        )
        decision_cursor = conn.execute(
            """
            INSERT INTO decision_snapshots (
                scan_context_id, created_at, target_date, market_ticker, label,
                action, side, approved, signal_approved, entry_block_reason,
                probability, probability_lcb, yes_bid,
                yes_ask, entry_bid, entry_ask, entry_bid_size, entry_ask_size,
                spread, fee_per_contract, cost_per_contract, edge, edge_lcb,
                kelly_fraction, recommended_contracts, recommended_spend,
                expected_profit, trade_quality_score, reasons_json, risk_profile,
                research_sleeve, research_policy_version, policy_fingerprint,
                objective_day, lead_bucket, scan_run_id, reentry_fingerprint
            ) VALUES (
                ?, '2026-07-18T12:00:01+00:00', ?, ?, ?, ?, ?, 0, 1,
                'research admission pending',
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
    target_date: str = "2026-07-19",
    lead_bucket: str = "day-ahead",
    strategy_config_json: str | None = None,
    decision_overrides: dict[str, object] | None = None,
    context_overrides: dict[str, object] | None = None,
):
    entry_decision_id = _insert_research_decision_evidence(
        store,
        policy,
        suffix,
        decision,
        objective_day=objective_day,
        target_date=target_date,
        lead_bucket=lead_bucket,
        strategy_config_json=strategy_config_json,
        decision_overrides=decision_overrides,
        context_overrides=context_overrides,
    )
    return replace(
        _admission(policy, suffix, lead_bucket=lead_bucket),
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
    motion_decision = _motion_atomic_decision(ticker)
    target_admission = _linked_admission(
        store, TARGET_POLICY, "target-first", decision
    )
    motion_admission = _linked_admission(
        store, MOTION_POLICY, "motion-first", motion_decision
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
        motion_decision,
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
    target_date = (
        datetime.fromisoformat(valid_day) + timedelta(days=1)
    ).date().isoformat()
    invalid_decision = _atomic_decision(
        f"KXHIGHTSFO-DST-INVALID-{int(now_utc.timestamp())}"
    )
    invalid = _linked_admission(
        store,
        TARGET_POLICY,
        f"dst-invalid-{int(now_utc.timestamp())}",
        invalid_decision,
        objective_day=invalid_day,
        target_date=target_date,
    )
    with pytest.raises(ValueError, match="current Pacific civil day"):
        store.record_research_order_atomic(
            target_date,
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
        target_date=target_date,
    )
    assert store.record_research_order_atomic(
        target_date,
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


@pytest.mark.parametrize("target_date", ("2026-07-17", "2026-07-18"))
def test_target_atomic_admission_rejects_past_and_same_day_targets(
    tmp_path: Path,
    target_date: str,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / f"target-min-lead-{target_date}.db",
        research_clock=_fixed_research_clock,
    )
    decision = _atomic_decision(f"KXHIGHTSFO-TARGET-MIN-LEAD-{target_date}")
    lead_bucket = "same-day" if target_date == "2026-07-18" else "day-ahead"
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        f"target-min-lead-{target_date}",
        decision,
        target_date=target_date,
        lead_bucket=lead_bucket,
    )

    with pytest.raises(ValueError, match="minimum lead"):
        store.record_research_order_atomic(
            target_date,
            decision,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0


def test_motion_atomic_admission_allows_canonical_same_day_target(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "motion-same-day.db",
        research_clock=_fixed_research_clock,
    )
    decision = _motion_atomic_decision("KXHIGHTSFO-MOTION-SAME-DAY", contracts=1)
    admission = _linked_admission(
        store,
        MOTION_POLICY,
        "motion-same-day",
        decision,
        target_date="2026-07-18",
        lead_bucket="same-day",
    )

    assert store.record_research_order_atomic(
        "2026-07-18",
        decision,
        admission=admission,
        strategy_config=strategy_config_for_profile("research"),
    ) is not None


@pytest.mark.parametrize("target_date", ("2026-07-19", "2026-07-22"))
def test_target_atomic_admission_uses_one_canonical_day_ahead_bucket(
    tmp_path: Path,
    target_date: str,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / f"target-day-ahead-{target_date}.db",
        research_clock=_fixed_research_clock,
    )
    decision = _atomic_decision(f"KXHIGHTSFO-TARGET-DAY-AHEAD-{target_date}")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        f"target-day-ahead-{target_date}",
        decision,
        target_date=target_date,
        lead_bucket="day-ahead",
    )

    assert store.record_research_order_atomic(
        target_date,
        decision,
        admission=admission,
        strategy_config=strategy_config_for_profile("research"),
    ) is not None


def test_atomic_admission_rejects_noncanonical_lead_bucket(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "noncanonical-lead.db",
        research_clock=_fixed_research_clock,
    )
    decision = _atomic_decision("KXHIGHTSFO-NONCANONICAL-LEAD")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        "noncanonical-lead",
        decision,
        target_date="2026-07-19",
        lead_bucket="same-day",
    )

    with pytest.raises(ValueError, match="canonical lead bucket"):
        store.record_research_order_atomic(
            "2026-07-19",
            decision,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )


@pytest.mark.parametrize(
    ("now_utc", "target_date"),
    (
        (datetime(2026, 3, 8, 7, 59, tzinfo=UTC), "2026-03-08"),
        (datetime(2026, 3, 8, 8, 0, tzinfo=UTC), "2026-03-09"),
        (datetime(2026, 11, 1, 6, 59, tzinfo=UTC), "2026-11-01"),
        (datetime(2026, 11, 1, 7, 0, tzinfo=UTC), "2026-11-02"),
    ),
)
def test_target_minimum_lead_uses_pacific_civil_day_across_dst(
    tmp_path: Path,
    now_utc: datetime,
    target_date: str,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / f"lead-dst-{now_utc.timestamp()}.db",
        research_clock=lambda: now_utc,
    )
    objective_day = (datetime.fromisoformat(target_date) - timedelta(days=1)).date()
    decision = _atomic_decision(f"KXHIGHTSFO-LEAD-DST-{int(now_utc.timestamp())}")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        f"lead-dst-{int(now_utc.timestamp())}",
        decision,
        objective_day=objective_day.isoformat(),
        target_date=target_date,
        lead_bucket="day-ahead",
    )

    assert store.record_research_order_atomic(
        target_date,
        decision,
        admission=admission,
        strategy_config=strategy_config_for_profile("research"),
    ) is not None


def test_atomic_admission_rejects_live_strategy_config_injection(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "live-config-injection.db",
        research_clock=_fixed_research_clock,
    )
    decision = _atomic_decision("KXHIGHTSFO-LIVE-CONFIG-INJECTION")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        "live-config-injection",
        decision,
    )

    with pytest.raises(ValueError, match="canonical research strategy"):
        store.record_research_order_atomic(
            "2026-07-19",
            decision,
            admission=admission,
            strategy_config=strategy_config_for_profile("live"),
        )


def test_atomic_admission_requires_type_exact_canonical_strategy_config(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "type-exact-config.db",
        research_clock=_fixed_research_clock,
    )
    decision = _atomic_decision("KXHIGHTSFO-TYPE-EXACT-CONFIG")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        "type-exact-config",
        decision,
    )
    type_changed = replace(
        strategy_config_for_profile("research"),
        paper_bankroll=1000,
    )

    with pytest.raises(ValueError, match="canonical research strategy"):
        store.record_research_order_atomic(
            "2026-07-19",
            decision,
            admission=admission,
            strategy_config=type_changed,
        )


@pytest.mark.parametrize(
    "bad_context_config",
    (
        "{}",
        "null",
        "not-json",
        json.dumps(
            {
                **(_strategy_config_snapshot(strategy_config_for_profile("research")) or {}),
                "min_edge": 0.99,
            },
            sort_keys=True,
        ),
        json.dumps(
            {
                **(_strategy_config_snapshot(strategy_config_for_profile("research")) or {}),
                "paper_bankroll": 1000,
            },
            sort_keys=True,
        ),
    ),
)
def test_atomic_admission_rejects_context_strategy_config_mismatch(
    tmp_path: Path,
    bad_context_config: str,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / f"context-config-{abs(hash(bad_context_config))}.db",
        research_clock=_fixed_research_clock,
    )
    decision = _atomic_decision("KXHIGHTSFO-CONTEXT-CONFIG-MISMATCH")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        f"context-config-{abs(hash(bad_context_config))}",
        decision,
        strategy_config_json=bad_context_config,
    )

    with pytest.raises(ValueError, match="scan context strategy configuration"):
        store.record_research_order_atomic(
            "2026-07-19",
            decision,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )


def test_atomic_admission_rechecks_research_spread_limit(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "spread-limit.db",
        research_clock=_fixed_research_clock,
    )
    decision = replace(
        _atomic_decision("KXHIGHTSFO-SPREAD-LIMIT"),
        spread=strategy_config_for_profile("research").max_spread + 0.01,
    )
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        "spread-limit",
        decision,
    )

    with pytest.raises(ValueError, match="research strategy entry limits"):
        store.record_research_order_atomic(
            "2026-07-19",
            decision,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )


def test_atomic_admission_rejects_limit_price_changed_after_evidence(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "changed-limit-price.db",
        research_clock=_fixed_research_clock,
    )
    evidenced = _atomic_decision("KXHIGHTSFO-CHANGED-LIMIT-PRICE")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        "changed-limit-price",
        evidenced,
    )
    mutated = replace(evidenced, limit_price=0.01)

    with pytest.raises(ValueError, match="canonical research limit quote"):
        store.record_research_order_atomic(
            "2026-07-19",
            mutated,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0


def test_atomic_admission_rejects_limit_fee_changed_after_evidence(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "changed-limit-fee.db",
        research_clock=_fixed_research_clock,
    )
    evidenced = _atomic_decision("KXHIGHTSFO-CHANGED-LIMIT-FEE")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        "changed-limit-fee",
        evidenced,
    )
    assert evidenced.limit_fee_per_contract is not None
    mutated = replace(
        evidenced,
        limit_fee_per_contract=evidenced.limit_fee_per_contract + 0.01,
    )

    with pytest.raises(ValueError, match="canonical research limit quote"):
        store.record_research_order_atomic(
            "2026-07-19",
            mutated,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )


def test_atomic_admission_rejects_limit_cost_changed_after_evidence(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "changed-limit-cost.db",
        research_clock=_fixed_research_clock,
    )
    evidenced = _atomic_decision("KXHIGHTSFO-CHANGED-LIMIT-COST")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        "changed-limit-cost",
        evidenced,
    )
    assert evidenced.limit_cost_per_contract is not None
    mutated = replace(
        evidenced,
        limit_cost_per_contract=evidenced.limit_cost_per_contract + 0.01,
    )

    with pytest.raises(ValueError, match="canonical research limit quote"):
        store.record_research_order_atomic(
            "2026-07-19",
            mutated,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )


def test_atomic_admission_rejects_limit_edge_changed_after_evidence(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "changed-limit-edge.db",
        research_clock=_fixed_research_clock,
    )
    evidenced = _atomic_decision("KXHIGHTSFO-CHANGED-LIMIT-EDGE")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        "changed-limit-edge",
        evidenced,
    )
    assert evidenced.limit_edge is not None
    mutated = replace(evidenced, limit_edge=evidenced.limit_edge + 0.01)

    with pytest.raises(ValueError, match="canonical research limit quote"):
        store.record_research_order_atomic(
            "2026-07-19",
            mutated,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )


def test_atomic_admission_rejects_limit_edge_lcb_changed_after_evidence(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / "changed-limit-edge-lcb.db",
        research_clock=_fixed_research_clock,
    )
    evidenced = _atomic_decision("KXHIGHTSFO-CHANGED-LIMIT-EDGE-LCB")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        "changed-limit-edge-lcb",
        evidenced,
    )
    assert evidenced.limit_edge_lcb is not None
    mutated = replace(evidenced, limit_edge_lcb=evidenced.limit_edge_lcb + 0.01)

    with pytest.raises(ValueError, match="canonical research limit quote"):
        store.record_research_order_atomic(
            "2026-07-19",
            mutated,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )


@pytest.mark.parametrize(
    ("resting", "expected_status"),
    ((True, "PAPER_LIMIT_RESTING"), (False, "PAPER_FILLED")),
)
def test_atomic_admission_accepts_canonical_resting_and_crossing_quotes(
    tmp_path: Path,
    resting: bool,
    expected_status: str,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / f"canonical-quote-{resting}.db",
        research_clock=_fixed_research_clock,
    )
    decision = _atomic_decision(
        f"KXHIGHTSFO-CANONICAL-QUOTE-{resting}",
        resting=resting,
    )
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        f"canonical-quote-{resting}",
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
    assert order["status"] == expected_status
    assert order["entry_mode"] == "limit"
    assert order["limit_price"] == pytest.approx(decision.limit_price)
    assert order["limit_fee_per_contract"] == pytest.approx(
        decision.limit_fee_per_contract
    )
    assert order["limit_cost_per_contract"] == pytest.approx(
        decision.limit_cost_per_contract
    )


@pytest.mark.parametrize("starts_resting", (True, False))
def test_atomic_admission_rejects_crossing_resting_transition_after_evidence(
    tmp_path: Path,
    starts_resting: bool,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / f"quote-transition-{starts_resting}.db",
        research_clock=_fixed_research_clock,
    )
    evidenced = _atomic_decision(
        f"KXHIGHTSFO-QUOTE-TRANSITION-{starts_resting}",
        resting=starts_resting,
    )
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        f"quote-transition-{starts_resting}",
        evidenced,
    )
    transitioned_price = evidenced.ask if starts_resting else evidenced.ask - 0.01
    transitioned = replace(evidenced, limit_price=transitioned_price)

    with pytest.raises(ValueError, match="canonical research limit quote"):
        store.record_research_order_atomic(
            "2026-07-19",
            transitioned,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )


@pytest.mark.parametrize(
    "field",
    (
        "limit_price",
        "limit_fee_per_contract",
        "limit_cost_per_contract",
        "limit_edge",
        "limit_edge_lcb",
    ),
)
def test_atomic_admission_rejects_nonfinite_canonical_quote_field(
    tmp_path: Path,
    field: str,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / f"nonfinite-quote-{field}.db",
        research_clock=_fixed_research_clock,
    )
    evidenced = _atomic_decision(f"KXHIGHTSFO-NONFINITE-QUOTE-{field}")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        f"nonfinite-quote-{field}",
        evidenced,
    )
    mutated = replace(evidenced, **{field: float("nan")})

    with pytest.raises(
        ValueError,
        match="(?:research strategy entry limits|canonical research limit quote)",
    ):
        store.record_research_order_atomic(
            "2026-07-19",
            mutated,
            admission=admission,
            strategy_config=strategy_config_for_profile("research"),
        )


def _replace_ledger_with_corruptible_amount_column(store) -> None:
    with store.connect() as conn:
        conn.executescript(
            """
            ALTER TABLE paper_account_ledger RENAME TO paper_account_ledger_strict;
            CREATE TABLE paper_account_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                account_id TEXT NOT NULL,
                order_id INTEGER,
                event_type TEXT NOT NULL,
                amount,
                idempotency_key TEXT NOT NULL UNIQUE,
                details_json TEXT
            );
            INSERT INTO paper_account_ledger
            SELECT * FROM paper_account_ledger_strict;
            DROP TABLE paper_account_ledger_strict;
            CREATE INDEX idx_paper_account_ledger_account
                ON paper_account_ledger (account_id, created_at, id);
            """
        )


@pytest.mark.parametrize(
    ("label", "bad_amount", "expected_type"),
    (
        ("text", "12.5", "text"),
        ("malformed", "not-money", "text"),
        ("blob", sqlite3.Binary(b"\x01\x02"), "blob"),
        ("null", None, "null"),
        ("nan", float("nan"), "null"),
        ("positive-infinity", float("inf"), "real"),
        ("negative-infinity", float("-inf"), "real"),
    ),
)
def test_research_admission_fails_closed_on_malformed_ledger_amount(
    tmp_path: Path,
    label: str,
    bad_amount: object,
    expected_type: str,
) -> None:
    from sfo_kalshi_quant.db import PaperStore

    store = PaperStore(
        tmp_path / f"malformed-ledger-{label}.db",
        research_clock=_fixed_research_clock,
    )
    _replace_ledger_with_corruptible_amount_column(store)
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO paper_account_ledger (
                created_at, account_id, order_id, event_type, amount,
                idempotency_key, details_json
            ) VALUES ('2026-07-18T12:00:00+00:00', ?, NULL, 'CORRUPT', ?, ?, '{}')
            """,
            (TARGET_POLICY.account_id, bad_amount, f"corrupt:{label}"),
        )
        assert conn.execute(
            "SELECT typeof(amount) FROM paper_account_ledger "
            "WHERE idempotency_key=?",
            (f"corrupt:{label}",),
        ).fetchone()[0] == expected_type

    capacity = store.account_policy_capacity(
        target_date="2026-07-19",
        market_ticker=f"KXHIGHTSFO-MALFORMED-LEDGER-{label}",
        risk_profile="research",
        account_id=TARGET_POLICY.account_id,
        requested_spend=1.0,
    )
    assert capacity["allowed_spend"] == 0.0
    assert capacity["reason"] == "research ledger amount is invalid"

    decision = _atomic_decision(f"KXHIGHTSFO-MALFORMED-LEDGER-{label}")
    admission = _linked_admission(
        store,
        TARGET_POLICY,
        f"malformed-ledger-{label}",
        decision,
    )
    assert store.record_research_order_atomic(
        "2026-07-19",
        decision,
        admission=admission,
        strategy_config=strategy_config_for_profile("research"),
    ) is None
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0


def test_research_evidence_api_binds_target_and_motion_execution_styles(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore, ResearchDecisionIdentity
    from sfo_kalshi_quant.paper import with_motion_taker_execution

    store = PaperStore(tmp_path / "research-styles.db", research_clock=_fixed_research_clock)
    config = strategy_config_for_profile("research")

    target = _atomic_decision("KXHIGHTSFO-TARGET-STYLE", resting=True)
    target_identity = ResearchDecisionIdentity.for_policy(
        TARGET_POLICY,
        objective_day="2026-07-18",
        lead_bucket="day-ahead",
        scan_run_id="scan-target-style",
        reentry_fingerprint="reentry-target-style",
    )
    target_decision_id = store.record_research_decision_evidence(
        "2026-07-19",
        target,
        identity=target_identity,
        strategy_config=config,
    )
    target_order_id = store.record_research_order_atomic(
        "2026-07-19",
        target,
        admission=target_identity.admission(target_decision_id),
        strategy_config=config,
    )

    raw_motion = replace(
        _atomic_decision("KXHIGHTSFO-MOTION-STYLE", resting=True),
        limit_price=None,
        limit_fee_per_contract=None,
        limit_cost_per_contract=None,
        limit_edge=None,
        limit_edge_lcb=None,
    )
    motion = with_motion_taker_execution(raw_motion, config)
    assert motion is not None
    motion_identity = ResearchDecisionIdentity.for_policy(
        MOTION_POLICY,
        objective_day="2026-07-18",
        lead_bucket="day-ahead",
        scan_run_id="scan-motion-style",
        reentry_fingerprint="reentry-motion-style",
    )
    motion_decision_id = store.record_research_decision_evidence(
        "2026-07-19",
        motion,
        identity=motion_identity,
        strategy_config=config,
    )
    motion_order_id = store.record_research_order_atomic(
        "2026-07-19",
        motion,
        admission=motion_identity.admission(motion_decision_id),
        strategy_config=config,
    )

    assert target_order_id is not None
    target_row = store.paper_order(target_order_id)
    assert target_row is not None
    assert target_row["entry_mode"] == "limit"
    assert target_row["fill_model"] == "maker_trade_through_required"
    assert motion_order_id is not None
    motion_row = store.paper_order(motion_order_id)
    assert motion_row is not None
    assert motion_row["entry_mode"] == "market"
    assert motion_row["status"] == "PAPER_FILLED"
    assert motion_row["fill_model"] == "immediate_visible_quote"
    assert motion_row["contracts"] == pytest.approx(1.0)
    assert motion_row["entry_price"] == pytest.approx(motion.ask)

    wrong_target_identity = ResearchDecisionIdentity.for_policy(
        TARGET_POLICY,
        objective_day="2026-07-18",
        lead_bucket="day-ahead",
        scan_run_id="scan-target-cross-style",
        reentry_fingerprint="reentry-target-cross-style",
    )
    wrong_target_id = store.record_research_decision_evidence(
        "2026-07-19",
        motion,
        identity=wrong_target_identity,
        strategy_config=config,
    )
    with pytest.raises(ValueError, match="target research requires canonical limit"):
        store.record_research_order_atomic(
            "2026-07-19",
            motion,
            admission=wrong_target_identity.admission(wrong_target_id),
            strategy_config=config,
        )

    wrong_motion_identity = ResearchDecisionIdentity.for_policy(
        MOTION_POLICY,
        objective_day="2026-07-18",
        lead_bucket="day-ahead",
        scan_run_id="scan-motion-cross-style",
        reentry_fingerprint="reentry-motion-cross-style",
    )
    wrong_motion_id = store.record_research_decision_evidence(
        "2026-07-19",
        target,
        identity=wrong_motion_identity,
        strategy_config=config,
    )
    with pytest.raises(ValueError, match="motion research requires immediate taker"):
        store.record_research_order_atomic(
            "2026-07-19",
            target,
            admission=wrong_motion_identity.admission(wrong_motion_id),
            strategy_config=config,
        )


def test_same_shared_research_context_blocks_same_day_target_and_fills_motion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sfo_kalshi_quant.db import PaperStore
    from sfo_kalshi_quant.paper import PaperTrader

    store = PaperStore(tmp_path / "same-context.db", research_clock=_fixed_research_clock)
    config = strategy_config_for_profile("research")
    decision = _atomic_decision("KXHIGHTSFO-SAME-CONTEXT", resting=True)
    opportunity = ResearchOpportunity(decision, "2026-07-18", 0)
    plans = allocate_research_plans([opportunity], run_id="shared-scan")

    monkeypatch.setattr(
        "sfo_kalshi_quant.paper._deterministic_sample",
        lambda *args, **kwargs: pytest.fail("legacy 25% sampler must not run"),
    )
    result = PaperTrader(
        store,
        config,
        risk_profile="research",
        entry_mode="limit",
    ).execute_research_plans(
        "2026-07-18",
        plans,
        source_decisions=[decision],
        objective_day="2026-07-18",
        lead_bucket="same-day",
        scan_run_id="shared-scan",
        observed_high_state="complete=0;high=unavailable",
    )

    assert result.target_order_ids == ()
    assert len(result.motion_order_ids) == 1
    assert len(result.target_decision_ids) == 1
    assert len(result.motion_decision_ids) == 1
    motion = store.paper_order(result.motion_order_ids[0])
    assert motion is not None
    assert motion["account_id"] == MOTION_POLICY.account_id
    assert motion["entry_mode"] == "market"
    assert motion["contracts"] == pytest.approx(1.0)
    with store.connect() as conn:
        evidence = conn.execute(
            "SELECT research_sleeve, approved, entry_block_reason "
            "FROM decision_snapshots ORDER BY id"
        ).fetchall()
    assert evidence == [
        ("target", 0, "target requires day-ahead lead"),
        ("motion", 1, None),
    ]


def test_motion_attempts_every_candidate_in_priority_order_without_minimum_notional(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore
    from sfo_kalshi_quant.paper import PaperTrader

    store = PaperStore(tmp_path / "motion-priority.db", research_clock=_fixed_research_clock)
    config = strategy_config_for_profile("research")
    low = replace(
        _atomic_decision("KXHIGHTSFO-MOTION-LOW", resting=True),
        edge=0.08,
        edge_lcb=0.04,
        expected_profit=0.08,
    )
    high = replace(
        _atomic_decision("KXHIGHTSFO-MOTION-HIGH", resting=True),
        probability=0.94,
        probability_lcb=0.91,
        edge=0.12,
        edge_lcb=0.09,
        expected_profit=0.12,
    )
    no_depth = replace(
        _atomic_decision("KXHIGHTSFO-MOTION-NO-DEPTH", resting=True),
        probability=0.92,
        probability_lcb=0.80,
        edge=0.10,
        edge_lcb=-0.02,
        expected_profit=0.10,
        entry_ask_size=0.5,
    )
    decisions = [low, no_depth, high]
    plans = allocate_research_plans(
        [ResearchOpportunity(row, "2026-07-19", 1) for row in decisions],
        run_id="motion-priority",
    )

    result = PaperTrader(
        store,
        config,
        risk_profile="research",
        entry_mode="limit",
    ).execute_research_plans(
        "2026-07-19",
        plans,
        source_decisions=decisions,
        objective_day="2026-07-18",
        lead_bucket="day-ahead",
        scan_run_id="motion-priority",
        observed_high_state="complete=0;high=unavailable",
    )

    assert len(result.motion_decision_ids) == 3
    assert len(result.motion_order_ids) == 2
    with store.connect() as conn:
        motion_orders = conn.execute(
            "SELECT market_ticker, contracts, cost_per_contract "
            "FROM paper_orders WHERE account_id=? ORDER BY id",
            (MOTION_POLICY.account_id,),
        ).fetchall()
        motion_evidence = conn.execute(
            "SELECT market_ticker, approved, entry_block_reason "
            "FROM decision_snapshots WHERE research_sleeve='motion' ORDER BY id"
        ).fetchall()
    assert [row[0] for row in motion_orders] == [
        "KXHIGHTSFO-MOTION-HIGH",
        "KXHIGHTSFO-MOTION-LOW",
    ]
    assert all(row[1] == pytest.approx(1.0) for row in motion_orders)
    assert all(row[2] < 1.0 for row in motion_orders)  # deliberately below the old $5 floor
    assert [row[0] for row in motion_evidence] == [
        "KXHIGHTSFO-MOTION-HIGH",
        "KXHIGHTSFO-MOTION-NO-DEPTH",
        "KXHIGHTSFO-MOTION-LOW",
    ]
    assert motion_evidence[1][1:] == (
        0,
        "motion visible-ask taker quote is not executable",
    )


@pytest.mark.parametrize(
    ("second_scan", "price_delta", "probability_delta", "observed_high", "expected"),
    (
        ("reentry-2", 0.0, 0.0, None, False),
        ("reentry-2", 0.009, 0.019, None, False),
        ("reentry-2", 0.01, 0.0, None, True),
        ("reentry-2", 0.0, 0.02, None, True),
        ("reentry-2", 0.0, 0.0, 81.0, True),
        ("reentry-1", 0.0, 0.0, None, False),
        ("reentry-1", 0.01, 0.0, None, False),
    ),
)
def test_terminal_motion_reentry_requires_new_scan_and_exact_change_threshold(
    tmp_path: Path,
    second_scan: str,
    price_delta: float,
    probability_delta: float,
    observed_high: float | None,
    expected: bool,
) -> None:
    from sfo_kalshi_quant.db import PaperStore
    from sfo_kalshi_quant.models import IntradaySnapshot
    from sfo_kalshi_quant.paper import PaperTrader

    store = PaperStore(tmp_path / "motion-reentry.db", research_clock=_fixed_research_clock)
    config = strategy_config_for_profile("research")
    trader = PaperTrader(store, config, risk_profile="research", entry_mode="limit")
    initial = replace(
        _atomic_decision("KXHIGHTSFO-MOTION-REENTRY", resting=True),
        probability_lcb=0.80,
        edge_lcb=-0.02,
    )
    initial_plans = allocate_research_plans(
        [ResearchOpportunity(initial, "2026-07-19", 1)],
        run_id="reentry-1",
    )
    first = trader.execute_research_plans(
        "2026-07-19",
        initial_plans,
        source_decisions=[initial],
        objective_day="2026-07-18",
        lead_bucket="day-ahead",
        scan_run_id="reentry-1",
        observed_high_state="complete=0;high=unavailable",
    )
    assert len(first.motion_order_ids) == 1
    store.close_paper_order(first.motion_order_ids[0], 0.80)

    changed = replace(
        initial,
        entry_ask=initial.ask + price_delta,
        probability=initial.probability + probability_delta,
        probability_lcb=initial.probability_lcb + probability_delta,
        edge=initial.edge + probability_delta - price_delta,
        edge_lcb=initial.edge_lcb + probability_delta - price_delta,
        expected_profit=initial.edge + probability_delta - price_delta,
    )
    intraday = (
        IntradaySnapshot(
            target_date=datetime(2026, 7, 19, tzinfo=UTC).date(),
            observed_high_f=observed_high,
            latest_temp_f=observed_high,
            latest_observed_at="2026-07-18T20:15:00+00:00",
            remaining_forecast_high_f=None,
            forecast_fetched_at=None,
        )
        if observed_high is not None
        else None
    )
    changed_plans = allocate_research_plans(
        [ResearchOpportunity(changed, "2026-07-19", 1)],
        run_id=second_scan,
    )
    second = trader.execute_research_plans(
        "2026-07-19",
        changed_plans,
        source_decisions=[changed],
        objective_day="2026-07-18",
        lead_bucket="day-ahead",
        scan_run_id=second_scan,
        observed_high_state=(
            f"complete=0;high={observed_high:.1f}"
            if observed_high is not None
            else "complete=0;high=unavailable"
        ),
        intraday=intraday,
    )

    assert bool(second.motion_order_ids) is expected
    with store.connect() as conn:
        attempts = conn.execute(
            "SELECT approved, entry_block_reason, reentry_fingerprint "
            "FROM decision_snapshots WHERE research_sleeve='motion' ORDER BY id"
        ).fetchall()
    assert len(attempts) == 2
    if expected:
        assert attempts[-1][0:2] == (1, None)
    else:
        assert attempts[-1][0] == 0
        assert attempts[-1][1].startswith("motion re-entry requires")
    if second_scan == "reentry-1" and price_delta == 0.01:
        assert attempts[0][2] != attempts[1][2]  # price changed, but scan id did not
    if second_scan == "reentry-1" and price_delta == probability_delta == 0.0:
        assert attempts[0][2] == attempts[1][2]


def test_policy_candidate_preparation_accepts_point_edge_below_legacy_minimum() -> None:
    from sfo_kalshi_quant.fees import quadratic_fee_average_per_contract
    from sfo_kalshi_quant.paper import prepare_research_sleeve_decisions

    config = strategy_config_for_profile("research")
    raw = replace(
        _atomic_decision("KXHIGHTSFO-EDGE-003", resting=False),
        limit_price=None,
        limit_fee_per_contract=None,
        limit_cost_per_contract=None,
        limit_edge=None,
        limit_edge_lcb=None,
        model_probability=None,
    )
    fee = quadratic_fee_average_per_contract(
        raw.ask,
        1.0,
        maker=False,
        fee_multiplier=config.fee_multiplier,
        taker_rate=config.taker_fee_rate,
        maker_rate=config.maker_fee_rate,
        series_ticker=raw.ticker,
    )
    point_probability = raw.ask + fee + 0.003
    structural = replace(
        raw,
        probability=point_probability,
        probability_lcb=point_probability,
        model_probability=point_probability,
        edge=0.003,
        edge_lcb=0.003,
        recommended_contracts=1.0,
        expected_profit=0.003,
        reasons=[],
        approved=True,
    )

    target, motion = prepare_research_sleeve_decisions([structural], config)

    assert target[0].approved is True
    assert target[0].edge == pytest.approx(0.003)
    assert motion[0].approved is True
    assert motion[0].edge == pytest.approx(0.003)


def test_target_uses_maker_when_taker_lcb_is_negative_but_maker_lcb_is_nonnegative() -> None:
    from sfo_kalshi_quant.paper import prepare_research_sleeve_decisions

    config = strategy_config_for_profile("research")
    structural = replace(
        _atomic_decision("KXHIGHTSFO-TARGET-MAKER"),
        approved=True,
        probability=0.92,
        probability_lcb=0.815,
        model_probability=0.92,
        entry_bid=0.80,
        entry_ask=0.90,
        spread=0.10,
        recommended_contracts=1.0,
        reasons=[],
        limit_price=None,
        limit_fee_per_contract=None,
        limit_cost_per_contract=None,
        limit_edge=None,
        limit_edge_lcb=None,
    )

    target, _motion = prepare_research_sleeve_decisions([structural], config)

    assert target[0].approved is True
    assert target[0].limit_price == pytest.approx(0.81)
    assert target[0].limit_price < target[0].ask
    assert target[0].limit_edge_lcb is not None
    assert target[0].limit_edge_lcb >= 0.0


def test_policy_candidate_preparation_does_not_revive_structural_rejection() -> None:
    from sfo_kalshi_quant.paper import prepare_research_sleeve_decisions

    invalid = replace(
        _atomic_decision("KXHIGHTSFO-STRUCTURAL-INVALID"),
        approved=False,
        recommended_contracts=0.0,
        expected_profit=0.0,
        reasons=["market status is closed, not active"],
    )

    target, motion = prepare_research_sleeve_decisions(
        [invalid], strategy_config_for_profile("research")
    )

    assert target[0].approved is False
    assert motion[0].approved is False
    assert "market status is closed" in target[0].entry_block_reason
    assert "market status is closed" in motion[0].entry_block_reason


def test_allocator_accepts_independent_target_and_motion_opportunity_views() -> None:
    target_decision = _atomic_decision("KXHIGHTSFO-TARGET-ONLY")
    motion_decision = _motion_atomic_decision("KXHIGHTSFO-MOTION-ONLY")

    plans = allocate_research_plans(
        [ResearchOpportunity(target_decision, "2026-07-19", 1)],
        motion_opportunities=[
            ResearchOpportunity(motion_decision, "2026-07-19", 1)
        ],
        run_id="independent-sleeve-views",
    )

    assert [leg.decision.ticker for leg in plans.target.legs] == [
        target_decision.ticker
    ]
    assert [leg.decision.ticker for leg in plans.motion.legs] == [
        motion_decision.ticker
    ]
    assert {row.ticker for row in plans.target.dispositions} == {
        target_decision.ticker
    }
    assert {row.ticker for row in plans.motion.dispositions} == {
        motion_decision.ticker
    }


def test_target_atomic_admission_uses_zero_lcb_floor_quote(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore
    from sfo_kalshi_quant.paper import (
        PaperTrader,
        prepare_research_sleeve_decisions,
    )

    config = strategy_config_for_profile("research")
    structural = replace(
        _atomic_decision("KXHIGHTSFO-TARGET-ZERO-LCB"),
        approved=True,
        probability=0.92,
        probability_lcb=0.815,
        model_probability=0.92,
        entry_bid=0.80,
        entry_ask=0.90,
        spread=0.10,
        recommended_contracts=1.0,
        reasons=[],
        limit_price=None,
        limit_fee_per_contract=None,
        limit_cost_per_contract=None,
        limit_edge=None,
        limit_edge_lcb=None,
    )
    target_decisions, _motion_decisions = prepare_research_sleeve_decisions(
        [structural],
        config,
    )
    plans = allocate_research_plans(
        [ResearchOpportunity(target_decisions[0], "2026-07-19", 1)],
        motion_opportunities=[],
        run_id="target-zero-lcb",
    )

    result = PaperTrader(
        PaperStore(tmp_path / "target-zero-lcb.db", research_clock=_fixed_research_clock),
        config,
        risk_profile="research",
        entry_mode="limit",
    ).execute_research_plans(
        "2026-07-19",
        plans,
        source_decisions=target_decisions,
        motion_source_decisions=[],
        objective_day="2026-07-18",
        lead_bucket="day-ahead",
        scan_run_id="target-zero-lcb",
        observed_high_state="complete=0;high=unavailable",
    )

    assert len(result.target_order_ids) == 1
    assert result.motion_order_ids == ()


def _crossing_target_candidate(
    ticker: str,
    *,
    contracts: float = 25.0,
    ask_size: object = 5.0,
) -> TradeDecision:
    return replace(
        _atomic_decision(ticker),
        approved=True,
        probability=0.95,
        probability_lcb=0.95,
        model_probability=0.95,
        entry_bid=0.89,
        entry_ask=0.90,
        entry_ask_size=ask_size,
        spread=0.01,
        recommended_contracts=contracts,
        expected_profit=0.05 * contracts,
        reasons=[],
        limit_price=None,
        limit_fee_per_contract=None,
        limit_cost_per_contract=None,
        limit_edge=None,
        limit_edge_lcb=None,
    )


def test_target_crossing_quote_downsizes_to_visible_whole_contract_depth() -> None:
    from sfo_kalshi_quant.fees import quadratic_fee_average_per_contract
    from sfo_kalshi_quant.paper import with_target_research_execution

    config = strategy_config_for_profile("research")
    candidate = _crossing_target_candidate("KXHIGHTSFO-TARGET-DEPTH")

    prepared = with_target_research_execution(candidate, config)

    assert prepared is not None
    assert prepared.recommended_contracts == 5.0
    fee = quadratic_fee_average_per_contract(
        0.90,
        5.0,
        maker=False,
        fee_multiplier=config.fee_multiplier,
        taker_rate=config.taker_fee_rate,
        maker_rate=config.maker_fee_rate,
        series_ticker=candidate.ticker,
    )
    assert prepared.fee_per_contract == pytest.approx(fee)
    assert prepared.cost_per_contract == pytest.approx(0.90 + fee)
    assert prepared.edge == pytest.approx(0.95 - 0.90 - fee)
    assert prepared.edge_lcb == pytest.approx(0.95 - 0.90 - fee)
    assert prepared.expected_profit == pytest.approx(prepared.edge * 5.0)
    assert prepared.limit_price == pytest.approx(0.90)


def test_target_crossing_quote_floors_fractional_depth() -> None:
    from sfo_kalshi_quant.paper import with_target_research_execution

    prepared = with_target_research_execution(
        _crossing_target_candidate(
            "KXHIGHTSFO-TARGET-FRACTIONAL-DEPTH",
            ask_size=5.9,
        ),
        strategy_config_for_profile("research"),
    )

    assert prepared is not None
    assert prepared.recommended_contracts == 5.0


@pytest.mark.parametrize("ask_size", [0.0, 0.99, -1.0, float("nan"), float("inf"), "bad"])
def test_target_crossing_quote_rejects_nonexecutable_depth(ask_size: object) -> None:
    from sfo_kalshi_quant.paper import with_target_research_execution

    assert with_target_research_execution(
        _crossing_target_candidate(
            "KXHIGHTSFO-TARGET-BAD-DEPTH",
            ask_size=ask_size,
        ),
        strategy_config_for_profile("research"),
    ) is None


def test_target_maker_quote_does_not_downsize_to_visible_ask_depth() -> None:
    from sfo_kalshi_quant.paper import with_target_research_execution

    candidate = replace(
        _crossing_target_candidate(
            "KXHIGHTSFO-TARGET-MAKER-DEPTH",
            ask_size=5.0,
        ),
        entry_bid=0.88,
        spread=0.02,
    )

    prepared = with_target_research_execution(
        candidate,
        strategy_config_for_profile("research"),
    )

    assert prepared is not None
    assert prepared.recommended_contracts == 25.0
    assert prepared.limit_price == pytest.approx(0.89)


def test_target_depth_downsize_is_exact_in_evidence_and_order_ledger(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore
    from sfo_kalshi_quant.paper import (
        PaperTrader,
        prepare_research_sleeve_decisions,
    )

    config = strategy_config_for_profile("research")
    target_decisions, _motion_decisions = prepare_research_sleeve_decisions(
        [_crossing_target_candidate("KXHIGHTSFO-TARGET-LEDGER")],
        config,
    )
    assert target_decisions[0].recommended_contracts == 5.0
    plans = allocate_research_plans(
        [ResearchOpportunity(target_decisions[0], "2026-07-19", 1)],
        motion_opportunities=[],
        run_id="target-depth-ledger",
    )
    store = PaperStore(
        tmp_path / "target-depth-ledger.db",
        research_clock=_fixed_research_clock,
    )

    result = PaperTrader(
        store,
        config,
        risk_profile="research",
        entry_mode="limit",
    ).execute_research_plans(
        "2026-07-19",
        plans,
        source_decisions=target_decisions,
        motion_source_decisions=[],
        objective_day="2026-07-18",
        lead_bucket="day-ahead",
        scan_run_id="target-depth-ledger",
        observed_high_state="complete=0;high=unavailable",
    )

    assert len(result.target_order_ids) == 1
    with store.connect() as conn:
        evidence = conn.execute(
            "SELECT recommended_contracts, recommended_spend FROM decision_snapshots "
            "WHERE id=?",
            (result.target_decision_ids[0],),
        ).fetchone()
        order = conn.execute(
            "SELECT contracts, requested_contracts, entry_price, status "
            "FROM paper_orders WHERE id=?",
            (result.target_order_ids[0],),
        ).fetchone()
    assert evidence[0] == pytest.approx(5.0)
    assert evidence[1] == pytest.approx(target_decisions[0].cost_per_contract * 5.0)
    assert order[0] == pytest.approx(5.0)
    assert order[1] == pytest.approx(5.0)
    assert order[2] == pytest.approx(0.90)
    assert order[3] == "PAPER_FILLED"


def test_research_scan_batches_one_shared_context_for_all_dispositions(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore
    from sfo_kalshi_quant.paper import PaperTrader

    store = PaperStore(tmp_path / "one-research-context.db", research_clock=_fixed_research_clock)
    config = strategy_config_for_profile("research")
    decisions = [
        _atomic_decision("KXHIGHTSFO-BATCH-A"),
        _atomic_decision("KXHIGHTSFO-BATCH-B"),
    ]
    plans = allocate_research_plans(
        [ResearchOpportunity(row, "2026-07-19", 1) for row in decisions],
        run_id="one-research-context",
    )

    result = PaperTrader(
        store,
        config,
        risk_profile="research",
        entry_mode="limit",
    ).execute_research_plans(
        "2026-07-19",
        plans,
        source_decisions=decisions,
        objective_day="2026-07-18",
        lead_bucket="day-ahead",
        scan_run_id="one-research-context",
        observed_high_state="complete=0;high=unavailable",
    )

    with store.connect() as conn:
        context_count = conn.execute(
            "SELECT COUNT(*) FROM scan_context_snapshots"
        ).fetchone()[0]
        decision_rows = conn.execute(
            "SELECT id, scan_context_id, research_sleeve, scan_run_id "
            "FROM decision_snapshots ORDER BY id"
        ).fetchall()
        context_identity = conn.execute(
            "SELECT research_sleeve, research_policy_version, policy_fingerprint, "
            "objective_day, lead_bucket, scan_run_id, reentry_fingerprint "
            "FROM scan_context_snapshots"
        ).fetchone()
        order_links = conn.execute(
            "SELECT research_sleeve, entry_decision_snapshot_id "
            "FROM paper_orders ORDER BY id"
        ).fetchall()

    expected_ids = (*result.target_decision_ids, *result.motion_decision_ids)
    assert context_count == 1
    assert len(decision_rows) == 4
    assert len({row[1] for row in decision_rows}) == 1
    assert tuple(row[0] for row in decision_rows) == expected_ids
    assert context_identity == (None, None, None, None, None, None, None)
    assert all(row[2] in {"target", "motion"} for row in decision_rows)
    assert all(row[3] == "one-research-context" for row in decision_rows)
    assert order_links == [
        ("target", result.target_decision_ids[0]),
        ("target", result.target_decision_ids[1]),
        ("motion", result.motion_decision_ids[0]),
        ("motion", result.motion_decision_ids[1]),
    ]


@pytest.mark.parametrize(
    ("admit_target", "admit_motion", "expected_sleeve"),
    ((True, False, "target"), (False, True, "motion")),
)
def test_research_scan_admits_sleeves_independently_in_one_batch(
    tmp_path: Path,
    admit_target: bool,
    admit_motion: bool,
    expected_sleeve: str,
) -> None:
    from sfo_kalshi_quant.db import PaperStore
    from sfo_kalshi_quant.paper import PaperTrader

    store = PaperStore(
        tmp_path / f"independent-{expected_sleeve}.db",
        research_clock=_fixed_research_clock,
    )
    decision = _atomic_decision(f"KXHIGHTSFO-INDEPENDENT-{expected_sleeve}")
    plans = allocate_research_plans(
        [ResearchOpportunity(decision, "2026-07-19", 1)],
        run_id=f"independent-{expected_sleeve}",
    )

    result = PaperTrader(
        store,
        strategy_config_for_profile("research"),
        risk_profile="research",
        entry_mode="limit",
    ).execute_research_plans(
        "2026-07-19",
        plans,
        source_decisions=[decision],
        objective_day="2026-07-18",
        lead_bucket="day-ahead",
        scan_run_id=f"independent-{expected_sleeve}",
        observed_high_state="complete=0;high=unavailable",
        admit_target_orders=admit_target,
        admit_motion_orders=admit_motion,
    )

    assert bool(result.target_order_ids) is admit_target
    assert bool(result.motion_order_ids) is admit_motion
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM scan_context_snapshots"
        ).fetchone()[0] == 1
        orders = conn.execute(
            "SELECT research_sleeve FROM paper_orders ORDER BY id"
        ).fetchall()
        disabled = conn.execute(
            "SELECT research_sleeve, approved, entry_block_reason "
            "FROM decision_snapshots WHERE entry_block_reason="
            "'research order admission disabled'"
        ).fetchall()
    assert orders == [(expected_sleeve,)]
    assert disabled == [
        (
            "motion" if expected_sleeve == "target" else "target",
            0,
            "research order admission disabled",
        )
    ]


def test_research_admission_exception_leaves_pending_then_next_scan_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sfo_kalshi_quant.db import PaperStore
    from sfo_kalshi_quant.paper import PaperTrader

    store = PaperStore(tmp_path / "pending-recovery.db", research_clock=_fixed_research_clock)
    config = strategy_config_for_profile("research")
    decision = _atomic_decision("KXHIGHTSFO-PENDING-RECOVERY")
    trader = PaperTrader(
        store,
        config,
        risk_profile="research",
        entry_mode="limit",
    )
    first_plans = allocate_research_plans(
        [ResearchOpportunity(decision, "2026-07-19", 1)],
        run_id="pending-crash",
    )
    original_atomic = store.record_research_order_atomic
    monkeypatch.setattr(
        store,
        "record_research_order_atomic",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated crash")),
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        trader.execute_research_plans(
            "2026-07-19",
            first_plans,
            source_decisions=[decision],
            objective_day="2026-07-18",
            lead_bucket="day-ahead",
            scan_run_id="pending-crash",
            observed_high_state="complete=0;high=unavailable",
        )

    with store.connect() as conn:
        pending = conn.execute(
            "SELECT approved, signal_approved, entry_block_reason "
            "FROM decision_snapshots WHERE scan_run_id='pending-crash' ORDER BY id"
        ).fetchall()
        assert conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0
        non_opening_ledger = conn.execute(
            "SELECT COUNT(*) FROM paper_account_ledger WHERE order_id IS NOT NULL"
        ).fetchone()[0]
    assert pending
    assert all(row == (0, 1, "research admission pending") for row in pending)
    assert non_opening_ledger == 0

    monkeypatch.setattr(store, "record_research_order_atomic", original_atomic)
    second_plans = allocate_research_plans(
        [ResearchOpportunity(decision, "2026-07-19", 1)],
        run_id="pending-recovery-next",
    )
    recovered = trader.execute_research_plans(
        "2026-07-19",
        second_plans,
        source_decisions=[decision],
        objective_day="2026-07-18",
        lead_bucket="day-ahead",
        scan_run_id="pending-recovery-next",
        observed_high_state="complete=0;high=unavailable",
    )

    assert recovered.target_order_ids or recovered.motion_order_ids
    with store.connect() as conn:
        abandoned = conn.execute(
            "SELECT approved, signal_approved, entry_block_reason, "
            "recommended_contracts, recommended_spend, expected_profit "
            "FROM decision_snapshots WHERE scan_run_id='pending-crash' ORDER BY id"
        ).fetchall()
        admitted = conn.execute(
            "SELECT approved, signal_approved, entry_block_reason "
            "FROM decision_snapshots WHERE scan_run_id='pending-recovery-next' "
            "AND id IN (SELECT entry_decision_snapshot_id FROM paper_orders) "
            "ORDER BY id"
        ).fetchall()
    assert all(
        row == (0, 1, "abandoned research admission", 0.0, 0.0, 0.0)
        for row in abandoned
    )
    assert admitted
    assert all(row == (1, 1, None) for row in admitted)


def test_research_approval_order_and_ledger_rollback_together(
    tmp_path: Path,
) -> None:
    from sfo_kalshi_quant.db import PaperStore, ResearchDecisionIdentity

    store = PaperStore(tmp_path / "approval-atomic.db", research_clock=_fixed_research_clock)
    config = strategy_config_for_profile("research")
    decision = _atomic_decision("KXHIGHTSFO-APPROVAL-ATOMIC")
    identity = ResearchDecisionIdentity.for_policy(
        TARGET_POLICY,
        objective_day="2026-07-18",
        lead_bucket="day-ahead",
        scan_run_id="approval-atomic",
        reentry_fingerprint="approval-atomic",
    )
    decision_id = store.record_research_decision_evidence(
        "2026-07-19",
        decision,
        identity=identity,
        strategy_config=config,
    )
    with store.connect() as conn:
        pending = conn.execute(
            "SELECT approved, signal_approved, entry_block_reason "
            "FROM decision_snapshots WHERE id=?",
            (decision_id,),
        ).fetchone()
        conn.execute(
            "CREATE TRIGGER abort_research_approval BEFORE UPDATE OF approved "
            "ON decision_snapshots WHEN NEW.id=%d AND NEW.approved=1 "
            "BEGIN SELECT RAISE(ABORT, 'simulated approval failure'); END" % decision_id
        )
    assert pending == (0, 1, "research admission pending")

    with pytest.raises(sqlite3.DatabaseError, match="simulated approval failure"):
        store.record_research_order_atomic(
            "2026-07-19",
            decision,
            admission=identity.admission(decision_id),
            strategy_config=config,
        )

    with store.connect() as conn:
        still_pending = conn.execute(
            "SELECT approved, signal_approved, entry_block_reason "
            "FROM decision_snapshots WHERE id=?",
            (decision_id,),
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0] == 0
        non_opening_ledger = conn.execute(
            "SELECT COUNT(*) FROM paper_account_ledger WHERE order_id IS NOT NULL"
        ).fetchone()[0]
    assert still_pending == (0, 1, "research admission pending")
    assert non_opening_ledger == 0


def test_motion_reentry_projects_root_when_newest_partial_child_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sfo_kalshi_quant.db import PaperStore
    from sfo_kalshi_quant.paper import PaperTrader

    store = PaperStore(tmp_path / "motion-root-reentry.db", research_clock=_fixed_research_clock)
    config = strategy_config_for_profile("research")
    trader = PaperTrader(store, config, risk_profile="research", entry_mode="limit")
    initial = replace(
        _motion_atomic_decision("KXHIGHTSFO-MOTION-ROOT"),
        probability_lcb=0.80,
        edge_lcb=-0.02,
    )
    first_plans = allocate_research_plans(
        [ResearchOpportunity(initial, "2026-07-19", 1)], run_id="root-first"
    )
    first = trader.execute_research_plans(
        "2026-07-19",
        first_plans,
        source_decisions=[initial],
        objective_day="2026-07-18",
        lead_bucket="day-ahead",
        scan_run_id="root-first",
        observed_high_state="complete=0;high=unavailable",
    )
    root_id = first.motion_order_ids[0]
    store.close_paper_order(root_id, 0.80, max_quantity=0.5)
    assert store.paper_order(root_id)["status"] == "PAPER_FILLED"

    changed = replace(
        initial,
        entry_ask=initial.ask + 0.01,
        probability=initial.probability + 0.02,
        probability_lcb=initial.probability_lcb + 0.02,
        edge=initial.edge + 0.01,
        edge_lcb=initial.edge_lcb + 0.01,
        expected_profit=initial.expected_profit + 0.01,
    )
    second_plans = allocate_research_plans(
        [ResearchOpportunity(changed, "2026-07-19", 1)], run_id="root-second"
    )
    monkeypatch.setattr(
        store,
        "record_research_order_atomic",
        lambda *args, **kwargs: pytest.fail("active logical root must block admission"),
    )
    second = trader.execute_research_plans(
        "2026-07-19",
        second_plans,
        source_decisions=[changed],
        objective_day="2026-07-18",
        lead_bucket="day-ahead",
        scan_run_id="root-second",
        observed_high_state="complete=0;high=unavailable",
    )

    assert second.motion_order_ids == ()
    with store.connect() as conn:
        latest = conn.execute(
            "SELECT approved, entry_block_reason FROM decision_snapshots "
            "WHERE research_sleeve='motion' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert latest == (0, "duplicate active motion research entry")


def test_atomic_none_downgrades_persisted_research_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sfo_kalshi_quant.db import PaperStore
    from sfo_kalshi_quant.paper import PaperTrader

    store = PaperStore(tmp_path / "atomic-none.db", research_clock=_fixed_research_clock)
    config = strategy_config_for_profile("research")
    decision = replace(
        _motion_atomic_decision("KXHIGHTSFO-ATOMIC-NONE"),
        probability_lcb=0.80,
        edge_lcb=-0.02,
    )
    plans = allocate_research_plans(
        [ResearchOpportunity(decision, "2026-07-19", 1)], run_id="atomic-none"
    )
    monkeypatch.setattr(store, "record_research_order_atomic", lambda *a, **k: None)

    result = PaperTrader(
        store, config, risk_profile="research", entry_mode="limit"
    ).execute_research_plans(
        "2026-07-19",
        plans,
        source_decisions=[decision],
        objective_day="2026-07-18",
        lead_bucket="day-ahead",
        scan_run_id="atomic-none",
        observed_high_state="complete=0;high=unavailable",
    )

    assert result.motion_order_ids == ()
    with store.connect() as conn:
        latest = conn.execute(
            "SELECT approved, entry_block_reason FROM decision_snapshots "
            "WHERE research_sleeve='motion' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert latest == (0, "atomic research admission rejected")
