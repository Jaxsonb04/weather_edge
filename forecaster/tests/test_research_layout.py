from __future__ import annotations

import importlib
import inspect
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FORECASTER = ROOT / "forecaster"
RESEARCH = FORECASTER / "research"

OFFLINE_MODULES = (
    "forecast_tomorrow",
    "load_to_db",
    "combine_psv",
    "eda",
    "lstm_model",
    "xgboost_model",
    "ab_test",
    "compare_models",
    "features",
    "forecast_validation",
    "fetch_inland_history",
)


def test_offline_forecaster_modules_live_only_in_research_package():
    assert (RESEARCH / "__init__.py").is_file()
    for module in OFFLINE_MODULES:
        assert (RESEARCH / f"{module}.py").is_file()
        assert not (FORECASTER / f"{module}.py").exists()


def test_lightweight_research_helpers_import_from_package():
    features = importlib.import_module("research.features")
    validation = importlib.import_module("research.forecast_validation")
    assert callable(features.engineer_features)
    assert callable(validation.forecast_unit_dates)


def test_production_source_sync_keeps_research_tree():
    filter_text = (
        ROOT / "trading/deploy/aws/forecaster-runtime.rsync-filter"
    ).read_text()
    assert "research/" not in filter_text


def test_root_manifest_is_the_only_install_manifest():
    manifests = sorted(
        path.relative_to(ROOT)
        for path in ROOT.rglob("pyproject.toml")
        if ".venv" not in path.parts
    )
    assert manifests == [Path("pyproject.toml")]


def test_retired_forecaster_symbols_are_gone():
    import clisfo
    import emos_forecast
    import forecast_postproc_backtest
    import nwp_archive

    assert not hasattr(emos_forecast, "load_emos_archive")
    assert not hasattr(nwp_archive, "SETTLEMENT_TZ")
    assert not hasattr(nwp_archive, "KSFO_LATITUDE")
    assert not hasattr(nwp_archive, "KSFO_LONGITUDE")
    assert not hasattr(clisfo, "fetch_recent_clisfo_settlements")
    assert not hasattr(clisfo, "_recent_clisfo_urls")
    assert "fallback_sigma" not in inspect.signature(
        forecast_postproc_backtest.make_nwp_consensus_predictor
    ).parameters
