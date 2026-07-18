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


def test_paper_summary_counts_three_close_lots_as_one_logical_position():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()

        local_now = _now_local()
        opened_date = local_now.date() - timedelta(days=2)
        first_close_date = opened_date
        second_close_date = opened_date + timedelta(days=1)
        final_close_date = opened_date + timedelta(days=2)
        root_id = store.record_paper_order(
            final_close_date.isoformat(),
            _decision("KXHIGHTPHX-TEST-B110.5"),
            risk_profile="live",
        )
        first_lot = store.close_paper_order(root_id, 0.70, max_quantity=2.0)
        second_lot = store.close_paper_order(root_id, 0.70, max_quantity=3.0)
        final_lot = store.close_paper_order(root_id, 0.70)

        def local_noon(day) -> str:
            return (
                datetime.combine(day, datetime.min.time(), tzinfo=SFO_TZ)
                + timedelta(hours=12)
            ).isoformat()

        close_days_by_id = {
            int(first_lot["id"]): first_close_date,
            int(second_lot["id"]): second_close_date,
            int(final_lot["id"]): final_close_date,
        }
        with store.connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute(
                "UPDATE paper_orders SET created_at=?, filled_at=? "
                "WHERE id=? OR parent_order_id=?",
                (
                    local_noon(opened_date),
                    local_noon(opened_date),
                    root_id,
                    root_id,
                ),
            )
            for order_id, close_day in close_days_by_id.items():
                conn.execute(
                    "UPDATE paper_orders SET closed_at=? WHERE id=?",
                    (local_noon(close_day), order_id),
                )
            lots = conn.execute(
                "SELECT id, contracts, cost_per_contract, realized_pnl, closed_at "
                "FROM paper_orders WHERE id=? OR parent_order_id=? ORDER BY id",
                (root_id, root_id),
            ).fetchall()

        expected_pnl = sum(float(lot["realized_pnl"]) for lot in lots)
        expected_capital = sum(
            float(lot["contracts"]) * float(lot["cost_per_contract"])
            for lot in lots
        )
        expected_by_day = {
            day.isoformat(): {
                "pnl": sum(
                    float(lot["realized_pnl"])
                    for lot in lots
                    if datetime.fromisoformat(lot["closed_at"]).date() == day
                ),
                "capital": sum(
                    float(lot["contracts"]) * float(lot["cost_per_contract"])
                    for lot in lots
                    if datetime.fromisoformat(lot["closed_at"]).date() == day
                ),
            }
            for day in (first_close_date, second_close_date, final_close_date)
        }

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            config=StrategyConfig(paper_bankroll=1000.0),
            days=7,
            now=local_now,
        )

        totals = payload["totals"]
        assert totals["trades_opened"] == 1
        assert totals["trades_closed"] == 1
        assert totals["trades_settled"] == 0
        assert totals["wins"] == 1
        assert totals["losses"] == 0
        assert totals["realized_pnl"] == round(expected_pnl, 2)
        assert totals["capital_resolved"] == round(expected_capital, 2)

        profile = next(row for row in payload["profiles"] if row["risk_profile"] == "live")
        assert profile["resolved"] == 1
        assert profile["wins"] == 1
        assert profile["losses"] == 0
        assert profile["realized_pnl"] == round(expected_pnl, 2)
        assert profile["capital_resolved"] == round(expected_capital, 2)

        yes_side = payload["side_performance"]["YES"]
        assert yes_side["trades"] == 1
        assert yes_side["wins"] == 1
        assert yes_side["losses"] == 0
        assert yes_side["realized_pnl"] == round(expected_pnl, 2)
        assert yes_side["capital"] == round(expected_capital, 2)
        assert payload["exit_reasons"]["closed_take_profit"] == 1

        assert len(payload["biggest_winners"]) == 1
        assert payload["biggest_winners"][0]["id"] == root_id
        assert payload["biggest_winners"][0]["contracts"] == 10.0
        assert payload["biggest_winners"][0]["realized_pnl"] == round(expected_pnl, 2)

        days = {row["date"]: row for row in payload["days"]}
        for close_date, expected in expected_by_day.items():
            assert days[close_date]["realized_pnl"] == round(expected["pnl"], 2)
            assert days[close_date]["resolved_spend"] == round(expected["capital"], 2)
        assert days[opened_date.isoformat()]["opened"] == 1
        assert days[first_close_date.isoformat()]["closed"] == 0
        assert days[second_close_date.isoformat()]["closed"] == 0
        assert days[final_close_date.isoformat()]["closed"] == 1
        assert days[final_close_date.isoformat()]["wins"] == 1


def test_paper_summary_keeps_partially_realized_open_root_undecided():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        forecaster_root = Path(tmp) / "forecaster"
        forecaster_root.mkdir()

        local_now = _now_local()
        today = local_now.date().isoformat()
        root_id = store.record_paper_order(
            today,
            _decision("KXHIGHTPHX-TEST-B111.5"),
            risk_profile="live",
        )
        store.close_paper_order(root_id, 0.70, max_quantity=2.0)
        with store.connect() as conn:
            realized_pnl, capital = conn.execute(
                "SELECT SUM(realized_pnl), SUM(contracts * cost_per_contract) "
                "FROM paper_orders WHERE parent_order_id=?",
                (root_id,),
            ).fetchone()

        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            config=StrategyConfig(paper_bankroll=1000.0),
            days=7,
            now=local_now,
        )

        totals = payload["totals"]
        assert totals["trades_opened"] == 1
        assert totals["trades_closed"] == 0
        assert totals["trades_settled"] == 0
        assert totals["open_positions"] == 1
        assert totals["wins"] == 0
        assert totals["losses"] == 0
        assert totals["hit_rate"] is None
        assert totals["realized_pnl"] == round(realized_pnl, 2)
        assert totals["capital_resolved"] == round(capital, 2)

        profile = next(row for row in payload["profiles"] if row["risk_profile"] == "live")
        assert profile["resolved"] == 0
        assert profile["wins"] == 0
        assert profile["losses"] == 0
        assert profile["realized_pnl"] == round(realized_pnl, 2)
        assert profile["capital_resolved"] == round(capital, 2)

        yes_side = payload["side_performance"]["YES"]
        assert yes_side["trades"] == 0
        assert yes_side["wins"] == 0
        assert yes_side["losses"] == 0
        assert yes_side["realized_pnl"] == round(realized_pnl, 2)
        assert yes_side["capital"] == round(capital, 2)
        assert sum(payload["exit_reasons"].values()) == 0
        assert payload["biggest_winners"] == []
        assert payload["biggest_losers"] == []

        today_row = payload["days"][-1]
        assert today_row["date"] == today
        assert today_row["opened"] == 1
        assert today_row["closed"] == 0
        assert today_row["settled"] == 0
        assert today_row["wins"] == 0
        assert today_row["losses"] == 0
        assert today_row["realized_pnl"] == round(realized_pnl, 2)
        assert today_row["resolved_spend"] == round(capital, 2)
