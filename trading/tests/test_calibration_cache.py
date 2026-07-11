from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys

import pytest

from sfo_kalshi_quant.backtest import (
    _prune_calibration_cache,
    _write_calibration_cache,
    run_walk_forward_calibration_backtest,
)
from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.models import ForecastOutcome
from sfo_kalshi_quant.probability import ResidualCalibrator
from sfo_kalshi_quant.report import calibration_diagnostics
from sfo_kalshi_quant.standard_bins import standard_sfo_bins


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


def _write_cache_batch(cache_dir: str, result, start: int, count: int) -> None:
    from pathlib import Path

    for index in range(start, start + count):
        _write_calibration_cache(Path(cache_dir) / f"stress-{index:03d}.json", result)


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


@pytest.mark.parametrize(
    "mutate",
    [
        lambda market: replace(
            market,
            yes_bid=0.21,
            yes_ask=0.24,
            no_bid=0.76,
            no_ask=0.79,
        ),
        lambda market: replace(
            market,
            yes_bid_size=123.0,
            yes_ask_size=456.0,
            raw={**market.raw, "no_bid_size_fp": "321.00"},
        ),
        lambda market: replace(market, status="closed"),
    ],
    ids=("price", "depth", "status"),
)
def test_cache_invalidates_on_any_normalized_market_input_change(
    tmp_path, mutate
):
    outcomes = _outcomes()
    markets = standard_sfo_bins("KXHIGHTSFO-CACHE")
    run_walk_forward_calibration_backtest(
        outcomes, min_train=30, markets=markets, cache_dir=tmp_path
    )
    changed = [mutate(markets[0]), *markets[1:]]

    cached_run = run_walk_forward_calibration_backtest(
        outcomes, min_train=30, markets=changed, cache_dir=tmp_path
    )
    fresh_run = run_walk_forward_calibration_backtest(
        outcomes, min_train=30, markets=changed, cache_dir=None
    )

    assert cached_run.cache_hit is False
    assert cached_run.brier_score == fresh_run.brier_score
    assert replace(cached_run, cache_hit=False) == fresh_run


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


def test_concurrent_cache_writers_leave_bounded_pruned_directory(tmp_path):
    result = run_walk_forward_calibration_backtest(
        _outcomes(), min_train=30, cache_dir=None
    )
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=8, mp_context=context) as pool:
        futures = [
            pool.submit(_write_cache_batch, str(tmp_path), result, start, 40)
            for start in range(0, 320, 40)
        ]
        for future in futures:
            future.result()

    assert len(list(tmp_path.glob("*.json"))) <= 128
    assert not list(tmp_path.glob("*.tmp"))


def test_prune_retries_when_a_concurrent_writer_removes_a_scanned_file(
    tmp_path, monkeypatch
):
    for index in range(140):
        (tmp_path / f"entry-{index:03d}.json").write_text("{}", encoding="utf-8")
    original_stat = Path.stat
    raced = False

    def racing_stat(path, *args, **kwargs):
        nonlocal raced
        if not raced and path.name == "entry-000.json":
            raced = True
            path.unlink()
            raise FileNotFoundError(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", racing_stat)

    _prune_calibration_cache(tmp_path)

    assert raced is True
    assert len(list(tmp_path.glob("*.json"))) <= 128
