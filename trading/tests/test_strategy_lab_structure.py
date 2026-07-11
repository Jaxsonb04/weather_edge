from pathlib import Path

from sfo_kalshi_quant import strategy_research
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
    assert strategy_research.build_strategy_research is build.build_strategy_research
    assert strategy_research._calibration_payload is calibration._calibration_payload
    assert strategy_research._forecast_health_payload is forecast_health._forecast_health_payload
    assert strategy_research._paper_payload is paper_card._paper_payload
    assert strategy_research._profile_views is profiles._profile_views
    assert strategy_research._dataset_research_summary is dataset_summary._dataset_research_summary
    assert strategy_research._real_money_readiness_payload is readiness._real_money_readiness_payload
    assert strategy_research._strategy_alerts is status_alerts._strategy_alerts
    assert strategy_research._market_consensus_payload is consensus_offline._market_consensus_payload

    shim = Path(strategy_research.__file__).read_text(encoding="utf-8")
    assert len(shim.splitlines()) < 100
