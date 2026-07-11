from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[2]
FORECASTER = ROOT / "forecaster"
if str(FORECASTER) not in sys.path:
    sys.path.insert(0, str(FORECASTER))

import google_weather_cache
from forecast_scoring import is_clean_next_day_forecast
from forecast_validation import chronological_unit_split_masks, forecast_unit_dates
from settlement_calendar import (
    integer_settlement_high_f,
    local_standard_date,
    utc_window_for_local_standard_date,
)
from sfo_kalshi_quant.datasets import DatasetStore
from sfo_kalshi_quant.forecast import ForecastDataError, SfoForecasterAdapter


SFO_TZ = ZoneInfo("America/Los_Angeles")


def _fetched_iso(local_day: date, hour: int = 23, minute: int = 30) -> str:
    return (
        datetime.combine(local_day, time(hour, minute), tzinfo=SFO_TZ)
        .astimezone(timezone.utc)
        .isoformat()
    )


def _create_blend_table(conn: sqlite3.Connection) -> None:
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
            scored_at TEXT
        )
        """
    )


def _insert_blend(
    conn: sqlite3.Connection,
    *,
    target: date,
    fetched_at: str,
    predicted: float,
    actual: float,
    google: float | None = None,
    nws: float | None = None,
    open_meteo: float | None = None,
    history: float | None = None,
    details: dict | None = None,
    refresh: int = 1,
) -> None:
    conn.execute(
        """
        INSERT INTO forecast_blend_daily_high (
            fetched_at, target_date, lead_hours, method, predicted_high_f,
            google_high_f, nws_high_f, open_meteo_high_f, history_high_f,
            google_weight, nws_weight, open_meteo_weight, history_weight,
            station_adjustment_f, fresh_station_count, source_count,
            time_zone, max_calls_per_day, calls_used_today, details_json,
            actual_high_f, abs_error_f, scored_at
        )
        VALUES (?, ?, 20, 'test blend', ?, ?, ?, ?, ?, 0.4, 0.3, 0.2, 0.1,
                0, 3, 4, 'America/Los_Angeles', 6, ?, ?, ?, ?, ?)
        """,
        (
            fetched_at,
            target.isoformat(),
            predicted,
            google,
            nws,
            open_meteo,
            history,
            refresh,
            json.dumps(details or {}),
            actual,
            abs(predicted - actual),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def _write_ab_test_results(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps({"target_daily_high_next_day": {"chart": {"daily": rows}}}),
        encoding="utf-8",
    )


def test_clean_next_day_forecast_excludes_same_day_lock_and_floor():
    target = date(2026, 6, 4)
    assert is_clean_next_day_forecast(target, _fetched_iso(date(2026, 6, 3)), "{}")
    assert not is_clean_next_day_forecast(target, _fetched_iso(target, 9), "{}")
    assert not is_clean_next_day_forecast(
        target,
        _fetched_iso(date(2026, 6, 3)),
        {"observed_high_decision": {"mode": "lock"}},
    )
    assert not is_clean_next_day_forecast(
        target,
        _fetched_iso(date(2026, 6, 3)),
        {"observed_high_decision": {"mode": "floor"}},
    )


def _adaptive_weights_for_db(db_path: Path):
    old_path = google_weather_cache.DB_PATH
    old_cache = getattr(google_weather_cache.adaptive_blend_weights, "_cached", None)
    if hasattr(google_weather_cache.adaptive_blend_weights, "_cached"):
        delattr(google_weather_cache.adaptive_blend_weights, "_cached")
    google_weather_cache.DB_PATH = db_path
    try:
        return google_weather_cache.adaptive_blend_weights()
    finally:
        google_weather_cache.DB_PATH = old_path
        if hasattr(google_weather_cache.adaptive_blend_weights, "_cached"):
            delattr(google_weather_cache.adaptive_blend_weights, "_cached")
        if old_cache is not None:
            google_weather_cache.adaptive_blend_weights._cached = old_cache


def test_adaptive_weights_learn_from_clean_rows_not_same_day_locks():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "weather.db"
        with sqlite3.connect(db_path) as conn:
            _create_blend_table(conn)
            start = date(2026, 5, 25)
            for offset in range(16):
                target = start + timedelta(days=offset)
                _insert_blend(
                    conn,
                    target=target,
                    fetched_at=_fetched_iso(target - timedelta(days=1)),
                    predicted=72,
                    actual=70,
                    google=70,
                    nws=80,
                    open_meteo=80,
                    history=80,
                )
                _insert_blend(
                    conn,
                    target=target,
                    fetched_at=_fetched_iso(target, 10),
                    predicted=70,
                    actual=70,
                    google=80,
                    nws=70,
                    open_meteo=80,
                    history=80,
                    details={"observed_high_decision": {"mode": "lock"}},
                )

        weights, metadata = _adaptive_weights_for_db(db_path)

        assert metadata["mode"] == "adaptive"
        assert metadata["scored_days"] == 16
        assert metadata["source_mae_f"]["google"] == 0.0
        assert metadata["source_mae_f"]["nws"] == 10.0
        assert metadata["holdout"]["candidate_mae_f"] < metadata["holdout"]["base_mae_f"]
        assert weights["google"] > weights["nws"]


def test_adaptive_weights_stay_base_below_minimum_scored_days():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "weather.db"
        with sqlite3.connect(db_path) as conn:
            _create_blend_table(conn)
            start = date(2026, 6, 10)
            for offset in range(5):
                target = start + timedelta(days=offset)
                _insert_blend(
                    conn,
                    target=target,
                    fetched_at=_fetched_iso(target - timedelta(days=1)),
                    predicted=72,
                    actual=70,
                    google=70,
                    nws=80,
                    open_meteo=80,
                    history=80,
                )

        weights, metadata = _adaptive_weights_for_db(db_path)

        assert metadata["mode"] == "base"
        assert metadata["scored_days"] == 5
        assert weights == google_weather_cache.BLEND_WEIGHTS


def test_adaptive_weights_rejected_when_holdout_does_not_improve():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "weather.db"
        with sqlite3.connect(db_path) as conn:
            _create_blend_table(conn)
            start = date(2026, 5, 25)
            for offset in range(16):
                target = start + timedelta(days=offset)
                # Source skill flips for the most recent third: weights
                # learned from the older days must not be promoted.
                google_good = offset < 10
                _insert_blend(
                    conn,
                    target=target,
                    fetched_at=_fetched_iso(target - timedelta(days=1)),
                    predicted=72,
                    actual=70,
                    google=70 if google_good else 84,
                    nws=80 if google_good else 70,
                    open_meteo=80,
                    history=80,
                )

        weights, metadata = _adaptive_weights_for_db(db_path)

        assert metadata["mode"] == "base"
        assert "holdout" in metadata
        assert weights == google_weather_cache.BLEND_WEIGHTS


def _write_dataset_research(path: Path, keys: list[str]) -> None:
    path.write_text(
        json.dumps(
            {
                "accuracy_gate": {
                    "candidates": [
                        {
                            "dataset_key": key,
                            "decision": "accuracy_candidate",
                        }
                        for key in keys
                    ]
                }
            }
        ),
        encoding="utf-8",
    )


def _dataset_row(target: date, value: float, *, issued: str = "2026-06-25T18:00:00+00:00") -> dict[str, object]:
    return {
        "source": "noaa-lamp",
        "model": "lamp",
        "issued_at": issued,
        "target_date": target.isoformat(),
        "valid_time": target.isoformat(),
        "lead_hours": 0.0,
        "latitude": 37.62,
        "longitude": -122.38,
        "variable": "temperature_2m_max",
        "value": value,
        "units": "degF",
        "source_url": "https://example.test/lamp",
        "raw": {},
    }


def test_promoted_dataset_guidance_reads_latest_accuracy_candidate_only():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "dataset.db"
        research_path = Path(tmp) / "dataset_research.json"
        target = date(2026, 6, 26)
        store = DatasetStore(db_path)
        store.upsert_forecast_features(
            [
                _dataset_row(target, 68.0, issued="2026-06-25T12:00:00+00:00"),
                _dataset_row(target, 71.0, issued="2026-06-25T18:00:00+00:00"),
            ]
        )
        _write_dataset_research(
            research_path,
            ["noaa-lamp/lamp/temperature_2m_max/0h"],
        )

        result = google_weather_cache.load_promoted_dataset_guidance(
            target.isoformat(),
            db_path=db_path,
            research_path=research_path,
        )

    assert result["highF"] == 71.0
    assert result["source"] == "Promoted dataset guidance"
    assert result["components"][0]["dataset_key"] == "noaa-lamp/lamp/temperature_2m_max/0h"
    assert result["metadata"]["promoted_count"] == 1


def test_unpromoted_dataset_guidance_is_reported_but_not_weighted():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "dataset.db"
        research_path = Path(tmp) / "dataset_research.json"
        target = date(2026, 6, 26)
        store = DatasetStore(db_path)
        store.upsert_forecast_features([_dataset_row(target, 71.0)])
        _write_dataset_research(research_path, [])

        result = google_weather_cache.load_promoted_dataset_guidance(
            target.isoformat(),
            db_path=db_path,
            research_path=research_path,
        )

    assert result["highF"] is None
    assert result["metadata"]["mode"] == "collect_only"
    assert result["metadata"]["available_unpromoted_count"] == 1


def test_blend_snapshot_includes_promoted_dataset_source_and_postprocessor_metadata():
    target = "2026-06-26"
    old_nws = google_weather_cache.load_nws_forecast_high
    old_open = google_weather_cache.load_open_meteo_forecast_high
    old_history = google_weather_cache.load_history_high
    old_station = google_weather_cache.station_adjustment
    old_weights = google_weather_cache.adaptive_blend_weights
    old_bias = google_weather_cache.rolling_blend_residual_bias
    old_dataset = google_weather_cache.load_promoted_dataset_guidance
    old_source_mos = google_weather_cache.source_mos_corrections
    try:
        google_weather_cache.load_nws_forecast_high = lambda target_iso: {"highF": 68.0, "source": "NWS"}
        google_weather_cache.load_open_meteo_forecast_high = lambda target_iso: {"highF": 68.0, "source": "Open-Meteo"}
        google_weather_cache.load_history_high = lambda target_iso: {"highF": 68.0, "source": "History"}
        google_weather_cache.station_adjustment = lambda: {"value": 0.0, "fresh_station_count": 0}
        google_weather_cache.adaptive_blend_weights = lambda: (dict(google_weather_cache.BLEND_WEIGHTS), {"mode": "base"})
        google_weather_cache.rolling_blend_residual_bias = lambda: (dict(google_weather_cache.DISABLED_BIAS_TABLE), {"mode": "disabled"})
        google_weather_cache.source_mos_corrections = lambda: ({}, {"mode": "disabled", "reason": "test"})
        google_weather_cache.load_promoted_dataset_guidance = lambda target_iso: {
            "highF": 76.0,
            "source": "Promoted dataset guidance",
            "detail": "noaa-lamp/lamp/temperature_2m_max/0h",
            "components": [{"dataset_key": "noaa-lamp/lamp/temperature_2m_max/0h", "corrected_high_f": 76.0}],
            "metadata": {"mode": "promoted", "promoted_count": 1},
        }

        blend = google_weather_cache.build_blend_snapshot(
            {
                "fetched_at": "2026-06-25T18:00:00+00:00",
                "daily_highs": [
                    {
                        "target_date": target,
                        "highF": 68.0,
                        "fetched_at": "2026-06-25T18:00:00+00:00",
                        "lead_hours": 14.0,
                        "time_zone": "Etc/GMT+8",
                    }
                ],
            },
            target,
        )
    finally:
        google_weather_cache.load_nws_forecast_high = old_nws
        google_weather_cache.load_open_meteo_forecast_high = old_open
        google_weather_cache.load_history_high = old_history
        google_weather_cache.station_adjustment = old_station
        google_weather_cache.adaptive_blend_weights = old_weights
        google_weather_cache.rolling_blend_residual_bias = old_bias
        google_weather_cache.load_promoted_dataset_guidance = old_dataset
        google_weather_cache.source_mos_corrections = old_source_mos

    assert blend["source_count"] == 5
    assert blend["predicted_high_f"] > 68.0
    assert blend["details"]["dataset_sources"]["metadata"]["mode"] == "promoted"
    assert blend["details"]["postprocessor_metadata"]["source_mos"]["mode"] == "disabled"
    assert blend["details"]["source_mos"]["corrected_sources"]


def test_clean_blend_outcomes_are_point_in_time_last_prior_day_rows():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_path = root / "weather.db"
        with sqlite3.connect(db_path) as conn:
            _create_blend_table(conn)
            target = date(2026, 6, 4)
            _insert_blend(
                conn,
                target=target,
                fetched_at=_fetched_iso(date(2026, 6, 3), 18),
                predicted=66,
                actual=70,
            )
            _insert_blend(
                conn,
                target=target,
                fetched_at=_fetched_iso(date(2026, 6, 3), 23),
                predicted=69,
                actual=69.6,
            )
            _insert_blend(
                conn,
                target=target,
                fetched_at=_fetched_iso(target, 9),
                predicted=70,
                actual=70,
                details={"observed_high_decision": {"mode": "floor"}},
            )

        outcomes = SfoForecasterAdapter(root).load_clean_blend_outcomes()

        assert len(outcomes) == 1
        assert outcomes[0].local_date == target
        assert outcomes[0].predicted_high_f == 69
        assert outcomes[0].actual_high_f == 70
        assert outcomes[0].model_name == "clean_blend"


def test_clean_blend_outcomes_require_final_cli_truth_when_finality_exists():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with sqlite3.connect(root / "weather.db") as conn:
            _create_blend_table(conn)
            target = date(2026, 7, 10)
            _insert_blend(
                conn,
                target=target,
                fetched_at=_fetched_iso(target - timedelta(days=1), 23),
                predicted=70,
                actual=68,
            )
            conn.execute(
                "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
                "max_temperature_f REAL, is_final INTEGER NOT NULL DEFAULT 1)"
            )
            conn.execute(
                "INSERT INTO cli_settlements VALUES ('KSFO', '2026-07-10', 71, 0)"
            )
        adapter = SfoForecasterAdapter(root)

        with pytest.raises(ForecastDataError, match="No clean next-day blend outcomes"):
            adapter.load_clean_blend_outcomes()

        with sqlite3.connect(root / "weather.db") as conn:
            conn.execute("UPDATE cli_settlements SET is_final=1")
        outcomes = adapter.load_clean_blend_outcomes()
        assert len(outcomes) == 1
        assert outcomes[0].actual_high_f == 71.0


def test_auto_calibration_prefers_clean_blend_when_enough_rows():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_path = root / "weather.db"
        with sqlite3.connect(db_path) as conn:
            _create_blend_table(conn)
            start = date(2026, 1, 1)
            for offset in range(30):
                target = start + timedelta(days=offset)
                _insert_blend(
                    conn,
                    target=target,
                    fetched_at=_fetched_iso(target - timedelta(days=1), 23),
                    predicted=68 + offset % 3,
                    actual=69.6,
                )
        _write_ab_test_results(
            root / "ab_test_results.json",
            [{"date": "2025-12-31", "lstm": 64, "actual": 64.4}],
        )

        outcomes = SfoForecasterAdapter(root).load_calibration_outcomes("auto", min_clean_blend=30)

        assert len(outcomes) == 30
        assert {row.model_name for row in outcomes} == {"clean_blend"}
        assert {row.actual_high_f for row in outcomes} == {70.0}


def test_auto_calibration_falls_back_to_lstm_without_clean_depth():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_ab_test_results(
            root / "ab_test_results.json",
            [{"date": "2025-12-31", "lstm": 64, "actual": 64.5}],
        )

        outcomes = SfoForecasterAdapter(root).load_calibration_outcomes("auto", min_clean_blend=30)

        assert len(outcomes) == 1
        assert outcomes[0].model_name == "lstm"
        assert outcomes[0].actual_high_f == 65.0


def test_chronological_unit_split_keeps_daily_target_dates_out_of_multiple_splits():
    index = pd.date_range("2026-01-01", periods=24 * 40, freq="h", tz="UTC")
    masks = chronological_unit_split_masks(index, "target_daily_high_next_day")
    units = forecast_unit_dates(index, "target_daily_high_next_day")

    split_units = {
        name: set(units[mask])
        for name, mask in masks.items()
    }

    assert split_units["train"].isdisjoint(split_units["val"])
    assert split_units["train"].isdisjoint(split_units["test"])
    assert split_units["val"].isdisjoint(split_units["test"])


def test_settlement_calendar_uses_pacific_standard_report_day():
    # 2026-06-08 00:30 PDT is still 2026-06-07 in Pacific standard time.
    observed = datetime(2026, 6, 8, 7, 30, tzinfo=timezone.utc)
    assert local_standard_date(observed) == date(2026, 6, 7)

    start_utc, end_utc = utc_window_for_local_standard_date(date(2026, 6, 8))
    assert start_utc == datetime(2026, 6, 8, 8, 0, tzinfo=timezone.utc)
    assert end_utc == datetime(2026, 6, 9, 8, 0, tzinfo=timezone.utc)


def test_settlement_high_rounds_to_integer_report_value():
    assert integer_settlement_high_f(69.4) == 69.0
    assert integer_settlement_high_f(69.5) == 70.0
    assert integer_settlement_high_f(69.6) == 70.0
