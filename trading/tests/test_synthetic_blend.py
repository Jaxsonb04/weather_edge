import json
import math
import tempfile
from datetime import date, timedelta
from pathlib import Path

from sfo_kalshi_quant.synthetic_blend import (
    build_synthetic_blend_calibration,
    load_ab_test_daily_rows,
)


def test_synthetic_blend_calibration_reports_fair_window_comparison():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ab_test_results.json"
        _write_ab_test_fixture(path, row_count=150)

        payload = build_synthetic_blend_calibration(
            path,
            stack_min_train=50,
            calibration_min_train=40,
            ridge_alpha=5.0,
        )

    ridge = payload["models"]["ridge_synthetic_blend"]
    lstm = payload["models"]["lstm_same_window"]

    assert payload["summary"]["synthetic_rows"] == 100
    assert payload["configuration"]["features"] == [
        "lstm_high_f",
        "xgb_high_f",
        "model_spread_f",
        "season_sin",
        "season_cos",
        "yesterday_high_f",
    ]
    assert ridge["point"]["n"] == lstm["point"]["n"] == 100
    assert ridge["calibration"]["n"] == lstm["calibration"]["n"] == 60
    assert ridge["point"]["mae_f"] < lstm["point"]["mae_f"]
    assert payload["summary"]["best_point_model"] == "ridge_synthetic_blend"
    assert payload["ridge_alpha_sweep"]
    assert payload["summary"]["best_ridge_alpha_by_brier"] == payload["ridge_alpha_sweep"][0]["ridge_alpha"]
    assert {
        row["ridge_alpha"] for row in payload["ridge_alpha_sweep"]
    } >= {0.1, 1.0, 10.0}


def test_synthetic_blend_loader_sorts_rows_and_uses_prior_actual():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ab_test_results.json"
        _write_ab_test_fixture(path, row_count=4, reverse=True)

        rows = load_ab_test_daily_rows(path)

    assert [row.local_date for row in rows] == [
        date(2025, 1, 1),
        date(2025, 1, 2),
        date(2025, 1, 3),
        date(2025, 1, 4),
    ]
    assert rows[0].yesterday_high_f == rows[0].lstm_high_f
    assert rows[1].yesterday_high_f == rows[0].actual_high_f


def _write_ab_test_fixture(path: Path, *, row_count: int, reverse: bool = False) -> None:
    start = date(2025, 1, 1)
    daily = []
    for idx in range(row_count):
        local_date = start + timedelta(days=idx)
        seasonal = 6.0 * math.sin(2.0 * math.pi * idx / 30.0)
        wobble = (idx % 7 - 3) * 0.25
        actual = 65.0 + seasonal + wobble
        daily.append(
            {
                "date": local_date.isoformat(),
                "actual": round(actual, 2),
                "lstm": round(actual + 2.0, 2),
                "xgb": round(actual - 2.0, 2),
            }
        )
    if reverse:
        daily.reverse()
    payload = {
        "target_daily_high_next_day": {
            "chart": {
                "daily": daily,
            },
        },
    }
    path.write_text(json.dumps(payload))
