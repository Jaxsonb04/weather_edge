from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import json
import os
import subprocess
import sys

from sfo_kalshi_quant.backtest import run_walk_forward_calibration_backtest
from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.models import ForecastOutcome
from sfo_kalshi_quant.probability import ResidualCalibrator
from sfo_kalshi_quant.report import calibration_diagnostics


def _outcomes() -> list[ForecastOutcome]:
    start = date(2024, 1, 1)
    return [
        ForecastOutcome(
            local_date=start + timedelta(days=index),
            predicted_high_f=62.0 + index % 12,
            actual_high_f=61.0 + (index * 7) % 15,
            model_name="clean-blend",
            station_id="KSFO",
        )
        for index in range(48)
    ]


def test_persistent_cache_skips_recalibration_and_preserves_numeric_result(
    tmp_path, monkeypatch
):
    outcomes = _outcomes()
    calls = 0
    original = ResidualCalibrator.bucket_probabilities

    def counted(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ResidualCalibrator, "bucket_probabilities", counted)
    first = run_walk_forward_calibration_backtest(
        outcomes, min_train=30, cache_dir=tmp_path
    )
    first_calls = calls
    second = run_walk_forward_calibration_backtest(
        outcomes, min_train=30, cache_dir=tmp_path
    )

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert first_calls == len(outcomes) - 30
    assert calls == first_calls
    assert replace(first, cache_hit=False) == replace(second, cache_hit=False)


def test_cache_invalidates_when_an_older_outcome_is_corrected(tmp_path):
    outcomes = _outcomes()
    run_walk_forward_calibration_backtest(outcomes, min_train=30, cache_dir=tmp_path)
    corrected = list(outcomes)
    corrected[3] = replace(corrected[3], actual_high_f=corrected[3].actual_high_f + 1.0)

    result = run_walk_forward_calibration_backtest(
        corrected, min_train=30, cache_dir=tmp_path
    )

    assert result.cache_hit is False


def test_cache_invalidates_when_strategy_config_changes(tmp_path):
    outcomes = _outcomes()
    run_walk_forward_calibration_backtest(outcomes, min_train=30, cache_dir=tmp_path)

    result = run_walk_forward_calibration_backtest(
        outcomes,
        min_train=30,
        config=StrategyConfig(shrinkage_samples=91),
        cache_dir=tmp_path,
    )

    assert result.cache_hit is False


def test_cache_read_failure_fails_open(tmp_path):
    outcomes = _outcomes()
    run_walk_forward_calibration_backtest(outcomes, min_train=30, cache_dir=tmp_path)
    cache_file = next(tmp_path.glob("*.json"))
    cache_file.write_text("not json", encoding="utf-8")

    result = run_walk_forward_calibration_backtest(
        outcomes, min_train=30, cache_dir=tmp_path
    )

    assert result.cache_hit is False


def test_identical_fresh_process_reads_persistent_cache(tmp_path):
    outcomes = _outcomes()
    run_walk_forward_calibration_backtest(outcomes, min_train=30, cache_dir=tmp_path)
    script = f"""
from datetime import date, timedelta
from sfo_kalshi_quant.backtest import run_walk_forward_calibration_backtest
from sfo_kalshi_quant.models import ForecastOutcome
start = date(2024, 1, 1)
rows = [ForecastOutcome(start + timedelta(days=i), 62.0 + i % 12, 61.0 + (i * 7) % 15, 'clean-blend', 'KSFO') for i in range(48)]
print(run_walk_forward_calibration_backtest(rows, min_train=30, cache_dir={str(tmp_path)!r}).cache_hit)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": "trading"},
    )

    assert completed.stdout.strip() == "True"


def test_calibration_diagnostics_exposes_cache_hit_metadata(tmp_path):
    outcomes = _outcomes()
    first = calibration_diagnostics(
        outcomes, config=StrategyConfig(), min_train=30, cache_dir=tmp_path
    )
    second = calibration_diagnostics(
        outcomes, config=StrategyConfig(), min_train=30, cache_dir=tmp_path
    )

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    first_without_cache = {key: value for key, value in first.items() if key != "cache_hit"}
    second_without_cache = {key: value for key, value in second.items() if key != "cache_hit"}
    assert json.dumps(first_without_cache, sort_keys=True) == json.dumps(
        second_without_cache, sort_keys=True
    )
