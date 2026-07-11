"""Regression tests for the live forecaster's dependency boundaries."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from postproc_recalibration import fit_by_cohort as forecaster_fit_by_cohort
from sfo_kalshi_quant.recalibration import fit_by_cohort as trading_fit_by_cohort


def test_forecaster_recalibration_is_numerically_identical_to_trading_reference():
    samples = {
        "cold": [(55.0, 2.0, 57.0), (58.0, 0.0, 61.0)],
        "warm": [(70.0, 2.0, 72.0), (74.0, 4.0, 70.0), (71.0, 1.0, 72.5)],
        "empty": [],
    }

    local = forecaster_fit_by_cohort(samples, shrinkage_k=17.0)
    reference = trading_fit_by_cohort(samples, shrinkage_k=17.0)

    assert local.keys() == reference.keys()
    for key in local:
        assert local[key].bias_f == pytest.approx(reference[key].bias_f)
        assert local[key].sigma_scale == pytest.approx(reference[key].sigma_scale)
        assert local[key].n == reference[key].n
        assert local[key].apply(72.0, 2.5) == pytest.approx(reference[key].apply(72.0, 2.5))


def test_live_emos_import_graph_excludes_research_and_trading_packages():
    forecaster = Path(__file__).resolve().parents[1]
    code = f"""
import json, sys
sys.path.insert(0, {str(forecaster)!r})
import emos_forecast
print(json.dumps(sorted(sys.modules)))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(forecaster)
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=forecaster,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    modules = set(json.loads(completed.stdout))

    assert "forecast_postproc_backtest" not in modules
    assert "forecast_backtest" not in modules
    assert "google_weather_cache" not in modules
    assert not any(name == "sfo_kalshi_quant" or name.startswith("sfo_kalshi_quant.") for name in modules)
