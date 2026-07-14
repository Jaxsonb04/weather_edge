from datetime import date, timedelta

from sfo_kalshi_quant.forecast_challengers import (
    ForecastCase,
    IntradayCase,
    evaluate_matched_lead_emos,
    evaluate_partial_pooled_intraday,
)


def test_matched_lead_challenger_improves_persistent_horizon_bias_but_stays_shadow() -> None:
    start = date(2026, 1, 1)
    cases = [
        ForecastCase(
            station_id="KSFO",
            target_date=start + timedelta(days=index),
            lead_days=1,
            mu=72.0,
            sigma=2.0,
            actual=70.0,
        )
        for index in range(40)
    ]

    result = evaluate_matched_lead_emos(reversed(cases))

    assert result["cases"] == 40
    assert result["candidate_crps"] < result["baseline_crps"]
    assert result["active"] is False
    assert result["promotion_eligible"] is False
    assert "after-fee" in " ".join(result["block_reasons"])


def test_partial_pooled_intraday_learns_city_season_hour_residual_forward_only() -> None:
    start = date(2026, 4, 1)
    cases = [
        IntradayCase(
            station_id="KSFO",
            target_date=start + timedelta(days=index),
            season=1,
            hour_bucket=6,
            observed_high_f=65.0,
            baseline_mu=68.0,
            baseline_sigma=1.5,
            actual=70.0,
        )
        for index in range(40)
    ]

    result = evaluate_partial_pooled_intraday(cases)

    assert result["cases"] == 40
    assert result["independent_days"] == 40
    assert result["candidate_crps"] < result["baseline_crps"]
    assert result["active"] is False
    assert result["promotion_eligible"] is False
