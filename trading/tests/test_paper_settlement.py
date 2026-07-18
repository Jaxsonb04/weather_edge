import io
import json
import sqlite3
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from datetime import date, datetime, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from sfo_kalshi_quant.arbitrage import build_arbitrage_opportunities
from sfo_kalshi_quant.cli import _completed_open_target_dates, main
from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.fees import quadratic_fee_average_per_contract
from sfo_kalshi_quant.models import ForecastSnapshot, IntradaySnapshot, MarketBin, TradeDecision
from sfo_kalshi_quant.paper import ArbitrageContainmentError, PaperTrader
from sfo_kalshi_quant.store.scoring import _sample_decision_rows

from support import pre_resolution_event


@pytest.mark.parametrize(
    "profile_probabilities",
    [
        [
            (None, 0.91),
            ("balanced", 0.91),
            ("fast-feedback", 0.71),
            ("research", 0.71),
        ],
        [
            ("fast_feedback", 0.71),
            ("research", 0.71),
            ("balanced", 0.91),
            (None, 0.91),
        ],
    ],
)
@pytest.mark.parametrize(
    "sample_mode", ["entry-per-market-side", "latest-per-market-side"]
)
def test_python_sampling_fallback_normalizes_profile_before_deduplication(
    profile_probabilities, sample_mode
):
    base = TradeDecision(
        ticker="KXHIGHTSFO-TEST-B66.5",
        label="66 to 67",
        action="BUY_YES",
        approved=True,
        probability=0.91,
        probability_lcb=0.61,
        yes_bid=0.20,
        yes_ask=0.30,
        spread=0.10,
        fee_per_contract=0.01,
        cost_per_contract=0.31,
        edge=0.60,
        edge_lcb=0.30,
        kelly_fraction=0.02,
        recommended_contracts=10.0,
        expected_profit=6.0,
        reasons=[],
        trade_quality_score=72.0,
        strike_type="between",
        floor_strike=66.0,
        cap_strike=67.0,
    )
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        for profile, probability in profile_probabilities:
            decision = replace(base, probability=probability)
            store.record_decisions(
                "2026-06-03",
                [decision],
                event=pre_resolution_event([decision]),
                risk_profile=profile,
            )
        raw_rows = store.sampled_decision_rows(sample_mode="all")

        rows = _sample_decision_rows(raw_rows, sample_mode)

    assert {float(row["probability"]) for row in rows} == {0.91, 0.71}
    assert len(rows) == 2


def test_auto_settle_waits_until_six_am_next_standard_day_in_winter():
    targets = ["2026-01-10"]
    sfo = get_city("sfo")

    assert _completed_open_target_dates(
        targets,
        now=datetime(2026, 1, 11, 13, 59, tzinfo=timezone.utc),
        city=sfo,
    ) == []
    assert _completed_open_target_dates(
        targets,
        now=datetime(2026, 1, 11, 14, 0, tzinfo=timezone.utc),
        city=sfo,
    ) == targets


def test_auto_settle_grace_uses_non_sfo_city_standard_clock_and_allows_older_targets():
    nyc = get_city("nyc")
    targets = ["2026-01-09", "2026-01-10"]

    assert _completed_open_target_dates(
        targets,
        now=datetime(2026, 1, 11, 10, 59, tzinfo=timezone.utc),
        city=nyc,
    ) == ["2026-01-09"]
    assert _completed_open_target_dates(
        targets,
        now=datetime(2026, 1, 11, 11, 0, tzinfo=timezone.utc),
        city=nyc,
    ) == targets


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
        assert round(summary["realized_pnl"], 2) == 9.68


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


def test_signal_backtest_accepts_precomputed_sample_rows_without_resampling(monkeypatch):
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
        rows = store.sampled_decision_rows(sample_mode="entry-per-market-side")
        expected = store.signal_backtest_summary(
            {"2026-06-03": 67.0}, sample_mode="entry-per-market-side"
        )
        monkeypatch.setattr(
            store,
            "sampled_decision_rows",
            lambda **kwargs: (_ for _ in ()).throw(AssertionError("resampled")),
        )

        actual = store.signal_backtest_summary(
            {"2026-06-03": 67.0},
            sample_mode="entry-per-market-side",
            sampled_rows=rows,
        )

        assert actual == expected


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
        assert round(summary["realized_pnl"], 2) == 7.58


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


def test_paper_auto_settle_leaves_order_open_without_archived_final_truth():
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
            "sfo_kalshi_quant.settlement.fetch_recent_cli_settlements",
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
        assert "no CLI truth" in out.getvalue()
        row = store.paper_orders(1)[0]
        assert row["status"] == "PAPER_FILLED"
        assert row["settlement_high_f"] is None

        assert store.settle_paper_orders("2026-06-03", 67.0) == 1
        verify_out = io.StringIO()
        with redirect_stdout(verify_out):
            assert main(
                [
                    "--forecaster-root", str(root),
                    "--db-path", str(db_path),
                    "--no-color", "paper-resettle", "--verify", "--days", "365",
                ]
            ) == 0
        assert "MISSING_FINAL" in verify_out.getvalue()
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT verification_status FROM paper_settlement_verifications"
            ).fetchone()[0] == "MISSING_FINAL"
        assert row["realized_pnl"] is None


def test_paper_auto_settle_fails_closed_for_legacy_cli_schema():
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
            "sfo_kalshi_quant.settlement.fetch_recent_cli_settlements",
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
        assert "no CLI truth" in out.getvalue()
        row = store.paper_orders(1)[0]
        assert row["status"] == "PAPER_FILLED"
        assert row["settlement_high_f"] is None


def test_paper_auto_settle_waits_for_grace_and_final_truth():
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        root.mkdir()
        weather_db = root / "weather.db"
        with sqlite3.connect(weather_db) as conn:
            conn.execute(
                "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
                "max_temperature_f INTEGER, fetched_at TEXT, source TEXT, "
                "is_final INTEGER NOT NULL DEFAULT 1)"
            )
            conn.execute(
                "INSERT INTO cli_settlements VALUES "
                "('KSFO', '2026-01-10', 68, 'early', 'nws_cli', 0)"
            )
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B70.5",
            label="70° to 71°",
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
            floor_strike=70.0,
            cap_strike=71.0,
        )
        store.record_paper_order("2026-01-10", decision)
        sfo = get_city("sfo")
        clock = [datetime(2026, 1, 11, 5, 59, tzinfo=sfo.fixed_standard_timezone())]

        def run_auto_settle():
            with patch(
                "sfo_kalshi_quant.settlement.fetch_recent_cli_settlements",
                lambda site, issuedby, timeout=20: {},
            ), patch(
                "sfo_kalshi_quant.cli.settlement_clock",
                lambda now=None, city=None: clock[0],
            ):
                return main(
                    [
                        "--forecaster-root",
                        str(root),
                        "--db-path",
                        str(db_path),
                        "--no-color",
                        "paper-auto-settle",
                        "--cities",
                        "sfo",
                    ]
                )

        assert run_auto_settle() == 0
        assert store.paper_orders(1)[0]["status"] == "PAPER_FILLED"

        clock[0] = datetime(2026, 1, 11, 6, 0, tzinfo=sfo.fixed_standard_timezone())
        assert run_auto_settle() == 0
        assert store.paper_orders(1)[0]["status"] == "PAPER_FILLED"

        with sqlite3.connect(weather_db) as conn:
            conn.execute(
                "UPDATE cli_settlements SET max_temperature_f=71, fetched_at='final', "
                "is_final=1 WHERE station_id='KSFO' AND local_date='2026-01-10'"
            )
        assert run_auto_settle() == 0
        row = store.paper_orders(1)[0]
        assert row["status"] == "PAPER_SETTLED"
        assert row["settlement_high_f"] == 71.0


def test_paper_auto_settle_prefers_archived_final_over_live_preliminary_version():
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        root.mkdir()
        with sqlite3.connect(root / "weather.db") as conn:
            conn.execute(
                "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
                "max_temperature_f INTEGER, is_final INTEGER NOT NULL DEFAULT 1)"
            )
            conn.execute(
                "INSERT INTO cli_settlements VALUES ('KSFO', '2026-01-10', 71, 1)"
            )
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B70.5",
            label="70° to 71°",
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
            floor_strike=70.0,
            cap_strike=71.0,
        )
        store.record_paper_order("2026-01-10", decision)

        with patch(
            "sfo_kalshi_quant.settlement.fetch_recent_cli_settlements",
            lambda site, issuedby, timeout=20: {date(2026, 1, 10): 68},
        ):
            assert main(
                [
                    "--forecaster-root",
                    str(root),
                    "--db-path",
                    str(db_path),
                    "--no-color",
                    "paper-auto-settle",
                    "--cities",
                    "sfo",
                ]
            ) == 0

        assert store.paper_orders(1)[0]["settlement_high_f"] == 71.0


def test_paper_resettle_verify_flags_mismatch_without_mutating_financial_truth():
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        root.mkdir()
        with sqlite3.connect(root / "weather.db") as conn:
            conn.execute(
                "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
                "max_temperature_f INTEGER, fetched_at TEXT, source TEXT, "
                "is_final INTEGER NOT NULL DEFAULT 1)"
            )
            conn.execute(
                "INSERT INTO cli_settlements VALUES "
                "('KSFO', '2026-01-10', 71, 'final', 'nws_cli', 1)"
            )
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B70.5",
            label="70° to 71°",
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
            floor_strike=70.0,
            cap_strike=71.0,
        )
        store.record_paper_order("2026-01-10", decision)
        assert store.settle_paper_orders("2026-01-10", 68.0) == 1
        store.record_paper_order("2026-01-11", decision)
        assert store.settle_paper_orders("2026-01-11", 69.0) == 1
        before = {row["id"]: dict(row) for row in store.paper_orders(10)}
        argv = [
            "--forecaster-root",
            str(root),
            "--db-path",
            str(db_path),
            "--no-color",
            "paper-resettle",
            "--verify",
            "--days",
            "365",
        ]

        out = io.StringIO()
        with redirect_stdout(out):
            assert main(argv) == 0
            assert main(argv) == 0

        assert "MISMATCH" in out.getvalue()
        assert "MISSING_FINAL" in out.getvalue()
        after = {row["id"]: dict(row) for row in store.paper_orders(10)}
        for order_id in before:
            for field in ("status", "settled_at", "settlement_high_f", "resolved_yes", "realized_pnl"):
                assert after[order_id][field] == before[order_id][field]
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT booked_high_f, final_high_f, verification_status "
                "FROM paper_settlement_verifications ORDER BY target_date"
            ).fetchall()
            assert rows == [
                (68.0, 71.0, "MISMATCH"),
                (69.0, None, "MISSING_FINAL"),
            ]
            assert conn.execute(
                "SELECT COUNT(*) FROM paper_settlement_verifications"
            ).fetchone()[0] == 2


def test_paper_resettle_days_cutoff_is_exactly_n_inclusive_calendar_dates():
    with TemporaryDirectory() as tmp:
        captured = {}

        def fake_verify(self, settlements, *, intervals):
            captured["intervals"] = intervals
            return {"checked": [], "mismatches": 0, "missing_truth": 0}

        with patch(
            "sfo_kalshi_quant.cli.settlement_today",
            lambda now=None, city=None: (
                date(2026, 7, 11) if city and city.slug == "nyc" else date(2026, 7, 10)
            ),
        ), patch.object(PaperStore, "verify_paper_settlements", fake_verify):
            assert main(
                [
                    "--forecaster-root",
                    tmp,
                    "--db-path",
                    str(Path(tmp) / "paper.db"),
                    "--no-color",
                    "paper-resettle",
                    "--verify",
                    "--days",
                    "2",
                ]
            ) == 0

        assert captured["intervals"]["KXHIGHTSFO"] == (
            "2026-07-09", "2026-07-10"
        )
        assert captured["intervals"]["KXHIGHNY"] == (
            "2026-07-10", "2026-07-11"
        )


def test_verify_paper_settlements_applies_closed_series_interval_to_actual_rows():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B70.5", label="70° to 71°", action="BUY_YES",
            approved=True, probability=0.3, probability_lcb=0.2, yes_bid=0.02,
            yes_ask=0.03, spread=0.01, fee_per_contract=0.01,
            cost_per_contract=0.04, edge=0.26, edge_lcb=0.16,
            kelly_fraction=0.01, recommended_contracts=1.0, expected_profit=0.26,
            reasons=[], strike_type="between", floor_strike=70.0, cap_strike=71.0,
        )
        truth = {}
        for target in ("2026-07-08", "2026-07-09", "2026-07-10", "2026-07-11"):
            store.record_paper_order(target, decision)
            store.settle_paper_orders(target, 71, series_ticker="KXHIGHTSFO")
            truth[("KXHIGHTSFO", target)] = 71

        result = store.verify_paper_settlements(
            truth,
            intervals={"KXHIGHTSFO": ("2026-07-09", "2026-07-10")},
        )

        assert [row["target_date"] for row in result["checked"]] == [
            "2026-07-09", "2026-07-10"
        ]


def test_paper_resettle_rejects_nonpositive_days():
    with TemporaryDirectory() as tmp:
        assert main(
            [
                "--forecaster-root",
                tmp,
                "--db-path",
                str(Path(tmp) / "paper.db"),
                "--no-color",
                "paper-resettle",
                "--verify",
                "--days",
                "0",
            ]
        ) == 1


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


def test_market_summary_counts_partial_exits_as_one_terminal_decision():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B70.5",
            label="70° to 71°",
            action="BUY_YES",
            approved=True,
            probability=0.70,
            probability_lcb=0.62,
            yes_bid=0.48,
            yes_ask=0.50,
            spread=0.02,
            fee_per_contract=0.02,
            cost_per_contract=0.52,
            edge=0.18,
            edge_lcb=0.10,
            kelly_fraction=0.01,
            recommended_contracts=4.0,
            expected_profit=0.72,
            reasons=[],
        )
        order_id = store.record_paper_order(
            "2026-06-03", decision, risk_profile="live"
        )
        store.close_paper_order(order_id, 0.20, max_quantity=1.0)
        store.close_paper_order(order_id, 0.20, max_quantity=1.0)
        store.close_paper_order(order_id, 0.20)

        with store.connect() as conn:
            raw_pnl, raw_capital = conn.execute(
                "SELECT SUM(realized_pnl), "
                "SUM(contracts * cost_per_contract) "
                "FROM paper_orders WHERE id=? OR parent_order_id=?",
                (order_id, order_id),
            ).fetchone()

        summary = store.market_backtest_summary()

        assert summary["orders"] == 1
        assert summary["losses"] == 1
        assert summary["wins"] == 0
        assert summary["contracts"] == 4
        assert summary["realized_pnl"] == raw_pnl
        assert summary["capital_at_risk"] == raw_capital


def test_market_summary_keeps_rejected_children_visible_to_group_validation():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B70.5",
            label="70° to 71°",
            action="BUY_YES",
            approved=True,
            probability=0.70,
            probability_lcb=0.62,
            yes_bid=0.48,
            yes_ask=0.50,
            spread=0.02,
            fee_per_contract=0.02,
            cost_per_contract=0.52,
            edge=0.18,
            edge_lcb=0.10,
            kelly_fraction=0.01,
            recommended_contracts=2.0,
            expected_profit=0.36,
            reasons=[],
        )
        root_id = store.record_paper_order(
            "2026-06-03", decision, risk_profile="live"
        )
        store.close_paper_order(root_id, 0.20)
        child_id = store.record_paper_order(
            "2026-06-03", decision, risk_profile="live"
        )
        store.close_paper_order(child_id, 0.20)
        with store.connect() as conn:
            conn.execute(
                "UPDATE paper_orders SET parent_order_id=?, status='REJECTED' "
                "WHERE id=?",
                (root_id, child_id),
            )
            raw_contracts, raw_capital, raw_pnl = conn.execute(
                "SELECT SUM(contracts), "
                "SUM(contracts * cost_per_contract), SUM(realized_pnl) "
                "FROM paper_orders WHERE id=? OR parent_order_id=?",
                (root_id, root_id),
            ).fetchone()

        summary = store.market_backtest_summary()

        assert summary["orders"] == 0
        assert summary["wins"] == 0
        assert summary["losses"] == 0
        assert summary["contracts"] == raw_contracts
        assert summary["capital_at_risk"] == raw_capital
        assert summary["realized_pnl"] == raw_pnl


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


def test_place_arbitrage_preflight_rejects_existing_resting_limit_before_any_leg():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store, risk_profile="research")
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
        resting = box.decisions[0]
        assert store.record_paper_order(
            "2026-06-03",
            resting,
            risk_profile="research",
            status="PAPER_LIMIT_RESTING",
            entry_mode="limit",
        ) is not None

        assert trader.place_arbitrage("2026-06-03", box, bankroll=1000.0) == []
        rows = store.paper_orders(10)
        assert len(rows) == 1
        assert rows[0]["status"] == "PAPER_LIMIT_RESTING"


def test_place_arbitrage_compensates_first_leg_when_second_leg_races():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store, risk_profile="research")
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
        original_record = store.record_paper_order
        calls = 0

        def race_on_second_leg(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                return None
            return original_record(*args, **kwargs)

        with patch.object(store, "record_paper_order", side_effect=race_on_second_leg):
            assert trader.place_arbitrage("2026-06-03", box, bankroll=1000.0) == []

        rows = store.paper_orders(10)
        assert len(rows) == 1
        assert rows[0]["status"] == "PAPER_CLOSED"
        assert rows[0]["closed_at"] is not None
        assert str(rows[0]["group_id"]).startswith("DEGRADED-ARB-")
        assert json.loads(rows[0]["outcome_diagnostics_json"])["event"] == "arbitrage_compensation"
        assert store.open_paper_orders(10) == []


def test_arbitrage_compensation_contains_resting_to_filled_cancel_race_at_bid():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store, risk_profile="research")
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B68.5",
            label="68° to 69°",
            action="ARBITRAGE_BUY_YES",
            approved=True,
            probability=0.60,
            probability_lcb=0.55,
            yes_bid=0.40,
            yes_ask=0.45,
            spread=0.05,
            fee_per_contract=0.01,
            cost_per_contract=0.46,
            edge=0.14,
            edge_lcb=0.09,
            kelly_fraction=0.01,
            recommended_contracts=20.0,
            expected_profit=2.8,
            reasons=[],
            side="YES",
            entry_bid=0.40,
            entry_ask=0.45,
            strike_type="between",
            floor_strike=68,
            cap_strike=69,
        )
        group_id = "ARB-fill-race"
        order_id = store.record_paper_order(
            "2026-06-03",
            decision,
            risk_profile="research",
            status="PAPER_LIMIT_RESTING",
            entry_mode="limit",
            group_id=group_id,
            strategy_config=StrategyConfig(),
        )
        assert order_id is not None

        def fill_before_cancel(current_order_id: int, *, reason: str):
            return store.fill_resting_limit_order(
                current_order_id,
                evidence={"race": "filled_before_cancel", "reason": reason},
            )

        with patch.object(
            store,
            "cancel_resting_limit_order",
            side_effect=fill_before_cancel,
        ):
            trader._compensate_partial_arbitrage(
                [order_id], group_id=group_id, reason="second leg failed"
            )

        row = store.paper_order(order_id)
        assert row["status"] == "PAPER_CLOSED"
        assert row["exit_price"] == row["entry_bid"] == 0.40
        assert row["realized_pnl"] < 0
        assert str(row["group_id"]).startswith("DEGRADED-ARB-")
        # Research-profile orders book against the research shadow ledger
        # (audit AC-01); the compensation close must reconcile there while the
        # live shared account stays untouched.
        state = store.research_account_state()
        assert abs(state["realized_equity"] - (1000.0 + row["realized_pnl"])) < 1e-9
        assert store.shared_account_state()["realized_equity"] == 1000.0
        with store.connect() as conn:
            ledger = conn.execute(
                "SELECT event_type, amount FROM paper_account_ledger "
                "WHERE order_id=? ORDER BY id",
                (order_id,),
            ).fetchall()
        event_types = [event_type for event_type, _amount in ledger]
        assert event_types == [
            "RESERVE",
            "RESERVATION_RELEASE",
            "ENTRY_FILL",
            "EXIT_PROCEEDS",
        ]
        assert sum(amount for _event_type, amount in ledger) == row["realized_pnl"]


def test_arbitrage_compensation_raises_fatal_when_active_leg_cannot_be_contained():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        trader = PaperTrader(store)
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B68.5",
            label="68° to 69°",
            action="ARBITRAGE_BUY_YES",
            approved=True,
            probability=0.60,
            probability_lcb=0.55,
            yes_bid=0.40,
            yes_ask=0.45,
            spread=0.05,
            fee_per_contract=0.01,
            cost_per_contract=0.46,
            edge=0.14,
            edge_lcb=0.09,
            kelly_fraction=0.01,
            recommended_contracts=20.0,
            expected_profit=2.8,
            reasons=[],
            side="YES",
            entry_bid=0.40,
            entry_ask=0.45,
        )
        order_id = store.record_paper_order(
            "2026-06-03",
            decision,
            status="PAPER_LIMIT_RESTING",
            entry_mode="limit",
            group_id="ARB-stuck",
        )
        assert order_id is not None

        with patch.object(
            store,
            "cancel_resting_limit_order",
            return_value=store.paper_order(order_id),
        ), pytest.raises(ArbitrageContainmentError):
            trader._compensate_partial_arbitrage(
                [order_id], group_id="ARB-stuck", reason="cannot cancel"
            )

        assert store.paper_order(order_id)["status"] == "PAPER_LIMIT_RESTING"
        assert str(store.paper_order(order_id)["group_id"]).startswith("DEGRADED-ARB-")


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

        # Shared-account v2 first applies the $30 normal-position cap, then the
        # 5% city/target cap: ~$30 + ~$20 exhausts the $50 city room, so the
        # third entry falls below the $5 executable minimum and is rejected --
        # the cumulative cap still binds at the same $50.
        first = trader.place_approved("2026-06-03", [decision_for("KXHIGHTSFO-TEST-B68.5")], bankroll=1000.0)
        second = trader.place_approved("2026-06-03", [decision_for("KXHIGHTSFO-TEST-B70.5")], bankroll=1000.0)
        third = trader.place_approved("2026-06-03", [decision_for("KXHIGHTSFO-TEST-B72.5")], bankroll=1000.0)

        assert len(first) == 1
        assert len(second) == 1
        assert third == []
        spend = store.paper_spend_for_target("2026-06-03")
        assert spend <= 50.0 + 1e-6
        assert spend >= 45.0


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


def test_paper_stake_uses_series_and_config_fee_rounding():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        config = StrategyConfig(taker_fee_rate=0.11, fee_multiplier=0.5)
        trader = PaperTrader(store, config)
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
        expected_fee = quadratic_fee_average_per_contract(
            decision.ask,
            adjusted.recommended_contracts,
            fee_multiplier=config.fee_multiplier,
            taker_rate=config.taker_fee_rate,
            maker_rate=config.maker_fee_rate,
            series_ticker=decision.ticker,
        )

        assert adjusted.fee_per_contract == expected_fee


def test_paper_stake_budget_solver_uses_final_booking_fee_semantics():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        config = StrategyConfig(
            taker_fee_rate=0.70,
            fee_multiplier=2.0,
            max_contracts_per_market=100.0,
        )
        trader = PaperTrader(store, config)
        decision = TradeDecision(
            ticker="KXHIGHTSFO-TEST-B70.5",
            label="70° to 71°",
            action="BUY_YES",
            approved=True,
            probability=0.90,
            probability_lcb=0.80,
            yes_bid=0.39,
            yes_ask=0.40,
            spread=0.01,
            fee_per_contract=0.01,
            cost_per_contract=0.41,
            edge=0.49,
            edge_lcb=0.39,
            kelly_fraction=0.01,
            recommended_contracts=3.0,
            expected_profit=1.47,
            reasons=[],
        )

        adjusted = trader.with_paper_stake(decision, 10.0)

        assert adjusted.recommended_contracts * adjusted.cost_per_contract <= 10.0 + 1e-9


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
