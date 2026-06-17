from __future__ import annotations

import io
import json
import sqlite3
import tempfile
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sfo_kalshi_quant.cli import main
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.config import SFO_TZ, StrategyConfig
from sfo_kalshi_quant.strategy_research import (
    _dataset_research_summary,
    _entry_block_reason,
    _strategy_alerts,
    _status_target_date,
    build_strategy_research,
)
from sfo_kalshi_quant.paper import PaperTrader

from support import pre_resolution_event


def _write_lstm_fixture(root: Path, n: int = 90) -> None:
    root.mkdir(parents=True, exist_ok=True)
    start = date(2025, 1, 1)
    daily = []
    for idx in range(n):
        predicted = 58.0 + (idx % 16) * 0.7
        residual = [-2.0, -1.0, 0.0, 1.0, 2.0, 3.0][idx % 6]
        daily.append(
            {
                "date": (start + timedelta(days=idx)).isoformat(),
                "lstm": round(predicted, 2),
                "actual": round(predicted + residual, 2),
            }
        )
    (root / "ab_test_results.json").write_text(
        json.dumps({"target_daily_high_next_day": {"chart": {"daily": daily}}}),
        encoding="utf-8",
    )


def _write_settlement(root: Path, target: str = "2026-06-03", high: float = 67.0) -> None:
    with sqlite3.connect(root / "weather.db") as conn:
        conn.execute(
            """
            CREATE TABLE nws_daily_high_ground_truth (
                station_id TEXT,
                local_date TEXT,
                high_f REAL,
                is_complete INTEGER
            )
            """
        )
        conn.execute(
            """
            INSERT INTO nws_daily_high_ground_truth (station_id, local_date, high_f, is_complete)
            VALUES ('KSFO', ?, ?, 1)
            """,
            (target, high),
        )


def _approved_decision() -> TradeDecision:
    return TradeDecision(
        ticker="KXHIGHTSFO-TEST-B66.5",
        label="66 to 67",
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
        floor_strike=66.0,
        cap_strike=67.0,
        model_probability=0.70,
        market_probability=0.42,
        residual_probability=0.68,
        ensemble_probability=0.72,
    )


def test_strategy_research_does_not_create_missing_paper_db():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        missing_db = Path(tmp) / "missing" / "paper.db"
        _write_lstm_fixture(root)

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=missing_db,
            calibration_min_train=40,
        )

        assert payload["mode"] == "paper_research_only"
        assert payload["live_orders_enabled"] is False
        assert payload["calibration_comparison"]["active"]["available"] is True
        assert payload["status"]["active_calibration_source"] == "lstm"
        assert not missing_db.exists()


def test_strategy_research_reads_decisions_and_open_paper_positions():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        _write_settlement(root)

        store = PaperStore(db_path)
        decision = _approved_decision()
        store.record_decisions(
            "2026-06-03", [decision], event=pre_resolution_event([decision])
        )
        store.record_paper_order("2026-06-03", decision)

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        assert payload["backtest_summary"]["counts"]["raw_signals"] == 1
        assert payload["backtest_summary"]["counts"]["deduped_signals"] == 1
        assert payload["paper_trading"]["summary"]["open_positions"] == 1
        assert payload["paper_trading"]["summary"]["marked_open_positions"] == 1
        assert payload["paper_trading"]["summary"]["unrealized_pnl"] < 0
        assert payload["paper_trading"]["summary"]["latest_monitor_action_at"] is None
        action = payload["paper_trading"]["recent_monitor_actions"][0]
        assert action["status"] == "OPEN"
        assert action["note"] == "paper order opened"
        assert action["ticker"] == decision.ticker
        position = payload["paper_trading"]["open_positions"][0]
        assert position["why_good"]
        assert position["initial_cost"] == position["risk"]
        assert position["max_loss"] == position["risk"]
        assert position["take_profit_bid"] is not None
        assert position["stop_loss_bid"] is not None
        assert position["current_bid"] == 0.2
        assert position["unrealized_pnl"] < 0
        assert position["position_status"] == "LOSING"
        assert payload["signal_quality"]["latest_candidates"][0]["approved"] is True
        assert payload["signal_quality"]["charts"]["probability_vs_market"]
        alert_codes = {alert["code"] for alert in payload["status"]["alerts"]}
        assert "settlement-backlog" in alert_codes


def test_strategy_research_surfaces_resting_limit_orders():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)

        store = PaperStore(db_path)
        trader = PaperTrader(
            store,
            StrategyConfig(limit_price_edge_lcb_buffer=0.02),
            entry_mode="limit",
        )
        decision = _approved_decision()

        order_ids = trader.place_approved("2026-06-15", [decision])
        assert len(order_ids) == 1
        row = store.paper_order(order_ids[0])
        assert row is not None
        assert row["status"] == "PAPER_LIMIT_RESTING"

        # The monitor never marks resting orders, but every scan records a
        # decision snapshot with the live bid/ask; that mark must reach the card.
        store.record_decisions("2026-06-15", [decision])

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        summary = payload["paper_trading"]["summary"]
        assert summary["open_positions"] == 0
        assert summary["pending_limit_orders"] == 1
        assert summary["published_pending_limit_orders"] == 1
        assert summary["pending_limit_risk"] > 0
        pending = payload["paper_trading"]["pending_limit_orders"][0]
        assert pending["status"] == "PAPER_LIMIT_RESTING"
        assert pending["limit_price"] == 0.29
        # The current market price is now shown for resting limits.
        assert pending["current_ask"] is not None
        assert pending["current_bid"] is not None
        assert "resting limit" in payload["status"]["paper_trading_status"]
        assert any(
            action["status"] == "LIMIT_RESTING"
            for action in payload["paper_trading"]["recent_monitor_actions"]
        )


def test_strategy_research_ignores_probability_only_targets_for_latest_status():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        _write_settlement(root)

        store = PaperStore(db_path)
        live_decision = _approved_decision()
        fallback_decision = replace(_approved_decision(), ticker="KXHIGHTSFO-26JUN04-PAPER-B66.5")
        store.record_decisions("2026-06-03", [live_decision])
        store.record_decisions("2026-06-04", [fallback_decision])

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        assert payload["status"]["latest_target_date"] == "2026-06-03"
        assert {
            row["target_date"] for row in payload["signal_quality"]["latest_candidates"]
        } == {"2026-06-03"}


def test_entry_block_reason_ignores_stale_prior_day_blocks():
    rows = [
        {
            "target_date": "2026-06-09",
            "reasons": ["same-day entry disabled: observed high is locked; monitor/settle only"],
        },
        {
            "target_date": "2026-06-10",
            "reasons": ["edge -0.040 below min 0.020"],
        },
    ]

    reason = _entry_block_reason(
        rows,
        now=datetime(2026, 6, 10, 2, 39, tzinfo=SFO_TZ),
    )

    assert reason is None


def test_monitor_alert_allows_fresh_open_position_before_first_mark():
    opened_at = datetime(2026, 6, 10, 9, 36, tzinfo=UTC)
    alerts = _strategy_alerts(
        paper={
            "available": True,
            "summary": {
                "open_positions": 1,
                "latest_opened_at": opened_at.isoformat(),
                "latest_monitor_action_at": None,
                "marked_open_positions": 0,
                "hidden_open_positions": 0,
            },
            "duplicate_open_groups": [],
        },
        entry_block_reason=None,
        now=opened_at + timedelta(minutes=2),
    )

    codes = {alert["code"] for alert in alerts}
    assert "monitor-not-recording" not in codes
    assert "monitor-pending" in codes


def test_status_target_date_prefers_today_before_same_day_block():
    target = _status_target_date(
        ["2026-06-10", "2026-06-11", "2026-06-12"],
        entry_block_reason=None,
        now=datetime(2026, 6, 10, 2, 10, tzinfo=SFO_TZ),
    )

    assert target == "2026-06-10"


def test_status_target_date_switches_to_next_day_when_same_day_blocked():
    target = _status_target_date(
        ["2026-06-10", "2026-06-11", "2026-06-12"],
        entry_block_reason="same-day entry disabled: local peak/high window has passed",
        now=datetime(2026, 6, 10, 15, 1, tzinfo=SFO_TZ),
    )

    assert target == "2026-06-11"


def test_strategy_research_prefers_monitor_marks_for_open_positions():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        _write_settlement(root)

        store = PaperStore(db_path)
        decision = _approved_decision()
        store.record_decisions("2026-06-03", [decision])
        store.record_paper_order("2026-06-03", decision)
        order = store.open_paper_orders(1)[0]
        for _ in range(13):
            store.record_monitor_snapshot(
                order,
                side="YES",
                action="HOLD",
                reason="inside exit bands",
                market_status="active",
                live_bid=0.60,
                exit_fee_per_contract=0.02,
                net_exit_per_contract=0.58,
                unrealized_pnl=2.70,
                unrealized_roi=0.87,
            )

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        position = payload["paper_trading"]["open_positions"][0]
        assert position["current_source"] == "paper_monitor_snapshot"
        assert position["current_bid"] == 0.6
        assert position["unrealized_pnl"] > 0
        assert payload["paper_trading"]["recent_monitor_actions"][0]["status"] == "HOLD"
        assert any(
            row["status"] == "OPEN"
            for row in payload["paper_trading"]["recent_monitor_actions"]
        )


def test_strategy_research_alerts_on_duplicate_open_positions():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        _write_settlement(root)

        store = PaperStore(db_path)
        decision = _approved_decision()
        store.record_paper_order("2026-06-03", decision)
        store.record_paper_order("2026-06-03", decision)

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        alert_codes = {alert["code"] for alert in payload["status"]["alerts"]}
        assert "duplicate-open-markets" in alert_codes
        assert payload["status"]["alert_level"] == "critical"
        assert payload["paper_trading"]["summary"]["duplicate_open_groups"] == 1
        assert payload["paper_trading"]["summary"]["largest_duplicate_open_group"] == 2


def test_strategy_research_does_not_alert_on_same_market_across_profiles():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        _write_settlement(root)

        store = PaperStore(db_path)
        decision = _approved_decision()
        store.record_paper_order("2026-06-03", decision, risk_profile="balanced")
        store.record_paper_order("2026-06-03", decision, risk_profile="fast-feedback")

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        alert_codes = {alert["code"] for alert in payload["status"]["alerts"]}
        assert "duplicate-open-markets" not in alert_codes
        assert payload["paper_trading"]["summary"]["duplicate_open_groups"] == 0


def test_strategy_research_scopes_duplicate_alerts_to_profile_views():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        target = (datetime.now(UTC).astimezone(SFO_TZ).date() + timedelta(days=1)).isoformat()

        store = PaperStore(db_path)
        balanced = replace(_approved_decision(), ticker="KXHIGHTSFO-TEST-B65.5")
        fast = _approved_decision()
        store.record_paper_order(target, balanced, risk_profile="balanced")
        store.record_paper_order(target, fast, risk_profile="fast-feedback")
        store.record_paper_order(target, fast, risk_profile="fast-feedback")

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        profiles = {row["risk_profile"]: row for row in payload["profiles"]}
        balanced_alerts = {
            alert["code"] for alert in profiles["balanced"]["status"]["alerts"]
        }
        fast_alerts = {
            alert["code"] for alert in profiles["fast-feedback"]["status"]["alerts"]
        }

        assert "duplicate-open-markets" in {
            alert["code"] for alert in payload["status"]["alerts"]
        }
        assert "duplicate-open-markets" not in balanced_alerts
        assert "duplicate-open-markets" in fast_alerts
        assert profiles["fast-feedback"]["paper_trading"]["summary"]["duplicate_open_groups"] == 1


def test_strategy_research_builds_isolated_profile_views():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        today_date = datetime.now(UTC).astimezone(SFO_TZ).date()
        today = today_date.isoformat()
        tomorrow = (today_date + timedelta(days=1)).isoformat()
        _write_settlement(root, target=today)

        store = PaperStore(db_path)
        balanced_win = _approved_decision()
        fast_loss = replace(
            _approved_decision(),
            ticker="KXHIGHTSFO-TEST-B68.5",
            label="68 to 69",
            floor_strike=68.0,
            cap_strike=69.0,
        )
        fast_open = replace(
            _approved_decision(),
            ticker="KXHIGHTSFO-TEST-B65.5",
            label="65 to 66",
            floor_strike=65.0,
            cap_strike=66.0,
        )

        store.record_decisions(today, [balanced_win], risk_profile="balanced")
        store.record_decisions(tomorrow, [fast_open], risk_profile="fast-feedback")
        store.record_paper_order(today, balanced_win, risk_profile="balanced")
        store.record_paper_order(today, fast_loss, risk_profile="fast-feedback")
        store.settle_paper_orders(today, 67.0)
        open_order_id = store.record_paper_order(
            tomorrow,
            fast_open,
            risk_profile="fast-feedback",
        )
        open_order = store.open_paper_order(open_order_id)
        assert open_order is not None
        store.record_monitor_snapshot(
            open_order,
            side="YES",
            action="HOLD",
            reason="inside exit bands",
            market_status="active",
            live_bid=0.42,
            exit_fee_per_contract=0.01,
            net_exit_per_contract=0.41,
            unrealized_pnl=1.0,
            unrealized_roi=0.32,
        )

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        assert payload["default_profile"] == "balanced"
        profiles = {row["risk_profile"]: row for row in payload["profiles"]}
        assert set(profiles) == {"balanced", "fast-feedback"}

        balanced = profiles["balanced"]
        fast = profiles["fast-feedback"]
        assert balanced["profile_type"] == "primary"
        assert fast["profile_type"] == "experimental"

        assert balanced["daily_summary"]["totals"]["realized_pnl"] > 0
        assert balanced["daily_summary"]["totals"]["losses"] == 0
        assert balanced["paper_trading"]["summary"]["open_risk"] == 0.0
        assert {
            row["risk_profile"]
            for row in balanced["paper_trading"]["recent_monitor_actions"]
        } == {"balanced"}
        assert {
            row["risk_profile"]
            for row in balanced["signal_quality"]["latest_candidates"]
        } == {"balanced"}

        assert fast["daily_summary"]["totals"]["realized_pnl"] < 0
        assert fast["daily_summary"]["totals"]["wins"] == 0
        assert fast["paper_trading"]["summary"]["open_risk"] > 0
        assert {
            row["risk_profile"]
            for row in fast["paper_trading"]["recent_monitor_actions"]
        } == {"fast-feedback"}
        assert {
            row["status"]
            for row in fast["paper_trading"]["recent_monitor_actions"]
        } >= {"OPEN", "HOLD"}
        assert {
            row["risk_profile"]
            for row in fast["signal_quality"]["latest_candidates"]
        } == {"fast-feedback"}
        assert any("fast-feedback" in note for note in fast["learnings"])

        # FIX E/F: per-profile live equity and YES/NO + exit breakdowns now live
        # on each profile's daily_summary, not just the All-profiles overview.
        b_summary = balanced["daily_summary"]
        f_summary = fast["daily_summary"]
        # Equity = shared starting notional + that profile's all-time realized.
        assert b_summary["current_equity"] == round(
            b_summary["starting_bankroll"] + b_summary["totals"]["cumulative_realized_pnl"], 2
        )
        # The winner is above start, the loser below -- and the two differ,
        # proving the value is profile-scoped, not the shared aggregate.
        assert b_summary["current_equity"] > b_summary["starting_bankroll"]
        assert f_summary["current_equity"] < f_summary["starting_bankroll"]
        assert b_summary["current_equity"] != f_summary["current_equity"]
        # Profile-scoped side split + exit reasons render on the profile tab.
        assert b_summary["side_performance"]["YES"]["wins"] == 1
        assert f_summary["side_performance"]["YES"]["losses"] == 1
        assert b_summary["exit_reasons"]["held_to_settlement"] == 1


def test_strategy_research_includes_config_rescore():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        _write_settlement(root, target="2026-06-03", high=70.0)

        store = PaperStore(db_path)
        decision = _approved_decision()
        store.record_decisions(
            "2026-06-03", [decision], event=pre_resolution_event([decision])
        )

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        rescore = payload["config_rescore"]
        assert rescore["available"] is True, rescore.get("reason")
        assert set(rescore["by_profile"]) == {"balanced", "fast-feedback", "exploratory"}
        for result in rescore["by_profile"].values():
            assert {"counts", "candidate", "recorded_config_own_book"} <= set(result)
            # per_day is trimmed from the published artifact to keep it lean.
            assert "per_day" not in result


def test_strategy_research_cli_writes_public_artifact():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "missing.db"
        output = root / "strategy_research.json"
        _write_lstm_fixture(root)

        out = io.StringIO()
        with redirect_stdout(out):
            code = main(
                [
                    "--forecaster-root",
                    str(root),
                    "--db-path",
                    str(db_path),
                    "--no-color",
                    "strategy-research",
                    "--calibration-min-train",
                    "40",
                    "--output",
                    str(output),
                ]
            )

        assert code == 0
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["disclaimer"].startswith("Paper-trading research only")
        assert payload["status"]["challenger_calibration_source"] == "clean-blend/combined"
        assert json.loads(out.getvalue())["schema_version"] == 1


def test_dataset_research_summary_reads_accuracy_gate_candidates():
    payload = {
        "generated_at": "2026-06-11T07:00:00+00:00",
        "status": "collect_only",
        "promotion_rule": "rule text",
        "accuracy_gate": {
            "available": True,
            "candidate_count": 2,
            "accuracy_candidate_count": 1,
            "candidates": [
                {
                    "dataset_key": "open-meteo/gfs/temperature_2m_max/24h",
                    "decision": "accuracy_candidate",
                    "matched_rows": 41,
                    "all_matched": {
                        "dataset_mae_f": 1.9,
                        "baseline_mae_f": 2.0,
                        "mae_delta_vs_baseline_f": -0.1,
                    },
                    "holdout": {
                        "dataset_mae_f": 1.7,
                        "baseline_mae_f": 2.1,
                        "mae_delta_vs_baseline_f": -0.4,
                    },
                },
                {
                    "dataset_key": "iem/asos/temperature_2m_max/48h",
                    "decision": "collect_only",
                    "matched_rows": 12,
                    "holdout": {
                        "dataset_mae_f": 2.6,
                        "baseline_mae_f": 2.1,
                        "mae_delta_vs_baseline_f": 0.5,
                    },
                },
            ],
        },
    }

    summary = _dataset_research_summary(payload)

    assert summary["available"] is True
    assert summary["candidate_count"] == 2
    assert summary["accuracy_candidate_count"] == 1
    assert len(summary["candidates"]) == 2
    top = summary["candidates"][0]
    assert top["dataset_key"] == "open-meteo/gfs/temperature_2m_max/24h"
    assert top["mae_delta_vs_baseline_f"] == -0.4
    assert top["dataset_mae_f"] == 1.7
    assert top["matched_rows"] == 41


def test_dataset_research_summary_keeps_legacy_top_level_candidates_readable():
    payload = {
        "generated_at": "2026-06-01T07:00:00+00:00",
        "status": "collect_only",
        "candidate_count": 1,
        "accuracy_candidate_count": 0,
        "candidates": [
            {
                "dataset_key": "legacy/key",
                "decision": "collect_only",
                "matched_rows": 5,
                "dataset_mae_f": 2.4,
                "baseline_mae_f": 2.2,
                "mae_delta_vs_baseline_f": 0.2,
            }
        ],
    }

    summary = _dataset_research_summary(payload)

    assert summary["candidate_count"] == 1
    assert summary["candidates"][0]["dataset_mae_f"] == 2.4


def test_dataset_research_summary_derives_actionable_legacy_verdict():
    payload = {
        "generated_at": "2026-06-11T09:25:08+00:00",
        "status": "collect_only",
        "baseline": {
            "source": "lstm",
            "outcome_count": 475,
            "settlement": "rounded SFO high temperature",
        },
        "promotion_rule": "rule text",
        "accuracy_gate": {
            "available": True,
            "candidate_count": 9,
            "accuracy_candidate_count": 0,
            "candidates": [
                {
                    "dataset_key": "open-meteo/best_match/temperature_2m_max/none",
                    "decision": "collect_only",
                    "matched_rows": 0,
                    "reason": "needs at least 30 matched settlement rows; has 0",
                }
            ],
        },
        "profitability_gate": {
            "decision": "collect_only",
            "market_history": {"markets": 180, "candles": 365, "trades": 0},
            "minimum_after_cost_trades": 30,
        },
    }

    summary = _dataset_research_summary(payload)

    assert summary["headline"].startswith("Dataset collection is live")
    assert summary["action_items"]
    assert any(
        "accuracy gate: best dataset feature has 0 matched settlement rows" in gate
        for gate in summary["blocking_gates"]
    )
    assert "market gate: 0 after-cost trade rows; needs 30" in summary["blocking_gates"]
    assert summary["dataset_stack"]["reason"].startswith("Combined dataset stack is waiting")
    assert summary["candidates"][0]["next_use"].startswith("Keep collecting")


def test_signal_quality_filters_resolved_candidates_from_both_extremes():
    """Resolved markets (winner ask~1.00 AND loser ask~0.00) are dropped from the
    candidate denominator; a live market (ask on the 1c..99c grid) survives."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        _write_settlement(root)

        store = PaperStore(db_path)
        live = replace(_approved_decision(), ticker="KXHIGHTSFO-TEST-LIVE", entry_ask=0.30)
        resolved_winner = replace(_approved_decision(), ticker="KXHIGHTSFO-TEST-WIN", entry_ask=0.999)
        resolved_loser = replace(_approved_decision(), ticker="KXHIGHTSFO-TEST-LOSS", entry_ask=0.001)
        store.record_decisions("2026-06-03", [live, resolved_winner, resolved_loser])

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        signal = payload["signal_quality"]
        assert signal["stale_candidates_filtered"] == 2
        assert {c["ticker"] for c in signal["latest_candidates"]} == {"KXHIGHTSFO-TEST-LIVE"}


def test_recent_monitor_actions_dedup_to_latest_per_order_and_flag_unrealized():
    """A position that HOLDs across several monitor cycles collapses to one
    inspection row per order, flagged as an unrealized (open) mark."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        _write_settlement(root)

        store = PaperStore(db_path)
        first = _approved_decision()
        second = replace(
            first,
            ticker="KXHIGHTSFO-TEST-B67.5",
            label="67 to 68",
            floor_strike=67.0,
            cap_strike=68.0,
        )
        store.record_decisions("2026-06-03", [first, second])
        store.record_paper_order("2026-06-03", first)
        store.record_paper_order("2026-06-03", second)
        orders = store.open_paper_orders(5)
        assert len(orders) == 2
        for cycle in range(3):
            for order in orders:
                store.record_monitor_snapshot(
                    order,
                    side="YES",
                    action="HOLD",
                    reason="inside exit bands",
                    market_status="active",
                    live_bid=0.60 + cycle * 0.01,
                    exit_fee_per_contract=0.02,
                    net_exit_per_contract=0.58,
                    unrealized_pnl=2.70,
                    unrealized_roi=0.87,
                )

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        monitor_rows = [
            row
            for row in payload["paper_trading"]["recent_monitor_actions"]
            if row.get("unrealized")
        ]
        ids = [row["id"] for row in monitor_rows]
        # 6 snapshots (2 orders x 3 cycles) dedup to one inspection per order.
        assert len(monitor_rows) == 2
        assert len(ids) == len(set(ids))
        assert all(row["status"] == "HOLD" for row in monitor_rows)


def _settlement_alerts(target_date: str, *, now: datetime):
    """Run _strategy_alerts against a single unresolved past target on an
    injected clock, isolating the settlement branch (no open positions)."""
    return _strategy_alerts(
        paper={
            "available": True,
            "summary": {
                "open_positions": 0,
                "unresolved_past_targets": [
                    {"target_date": target_date, "open_orders": 1}
                ],
            },
            "duplicate_open_groups": [],
        },
        entry_block_reason=None,
        now=now,
    )


def test_settlement_alert_is_a_benign_warning_during_normal_overnight_lag():
    """A target that was yesterday is in normal settlement lag (the official
    CLISFO high publishes the morning after), so it must read as a 'pending'
    warning, not a CRITICAL 'backlog' false alarm."""
    # 12:00 UTC == 04:00 fixed-PST, so settlement_today == 2026-06-12.
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)

    alerts = _settlement_alerts("2026-06-11", now=now)
    by_code = {alert["code"]: alert for alert in alerts}

    assert "settlement-pending" in by_code
    assert "settlement-backlog" not in by_code
    assert by_code["settlement-pending"]["level"] == "warning"


def test_settlement_alert_escalates_to_critical_when_two_days_stale():
    """A target >= 2 days past settlement means the settlement-high lookup
    genuinely failed; that is a real CRITICAL backlog."""
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)

    alerts = _settlement_alerts("2026-06-09", now=now)
    by_code = {alert["code"]: alert for alert in alerts}

    assert "settlement-backlog" in by_code
    assert "settlement-pending" not in by_code
    assert by_code["settlement-backlog"]["level"] == "critical"
    assert "3 days" in by_code["settlement-backlog"]["detail"]
