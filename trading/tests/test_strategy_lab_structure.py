from datetime import UTC
from pathlib import Path

from sfo_kalshi_quant import strategy_research
from sfo_kalshi_quant.cities import CITIES
from sfo_kalshi_quant.config import DEFAULT_DB_PATH, DEFAULT_FORECASTER_ROOT, SFO_TZ
from sfo_kalshi_quant.dataset_research import (
    DEFAULT_MIN_AFTER_COST_TRADES,
    DEFAULT_MIN_MATCHED_ROWS,
)
from sfo_kalshi_quant.exits import (
    DEFAULT_NO_STOP_LOSS_PCT,
    DEFAULT_NO_TAKE_PROFIT_PCT,
    DEFAULT_RESEARCH_NO_SETTLEMENT_FIRST_MIN_COST,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
    DEFAULT_YES_STOP_LOSS_PCT,
    DEFAULT_YES_TAKE_PROFIT_PCT,
)
from sfo_kalshi_quant.strategy_lab import (
    build,
    calibration,
    consensus_offline,
    dataset_summary,
    forecast_health,
    paper_card,
    profiles,
    readiness,
    status_alerts,
)


def test_strategy_research_is_a_thin_compatibility_shim():
    assert strategy_research.build_strategy_research is not build.build_strategy_research
    assert strategy_research._build_strategy_research is build.build_strategy_research
    assert strategy_research._calibration_payload is calibration._calibration_payload
    assert strategy_research._forecast_health_payload is forecast_health._forecast_health_payload
    assert strategy_research._paper_payload is paper_card._paper_payload
    assert strategy_research._profile_views is profiles._profile_views
    assert strategy_research._dataset_research_summary is dataset_summary._dataset_research_summary
    assert strategy_research._real_money_readiness_payload is readiness._real_money_readiness_payload
    assert strategy_research._strategy_alerts is status_alerts._strategy_alerts
    assert strategy_research._market_consensus_payload is consensus_offline._market_consensus_payload

    shim = Path(strategy_research.__file__).read_text(encoding="utf-8")
    assert len(shim.splitlines()) < 300
    assert "globals().update" not in shim


def test_strategy_research_preserves_historical_uppercase_imports():
    expected = {
        "UTC": UTC,
        "CITIES": CITIES,
        "DEFAULT_DB_PATH": DEFAULT_DB_PATH,
        "DEFAULT_FORECASTER_ROOT": DEFAULT_FORECASTER_ROOT,
        "SFO_TZ": SFO_TZ,
        "DEFAULT_MIN_AFTER_COST_TRADES": DEFAULT_MIN_AFTER_COST_TRADES,
        "DEFAULT_MIN_MATCHED_ROWS": DEFAULT_MIN_MATCHED_ROWS,
        "DEFAULT_NO_STOP_LOSS_PCT": DEFAULT_NO_STOP_LOSS_PCT,
        "DEFAULT_NO_TAKE_PROFIT_PCT": DEFAULT_NO_TAKE_PROFIT_PCT,
        "DEFAULT_RESEARCH_NO_SETTLEMENT_FIRST_MIN_COST": (
            DEFAULT_RESEARCH_NO_SETTLEMENT_FIRST_MIN_COST
        ),
        "DEFAULT_STOP_LOSS_PCT": DEFAULT_STOP_LOSS_PCT,
        "DEFAULT_TAKE_PROFIT_PCT": DEFAULT_TAKE_PROFIT_PCT,
        "DEFAULT_YES_STOP_LOSS_PCT": DEFAULT_YES_STOP_LOSS_PCT,
        "DEFAULT_YES_TAKE_PROFIT_PCT": DEFAULT_YES_TAKE_PROFIT_PCT,
    }

    assert {name: getattr(strategy_research, name) for name in expected} == expected
