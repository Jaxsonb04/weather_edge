from __future__ import annotations

import io
import json
import sqlite3
import tempfile
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sfo_kalshi_quant.cli import main
from sfo_kalshi_quant import report
from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.models import EventSnapshot, MarketBin
from sfo_kalshi_quant.report import _best_signal, build_daily_report


def _write_forecaster_fixture(root: Path, target: date) -> None:
    root.mkdir(parents=True, exist_ok=True)
    daily = []
    start = date(2025, 1, 1)
    for idx in range(220):
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

    with sqlite3.connect(root / "weather.db") as conn:
        conn.execute(
            """
            CREATE TABLE forecast_blend_daily_high (
                fetched_at TEXT NOT NULL,
                target_date TEXT NOT NULL,
                lead_hours REAL,
                method TEXT NOT NULL,
                predicted_high_f REAL NOT NULL,
                google_high_f REAL,
                nws_high_f REAL,
                open_meteo_high_f REAL,
                history_high_f REAL,
                google_weight REAL,
                nws_weight REAL,
                open_meteo_weight REAL,
                history_weight REAL,
                station_adjustment_f REAL,
                fresh_station_count INTEGER,
                source_count INTEGER,
                time_zone TEXT,
                max_calls_per_day INTEGER,
                calls_used_today INTEGER,
                details_json TEXT,
                actual_high_f REAL,
                abs_error_f REAL,
                scored_at TEXT,
                PRIMARY KEY (fetched_at, target_date)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO forecast_blend_daily_high (
                fetched_at, target_date, lead_hours, method, predicted_high_f,
                google_high_f, nws_high_f, open_meteo_high_f, history_high_f,
                google_weight, nws_weight, open_meteo_weight, history_weight,
                station_adjustment_f, fresh_station_count, source_count,
                max_calls_per_day, calls_used_today, details_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-03T22:13:44+00:00",
                target.isoformat(),
                24.0,
                "weighted blend",
                66.4,
                66.0,
                67.0,
                65.5,
                64.0,
                0.38,
                0.36,
                0.18,
                0.08,
                0.1,
                6,
                4,
                260,
                12,
                json.dumps({"blend_weighting": {"mode": "base"}}),
            ),
        )


def test_daily_report_json_uses_fallback_without_recording_state():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        target = date(2026, 6, 4)
        _write_forecaster_fixture(root, target)

        out = io.StringIO()
        with redirect_stdout(out):
            code = main(
                [
                    "--forecaster-root",
                    str(root),
                    "--no-color",
                    "daily-report",
                    "--target-date",
                    target.isoformat(),
                    "--side",
                    "both",
                    "--format",
                    "json",
                    "--no-live-market",
                    "--no-ensemble",
                ]
            )

        payload = json.loads(out.getvalue())
        assert code == 0
        assert payload["mode"] == "paper_research_only"
        assert payload["live_orders_enabled"] is False
        assert payload["targets"][0]["market_available"] is False
        assert payload["targets"][0]["decisions"]
        assert payload["calibration"]["n"] == 40
        assert datetime.fromisoformat(payload["generated_at"]).tzinfo is not None
        assert payload["market_data_at"] is None
        assert payload["targets"][0]["target_status"] in {
            "settlement_day",
            "upcoming",
            "past",
        }
        assert not (Path(tmp) / "trading" / "data").exists()


def test_daily_report_can_write_public_trading_signal_artifact():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        output = root / "trading_signal.json"
        target = date(2026, 6, 4)
        _write_forecaster_fixture(root, target)

        out = io.StringIO()
        with redirect_stdout(out):
            code = main(
                [
                    "--forecaster-root",
                    str(root),
                    "--no-color",
                    "daily-report",
                    "--target-date",
                    target.isoformat(),
                    "--format",
                    "json",
                    "--no-live-market",
                    "--no-ensemble",
                    "--output",
                    str(output),
                ]
            )

        assert code == 0
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["disclaimer"].startswith("Paper-trading research only")
        assert payload["summary"]["best_signal"] is not None
        sides = {row["side"] for row in payload["targets"][0]["decisions"]}
        assert sides == {"YES", "NO"}


def test_daily_report_uses_fixed_standard_settlement_day_for_target_status():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        target = date(2026, 6, 4)
        _write_forecaster_fixture(root, target)

        payload = build_daily_report(
            forecaster_root=root,
            targets=[target],
            config=StrategyConfig(),
            side="both",
            no_ensemble=True,
            allow_live_market=False,
            calibration_source="lstm",
            now=datetime(2026, 6, 4, 7, 30, tzinfo=timezone.utc),
        )

    # 07:30 UTC is still June 3 on SFO's fixed UTC-8 settlement clock.
    assert payload["generated_at"] == "2026-06-04T07:30:00+00:00"
    assert payload["targets"][0]["target_status"] == "upcoming"
    assert payload["market_data_at"] is None


def test_market_data_at_uses_latest_available_source_timestamp():
    market = MarketBin(
        ticker="KXHIGHTSFO-26JUN04-B65.5",
        event_ticker="KXHIGHTSFO-26JUN04",
        title="",
        yes_sub_title="65 to 66",
        strike_type="between",
        floor_strike=65.0,
        cap_strike=66.0,
        yes_bid=0.2,
        yes_ask=0.3,
        no_bid=0.7,
        no_ask=0.8,
        yes_bid_size=10.0,
        yes_ask_size=10.0,
        status="active",
        raw={"updated_time": "2026-06-04T07:25:00Z"},
    )
    event = EventSnapshot(
        event_ticker="KXHIGHTSFO-26JUN04",
        title="",
        target_date=date(2026, 6, 4),
        markets=[market],
        raw={"updated_time": "2026-06-04T07:20:00Z"},
    )

    assert report._market_data_at(event) == "2026-06-04T07:25:00+00:00"
    assert report._market_data_at(None) is None


def test_best_signal_prefers_live_market_over_probability_only_ladder():
    live = _target_report("2026-06-08", market_available=True, quality=40.0, edge_lcb=0.05)
    fallback = _target_report("2026-06-09", market_available=False, quality=90.0, edge_lcb=0.30)

    best = _best_signal([fallback, live])

    assert best is not None
    assert best["target_date"] == "2026-06-08"
    assert best["market_available"] is True


def _target_report(target: str, *, market_available: bool, quality: float, edge_lcb: float):
    return {
        "target_date": target,
        "market_available": market_available,
        "decisions": [
            {
                "approved": True,
                "trade_quality_score": quality,
                "edge_lcb": edge_lcb,
                "edge": edge_lcb + 0.10,
            }
        ],
    }
