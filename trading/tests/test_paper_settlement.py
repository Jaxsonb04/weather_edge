import io
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
from datetime import date
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sfo_kalshi_quant.arbitrage import build_arbitrage_opportunities
from sfo_kalshi_quant.cli import main
from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import ForecastSnapshot, IntradaySnapshot, MarketBin, TradeDecision
from sfo_kalshi_quant.paper import PaperTrader

from support import pre_resolution_event


def test_settle_paper_orders_computes_realized_pnl():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B66.5",
            label="66° to 67°",
            action="BUY_YES",
            approved=True,
            probability=0.30,
            probability_lcb=0.20,
            yes_bid=0.02,
            yes_ask=0.03,
            spread=0.01,
            fee_per_contract=0.01,
            cost_per_contract=0.04,
            edge=0.26,
            edge_lcb=0.16,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=2.6,
            reasons=[],
        )
        store.record_paper_order("2026-06-03", decision)
        assert store.settle_paper_orders("2026-06-03", 67) == 1
        summary = store.market_backtest_summary()
        assert summary["orders"] == 1
        assert round(summary["realized_pnl"], 2) == 9.67


def test_recorded_decisions_backtest_against_settlements():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        approved = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B66.5",
            label="66° to 67°",
            action="BUY_YES",
            approved=True,
            probability=0.70,
            probability_lcb=0.60,
            yes_bid=0.20,
            yes_ask=0.30,
            spread=0.10,
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
        )
        rejected = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B68.5",
            label="68° to 69°",
            action="BUY_NO",
            approved=False,
            probability=0.55,
            probability_lcb=0.40,
            yes_bid=0.45,
            yes_ask=0.50,
            spread=0.05,
            fee_per_contract=0.02,
            cost_per_contract=0.52,
            edge=0.03,
            edge_lcb=-0.12,
            kelly_fraction=0.0,
            recommended_contracts=0.0,
            expected_profit=0.0,
            reasons=["lower-bound edge below min"],
            side="NO",
            entry_bid=0.50,
            entry_ask=0.55,
            trade_quality_score=38.0,
            strike_type="between",
            floor_strike=68.0,
            cap_strike=69.0,
        )

        store.record_decisions(
            "2026-06-03",
            [approved, rejected],
            event=pre_resolution_event([approved, rejected]),
        )
        summary = store.signal_backtest_summary({"2026-06-03": 67.0})

        assert summary["signals"] == 2.0
        assert summary["settled_signals"] == 2.0
        assert summary["approved_signals"] == 1.0
        assert summary["approved_paper_pnl"] > 0.0
        assert summary["approved_hit_rate"] == 1.0
        assert len(summary["quality_buckets"]) == 2


def test_signal_backtest_dedupes_repeated_scans_by_default():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        first = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B66.5",
            label="66° to 67°",
            action="BUY_YES",
            approved=False,
            probability=0.30,
            probability_lcb=0.20,
            yes_bid=0.20,
            yes_ask=0.30,
            spread=0.10,
            fee_per_contract=0.01,
            cost_per_contract=0.31,
            edge=-0.01,
            edge_lcb=-0.11,
            kelly_fraction=0.0,
            recommended_contracts=0.0,
            expected_profit=0.0,
            reasons=["first scan"],
            trade_quality_score=20.0,
            strike_type="between",
            floor_strike=66.0,
            cap_strike=67.0,
        )
        latest = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B66.5",
            label="66° to 67°",
            action="BUY_YES",
            approved=True,
            probability=0.70,
            probability_lcb=0.60,
            yes_bid=0.20,
            yes_ask=0.30,
            spread=0.10,
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
        )

        store.record_decisions("2026-06-03", [first], event=pre_resolution_event([first]))
        store.record_decisions("2026-06-03", [latest], event=pre_resolution_event([latest]))

        summary = store.signal_backtest_summary({"2026-06-03": 67.0})
        all_rows = store.signal_backtest_summary({"2026-06-03": 67.0}, sample_mode="all")

        assert summary["raw_signals"] == 2.0
        assert summary["signals"] == 1.0
        assert summary["approved_signals"] == 1.0
        assert summary["approved_hit_rate"] == 1.0
        assert all_rows["signals"] == 2.0


def test_signal_backtest_excludes_post_resolution_rows_by_default():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B66.5",
            label="66° to 67°",
            action="BUY_YES",
            approved=True,
            probability=0.70,
            probability_lcb=0.60,
            yes_bid=0.20,
            yes_ask=0.30,
            spread=0.10,
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
        )
        forecast = ForecastSnapshot(
            target_date=date(2026, 6, 3),
            predicted_high_f=67.0,
            fetched_at="2026-06-03T23:00:00+00:00",
            raw={"observed_high_decision": {"mode": "lock"}},
        )
        intraday = IntradaySnapshot(
            target_date=date(2026, 6, 3),
            observed_high_f=67.0,
            latest_temp_f=67.0,
            latest_observed_at="2026-06-03T23:00:00+00:00",
            remaining_forecast_high_f=None,
            forecast_fetched_at="2026-06-03T23:00:00+00:00",
            is_complete=True,
        )

        store.record_decisions(
            "2026-06-03",
            [decision],
            forecast=forecast,
            intraday=intraday,
        )

        strict = store.signal_backtest_summary({"2026-06-03": 67.0})
        included = store.signal_backtest_summary(
            {"2026-06-03": 67.0},
            pre_resolution_only=False,
        )

        assert strict["raw_signals"] == 1.0
        assert strict["signals"] == 0.0
        assert strict["excluded_post_resolution_signals"] == 1.0
        assert included["signals"] == 1.0


def test_market_summary_excludes_expired_resting_orders_from_outcomes():
    """A resting limit that expires never deployed capital, so it must NOT count
    as a settled loss. It should be excluded from the order count, the hit-rate
    denominator, and the capital-at-risk ROI denominator. Regression for the
    PAPER_EXPIRED (realized_pnl=0.0) pollution bug."""

    from sfo_kalshi_quant.config import StrategyConfig
    from sfo_kalshi_quant.paper import PaperTrader

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        # A real filled NO favorite that settles as a WIN (high 67 -> 68/69 NO).
        won = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B68.5",
            label="68° to 69°",
            action="BUY_NO",
            side="NO",
            approved=True,
            probability=0.80,
            probability_lcb=0.70,
            yes_bid=0.20,
            yes_ask=0.22,
            spread=0.02,
            fee_per_contract=0.01,
            cost_per_contract=0.24,
            edge=0.56,
            edge_lcb=0.46,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=5.6,
            reasons=[],
            entry_bid=0.76,
            entry_ask=0.23,
        )
        store.record_paper_order("2026-06-03", won)

        # A resting limit order on a different market that never fills and expires
        # at settlement (proven resting config from test_limit_orders).
        limit_trader = PaperTrader(
            store,
            StrategyConfig(limit_price_edge_lcb_buffer=0.02),
            entry_mode="limit",
        )
        resting = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B74.5",
            label="74° to 75°",
            action="BUY_NO",
            side="NO",
            approved=True,
            probability=0.85,
            probability_lcb=0.81,
            yes_bid=0.22,
            yes_ask=0.24,
            spread=0.03,
            fee_per_contract=0.02,
            cost_per_contract=0.77,
            edge=0.05,
            edge_lcb=0.01,
            kelly_fraction=0.01,
            recommended_contracts=2.0,
            expected_profit=0.1,
            reasons=[],
            entry_bid=0.73,
            entry_ask=0.75,
            entry_bid_size=10.0,
            entry_ask_size=10.0,
        )
        assert limit_trader.place_approved("2026-06-03", [resting])

        store.settle_paper_orders("2026-06-03", 67)
        rows = {row["market_ticker"]: row for row in store.paper_orders(10)}
        assert rows["KXHIGHTSFO-TEST-B74.5"]["status"] == "PAPER_EXPIRED"
        assert rows["KXHIGHTSFO-TEST-B68.5"]["status"] == "PAPER_SETTLED"

        summary = store.market_backtest_summary()
        # One real outcome (a clean win); the expired non-fill is excluded from
        # every denominator, so hit-rate is 1.0 and capital is the filled stake
        # only, not diluted by the resting limit's notional.
        won_row = rows["KXHIGHTSFO-TEST-B68.5"]
        expected_capital = float(won_row["contracts"]) * float(won_row["cost_per_contract"])
        assert summary["orders"] == 1
        assert summary["hit_rate"] == 1.0
        assert round(summary["capital_at_risk"], 4) == round(expected_capital, 4)


def test_settle_paper_orders_pays_buy_no_when_bucket_resolves_no():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B68.5",
            label="68° to 69°",
            action="BUY_NO",
            side="NO",
            approved=True,
            probability=0.80,
            probability_lcb=0.70,
            yes_bid=0.20,
            yes_ask=0.22,
            spread=0.02,
            fee_per_contract=0.01,
            cost_per_contract=0.24,
            edge=0.56,
            edge_lcb=0.46,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=5.6,
            reasons=[],
            entry_bid=0.76,
            entry_ask=0.23,
        )
        store.record_paper_order("2026-06-03", decision)
        assert store.settle_paper_orders("2026-06-03", 67) == 1
        summary = store.market_backtest_summary()
        assert summary["orders"] == 1
        assert summary["hit_rate"] == 1.0
        assert round(summary["realized_pnl"], 2) == 7.57


def test_settle_paper_orders_prefers_structured_strikes_over_labels():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B66.5",
            label="80° to 81°",
            action="BUY_YES",
            approved=True,
            probability=0.30,
            probability_lcb=0.20,
            yes_bid=0.02,
            yes_ask=0.03,
            spread=0.01,
            fee_per_contract=0.01,
            cost_per_contract=0.04,
            edge=0.26,
            edge_lcb=0.16,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=2.6,
            reasons=[],
            strike_type="between",
            floor_strike=65.5,
            cap_strike=67.5,
        )
        store.record_paper_order("2026-06-03", decision)
        assert store.settle_paper_orders("2026-06-03", 67) == 1
        row = store.paper_orders(1)[0]
        assert row["resolved_yes"] == 1
        assert row["realized_pnl"] > 0


def test_paper_auto_settle_prefers_clisfo_over_weatheredge_ground_truth():
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        root.mkdir()
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
                VALUES ('KSFO', '2026-06-03', 63, 1)
                """
            )
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B66.5",
            label="66° to 67°",
            action="BUY_YES",
            approved=True,
            probability=0.30,
            probability_lcb=0.20,
            yes_bid=0.02,
            yes_ask=0.03,
            spread=0.01,
            fee_per_contract=0.01,
            cost_per_contract=0.04,
            edge=0.26,
            edge_lcb=0.16,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=2.6,
            reasons=[],
            strike_type="between",
            floor_strike=66.0,
            cap_strike=67.0,
        )
        store.record_paper_order("2026-06-03", decision)

        out = io.StringIO()
        with patch(
            "sfo_kalshi_quant.cli.fetch_recent_cli_settlements",
            lambda site, issuedby, timeout=20: {date(2026, 6, 3): 64},
        ), redirect_stdout(out):
            code = main(
                [
                    "--forecaster-root",
                    str(root),
                    "--db-path",
                    str(db_path),
                    "--no-color",
                    "paper-auto-settle",
                ]
            )

        assert code == 0
        assert "from CLI" in out.getvalue()
        row = store.paper_orders(1)[0]
        assert row["status"] == "PAPER_SETTLED"
        assert row["settlement_high_f"] == 64.0


def test_paper_auto_settle_falls_back_to_weatheredge_ground_truth():
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        root.mkdir()
        with sqlite3.connect(root / "weather.db") as conn:
            # The archived-CLI fallback settles from cli_settlements -- the
            # same instrument Kalshi resolves on -- never from the
            # observation-derived nws_daily_high table (which runs low).
            conn.execute(
                """
                CREATE TABLE cli_settlements (
                    station_id TEXT,
                    local_date TEXT,
                    max_temperature_f INTEGER,
                    fetched_at TEXT,
                    source TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO cli_settlements VALUES ('KSFO', '2026-06-03', 67, 't', 'iem_cli')
                """
            )
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B66.5",
            label="66° to 67°",
            action="BUY_YES",
            approved=True,
            probability=0.30,
            probability_lcb=0.20,
            yes_bid=0.02,
            yes_ask=0.03,
            spread=0.01,
            fee_per_contract=0.01,
            cost_per_contract=0.04,
            edge=0.26,
            edge_lcb=0.16,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=2.6,
            reasons=[],
            strike_type="between",
            floor_strike=66.0,
            cap_strike=67.0,
        )
        store.record_paper_order("2026-06-03", decision)

        out = io.StringIO()
        with patch(
            "sfo_kalshi_quant.cli.fetch_recent_cli_settlements",
            lambda site, issuedby, timeout=20: {},
        ), redirect_stdout(out):
            code = main(
                [
                    "--forecaster-root",
                    str(root),
                    "--db-path",
                    str(db_path),
                    "--no-color",
                    "paper-auto-settle",
                ]
            )

        assert code == 0
        assert "archived CLI truth" in out.getvalue()
        row = store.paper_orders(1)[0]
        assert row["status"] == "PAPER_SETTLED"
        assert row["settlement_high_f"] == 67.0


def test_close_paper_order_computes_exit_pnl():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B68.5",
            label="68° to 69°",
            action="BUY_YES",
            approved=True,
            probability=0.35,
            probability_lcb=0.25,
            yes_bid=0.10,
            yes_ask=0.12,
            spread=0.02,
            fee_per_contract=0.01,
            cost_per_contract=0.13,
            edge=0.22,
            edge_lcb=0.12,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=2.2,
            reasons=[],
        )
        order_id = store.record_paper_order("2026-06-03", decision)
        row = store.close_paper_order(order_id, 0.30)
        assert row["status"] == "PAPER_CLOSED"
        assert row["exit_price"] == 0.30
        assert row["realized_pnl"] > 0


def test_open_paper_orders_returns_named_rows_for_monitor():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B68.5",
            label="68° to 69°",
            action="BUY_NO",
            side="NO",
            approved=True,
            probability=0.65,
            probability_lcb=0.55,
            yes_bid=0.30,
            yes_ask=0.32,
            entry_bid=0.68,
            entry_ask=0.70,
            spread=0.02,
            fee_per_contract=0.01,
            cost_per_contract=0.71,
            edge=0.08,
            edge_lcb=0.03,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=0.8,
            reasons=[],
        )
        store.record_paper_order("2026-06-03", decision)
        row = store.open_paper_orders(1)[0]
        assert row["side"] == "NO"
        assert row["market_ticker"] == "KXHIGHTSFO-TEST-B68.5"


def test_place_approved_skips_existing_open_market_position():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store)
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B68.5",
            label="68° to 69°",
            action="BUY_YES",
            approved=True,
            probability=0.65,
            probability_lcb=0.55,
            yes_bid=0.30,
            yes_ask=0.32,
            spread=0.02,
            fee_per_contract=0.01,
            cost_per_contract=0.33,
            edge=0.32,
            edge_lcb=0.22,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=3.2,
            reasons=[],
        )
        first_ids = trader.place_approved("2026-06-03", [decision])
        second_ids = trader.place_approved("2026-06-03", [decision])

        assert len(first_ids) == 1
        assert second_ids == []
        assert len(store.open_paper_orders(10)) == 1


def test_place_arbitrage_records_same_market_yes_and_no_as_group():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store)
        market = MarketBin(
            ticker="KXHIGHTSFO-TEST-B68.5",
            event_ticker="KXHIGHTSFO-TEST",
            title="SFO high 68 to 69",
            yes_sub_title="68° to 69°",
            strike_type="between",
            floor_strike=68,
            cap_strike=69,
            yes_bid=0.44,
            yes_ask=0.45,
            no_bid=0.47,
            no_ask=0.48,
            yes_bid_size=20.0,
            yes_ask_size=20.0,
            status="active",
        )
        box = next(
            opportunity
            for opportunity in build_arbitrage_opportunities(
                [market],
                config=StrategyConfig(max_event_risk_pct=0.50),
                bankroll=1000.0,
            )
            if opportunity.kind == "BOX_YES_NO"
        )

        order_ids = trader.place_arbitrage("2026-06-03", box, bankroll=1000.0)

        assert len(order_ids) == 2
        rows = store.paper_orders(10)
        assert {row["side"] for row in rows} == {"YES", "NO"}
        assert {row["market_ticker"] for row in rows} == {"KXHIGHTSFO-TEST-B68.5"}
        assert len({float(row["contracts"]) for row in rows}) == 1


def test_place_arbitrage_blocks_when_market_already_has_open_position():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store)
        existing = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B68.5",
            label="68° to 69°",
            action="BUY_YES",
            approved=True,
            probability=0.65,
            probability_lcb=0.55,
            yes_bid=0.30,
            yes_ask=0.32,
            spread=0.02,
            fee_per_contract=0.01,
            cost_per_contract=0.33,
            edge=0.32,
            edge_lcb=0.22,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=3.2,
            reasons=[],
        )
        assert len(trader.place_approved("2026-06-03", [existing])) == 1
        market = MarketBin(
            ticker="KXHIGHTSFO-TEST-B68.5",
            event_ticker="KXHIGHTSFO-TEST",
            title="SFO high 68 to 69",
            yes_sub_title="68° to 69°",
            strike_type="between",
            floor_strike=68,
            cap_strike=69,
            yes_bid=0.44,
            yes_ask=0.45,
            no_bid=0.47,
            no_ask=0.48,
            yes_bid_size=20.0,
            yes_ask_size=20.0,
            status="active",
        )
        box = next(
            opportunity
            for opportunity in build_arbitrage_opportunities(
                [market],
                config=StrategyConfig(max_event_risk_pct=0.50),
                bankroll=1000.0,
            )
            if opportunity.kind == "BOX_YES_NO"
        )

        assert trader.place_arbitrage("2026-06-03", box, bankroll=1000.0) == []
        assert len(store.open_paper_orders(10)) == 1


def test_place_approved_keeps_profiles_in_separate_paper_books():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        balanced = PaperTrader(store, risk_profile="live")
        fast = PaperTrader(store, risk_profile="research")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B68.5",
            label="68° to 69°",
            action="BUY_YES",
            approved=True,
            probability=0.65,
            probability_lcb=0.55,
            yes_bid=0.30,
            yes_ask=0.32,
            spread=0.02,
            fee_per_contract=0.01,
            cost_per_contract=0.33,
            edge=0.32,
            edge_lcb=0.22,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=3.2,
            reasons=[],
        )

        assert len(balanced.place_approved("2026-06-03", [decision])) == 1
        assert balanced.place_approved("2026-06-03", [decision]) == []
        assert len(fast.place_approved("2026-06-03", [decision])) == 1

        rows = store.paper_orders(10)
        assert {row["risk_profile"] for row in rows} == {"live", "research"}
        assert len(store.open_paper_orders(10)) == 2


def test_place_approved_blocks_reentry_after_close_by_default():
    """A stop-loss exit must not be followed by a same-market re-buy churn loop."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store)
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B68.5",
            label="68° to 69°",
            action="BUY_YES",
            approved=True,
            probability=0.65,
            probability_lcb=0.55,
            yes_bid=0.30,
            yes_ask=0.32,
            spread=0.02,
            fee_per_contract=0.01,
            cost_per_contract=0.33,
            edge=0.32,
            edge_lcb=0.22,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=3.2,
            reasons=[],
        )
        first_id = trader.place_approved("2026-06-03", [decision])[0]
        store.close_paper_order(first_id, 0.40)
        second_ids = trader.place_approved("2026-06-03", [decision])

        assert second_ids == []
        assert store.entries_for_market_side("2026-06-03", decision.ticker, "YES") == 1


def test_place_approved_allows_reentry_when_config_permits_more_entries():
    from dataclasses import replace as dc_replace

    from sfo_kalshi_quant.config import StrategyConfig

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store, dc_replace(StrategyConfig(), max_entries_per_market_side=2))
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B68.5",
            label="68° to 69°",
            action="BUY_YES",
            approved=True,
            probability=0.65,
            probability_lcb=0.55,
            yes_bid=0.30,
            yes_ask=0.32,
            spread=0.02,
            fee_per_contract=0.01,
            cost_per_contract=0.33,
            edge=0.32,
            edge_lcb=0.22,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=3.2,
            reasons=[],
        )
        first_id = trader.place_approved("2026-06-03", [decision])[0]
        store.close_paper_order(first_id, 0.40)
        second_ids = trader.place_approved("2026-06-03", [decision])
        third_ids = trader.place_approved("2026-06-03", [decision])

        assert len(second_ids) == 1
        assert third_ids == []


def test_place_approved_enforces_cumulative_target_exposure_cap():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store)
        def decision_for(ticker: str) -> TradeDecision:
            return TradeDecision(
                ticker=ticker,
                label="68° to 69°",
                action="BUY_YES",
                approved=True,
                probability=0.65,
                probability_lcb=0.55,
                yes_bid=0.30,
                yes_ask=0.32,
                spread=0.02,
                fee_per_contract=0.01,
                cost_per_contract=0.33,
                edge=0.32,
                edge_lcb=0.22,
                kelly_fraction=0.01,
                recommended_contracts=100.0,
                expected_profit=32.0,
                reasons=[],
            )

        # Cap = 1000 * 5% = $50. First order costs $33; the second is trimmed
        # to the remaining $17 (51 whole contracts at $0.33).
        first = trader.place_approved("2026-06-03", [decision_for("KXHIGHTSFO-TEST-B68.5")], bankroll=1000.0)
        second = trader.place_approved("2026-06-03", [decision_for("KXHIGHTSFO-TEST-B70.5")], bankroll=1000.0)
        third = trader.place_approved("2026-06-03", [decision_for("KXHIGHTSFO-TEST-B72.5")], bankroll=1000.0)

        assert len(first) == 1
        assert len(second) == 1
        assert third == []
        spend = store.paper_spend_for_target("2026-06-03")
        assert spend <= 50.0 + 1e-6


def test_paper_stake_sets_contracts_from_dollars():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store)
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-T66",
            label="65° or below",
            action="BUY_YES",
            approved=True,
            probability=0.25,
            probability_lcb=0.20,
            yes_bid=0.01,
            yes_ask=0.04,
            spread=0.03,
            fee_per_contract=0.01,
            cost_per_contract=0.05,
            edge=0.20,
            edge_lcb=0.15,
            kelly_fraction=0.01,
            recommended_contracts=3.0,
            expected_profit=0.6,
            reasons=[],
        )
        adjusted = trader.with_paper_stake(decision, 10.0)
        assert adjusted.recommended_contracts == 25.0
        assert adjusted.recommended_contracts == int(adjusted.recommended_contracts)
        assert adjusted.recommended_contracts * adjusted.cost_per_contract <= 10.0 + 1e-9
        order_ids = trader.place_approved("2026-06-03", [decision], stake_dollars=10.0)
        assert len(order_ids) == 1
        row = store.paper_orders(1)[0]
        assert float(row["contracts"]) == int(row["contracts"])
        spend = float(row["contracts"]) * float(row["cost_per_contract"])
        assert spend <= 10.0 + 1e-9
        assert spend > 1.0


def test_paper_stake_caps_contracts_at_visible_ask_size():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store)
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-T66",
            label="65° or below",
            action="BUY_YES",
            approved=True,
            probability=0.25,
            probability_lcb=0.20,
            yes_bid=0.01,
            yes_ask=0.04,
            spread=0.03,
            fee_per_contract=0.01,
            cost_per_contract=0.05,
            edge=0.20,
            edge_lcb=0.15,
            kelly_fraction=0.01,
            recommended_contracts=3.0,
            expected_profit=0.6,
            reasons=[],
            yes_ask_size=20.0,
        )
        adjusted = trader.with_paper_stake(decision, 10.0)
        assert adjusted.recommended_contracts == 20.0


def test_daily_budget_caps_approved_trade_risk():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store)
        decisions = []
        for ticker in ("KXHIGHTSFO-TEST-T66", "KXHIGHTSFO-TEST-B66.5"):
            decisions.append(
                TradeDecision(
                    ticker=ticker,
                    label="65° or below",
                    action="BUY_YES",
                    approved=True,
                    probability=0.25,
                    probability_lcb=0.20,
                    yes_bid=0.01,
                    yes_ask=0.04,
                    spread=0.03,
                    fee_per_contract=0.01,
                    cost_per_contract=0.05,
                    edge=0.20,
                    edge_lcb=0.15,
                    kelly_fraction=0.01,
                    recommended_contracts=3.0,
                    expected_profit=0.6,
                    reasons=[],
                )
            )
        adjusted = trader.with_daily_budget(decisions, 50.0)
        assert [row.recommended_contracts for row in adjusted] == [3.0, 3.0]
        order_ids = trader.place_approved("2026-06-03", decisions, daily_budget=50.0)
        assert len(order_ids) == 2
        rows = store.paper_orders(2)
        spend = sum(float(row["contracts"]) * float(row["cost_per_contract"]) for row in rows)
        assert round(spend, 2) == 0.26
        assert round(store.remaining_daily_budget("2026-06-03", 50.0), 2) == 49.74


def test_daily_budget_scales_down_when_risk_exceeds_cap():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store)
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-T66",
            label="65° or below",
            action="BUY_YES",
            approved=True,
            probability=0.25,
            probability_lcb=0.20,
            yes_bid=0.01,
            yes_ask=0.04,
            spread=0.03,
            fee_per_contract=0.01,
            cost_per_contract=0.05,
            edge=0.20,
            edge_lcb=0.15,
            kelly_fraction=0.01,
            recommended_contracts=2000.0,
            expected_profit=400.0,
            reasons=[],
        )
        adjusted = trader.with_daily_budget([decision], 50.0)
        assert adjusted[0].recommended_contracts == 1000.0
        order_ids = trader.place_approved("2026-06-03", [decision], daily_budget=50.0)
        assert len(order_ids) == 1
        row = store.paper_orders(1)[0]
        spend = float(row["contracts"]) * float(row["cost_per_contract"])
        assert spend <= 50.0 + 1e-9
        assert round(spend, 2) == 42.69


def test_market_summary_filters_by_target_date_and_tracks_open_capital():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-T66",
            label="65° or below",
            action="BUY_YES",
            approved=True,
            probability=0.25,
            probability_lcb=0.20,
            yes_bid=0.01,
            yes_ask=0.04,
            spread=0.03,
            fee_per_contract=0.01,
            cost_per_contract=0.05,
            edge=0.20,
            edge_lcb=0.15,
            kelly_fraction=0.01,
            recommended_contracts=200.0,
            expected_profit=40.0,
            reasons=[],
        )
        store.record_paper_order("2026-06-03", decision)
        store.record_paper_order("2026-06-10", decision)
        summary = store.market_backtest_summary(since="2026-06-03", until="2026-06-09")
        assert summary["orders"] == 0
        assert summary["open_orders"] == 1
        assert round(summary["open_capital_at_risk"], 2) == 8.54


def test_signal_backtest_entry_mode_keeps_first_approved_row():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        base = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B66.5",
            label="66° to 67°",
            action="BUY_YES",
            approved=False,
            probability=0.30,
            probability_lcb=0.20,
            yes_bid=0.20,
            yes_ask=0.30,
            spread=0.10,
            fee_per_contract=0.01,
            cost_per_contract=0.31,
            edge=-0.01,
            edge_lcb=-0.11,
            kelly_fraction=0.0,
            recommended_contracts=0.0,
            expected_profit=0.0,
            reasons=["first scan rejected"],
            trade_quality_score=20.0,
            strike_type="between",
            floor_strike=66.0,
            cap_strike=67.0,
        )
        from dataclasses import replace as dc_replace

        entry = dc_replace(
            base,
            approved=True,
            probability=0.62,
            probability_lcb=0.52,
            edge=0.31,
            edge_lcb=0.21,
            recommended_contracts=10.0,
            expected_profit=3.1,
            reasons=[],
            trade_quality_score=70.0,
        )
        later = dc_replace(
            base,
            approved=True,
            probability=0.90,
            probability_lcb=0.85,
            edge=0.59,
            edge_lcb=0.54,
            recommended_contracts=10.0,
            expected_profit=5.9,
            reasons=[],
            trade_quality_score=90.0,
        )

        store.record_decisions("2026-06-03", [base], event=pre_resolution_event([base]))
        store.record_decisions("2026-06-03", [entry], event=pre_resolution_event([entry]))
        store.record_decisions("2026-06-03", [later], event=pre_resolution_event([later]))

        entry_summary = store.signal_backtest_summary(
            {"2026-06-03": 67.0}, sample_mode="entry-per-market-side"
        )
        latest_summary = store.signal_backtest_summary({"2026-06-03": 67.0})

        assert entry_summary["signals"] == 1.0
        # Entry mode scores the first approved snapshot, not the last scan.
        assert round(entry_summary["avg_probability"], 3) == 0.62
        assert round(latest_summary["avg_probability"], 3) == 0.90


def test_signal_backtest_separates_probability_streams():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B66.5",
            label="66° to 67°",
            action="BUY_YES",
            approved=True,
            probability=0.70,
            probability_lcb=0.60,
            yes_bid=0.20,
            yes_ask=0.30,
            spread=0.10,
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
            model_probability=0.80,
            market_probability=0.40,
        )

        store.record_decisions("2026-06-03", [decision], event=pre_resolution_event([decision]))
        summary = store.signal_backtest_summary({"2026-06-03": 67.0})

        streams = summary["probability_streams"]
        assert round(streams["traded"]["brier_score"], 4) == round((0.70 - 1.0) ** 2, 4)
        assert round(streams["weather_model"]["brier_score"], 4) == round((0.80 - 1.0) ** 2, 4)
        assert round(streams["market_prior"]["brier_score"], 4) == round((0.40 - 1.0) ** 2, 4)
        assert streams["weather_model"]["settled"] == 1.0


def test_signal_backtest_excludes_null_close_time_recorded_now():
    # A decision recorded without an event carries market_close_time = NULL.
    # created_at is wall-clock "now" while target_date is in the past, so the row
    # cannot be proven to predate market close -- the look-ahead guard must
    # exclude it by default (otherwise a decision recorded after the market
    # resolved would leak into the backtest). It is only scored when
    # post-resolution rows are explicitly included.
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B66.5",
            label="66° to 67°",
            action="BUY_YES",
            approved=True,
            probability=0.70,
            probability_lcb=0.60,
            yes_bid=0.20,
            yes_ask=0.30,
            spread=0.10,
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
        )
        store.record_decisions("2026-06-03", [decision])  # no event -> NULL close_time

        strict = store.signal_backtest_summary({"2026-06-03": 67.0})
        included = store.signal_backtest_summary(
            {"2026-06-03": 67.0}, pre_resolution_only=False
        )

        assert strict["signals"] == 0.0
        assert strict["excluded_post_resolution_signals"] == 1.0
        assert included["signals"] == 1.0


def test_settle_paper_orders_rounds_fractional_high_to_integer_kalshi_settlement():
    """Kalshi settles on the integer high. A raw NWS/provisional high of 65.6
    must resolve the 66-67 bin as YES (rounds to 66), not NO. Regression for the
    fractional-settlement mismatch that mis-resolved bins near half-degree edges
    and stored a fractional settlement_high_f the rest of the system disagreed
    with."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B66.5",
            label="66° to 67°",
            action="BUY_YES",
            approved=True,
            probability=0.30,
            probability_lcb=0.20,
            yes_bid=0.02,
            yes_ask=0.03,
            spread=0.01,
            fee_per_contract=0.01,
            cost_per_contract=0.04,
            edge=0.26,
            edge_lcb=0.16,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=2.6,
            reasons=[],
            strike_type="between",
            floor_strike=66.0,
            cap_strike=67.0,
        )
        store.record_paper_order("2026-06-03", decision)
        # Raw high 65.6 would resolve 66 <= 65.6 <= 67 as False (a YES loss);
        # rounded to the integer 66 it is a YES win.
        assert store.settle_paper_orders("2026-06-03", 65.6) == 1
        row = store.paper_orders(1)[0]
        assert row["settlement_high_f"] == 66.0
        assert row["resolved_yes"] == 1
        assert row["realized_pnl"] > 0


def test_signal_backtest_scores_against_integer_kalshi_settlement():
    """win_rate / Brier / hit-rate must use the integer Kalshi settlement, not
    the raw fractional high. A YES decision on the 66-67 bin is a WIN when the
    true high 65.6 rounds to 66; scoring it against the raw 65.6 wrongly counts
    it a loss. Regression for the metrics-path settlement mismatch."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B66.5",
            label="66° to 67°",
            action="BUY_YES",
            approved=True,
            probability=0.70,
            probability_lcb=0.60,
            yes_bid=0.20,
            yes_ask=0.30,
            spread=0.10,
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
        )
        store.record_decisions(
            "2026-06-03", [decision], event=pre_resolution_event([decision])
        )
        summary = store.signal_backtest_summary({"2026-06-03": 65.6})

        assert summary["settled_signals"] == 1.0
        assert summary["win_rate"] == 1.0
        assert summary["approved_hit_rate"] == 1.0
        # A confident-correct YES: Brier = (probability - 1)^2.
        assert round(summary["brier_score"], 4) == round((0.70 - 1.0) ** 2, 4)


def test_close_paper_order_refuses_to_clobber_concurrently_settled_order():
    """The q2min monitor and the settle path race on one DB. If a settle resolves
    an order between the monitor's open-snapshot read and its close UPDATE, the
    close must NOT overwrite the true settlement outcome with an intraday exit
    price. Regression for the unguarded `WHERE id = ?` close UPDATE."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B68.5",
            label="68° to 69°",
            action="BUY_NO",
            side="NO",
            approved=True,
            probability=0.80,
            probability_lcb=0.70,
            yes_bid=0.20,
            yes_ask=0.22,
            spread=0.02,
            fee_per_contract=0.01,
            cost_per_contract=0.24,
            edge=0.56,
            edge_lcb=0.46,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=5.6,
            reasons=[],
            entry_bid=0.76,
            entry_ask=0.23,
        )
        order_id = store.record_paper_order("2026-06-03", decision)
        # Snapshot the order while it is still open (what the monitor sees).
        stale_open = store._open_order(order_id)
        assert stale_open is not None
        # A concurrent settle wins the race and resolves the row (high 67 -> the
        # 68-69 NO favorite wins).
        assert store.settle_paper_orders("2026-06-03", 67) == 1
        # The monitor now tries to close using its now-stale open snapshot.
        with patch.object(store, "_open_order", return_value=stale_open):
            try:
                store.close_paper_order(order_id, 0.40)
            except ValueError as exc:
                assert "resolved concurrently" in str(exc)
            else:  # pragma: no cover - guard regression
                raise AssertionError("expected concurrent-resolve guard to raise")

        row = store.paper_orders(1)[0]
        # Settlement outcome preserved, not clobbered into a PAPER_CLOSED exit.
        assert row["status"] == "PAPER_SETTLED"
        assert row["settlement_high_f"] == 67.0
        assert row["exit_price"] is None


def test_paper_spend_excludes_expired_resting_orders():
    """A resting limit that expires deployed ZERO capital, so it must not consume
    the per-target exposure cap -- counting its never-filled notional blocked
    valid re-entries on the next scan. Regression for paper_spend_for_target
    including PAPER_EXPIRED rows."""

    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        filled = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B68.5",
            label="68° to 69°",
            action="BUY_NO",
            side="NO",
            approved=True,
            probability=0.80,
            probability_lcb=0.70,
            yes_bid=0.20,
            yes_ask=0.22,
            spread=0.02,
            fee_per_contract=0.01,
            cost_per_contract=0.24,
            edge=0.56,
            edge_lcb=0.46,
            kelly_fraction=0.01,
            recommended_contracts=10.0,
            expected_profit=5.6,
            reasons=[],
            entry_bid=0.76,
            entry_ask=0.23,
        )
        store.record_paper_order("2026-06-03", filled)
        filled_spend = store.paper_spend_for_target("2026-06-03")
        assert filled_spend > 0.0

        # A resting limit on a different market that never crosses (proven resting
        # config from the market-summary expiry test).
        limit_trader = PaperTrader(
            store,
            StrategyConfig(limit_price_edge_lcb_buffer=0.02),
            entry_mode="limit",
        )
        resting = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B74.5",
            label="74° to 75°",
            action="BUY_NO",
            side="NO",
            approved=True,
            probability=0.85,
            probability_lcb=0.81,
            yes_bid=0.22,
            yes_ask=0.24,
            spread=0.03,
            fee_per_contract=0.02,
            cost_per_contract=0.77,
            edge=0.05,
            edge_lcb=0.01,
            kelly_fraction=0.01,
            recommended_contracts=2.0,
            expected_profit=0.1,
            reasons=[],
            entry_bid=0.73,
            entry_ask=0.75,
            entry_bid_size=10.0,
            entry_ask_size=10.0,
        )
        assert limit_trader.place_approved("2026-06-03", [resting])
        # While RESTING, its reserved notional legitimately inflates spend.
        assert store.paper_spend_for_target("2026-06-03") > filled_spend

        # Settle expires the unreachable resting order (high 67 -> B74.5 never fills).
        store.settle_paper_orders("2026-06-03", 67)
        rows = {row["market_ticker"]: row for row in store.paper_orders(10)}
        assert rows["KXHIGHTSFO-TEST-B74.5"]["status"] == "PAPER_EXPIRED"
        # After expiry the zero-capital order no longer consumes the cap: spend is
        # back to just the filled order's notional, freeing re-entry headroom.
        assert abs(store.paper_spend_for_target("2026-06-03") - filled_spend) < 1e-9
