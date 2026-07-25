from datetime import date, timedelta

from emos_forecast import SERVE_RECAL_BIAS, SERVE_RECAL_SIGMA
from emos_recalibration import BIAS_DEADBAND_T, SHRINKAGE_K, TRAILING_WINDOW_DAYS
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


def test_matched_lead_challenger_waits_for_truth_available_at_forecast_time() -> None:
    start = date(2026, 1, 1)
    cases = [
        ForecastCase(
            station_id="KSFO",
            target_date=start + timedelta(days=index),
            lead_days=2,
            mu=72.0,
            sigma=2.0,
            actual=70.0,
        )
        for index in range(5)
    ]

    result = evaluate_matched_lead_emos(cases)

    # A two-day-ahead forecast for D can use truth only through D-3. During
    # these first five consecutive targets, no case has the three available
    # residuals required by the production bias correction, so the shadow
    # candidate must remain identical to its baseline.
    assert result["candidate_crps"] == result["baseline_crps"]


def test_matched_lead_challenger_records_production_calibration_configuration() -> None:
    result = evaluate_matched_lead_emos(
        [
            ForecastCase(
                station_id="KSFO",
                target_date=date(2026, 1, 1),
                lead_days=1,
                mu=72.0,
                sigma=2.0,
                actual=70.0,
            )
        ]
    )

    assert result["configuration"] == {
        "window_days": TRAILING_WINDOW_DAYS,
        "shrinkage_k": SHRINKAGE_K,
        "bias_deadband_t": BIAS_DEADBAND_T,
        "apply_bias": SERVE_RECAL_BIAS,
        "apply_sigma": SERVE_RECAL_SIGMA,
        "truth_availability": "history_target_date < forecast_serve_date",
    }


def test_matched_lead_challenger_deduplicates_identical_station_lead_targets() -> None:
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

    baseline = evaluate_matched_lead_emos(cases)
    duplicated = evaluate_matched_lead_emos(
        case for case in cases for _copy in range(2)
    )

    assert duplicated["cases"] == baseline["cases"] == 40
    assert duplicated["candidate_crps"] == baseline["candidate_crps"]
    assert duplicated["duplicate_cases_dropped"] == 40


def test_matched_lead_challenger_fails_closed_on_conflicting_target_rows() -> None:
    target = date(2026, 1, 1)
    result = evaluate_matched_lead_emos(
        [
            ForecastCase("KSFO", target, 1, 72.0, 2.0, 70.0),
            ForecastCase("KSFO", target, 1, 73.0, 2.0, 70.0),
        ]
    )

    assert result["available"] is False
    assert result["cases"] == 0
    assert "conflicting" in " ".join(result["block_reasons"]).lower()


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
