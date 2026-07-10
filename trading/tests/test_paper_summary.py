import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.config import SFO_TZ, StrategyConfig
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.summary import (
    build_paper_summary,
    write_paper_summary,
    write_paper_summary_csv,
)


def _decision(
    ticker: str,
    *,
    edge_lcb: float = 0.05,
    floor_strike: float = 66.0,
    cap_strike: float = 67.0,
) -> TradeDecision:
    return TradeDecision(
        ticker=ticker,
        label=f"{floor_strike:.0f}° to {cap_strike:.0f}°",
        action="BUY_YES",
        approved=True,
        probability=0.40,
        probability_lcb=0.30,
        yes_bid=0.20,
        yes_ask=0.25,
        spread=0.05,
        fee_per_contract=0.01,
        cost_per_contract=0.26,
        edge=0.14,
        edge_lcb=edge_lcb,
        kelly_fraction=0.01,
        recommended_contracts=10.0,
        expected_profit=1.4,
        reasons=[],
        strike_type="between",
        floor_strike=floor_strike,
        cap_strike=cap_strike,
    )


def _now_local() -> datetime:
    return datetime.now(UTC).astimezone(SFO_TZ)


def test_paper_summary_attributes_pnl_to_resolution_day():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()

        today = _now_local().date().isoformat()
        winner = store.record_paper_order(today, _decision("KXHIGHTSFO-TEST-B66.5"))
        loser = store.record_paper_order(
            today,
            _decision("KXHIGHTSFO-TEST-B68.5", floor_strike=68.0, cap_strike=69.0),
        )
        store.settle_paper_orders(today, 67.0)  # B66.5 YES wins, B68.5 YES loses

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            config=StrategyConfig(paper_bankroll=1000.0),
            days=7,
        )

        totals = payload["totals"]
        assert totals["trades_opened"] == 2
        assert totals["trades_settled"] == 2
        assert totals["wins"] == 1
        assert totals["losses"] == 1
        assert totals["hit_rate"] == 0.5
        assert payload["bankroll"] == 1000.0
        assert len(payload["days"]) == 7
        today_row = payload["days"][-1]
        assert today_row["date"] == today
        assert today_row["settled"] == 2
        assert today_row["realized_pnl"] != 0.0
        assert today_row["closing_equity"] == payload["current_equity"]
        assert today_row["daily_realized_pnl"] == today_row["realized_pnl"]
        assert payload["biggest_winners"][0]["id"] == winner
        assert payload["biggest_losers"][0]["id"] == loser
        assert payload["learnings"]
        assert payload["recommended_changes"]


def test_paper_summary_handles_empty_database():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            days=3,
        )

        assert payload["totals"]["trades_opened"] == 0
        assert payload["totals"]["hit_rate"] is None
        assert payload["totals"]["roi"] is None
        assert len(payload["days"]) == 3
        assert any("No resolved trades" in note for note in payload["learnings"])


def test_paper_summary_includes_clean_forecast_error():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()

        target = _now_local().date()
        fetched = (
            datetime.combine(target - timedelta(days=1), datetime.min.time(), tzinfo=SFO_TZ)
            + timedelta(hours=18)
        )
        with sqlite3.connect(forecaster_root / "weather.db") as conn:
            conn.execute(
                """
                CREATE TABLE forecast_blend_daily_high (
                    target_date TEXT,
                    predicted_high_f REAL,
                    actual_high_f REAL,
                    abs_error_f REAL,
                    fetched_at TEXT,
                    details_json TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO forecast_blend_daily_high VALUES (?, ?, ?, ?, ?, ?)",
                (target.isoformat(), 66.2, 68.0, 1.8, fetched.isoformat(), "{}"),
            )

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            days=7,
        )

        today_row = payload["days"][-1]
        assert today_row["forecast_predicted_high_f"] == 66.2
        assert today_row["forecast_actual_high_f"] == 68.0
        assert today_row["forecast_error_f"] == 1.8
        assert payload["totals"]["mean_abs_forecast_error_f"] == 1.8


def test_paper_summary_writers_produce_files():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()

        payload = build_paper_summary(db_path=db_path, forecaster_root=forecaster_root, days=2)
        json_path = Path(tmp) / "out" / "summary.json"
        csv_path = Path(tmp) / "out" / "summary.csv"
        write_paper_summary(json_path, payload)
        write_paper_summary_csv(csv_path, payload)

        assert json_path.exists()
        text = csv_path.read_text()
        assert text.count("\n") >= 3  # header + 2 day rows
        assert "realized_pnl" in text


def test_paper_summary_splits_results_by_risk_profile():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()

        today = _now_local().date().isoformat()
        store.record_paper_order(
            today, _decision("KXHIGHTSFO-TEST-B66.5"), risk_profile="live"
        )
        store.record_paper_order(
            today,
            _decision("KXHIGHTSFO-TEST-B68.5", floor_strike=68.0, cap_strike=69.0),
            risk_profile="research",
        )
        store.settle_paper_orders(today, 67.0)  # balanced wins, fast-feedback loses

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            config=StrategyConfig(paper_bankroll=1000.0),
            days=7,
        )

        profiles = {row["risk_profile"]: row for row in payload["profiles"]}
        assert set(profiles) == {"live", "research"}
        assert profiles["live"]["wins"] == 1
        assert profiles["live"]["realized_pnl"] > 0
        assert profiles["research"]["losses"] == 1
        assert profiles["research"]["realized_pnl"] < 0
        day_profiles = next(
            day["profiles"] for day in payload["days"] if day["date"] == today.split("T")[0]
        )
        assert day_profiles["live"]["realized_pnl"] > 0
        assert day_profiles["research"]["realized_pnl"] < 0
