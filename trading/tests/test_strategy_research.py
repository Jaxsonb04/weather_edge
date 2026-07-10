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
from sfo_kalshi_quant.models import EventSnapshot, ForecastSnapshot, IntradaySnapshot, TradeDecision
from sfo_kalshi_quant.config import SFO_TZ, StrategyConfig
from sfo_kalshi_quant.exits import convergence_take_profit_net, exit_bid_for_net, net_exit_per_contract
from sfo_kalshi_quant.strategy_research import (
    _dataset_research_summary,
    _entry_block_reason,
    _forecast_health_payload,
    _live_frequency_tuning_payload,
    _market_consensus_payload,
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
            CREATE TABLE cli_settlements (
                station_id TEXT,
                local_date TEXT,
                max_temperature_f REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO cli_settlements VALUES ('KSFO', ?, ?)",
            (target, high),
        )
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


def _no_favorite_decision() -> TradeDecision:
    return TradeDecision(
        ticker="KXHIGHTSFO-TEST-B71.5",
        label="71 to 72",
        action="BUY_NO",
        approved=True,
        probability=0.97,
        probability_lcb=0.91,
        yes_bid=0.12,
        yes_ask=0.14,
        spread=0.03,
        fee_per_contract=0.01,
        cost_per_contract=0.87,
        edge=0.11,
        edge_lcb=0.05,
        kelly_fraction=0.02,
        recommended_contracts=5.0,
        expected_profit=0.55,
        reasons=[],
        side="NO",
        entry_bid=0.84,
        entry_ask=0.86,
        entry_bid_size=40.0,
        entry_ask_size=40.0,
        trade_quality_score=81.0,
        strike_type="between",
        floor_strike=71.0,
        cap_strike=72.0,
        model_probability=0.97,
        market_probability=0.86,
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


def test_strategy_research_exposes_compact_learning_diagnostics():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        _write_settlement(root)

        store = PaperStore(db_path)
        decision = _approved_decision()
        forecast = ForecastSnapshot(
            target_date=date(2026, 6, 3),
            predicted_high_f=66.0,
            fetched_at="2026-06-03T12:00:00+00:00",
            lead_hours=8.0,
            method="weatheredge-blend",
            google_high_f=66.0,
            nws_high_f=67.0,
            open_meteo_high_f=65.5,
            history_high_f=64.0,
            source_count=4,
            raw={"marine_layer_index": 0.6},
        )
        event = pre_resolution_event([decision])
        forecast_snapshot_id = store.record_forecast(forecast)
        market_snapshot_id = store.record_market(event)
        store.record_decisions(
            "2026-06-03",
            [decision],
            forecast=forecast,
            event=event,
            risk_profile="research",
            bankroll=1000.0,
            strategy_config=StrategyConfig(),
            forecast_snapshot_id=forecast_snapshot_id,
            market_snapshot_id=market_snapshot_id,
        )
        store.record_paper_order(
            "2026-06-03",
            decision,
            risk_profile="research",
            strategy_config=StrategyConfig(),
        )
        store.settle_paper_orders("2026-06-03", 67.0)

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        profiles = {row["risk_profile"]: row for row in payload["profiles"]}
        candidate = profiles["research"]["signal_quality"]["latest_candidates"][0]
        assert candidate["diagnostics_available"] is True
        assert candidate["forecast_snapshot_id"] == forecast_snapshot_id
        assert candidate["market_snapshot_id"] == market_snapshot_id

        closed = profiles["research"]["paper_trading"]["closed_positions"][0]
        assert closed["diagnostics_available"] is True
        assert closed["entry_decision_snapshot_id"] is not None
        assert closed["outcome_reason"] == "YES position won because the market resolved YES."
        assert closed["forecast_error_f"] == 1.0


def test_strategy_research_summary_win_loss_counts_use_full_book():
    """Summary win/loss counts must match the aggregate book, not the latest
    30 published closed-position rows."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        _write_settlement(root)
        PaperStore(db_path)

        with sqlite3.connect(db_path) as conn:
            for idx in range(35):
                won = idx >= 5
                created_at = f"2026-06-{1 + idx // 30:02d}T00:{idx % 60:02d}:00+00:00"
                conn.execute(
                    """
                    INSERT INTO paper_orders (
                        created_at, target_date, market_ticker, label, action, risk_profile,
                        side, contracts, yes_ask, entry_price, entry_bid, entry_ask_size,
                        strike_type, floor_strike, cap_strike, fee_per_contract,
                        cost_per_contract, probability, probability_lcb, edge, edge_lcb,
                        trade_quality_score, expected_profit, status, reasons_json,
                        settled_at, settlement_high_f, resolved_yes, realized_pnl
                    )
                    VALUES (?, '2026-06-03', ?, '66 to 67', 'BUY_YES', 'live',
                            'YES', 1.0, 0.30, 0.30, 0.20, 40.0,
                            'between', 66.0, 67.0, 0.01,
                            0.31, 0.70, 0.60, 0.39, 0.29,
                            72.0, 0.39, 'PAPER_SETTLED', '[]',
                            ?, 67.0, ?, ?)
                    """,
                    (
                        created_at,
                        f"KXHIGHTSFO-TEST-{idx:02d}",
                        created_at,
                        1 if won else 0,
                        0.69 if won else -0.31,
                    ),
                )

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        summary = payload["paper_trading"]["summary"]
        assert summary["closed_positions"] == 35
        assert summary["win_count"] == 30
        assert summary["loss_count"] == 5
        assert len(payload["paper_trading"]["closed_positions"]) == 30
        diagnostics = payload["paper_trading"]["diagnostics"]
        assert diagnostics["by_side"]["YES"]["resolved"] == 35
        assert diagnostics["by_exit_reason"]["held_to_settlement"]["losses"] == 5
        assert diagnostics["worst_segments"][0]["exit_reason"] == "held_to_settlement"


def test_forecast_health_surfaces_healthy_nwp_emos_and_clisfo():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        root.mkdir(parents=True, exist_ok=True)
        now = datetime(2026, 6, 27, 16, 0, tzinfo=UTC)
        targets = ["2026-06-27", "2026-06-28", "2026-06-29"]
        fetched_at = "2026-06-27T15:00:00+00:00"
        with sqlite3.connect(root / "weather.db") as conn:
            conn.execute(
                """
                CREATE TABLE nwp_model_forecasts (
                    target_date TEXT,
                    model TEXT,
                    lead_days INTEGER,
                    predicted_high_f REAL,
                    fetched_at TEXT,
                    source TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE forecast_emos_daily_high (
                    target_date TEXT,
                    lead_days INTEGER,
                    predicted_high_f REAL,
                    sigma_f REAL,
                    n_models INTEGER,
                    fetched_at TEXT,
                    method TEXT,
                    source TEXT,
                    actual_high_f REAL
                )
                """
            )
            conn.execute(
                "CREATE TABLE clisfo_settlements "
                "(local_date TEXT PRIMARY KEY, max_temperature_f INTEGER, fetched_at TEXT, source TEXT)"
            )
            conn.execute(
                "CREATE TABLE nws_daily_high_ground_truth "
                "(station_id TEXT, local_date TEXT, high_f REAL, is_complete INTEGER)"
            )
            for target in targets:
                for model_idx in range(6):
                    conn.execute(
                        "INSERT INTO nwp_model_forecasts VALUES (?, ?, 1, ?, ?, 'test')",
                        (target, f"model_{model_idx}", 68.0 + model_idx / 10, fetched_at),
                    )
                conn.execute(
                    """
                    INSERT INTO forecast_emos_daily_high
                    VALUES (?, 1, 69.0, 2.5, 6, ?, 'emos_wmean', 'live', NULL)
                    """,
                    (target, fetched_at),
                )
            conn.execute(
                "INSERT INTO forecast_emos_daily_high "
                "VALUES ('2026-06-20', 1, 68.0, 2.5, 6, ?, 'emos_wmean', 'rolling_origin', 68.0)",
                (fetched_at,),
            )
            conn.execute(
                "INSERT INTO clisfo_settlements VALUES ('2026-06-26', 68, ?, 'CLISFO')",
                (fetched_at,),
            )
            conn.execute(
                "INSERT INTO nws_daily_high_ground_truth VALUES ('KSFO', '2026-06-27', 69.0, 1)"
            )

        payload = _forecast_health_payload(
            root,
            config=StrategyConfig(emos_distribution_enabled=True),
            now=now,
        )

        assert payload["available"] is True
        assert payload["warnings"] == []
        assert payload["nwp"]["targets"][0]["model_count"] == 6
        assert payload["emos"]["live_targets"][0]["method"] == "emos_wmean"
        assert payload["clisfo"]["lag_days"] == 1


def test_strategy_research_splits_day_ahead_and_intraday_lead_modes():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        _write_settlement(root)
        store = PaperStore(db_path)
        day_ahead = _approved_decision()
        intraday = replace(day_ahead, ticker="KXHIGHTSFO-TEST-B67.5", label="67 to 68")

        store.record_decisions(
            "2026-06-04",
            [day_ahead],
            forecast=ForecastSnapshot(
                target_date=date(2026, 6, 4),
                predicted_high_f=67.0,
                lead_hours=24.0,
                method="weighted blend",
            ),
            event=pre_resolution_event([day_ahead]),
        )
        store.record_decisions(
            "2026-06-03",
            [intraday],
            forecast=ForecastSnapshot(
                target_date=date(2026, 6, 3),
                predicted_high_f=68.0,
                lead_hours=0.5,
                method="weighted blend + intraday high-so-far update",
            ),
            intraday=IntradaySnapshot(
                target_date=date(2026, 6, 3),
                observed_high_f=67.0,
                latest_temp_f=66.0,
                latest_observed_at="2026-06-03T20:00:00+00:00",
                remaining_forecast_high_f=68.0,
                forecast_fetched_at="2026-06-03T18:00:00+00:00",
                observed_high_source="nws_station_observations",
            ),
            event=pre_resolution_event([intraday]),
        )

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        counts = payload["backtest_summary"]["lead_mode_counts"]
        assert counts["day_ahead"]["total"] == 1
        assert counts["intraday_high_so_far"]["total"] == 1
        lead_modes = {
            row["ticker"]: row["lead_mode"]
            for row in payload["signal_quality"]["latest_candidates"]
        }
        assert lead_modes[day_ahead.ticker] == "day_ahead"
        assert lead_modes[intraday.ticker] == "intraday_high_so_far"


def test_strategy_research_exit_targets_match_edge_based_monitor_logic():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)

        store = PaperStore(db_path)
        decision = _no_favorite_decision()
        store.record_decisions("2026-06-20", [decision])
        order_id = store.record_paper_order("2026-06-20", decision)
        assert order_id is not None
        order = store.paper_order(order_id)
        assert order is not None
        contracts = float(order["contracts"])
        live_bid = 0.94
        net_exit = net_exit_per_contract(live_bid, contracts)
        store.record_monitor_snapshot(
            order,
            side="NO",
            action="HOLD",
            reason="inside exit bands",
            market_status="active",
            live_bid=live_bid,
            exit_fee_per_contract=live_bid - net_exit,
            net_exit_per_contract=net_exit,
            unrealized_pnl=contracts * (net_exit - float(order["cost_per_contract"])),
            unrealized_roi=(net_exit - float(order["cost_per_contract"])) / float(order["cost_per_contract"]),
        )

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        position = payload["paper_trading"]["open_positions"][0]
        expected_net = convergence_take_profit_net(decision.model_probability)
        assert position["take_profit_basis"] == "model_fair_value"
        assert position["take_profit_net_exit"] == expected_net
        assert position["take_profit_bid"] == exit_bid_for_net(expected_net, contracts)
        assert position["monitor_action"] == "HOLD"


def test_strategy_research_mirrors_research_no_settlement_first_hold():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)

        store = PaperStore(db_path)
        decision = _no_favorite_decision()
        store.record_decisions("2026-06-20", [decision])
        order_id = store.record_paper_order("2026-06-20", decision, risk_profile="research")
        assert order_id is not None
        order = store.paper_order(order_id)
        assert order is not None
        contracts = float(order["contracts"])
        live_bid = 0.99
        net_exit = net_exit_per_contract(live_bid, contracts)
        store.record_monitor_snapshot(
            order,
            side="NO",
            action="HOLD",
            reason="inside exit bands",
            market_status="active",
            live_bid=live_bid,
            exit_fee_per_contract=live_bid - net_exit,
            net_exit_per_contract=net_exit,
            unrealized_pnl=contracts * (net_exit - float(order["cost_per_contract"])),
            unrealized_roi=(net_exit - float(order["cost_per_contract"])) / float(order["cost_per_contract"]),
        )

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        position = payload["paper_trading"]["open_positions"][0]
        assert position["risk_profile"] == "research"
        assert position["monitor_action"] == "HOLD_SETTLEMENT_FIRST"
        assert "settlement-first" in position["exit_rule_reason"]


def test_signal_quality_prefers_newest_target_before_old_approved_candidates():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)

        store = PaperStore(db_path)
        old_approved = replace(
            _approved_decision(),
            ticker="KXHIGHTSFO-26JUN18-B66.5",
            trade_quality_score=95.0,
        )
        current_blocked = replace(
            _approved_decision(),
            ticker="KXHIGHTSFO-26JUN20-B68.5",
            label="68 to 69",
            approved=False,
            trade_quality_score=20.0,
            reasons=["research paused: daily loss cap reached"],
        )
        store.record_decisions("2026-06-18", [old_approved])
        store.record_decisions("2026-06-20", [current_blocked])

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        candidates = payload["signal_quality"]["latest_candidates"]
        assert candidates[0]["target_date"] == "2026-06-20"
        assert candidates[0]["approved"] is False
        assert payload["status"]["latest_target_date"] == "2026-06-20"


def test_profile_signal_quality_keeps_live_rows_when_research_scans_later():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)

        store = PaperStore(db_path)
        live_rows = [
            replace(
                _approved_decision(),
                ticker=f"KXHIGHTSFO-26JUN20-LIVE-{idx}",
                label=f"live {idx}",
                trade_quality_score=90.0 - idx,
            )
            for idx in range(2)
        ]
        research_rows = [
            replace(
                _approved_decision(),
                ticker=f"KXHIGHTSFO-26JUN20-RESEARCH-{idx}",
                label=f"research {idx}",
                trade_quality_score=80.0 - idx,
            )
            for idx in range(30)
        ]
        store.record_decisions("2026-06-20", live_rows, risk_profile="live")
        store.record_decisions("2026-06-20", research_rows, risk_profile="research")

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        profiles = {row["risk_profile"]: row for row in payload["profiles"]}
        live_candidates = profiles["live"]["signal_quality"]["latest_candidates"]
        research_candidates = profiles["research"]["signal_quality"]["latest_candidates"]
        assert {row["ticker"] for row in live_candidates} == {
            "KXHIGHTSFO-26JUN20-LIVE-0",
            "KXHIGHTSFO-26JUN20-LIVE-1",
        }
        assert len(research_candidates) == 24
        assert set(payload["signal_quality"]["latest_candidates_by_profile"]) >= {
            "live",
            "research",
        }


def test_profile_gate_behavior_includes_profile_scoped_rejections_and_entry_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)

        store = PaperStore(db_path)
        live_rejected = replace(
            _approved_decision(),
            ticker="KXHIGHTSFO-26JUN20-LIVE-BLOCKED",
            approved=False,
            recommended_contracts=0.0,
            expected_profit=0.0,
            reasons=[
                "forecast source spread 8.4F exceeds max 7.0F; point blend is unreliable",
                "edge -0.010 below min 0.020",
            ],
        )
        research_paused = replace(
            _approved_decision(),
            ticker="KXHIGHTSFO-26JUN20-RESEARCH-PAUSED",
            approved=False,
            signal_approved=True,
            entry_block_reason="research paused: daily loss cap reached",
            recommended_contracts=0.0,
            expected_profit=0.0,
            reasons=[
                "research paused: daily loss cap reached",
                "portfolio PF-test: sleeve=no_core, growth=0.001000",
            ],
        )
        store.record_decisions("2026-06-20", [live_rejected], risk_profile="live")
        store.record_decisions("2026-06-20", [research_paused], risk_profile="research")

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        profiles = {row["risk_profile"]: row for row in payload["profiles"]}
        live_gate = profiles["live"]["daily_summary"]["gate_behavior"]
        research_gate = profiles["research"]["daily_summary"]["gate_behavior"]
        assert live_gate["top_rejections"][0]["reason"] == "source spread"
        assert live_gate["rejection_categories"]["no_data"] == 1
        assert research_gate["entry_block_reasons"] == [
            {"reason": "research paused: daily loss cap reached", "count": 1}
        ]
        candidate = profiles["research"]["signal_quality"]["latest_candidates"][0]
        assert candidate["signal_approved"] is True
        assert candidate["approved"] is False
        assert candidate["entry_block_reason"] == "research paused: daily loss cap reached"


def test_live_frequency_tuning_report_does_not_loosen_guardrails_without_evidence():
    live_config = StrategyConfig(min_edge_lcb=0.0, blocked_forecast_cohorts=("warm",))
    report = _live_frequency_tuning_payload(
        {
            "available": True,
            "by_profile": {
                "live": {
                    "counts": {
                        "approved_under_candidate_config": 0,
                        "considered": 12,
                        "independent_days": 4,
                    }
                }
            },
        },
        live_config,
    )

    assert report["available"] is True
    assert report["status"] == "BELOW_TARGET_COLLECT_ONLY"
    assert report["safe_config_change"] is None
    assert report["guardrails"]["min_edge_lcb"] == 0.0
    assert report["guardrails"]["blocked_forecast_cohorts"] == ["warm"]


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
        # The open-position guard index now blocks a duplicate at the DB layer.
        # The duplicate-open alert is the backstop for a legacy book that predates
        # the index (or one where it could not be built), so drop it to reproduce
        # that state and prove the alert still fires.
        with store.connect() as conn:
            conn.execute("DROP INDEX IF EXISTS ux_paper_orders_open_market_side_profile")
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
        store.record_paper_order("2026-06-03", decision, risk_profile="live")
        store.record_paper_order("2026-06-03", decision, risk_profile="research")

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
        # Reproduce a legacy book carrying a research duplicate (see the
        # alert-backstop note above); the guard index would otherwise reject the
        # second research insert.
        with store.connect() as conn:
            conn.execute("DROP INDEX IF EXISTS ux_paper_orders_open_market_side_profile")
        balanced = replace(_approved_decision(), ticker="KXHIGHTSFO-TEST-B65.5")
        fast = _approved_decision()
        store.record_paper_order(target, balanced, risk_profile="live")
        store.record_paper_order(target, fast, risk_profile="research")
        store.record_paper_order(target, fast, risk_profile="research")

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        profiles = {row["risk_profile"]: row for row in payload["profiles"]}
        balanced_alerts = {
            alert["code"] for alert in profiles["live"]["status"]["alerts"]
        }
        fast_alerts = {
            alert["code"] for alert in profiles["research"]["status"]["alerts"]
        }

        assert "duplicate-open-markets" in {
            alert["code"] for alert in payload["status"]["alerts"]
        }
        assert "duplicate-open-markets" not in balanced_alerts
        assert "duplicate-open-markets" in fast_alerts
        assert profiles["research"]["paper_trading"]["summary"]["duplicate_open_groups"] == 1


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

        store.record_decisions(today, [balanced_win], risk_profile="live")
        store.record_decisions(tomorrow, [fast_open], risk_profile="research")
        store.record_paper_order(today, balanced_win, risk_profile="live")
        store.record_paper_order(today, fast_loss, risk_profile="research")
        store.settle_paper_orders(today, 67.0)
        open_order_id = store.record_paper_order(
            tomorrow,
            fast_open,
            risk_profile="research",
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

        assert payload["default_profile"] == "live"
        profiles = {row["risk_profile"]: row for row in payload["profiles"]}
        assert set(profiles) == {"live", "research"}

        balanced = profiles["live"]
        fast = profiles["research"]
        assert balanced["profile_type"] == "primary"
        assert fast["profile_type"] == "experimental"

        assert balanced["daily_summary"]["totals"]["realized_pnl"] > 0
        assert balanced["daily_summary"]["totals"]["losses"] == 0
        assert balanced["paper_trading"]["summary"]["open_risk"] == 0.0
        assert {
            row["risk_profile"]
            for row in balanced["paper_trading"]["recent_monitor_actions"]
        } == {"live"}
        assert {
            row["risk_profile"]
            for row in balanced["signal_quality"]["latest_candidates"]
        } == {"live"}

        assert fast["daily_summary"]["totals"]["realized_pnl"] < 0
        assert fast["daily_summary"]["totals"]["wins"] == 0
        assert fast["paper_trading"]["summary"]["open_risk"] > 0
        assert {
            row["risk_profile"]
            for row in fast["paper_trading"]["recent_monitor_actions"]
        } == {"research"}
        assert {
            row["status"]
            for row in fast["paper_trading"]["recent_monitor_actions"]
        } >= {"OPEN", "HOLD"}
        assert {
            row["risk_profile"]
            for row in fast["signal_quality"]["latest_candidates"]
        } == {"research"}
        assert any("research" in note for note in fast["learnings"])

        # Profiles are P&L attribution slices of one account, never independent
        # $1,000 equity accounts.
        b_summary = balanced["daily_summary"]
        f_summary = fast["daily_summary"]
        assert "current_equity" not in b_summary
        assert "starting_bankroll" not in b_summary
        assert b_summary["current_attributed_pnl"] > 0
        assert f_summary["current_attributed_pnl"] < 0
        assert payload["accounting"]["profile_attributed_pnl"] == round(
            b_summary["current_attributed_pnl"] + f_summary["current_attributed_pnl"], 2
        )
        # Profile-scoped side split + exit reasons render on the profile tab.
        assert b_summary["side_performance"]["YES"]["wins"] == 1
        assert f_summary["side_performance"]["YES"]["losses"] == 1
        assert b_summary["exit_reasons"]["held_to_settlement"] == 1


def test_accounting_and_equity_curve_reconcile_all_time_pnl_before_window():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        store = PaperStore(db_path)
        old_id = store.record_paper_order("2026-06-20", _approved_decision(), risk_profile="research")
        recent_id = store.record_paper_order("2026-07-09", _approved_decision(), risk_profile="research")
        now = datetime.now(UTC)
        old = (now - timedelta(days=10)).isoformat()
        recent = now.isoformat()
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET status='PAPER_SETTLED', realized_pnl=-38.12, "
                "settled_at=?, created_at=? WHERE id=?",
                (old, old, old_id),
            )
            conn.execute(
                "UPDATE paper_orders SET status='PAPER_SETTLED', realized_pnl=-1.34, "
                "settled_at=?, created_at=? WHERE id=?",
                (recent, recent, recent_id),
            )

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        accounting = payload["accounting"]
        assert accounting["all_time_realized_pnl"] == -39.46
        assert accounting["window_realized_pnl"] == -1.34
        assert accounting["realized_equity"] == 960.54
        assert accounting["reconciliation_status"] == "reconciled"
        curve = payload["daily_summary"]["days"]
        assert curve[0]["opening_equity"] == 961.88
        assert curve[-1]["closing_equity"] == 960.54
        assert curve[-1]["cumulative_realized"] == -39.46
        research = next(row for row in payload["profiles"] if row["risk_profile"] == "research")
        assert research["daily_summary"]["days"][-1]["cumulative_realized"] == -39.46


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
        assert set(rescore["by_profile"]) == {"live", "research"}
        for result in rescore["by_profile"].values():
            assert {"counts", "candidate", "recorded_config_own_book"} <= set(result)
            # per_day is trimmed from the published artifact to keep it lean.
            assert "per_day" not in result

        # The real-money readiness gauge is derived for the LIVE profile only and
        # exposes a percentage + per-check breakdown for the dashboard.
        readiness = payload["real_money_readiness"]
        assert readiness["available"] is True, readiness.get("reason")
        assert readiness["profile"] == "live"
        assert readiness["status"] == "REPLAY_REQUIRED"
        assert readiness["verdict"] == "REPLAY REQUIRED"
        assert readiness["status_reasons"]
        assert 0.0 <= readiness["readiness_pct"] <= 100.0
        assert readiness["ready"] is False  # a one-day fixture cannot be ready
        assert readiness["checks"] and all("progress" in c for c in readiness["checks"])


def test_strategy_research_includes_research_shadow_comparison():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        _write_settlement(root, target="2026-06-03", high=70.0)

        store = PaperStore(db_path)
        decision = replace(
            _approved_decision(),
            action="BUY_NO",
            side="NO",
            probability=0.78,
            probability_lcb=0.58,
            edge=0.16,
            edge_lcb=-0.04,
            reasons=["portfolio PF-test: sleeve=research_explore, growth=0.001"],
            entry_bid=0.36,
            entry_ask=0.40,
            entry_bid_size=10.0,
            entry_ask_size=10.0,
        )
        store.record_research_shadow_order(
            "2026-06-03",
            decision,
            risk_profile="research",
            sample_probability=0.25,
            sampled=False,
        )

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        shadow = payload["research_shadow"]
        assert shadow["available"] is True, shadow.get("reason")
        assert shadow["summary"]["shadow_orders"] == 1
        assert shadow["paper_executed"]["trades"] == 0
        assert shadow["shadow_hold_to_settlement"]["trades"] == 1
        assert shadow["shadow_current_exit_policy"]["trades"] == 0


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


def _kalshi_ladder_event(event_ticker: str = "KXHIGHTSFO-26JUN03") -> EventSnapshot:
    """A realistic two-sided Kalshi event payload peaking on the 66-67 bin.

    Built as the raw ``with_nested_markets`` body that the public client returns
    so it round-trips through ``record_market`` -> ``latest_market_snapshot``.
    The event ticker is date-encoded (as production tickers are) so the stored
    snapshot's ``target_date`` column resolves to 2026-06-03 — the column the
    accessor reads. The decision tickers stay ``KXHIGHTSFO-TEST-*`` to match the
    shared ``_approved_decision`` fixture's model-probability join.
    """

    def market(label, strike_type, floor, cap, yes_bid, yes_ask):
        return {
            "ticker": f"KXHIGHTSFO-TEST-{label}",
            "event_ticker": event_ticker,
            "title": "",
            "yes_sub_title": label,
            "strike_type": strike_type,
            "floor_strike": floor,
            "cap_strike": cap,
            "yes_bid_dollars": yes_bid,
            "yes_ask_dollars": yes_ask,
            "no_bid_dollars": round(1.0 - yes_ask, 2),
            "no_ask_dollars": round(1.0 - yes_bid, 2),
            "yes_bid_size_fp": 150,
            "yes_ask_size_fp": 150,
            "status": "active",
        }

    payload = {
        "event_ticker": event_ticker,
        "title": "SFO daily high",
        "markets": [
            market("T63", "less", 63, 63, 0.01, 0.03),
            market("B64.5", "between", 64, 65, 0.10, 0.13),
            market("B66.5", "between", 66, 67, 0.43, 0.46),
            market("B68.5", "between", 68, 69, 0.22, 0.25),
            market("G70", "greater", 70, 70, 0.04, 0.06),
        ],
    }
    return EventSnapshot.from_kalshi(payload)


def _forecast_snapshot(target: str = "2026-06-03", high: float = 66.5) -> ForecastSnapshot:
    return ForecastSnapshot(
        target_date=date.fromisoformat(target),
        predicted_high_f=high,
        fetched_at="2026-06-02T18:00:00+00:00",
        source_count=4,
    )


def test_market_consensus_payload_reconstructs_from_stored_ladder():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "trading" / "paper.db"
        store = PaperStore(db_path)
        decision = _approved_decision()  # ticker KXHIGHTSFO-TEST-B66.5
        store.record_market(_kalshi_ladder_event())
        store.record_decisions(
            "2026-06-03",
            [decision],
            event=pre_resolution_event([decision]),
            forecast=_forecast_snapshot(),
        )

        payload = _market_consensus_payload(db_path)

        assert payload["available"] is True
        assert payload["target_date"] == "2026-06-03"
        # Model high comes from the decision snapshot's forecast_predicted_high_f.
        assert payload["model_high_f"] == 66.5
        # Market consensus is distilled from the de-vigged ladder; the gap is the
        # signed model-minus-market high.
        assert payload["implied_high_f"] is not None
        assert (
            round(payload["model_high_f"] - payload["implied_high_f"], 2)
            == payload["model_minus_market_f"]
        )
        # Mirrors report.consensus_to_dict: a per-bin distribution overlaying the
        # de-vigged market probability against the stored model probability.
        assert payload["modal_bin_label"] == "B66.5"
        assert payload["distribution"], "distribution should not be empty"
        modal_bin = next(
            row for row in payload["distribution"] if row["label"] == "B66.5"
        )
        assert modal_bin["implied_probability"] > 0
        assert modal_bin["model_probability"] == 0.70  # the recorded model prob


def test_market_consensus_unavailable_without_stored_ladder():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "trading" / "paper.db"
        store = PaperStore(db_path)
        decision = _approved_decision()
        # Decisions recorded, but no market_snapshot ever stored.
        store.record_decisions(
            "2026-06-03", [decision], event=pre_resolution_event([decision])
        )

        payload = _market_consensus_payload(db_path)

        assert payload == {"available": False}


def test_market_consensus_unavailable_when_db_missing():
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "missing" / "paper.db"
        assert _market_consensus_payload(missing) == {"available": False}


def test_latest_market_snapshot_roundtrips_the_ladder():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "trading" / "paper.db"
        store = PaperStore(db_path)
        store.record_market(_kalshi_ladder_event())

        event = store.latest_market_snapshot("2026-06-03")
        assert event is not None
        labels = {market.yes_sub_title for market in event.markets}
        assert labels == {"T63", "B64.5", "B66.5", "B68.5", "G70"}
        # Bid/ask survive the round-trip so the consensus de-vig is meaningful.
        modal = next(m for m in event.markets if m.yes_sub_title == "B66.5")
        assert modal.yes_bid == 0.43 and modal.yes_ask == 0.46

        assert store.latest_market_snapshot("2099-01-01") is None


def test_strategy_research_surfaces_market_consensus_in_signal_quality():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        db_path = Path(tmp) / "trading" / "paper.db"
        _write_lstm_fixture(root)
        _write_settlement(root)

        store = PaperStore(db_path)
        decision = _approved_decision()
        store.record_market(_kalshi_ladder_event())
        store.record_decisions(
            "2026-06-03",
            [decision],
            event=pre_resolution_event([decision]),
            forecast=_forecast_snapshot(),
        )

        payload = build_strategy_research(
            forecaster_root=root,
            db_path=db_path,
            calibration_min_train=40,
        )

        consensus = payload["signal_quality"]["market_consensus"]
        assert consensus["available"] is True
        assert consensus["modal_bin_label"] == "B66.5"
        assert consensus["distribution"]
