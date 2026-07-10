from __future__ import annotations

import json
import sqlite3
from contextlib import redirect_stdout
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sfo_kalshi_quant.cli import main
from sfo_kalshi_quant.dataset_research import build_dataset_research
from sfo_kalshi_quant.datasets import DatasetResult, DatasetStore


def test_dataset_research_promotes_only_sources_that_improve_heldout_accuracy():
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        root.mkdir()
        db_path = Path(tmp) / "paper.db"
        _write_ab_test_fixture(root / "ab_test_results.json", row_count=50)
        store = DatasetStore(db_path)
        start = date(2026, 1, 1)
        rows = []
        for idx in range(50):
            target = start + timedelta(days=idx)
            actual = _actual_for_idx(idx)
            rows.extend(
                [
                    _feature_row("open-meteo-previous-runs", "candidate_model", target, actual + 0.4),
                    _feature_row("open-meteo-previous-runs", "worse_model", target, actual + 3.0),
                ]
            )
        store.upsert_forecast_features(rows)

        payload = build_dataset_research(
            db_path=db_path,
            forecaster_root=root,
            min_matched_rows=30,
            min_mae_improvement_f=0.25,
        )

    candidates = {
        row["dataset_key"]: row
        for row in payload["accuracy_gate"]["candidates"]
    }
    good = candidates["open-meteo-previous-runs/candidate_model/temperature_2m_max/0h"]
    bad = candidates["open-meteo-previous-runs/worse_model/temperature_2m_max/0h"]

    assert payload["status"] == "collect_only"
    assert good["decision"] == "accuracy_candidate"
    assert good["holdout"]["mae_delta_vs_baseline_f"] < -0.25
    assert bad["decision"] == "collect_only"
    assert "does not beat baseline" in bad["reason"]
    assert payload["dataset_stack"]["available"] is True
    assert payload["dataset_stack"]["decision"] == "research_candidate"
    assert payload["dataset_stack"]["holdout"]["mae_delta_vs_baseline_f"] < -0.25
    assert payload["summary"]["combined_stack_candidate"] is True
    assert payload["summary"]["action_items"]
    assert payload["profitability_gate"]["decision"] == "collect_only"
    assert "after-cost" in payload["profitability_gate"]["reason"]
    nbm = payload["probabilistic_benchmarks"]["nbm"]
    assert nbm["models"] == ["ncep_nbm_conus"]
    assert "percentile_temperature_fields" in nbm["desired_fields"]
    assert "ranked_probability_score" in nbm["scoring"]


def test_dataset_research_keeps_small_samples_collect_only():
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        root.mkdir()
        db_path = Path(tmp) / "paper.db"
        _write_ab_test_fixture(root / "ab_test_results.json", row_count=12)
        store = DatasetStore(db_path)
        start = date(2026, 1, 1)
        rows = [
            _feature_row("open-meteo-historical-forecast", "best_match", start + timedelta(days=idx), _actual_for_idx(idx))
            for idx in range(12)
        ]
        store.upsert_forecast_features(rows)

        payload = build_dataset_research(
            db_path=db_path,
            forecaster_root=root,
            min_matched_rows=30,
        )

    candidate = payload["accuracy_gate"]["candidates"][0]
    assert candidate["decision"] == "collect_only"
    assert "needs at least 30" in candidate["reason"]


def test_dataset_research_surfaces_source_health_and_market_history():
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        root.mkdir()
        db_path = Path(tmp) / "paper.db"
        _write_ab_test_fixture(root / "ab_test_results.json", row_count=12)
        store = DatasetStore(db_path)
        run_id = store.start_run("iem-asos", {"source": "iem-asos"})
        store.finish_run(run_id, status="success", rows_written=3, message="ok")
        failed_id = store.start_run("kalshi-history", {"source": "kalshi-history"})
        store.finish_run(failed_id, status="failed", rows_written=0, message="HTTP 429")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO dataset_kalshi_trades (
                    trade_id, ticker, created_time, count, yes_price, no_price,
                    is_block_trade, raw_json, fetched_at
                )
                VALUES ('t1', 'KXHIGHTSFO-TEST', '2026-01-01T00:00:00Z', 1,
                        0.2, 0.8, 0, '{}', '2026-01-01T00:00:00Z')
                """
            )

        payload = build_dataset_research(
            db_path=db_path,
            forecaster_root=root,
            min_matched_rows=30,
        )

    warnings = {row["code"]: row["message"] for row in payload["source_health"]["warnings"]}
    assert payload["dataset_coverage"]["market_history"]["trades"] == 1
    assert payload["dataset_coverage"]["market_history"]["trade_rows_ready"] is False
    assert "kalshi-history-latest-failed" in warnings
    assert "lamp-missing" in warnings


def test_dataset_research_cli_writes_collect_only_report():
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        root.mkdir()
        db_path = Path(tmp) / "paper.db"
        output = Path(tmp) / "dataset_research.json"
        _write_ab_test_fixture(root / "ab_test_results.json", row_count=12)
        store = DatasetStore(db_path)
        store.upsert_forecast_features(
            [_feature_row("open-meteo-historical-forecast", "best_match", date(2026, 1, 1), 62.0)]
        )

        buffer = StringIO()
        with redirect_stdout(buffer):
            rc = main(
                [
                    "--no-color",
                    "--forecaster-root",
                    str(root),
                    "--db-path",
                    str(db_path),
                    "dataset-research",
                    "--output",
                    str(output),
                ]
            )

        payload = json.loads(output.read_text(encoding="utf-8"))

    assert rc == 0
    assert payload["status"] == "collect_only"
    assert payload["profitability_gate"]["decision"] == "collect_only"
    assert "collect_only" in buffer.getvalue()


def test_dataset_backfill_cli_accepts_lamp_source_choice():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        calls = []

        def fake_lamp(
            store, *, start, end, station_id="KSFO",
            standard_utc_offset_hours=-8, timeout=30,
        ):
            calls.append((store.db_path, start, end, station_id, timeout))
            return DatasetResult("noaa-lamp", 0, "test")

        buffer = StringIO()
        with patch("sfo_kalshi_quant.cli.backfill_lamp", fake_lamp), redirect_stdout(buffer):
            rc = main(
                [
                    "--no-color",
                    "--db-path",
                    str(db_path),
                    "dataset-backfill",
                    "--source",
                    "lamp",
                    "--start-date",
                    "2026-06-26",
                    "--cities",
                    "sfo",
                    "--timeout",
                    "1",
                ]
            )

    assert rc == 0
    assert calls == [(db_path, date(2026, 6, 26), date(2026, 6, 26), "KSFO", 1)]
    assert "noaa-lamp" in buffer.getvalue()


def _feature_row(source: str, model: str, target: date, value: float) -> dict[str, object]:
    return {
        "source": source,
        "model": model,
        "issued_at": f"{target.isoformat()}T07:00:00+00:00",
        "target_date": target.isoformat(),
        "valid_time": target.isoformat(),
        "lead_hours": 0.0,
        "latitude": 37.62,
        "longitude": -122.38,
        "variable": "temperature_2m_max",
        "value": value,
        "units": "degF",
        "source_url": "https://example.test",
        "raw": {},
    }


def _write_ab_test_fixture(path: Path, *, row_count: int) -> None:
    start = date(2026, 1, 1)
    daily = []
    for idx in range(row_count):
        actual = _actual_for_idx(idx)
        daily.append(
            {
                "date": (start + timedelta(days=idx)).isoformat(),
                "actual": actual,
                "lstm": actual + 2.0,
                "xgb": actual + 1.0,
            }
        )
    payload = {"target_daily_high_next_day": {"chart": {"daily": daily}}}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _actual_for_idx(idx: int) -> float:
    return float(62 + (idx % 9))
