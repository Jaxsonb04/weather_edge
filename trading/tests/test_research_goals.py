from __future__ import annotations

import math
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
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
            "UPDATE decision_snapshots SET research_sleeve='target', "
            "research_policy_version=?, policy_fingerprint=?, objective_day=?, "
            "lead_bucket='day-ahead', scan_run_id='feasibility-scan', "
            "reentry_fingerprint='feasibility-entry' WHERE id=?",
            (
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
