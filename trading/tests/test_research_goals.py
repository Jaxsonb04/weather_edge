from __future__ import annotations

import math
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.profile_identity import published_profile_key
from sfo_kalshi_quant.research_goals import daily_goal_state, summarize_daily_goals
from sfo_kalshi_quant.research_policy import MOTION_POLICY, TARGET_POLICY


def _approved_decision() -> TradeDecision:
    return TradeDecision(
        ticker="KXHIGHTPHX-TEST-B110.5",
        label="110 to 111",
        action="BUY_YES",
        approved=True,
        probability=0.70,
        probability_lcb=0.60,
        yes_bid=0.20,
        yes_ask=0.30,
        spread=0.04,
        fee_per_contract=0.01,
        cost_per_contract=0.31,
        edge=0.39,
        edge_lcb=0.29,
        kelly_fraction=0.02,
        recommended_contracts=10.0,
        expected_profit=3.9,
        reasons=[],
        trade_quality_score=72.0,
        strike_type="between",
        floor_strike=110.0,
        cap_strike=111.0,
        model_probability=0.70,
        market_probability=0.42,
        residual_probability=0.68,
        ensemble_probability=0.72,
    )


def _pacific_noon(day: date) -> str:
    return datetime.combine(
        day,
        time(hour=12),
        tzinfo=ZoneInfo("America/Los_Angeles"),
    ).isoformat()


def test_daily_goal_is_frozen_at_50_from_original_equity_on_pacific_day(
    tmp_path,
) -> None:
    current = [datetime(2026, 7, 18, 6, 59, tzinfo=UTC)]
    store = PaperStore(
        tmp_path / "paper.db",
        research_clock=lambda: current[0],
    )

    state = store.research_daily_goal_state()

    assert state.objective_day == date(2026, 7, 17)
    assert state.target_pnl == 50.0
    assert state.realized_pnl == 0.0
    assert state.remaining_pnl == 50.0
    assert state.achieved is False
    assert state.locked is False

    # Account performance cannot compound a frozen original-equity objective,
    # and the journal rejects attempts to rewrite the historical target.
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_accounts SET high_water_equity=1250 "
            "WHERE account_id=?",
            (TARGET_POLICY.account_id,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE research_daily_goals SET reference_equity=1250, "
                "target_return=0.05, target_pnl=62.5 "
                "WHERE objective_day=? AND account_id=? AND policy_version=?",
                (
                    state.objective_day.isoformat(),
                    TARGET_POLICY.account_id,
                    TARGET_POLICY.policy_version,
                ),
            )

    again = store.research_daily_goal_state(objective_day=state.objective_day)
    assert again.target_pnl == 50.0
    assert again.remaining_pnl == 50.0


def test_malformed_persisted_goal_fails_closed(tmp_path) -> None:
    store = PaperStore(
        tmp_path / "paper.db",
        research_clock=lambda: datetime(2026, 7, 18, 19, 0, tzinfo=UTC),
    )
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO research_daily_goals "
            "(objective_day, account_id, policy_version, policy_fingerprint, created_at, "
            "reference_equity, target_return, target_pnl) "
            "VALUES ('2026-07-18', ?, ?, ?, ?, 1000, 0.05, 60)",
            (
                TARGET_POLICY.account_id,
                TARGET_POLICY.policy_version,
                TARGET_POLICY.policy_fingerprint,
                datetime.now(UTC).isoformat(),
            ),
        )

    with pytest.raises(ValueError, match="daily goal is malformed"):
        store.research_daily_goal_state(objective_day=date(2026, 7, 18))


def test_self_consistent_non_policy_goal_fails_closed(tmp_path) -> None:
    store = PaperStore(
        tmp_path / "paper.db",
        research_clock=lambda: datetime(2026, 7, 18, 19, 0, tzinfo=UTC),
    )
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO research_daily_goals "
            "(objective_day, account_id, policy_version, policy_fingerprint, created_at, "
            "reference_equity, target_return, target_pnl) "
            "VALUES ('2026-07-18', ?, ?, ?, ?, 2000, 0.05, 100)",
            (
                TARGET_POLICY.account_id,
                TARGET_POLICY.policy_version,
                TARGET_POLICY.policy_fingerprint,
                datetime.now(UTC).isoformat(),
            ),
        )

    with pytest.raises(ValueError, match="active immutable policy"):
        store.research_daily_goal_state(objective_day=date(2026, 7, 18))


def test_research_goal_uses_pacific_midnight_not_utc_midnight(tmp_path) -> None:
    current = [datetime(2026, 7, 18, 6, 59, tzinfo=UTC)]
    store = PaperStore(
        tmp_path / "paper.db",
        research_clock=lambda: current[0],
    )
    assert store.research_daily_goal_state().objective_day == date(2026, 7, 17)

    current[0] = datetime(2026, 7, 18, 7, 0, tzinfo=UTC)
    assert store.research_daily_goal_state().objective_day == date(2026, 7, 18)

    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM research_daily_goals WHERE account_id=?",
            (TARGET_POLICY.account_id,),
        ).fetchone()[0] == 2


def test_first_target_scan_read_freezes_the_daily_goal(tmp_path) -> None:
    store = PaperStore(
        tmp_path / "paper.db",
        research_clock=lambda: datetime(2026, 7, 18, 19, 0, tzinfo=UTC),
    )

    assert store.research_realized_pnl_for_day(
        account_id=TARGET_POLICY.account_id,
        objective_day=date(2026, 7, 18),
    ) == 0.0

    with store.connect() as conn:
        frozen = conn.execute(
            "SELECT reference_equity, target_return, target_pnl "
            "FROM research_daily_goals WHERE objective_day='2026-07-18' "
            "AND account_id=? AND policy_version=?",
            (TARGET_POLICY.account_id, TARGET_POLICY.policy_version),
        ).fetchone()
    assert frozen == (1000.0, 0.05, 50.0)


def test_crossed_research_identity_is_unknown_and_cannot_contaminate_kpis(
    tmp_path,
) -> None:
    objective_day = date(2026, 7, 18)
    store = PaperStore(
        tmp_path / "paper.db",
        research_clock=lambda: datetime(2026, 7, 18, 19, 0, tzinfo=UTC),
    )
    target_account_row = store.record_paper_order(
        "2026-07-19", _approved_decision(), risk_profile="research"
    )
    motion_account_row = store.record_paper_order(
        "2026-07-20",
        replace(_approved_decision(), ticker="KXHIGHTPHX-CROSSED-B111.5"),
        risk_profile="research",
    )
    assert target_account_row is not None
    assert motion_account_row is not None

    with store.connect() as conn:
        for order_id, account, sleeve_policy in (
            (target_account_row, TARGET_POLICY.account_id, MOTION_POLICY),
            (motion_account_row, MOTION_POLICY.account_id, TARGET_POLICY),
        ):
            conn.execute(
                "UPDATE paper_orders SET account_id=?, research_sleeve=?, "
                "research_policy_version=?, policy_fingerprint=?, "
                "objective_day=?, lead_bucket='day-ahead', "
                "scan_run_id=?, reentry_fingerprint=?, status='PAPER_CLOSED', "
                "realized_pnl=7, closed_at=? WHERE id=?",
                (
                    account,
                    sleeve_policy.sleeve.value,
                    sleeve_policy.policy_version,
                    sleeve_policy.policy_fingerprint,
                    objective_day.isoformat(),
                    f"crossed-scan-{order_id}",
                    f"crossed-entry-{order_id}",
                    _pacific_noon(objective_day),
                    order_id,
                ),
            )

    assert published_profile_key(
        "research",
        account_id=TARGET_POLICY.account_id,
        research_sleeve=TARGET_POLICY.sleeve.value,
        research_policy_version=TARGET_POLICY.policy_version,
        policy_fingerprint=TARGET_POLICY.policy_fingerprint,
    ) == "research-target"
    assert published_profile_key(
        "research",
        account_id=TARGET_POLICY.account_id,
        research_sleeve=MOTION_POLICY.sleeve.value,
        research_policy_version=MOTION_POLICY.policy_version,
        policy_fingerprint=MOTION_POLICY.policy_fingerprint,
    ) == "unknown"
    assert published_profile_key("research") == "research"
    assert published_profile_key("research-target") == "unknown"

    assert store.research_realized_pnl_for_day(
        account_id=TARGET_POLICY.account_id,
        objective_day=objective_day,
    ) == 0.0
    assert store.research_realized_pnl_for_day(
        account_id=MOTION_POLICY.account_id,
        objective_day=objective_day,
    ) == 0.0


def test_init_repairs_and_enforces_immutable_research_plan_snapshots(
    tmp_path,
) -> None:
    db_path = tmp_path / "paper.db"
    PaperStore(db_path)
    with sqlite3.connect(db_path) as conn:
        for name in (
            "trg_research_plan_snapshots_immutable_update",
            "trg_research_plan_snapshots_immutable_delete",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute(
            "CREATE TRIGGER trg_research_plan_snapshots_immutable_update "
            "BEFORE UPDATE ON research_plan_snapshots BEGIN SELECT 1; END"
        )
        conn.execute(
            "CREATE TRIGGER trg_research_plan_snapshots_immutable_delete "
            "BEFORE DELETE ON research_plan_snapshots BEGIN SELECT 1; END"
        )

    PaperStore(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO research_plan_snapshots "
            "(created_at, objective_day, scan_run_id, account_id, policy_version, "
            "policy_fingerprint, target_pnl, realized_today, remaining_target, "
            "available_conservative_expected_profit, target_feasible) "
            "VALUES (?, '2026-07-18', 'immutable-plan', ?, ?, ?, 50, 0, 50, 0, 0)",
            (
                datetime.now(UTC).isoformat(),
                TARGET_POLICY.account_id,
                TARGET_POLICY.policy_version,
                TARGET_POLICY.policy_fingerprint,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE research_plan_snapshots SET realized_today=1 "
                "WHERE scan_run_id='immutable-plan'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "DELETE FROM research_plan_snapshots "
                "WHERE scan_run_id='immutable-plan'"
            )


def test_upgrade_backfills_exact_legacy_daily_goal_without_bricking_report(
    tmp_path,
) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE research_daily_goals (
                objective_day TEXT NOT NULL,
                account_id TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reference_equity REAL NOT NULL,
                target_return REAL NOT NULL,
                target_pnl REAL NOT NULL,
                PRIMARY KEY(objective_day, account_id, policy_version)
            );
            CREATE TRIGGER trg_research_daily_goals_immutable_update
            BEFORE UPDATE ON research_daily_goals
            BEGIN
                SELECT RAISE(ABORT, 'research daily goals are immutable');
            END;
            """
        )
        conn.execute(
            "INSERT INTO research_daily_goals VALUES "
            "('2026-07-18', ?, ?, ?, 1000, 0.05, 50)",
            (
                TARGET_POLICY.account_id,
                TARGET_POLICY.policy_version,
                datetime.now(UTC).isoformat(),
            ),
        )

    store = PaperStore(
        db_path,
        research_clock=lambda: datetime(2026, 7, 18, 19, 0, tzinfo=UTC),
    )

    with store.connect() as conn:
        row = conn.execute(
            "SELECT policy_fingerprint FROM research_daily_goals "
            "WHERE objective_day='2026-07-18'"
        ).fetchone()
        assert row == (TARGET_POLICY.policy_fingerprint,)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE research_daily_goals SET target_pnl=51 "
                "WHERE objective_day='2026-07-18'"
            )

    report = store.research_daily_goal_report(
        through_day=date(2026, 7, 18),
        window_days=1,
    )
    assert report["target_pnl"] == 50.0
    assert report["realized_pnl"] == 0.0


def test_goal_report_bounds_activity_to_window_and_uses_lifecycle_indexes(
    tmp_path,
) -> None:
    db_path = tmp_path / "paper.db"
    store = PaperStore(
        db_path,
        research_clock=lambda: datetime(2026, 7, 18, 19, 0, tzinfo=UTC),
    )

    def closed_target_order(target_date: str, resolved_day: date, pnl: float) -> int:
        order_id = store.record_paper_order(
            target_date,
            replace(
                _approved_decision(),
                ticker=f"KXHIGHTPHX-WINDOW-{resolved_day.isoformat()}-B110.5",
            ),
            risk_profile="research",
        )
        assert order_id is not None
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET account_id=?, research_sleeve='target', "
                "research_policy_version=?, policy_fingerprint=?, "
                "objective_day=?, lead_bucket='day-ahead', scan_run_id=?, "
                "reentry_fingerprint=?, status='PAPER_CLOSED', realized_pnl=?, "
                "closed_at=? WHERE id=?",
                (
                    TARGET_POLICY.account_id,
                    TARGET_POLICY.policy_version,
                    TARGET_POLICY.policy_fingerprint,
                    resolved_day.isoformat(),
                    f"window-scan-{order_id}",
                    f"window-entry-{order_id}",
                    pnl,
                    _pacific_noon(resolved_day),
                    order_id,
                ),
            )
        return order_id

    closed_target_order("2026-07-02", date(2026, 7, 1), 100.0)
    store.research_daily_goal_state(objective_day=date(2026, 7, 1))
    closed_target_order("2026-07-19", date(2026, 7, 18), 10.0)

    report = store.research_daily_goal_report(
        through_day=date(2026, 7, 18),
        window_days=2,
    )

    assert report["activation_day"] == "2026-07-01"
    assert report["total_observed_days_since_activation"] == 18
    assert report["window_start"] == "2026-07-17"
    assert report["observed_days"] == 2
    assert report["realized_pnl"] == 10.0
    assert report["mean_daily_pnl"] == 5.0
    assert report["logical_decisions"] == 1
    assert report["exit_breakdown"]["take_profit"]["realized_pnl"] == 10.0

    lower = datetime(2026, 7, 17, tzinfo=UTC).isoformat()
    upper = datetime(2026, 7, 19, tzinfo=UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        plans = []
        for column in ("closed_at", "settled_at", "expires_at"):
            plans.extend(
                conn.execute(
                    "EXPLAIN QUERY PLAN SELECT id FROM paper_orders "
                    f"WHERE account_id=? AND status!='REJECTED' AND {column}>=? "
                    f"AND {column}<?",
                    (TARGET_POLICY.account_id, lower, upper),
                ).fetchall()
            )
        plans.extend(
            conn.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM paper_orders "
                "WHERE parent_order_id=?",
                (1,),
            ).fetchall()
        )
    plan_text = " ".join(str(row[3]) for row in plans)
    assert "idx_paper_orders_account_closed" in plan_text
    assert "idx_paper_orders_account_settled" in plan_text
    assert "idx_paper_orders_account_expires" in plan_text
    assert "idx_paper_orders_parent" in plan_text


def test_daily_history_includes_explicit_zero_pnl_calendar_days(tmp_path) -> None:
    store = PaperStore(
        tmp_path / "paper.db",
        research_clock=lambda: datetime(2026, 7, 19, 19, 0, tzinfo=UTC),
    )
    store.research_daily_goal_state(objective_day=date(2026, 7, 17))

    report = store.research_daily_goal_report(through_day=date(2026, 7, 19))

    assert [row["objective_day"] for row in report["days"]] == [
        "2026-07-17",
        "2026-07-18",
        "2026-07-19",
    ]
    assert [row["realized_pnl"] for row in report["days"]] == [0.0, 0.0, 0.0]
    assert report["observed_days"] == 3
    assert report["zero_activity_days"] == 3
    assert report["hit_count"] == 0
    assert report["attainment_rate"] == 0.0
    assert report["disclaimer"] == (
        "Hard paper-research objective; not a guaranteed return. "
        "Risk and edge gates remain binding."
    )


def test_partial_lot_money_uses_actual_pacific_day_and_counts_one_logical_decision(
    tmp_path,
) -> None:
    store = PaperStore(
        tmp_path / "paper.db",
        research_clock=lambda: datetime(2026, 7, 19, 19, 0, tzinfo=UTC),
    )
    root_id = store.record_paper_order(
        "2026-07-20",
        _approved_decision(),
        risk_profile="research",
    )
    assert root_id is not None
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_orders SET account_id=?, research_sleeve='target', "
            "research_policy_version=?, policy_fingerprint=?, "
            "objective_day='2026-07-17', lead_bucket='day-ahead', "
            "scan_run_id='goal-test-scan', reentry_fingerprint='goal-test-reentry' "
            "WHERE id=?",
            (
                TARGET_POLICY.account_id,
                TARGET_POLICY.policy_version,
                TARGET_POLICY.policy_fingerprint,
                root_id,
            ),
        )

    first_lot = store.close_paper_order(root_id, 0.70, max_quantity=4.0)
    final_lot = store.close_paper_order(root_id, 0.70)
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_orders SET closed_at=? WHERE id=?",
            (_pacific_noon(date(2026, 7, 17)), int(first_lot["id"])),
        )
        conn.execute(
            "UPDATE paper_orders SET closed_at=? WHERE id=?",
            (_pacific_noon(date(2026, 7, 18)), int(final_lot["id"])),
        )

    store.research_daily_goal_state(objective_day=date(2026, 7, 17))
    report = store.research_daily_goal_report(through_day=date(2026, 7, 19))

    pnl_by_day = {
        row["objective_day"]: row["realized_pnl"] for row in report["days"]
    }
    assert pnl_by_day["2026-07-17"] == pytest.approx(first_lot["realized_pnl"])
    assert pnl_by_day["2026-07-18"] == pytest.approx(final_lot["realized_pnl"])
    assert pnl_by_day["2026-07-19"] == 0.0
    assert report["logical_decisions"] == 1
    assert report["resolved_lots"] == 2
    assert report["resolution_days"] == 2
    assert report["independent_city_target_days"] == 1
    assert report["lead_split"]["day-ahead"]["logical_decisions"] == 1
    assert report["lead_split"]["day-ahead"]["resolved_lots"] == 2
    assert report["execution"]["partial_exit_positions"] == 1
    assert report["execution"]["total_fees"] > 0
    assert report["exit_breakdown"]["take_profit"]["logical_decisions"] == 1


def test_daily_goal_summary_reports_day_clustered_statistics_without_a_guarantee() -> None:
    pnls = [0.0, 50.0, -10.0, 20.0]
    states = [
        daily_goal_state(
            objective_day=date(2026, 7, 17 + offset),
            realized_pnl=pnl,
            target_pnl=50.0,
        )
        for offset, pnl in enumerate(pnls)
    ]

    report = summarize_daily_goals(states, target_feasible=False)

    assert report["mean_daily_pnl"] == pytest.approx(15.0)
    assert report["median_daily_pnl"] == pytest.approx(10.0)
    assert report["p25_daily_pnl"] == pytest.approx(-2.5)
    assert report["p75_daily_pnl"] == pytest.approx(27.5)
    assert report["daily_pnl_stddev"] == pytest.approx(22.9128784748)
    assert report["day_cluster_bootstrap_95_ci"]["method"] == (
        "deterministic_day_cluster_bootstrap"
    )
    assert report["day_cluster_bootstrap_95_ci"]["samples"] == 4000
    assert report["day_cluster_bootstrap_95_ci"]["lower"] <= 15.0
    assert report["day_cluster_bootstrap_95_ci"]["upper"] >= 15.0
    assert report["maximum_drawdown_dollars"] == pytest.approx(10.0)
    assert report["maximum_drawdown_pct"] == pytest.approx(10.0 / 1050.0)
    assert report["log_growth"] == pytest.approx(math.log(1060.0 / 1000.0))
    assert report["target_feasible"] is False
    assert "guaranteed" in report["disclaimer"]


def test_target_only_new_risk_locks_after_50_while_motion_continues(tmp_path) -> None:
    objective_day = date(2026, 7, 18)
    store = PaperStore(
        tmp_path / "paper.db",
        research_clock=lambda: datetime(2026, 7, 18, 19, 0, tzinfo=UTC),
    )
    root_id = store.record_paper_order(
        "2026-07-19", _approved_decision(), risk_profile="research"
    )
    assert root_id is not None
    with store.connect() as conn:
        conn.execute(
            "UPDATE paper_orders SET account_id=?, research_sleeve='target', "
            "research_policy_version=?, policy_fingerprint=?, "
            "objective_day=?, lead_bucket='day-ahead', "
            "scan_run_id='lock-test', reentry_fingerprint='lock-test-entry', "
            "status='PAPER_CLOSED', realized_pnl=50, closed_at=? WHERE id=?",
            (
                TARGET_POLICY.account_id,
                TARGET_POLICY.policy_version,
                TARGET_POLICY.policy_fingerprint,
                objective_day.isoformat(),
                _pacific_noon(objective_day),
                root_id,
            ),
        )

    state = store.research_daily_goal_state(objective_day=objective_day)
    target_capacity = store.account_policy_capacity(
        target_date="2026-07-19",
        market_ticker="KXHIGHTSFO-TEST-B70.5",
        risk_profile="research",
        requested_spend=0.50,
        account_id=TARGET_POLICY.account_id,
    )
    motion_capacity = store.account_policy_capacity(
        target_date="2026-07-18",
        market_ticker="KXHIGHTSFO-TEST-B70.5",
        risk_profile="research",
        requested_spend=0.50,
        account_id=MOTION_POLICY.account_id,
    )

    assert state.achieved is True
    assert state.locked is True
    assert target_capacity["allowed_spend"] == 0.0
    assert "target attained" in target_capacity["reason"]
    assert motion_capacity["allowed_spend"] == pytest.approx(0.50)


def test_report_does_not_infer_feasibility_from_decision_rows(tmp_path) -> None:
    objective_day = date(2026, 7, 18)
    store = PaperStore(
        tmp_path / "paper.db",
        research_clock=lambda: datetime(2026, 7, 18, 19, 0, tzinfo=UTC),
    )
    decision_id = store.record_decisions(
        "2026-07-19",
        [_approved_decision()],
        risk_profile="research",
    )[0]
    with store.connect() as conn:
        conn.execute(
            "UPDATE decision_snapshots SET account_id=?, research_sleeve='target', "
            "research_policy_version=?, policy_fingerprint=?, objective_day=?, "
            "lead_bucket='day-ahead', scan_run_id='feasibility-scan', "
            "reentry_fingerprint='feasibility-entry' WHERE id=?",
            (
                TARGET_POLICY.account_id,
                TARGET_POLICY.policy_version,
                TARGET_POLICY.policy_fingerprint,
                objective_day.isoformat(),
                decision_id,
            ),
        )
    store.research_daily_goal_state(objective_day=objective_day)

    report = store.research_daily_goal_report(through_day=objective_day)

    assert report["available_conservative_expected_profit"] is None
    assert report["remaining_pnl"] == 50.0
    assert report["target_feasible"] is None
    assert report["feasibility_evidence"] == "unavailable"


def test_report_uses_persisted_allocator_feasibility_including_empty_scan(
    tmp_path,
) -> None:
    from sfo_kalshi_quant.config import strategy_config_for_profile
    from sfo_kalshi_quant.paper import PaperTrader
    from sfo_kalshi_quant.research_portfolio import allocate_research_plans

    objective_day = date(2026, 7, 18)
    store = PaperStore(
        tmp_path / "paper.db",
        research_clock=lambda: datetime(2026, 7, 18, 19, 0, tzinfo=UTC),
    )
    plans = allocate_research_plans([], run_id="empty-opportunity-scan")
    assert plans.available_conservative_expected_profit == 0.0
    assert plans.target_feasible_from_current_opportunity_set is False

    trader = PaperTrader(
        store,
        strategy_config_for_profile("research"),
        risk_profile="research",
        entry_mode="limit",
    )
    for _ in range(2):
        result = trader.execute_research_plans(
            "2026-07-19",
            plans,
            source_decisions=[],
            objective_day=objective_day.isoformat(),
            lead_bucket="day-ahead",
            scan_run_id="empty-opportunity-scan",
            observed_high_state="complete=0;high=unavailable",
        )
        assert result.target_decision_ids == ()
        assert result.motion_decision_ids == ()
    report = store.research_daily_goal_report(through_day=objective_day)

    assert report["feasibility_evidence"] == "current_scan"
    assert report["available_conservative_expected_profit"] == 0.0
    assert report["target_feasible"] is False
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM research_plan_snapshots "
            "WHERE scan_run_id='empty-opportunity-scan'"
        ).fetchone()[0] == 1


def test_report_exit_breakdown_uses_exact_audited_terminal_categories(tmp_path) -> None:
    objective_day = date(2026, 7, 18)
    store = PaperStore(
        tmp_path / "paper.db",
        research_clock=lambda: datetime(2026, 7, 18, 19, 0, tzinfo=UTC),
    )
    created: list[tuple[int, str, float, str | None, str | None]] = []
    for index, (status, pnl, closed_at, settled_at) in enumerate(
        [
            ("PAPER_CLOSED", 2.0, _pacific_noon(objective_day), None),
            ("PAPER_CLOSED", -2.0, _pacific_noon(objective_day), None),
            ("PAPER_CLOSED", 0.0, _pacific_noon(objective_day), None),
            ("PAPER_SETTLED", 2.0, None, _pacific_noon(objective_day)),
            ("PAPER_EXPIRED", 0.0, None, None),
        ]
    ):
        decision = replace(
            _approved_decision(),
            ticker=f"KXHIGHTPHX-EXIT-{index}-B110.5",
        )
        order_id = store.record_paper_order(
            f"2026-07-{19 + index:02d}",
            decision,
            risk_profile="research",
        )
        assert order_id is not None
        created.append((order_id, status, pnl, closed_at, settled_at))

    with store.connect() as conn:
        for order_id, status, pnl, closed_at, settled_at in created:
            conn.execute(
                "UPDATE paper_orders SET account_id=?, research_sleeve='target', "
                "research_policy_version=?, policy_fingerprint=?, "
                "objective_day=?, lead_bucket='day-ahead', "
                "scan_run_id=?, reentry_fingerprint=?, status=?, "
                "realized_pnl=?, closed_at=?, settled_at=? WHERE id=?",
                (
                    TARGET_POLICY.account_id,
                    TARGET_POLICY.policy_version,
                    TARGET_POLICY.policy_fingerprint,
                    objective_day.isoformat(),
                    f"exit-scan-{order_id}",
                    f"exit-entry-{order_id}",
                    status,
                    pnl,
                    closed_at,
                    settled_at,
                    order_id,
                ),
            )

    report = store.research_daily_goal_report(through_day=objective_day)

    assert {
        reason: bucket["logical_decisions"]
        for reason, bucket in report["exit_breakdown"].items()
    } == {
        "take_profit": 1,
        "stop_loss": 1,
        "break_even": 1,
        "held_to_settlement": 1,
        "expired_unfilled": 1,
    }


def test_goal_report_bounds_ancient_activation_without_write_amplification(
    tmp_path,
) -> None:
    store = PaperStore(
        tmp_path / "paper.db",
        research_clock=lambda: datetime(2026, 7, 18, 19, 0, tzinfo=UTC),
    )
    ancient_day = date(2025, 1, 1)
    through_day = date(2026, 7, 18)
    store.research_daily_goal_state(objective_day=ancient_day)

    report = store.research_daily_goal_report(through_day=through_day)

    assert report["activation_day"] == ancient_day.isoformat()
    assert report["window_days"] == 30
    assert report["window_start"] == "2026-06-19"
    assert len(report["days"]) == 30
    with store.connect() as conn:
        persisted = conn.execute(
            "SELECT COUNT(*) FROM research_daily_goals WHERE account_id=?",
            (TARGET_POLICY.account_id,),
        ).fetchone()[0]
    assert persisted == 31
