from datetime import date, timedelta

import pytest

from emos_forecast import SERVE_RECAL_BIAS, SERVE_RECAL_SIGMA
from emos_recalibration import (
    BIAS_DEADBAND_T,
    SHRINKAGE_K,
    TRAILING_WINDOW_DAYS,
    compute_correction,
    window_rows,
)

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


def _matched_predictions(monkeypatch, cases):
    import sfo_kalshi_quant.forecast_challengers as challengers

    scored = []
    original = challengers._crps

    def capture(mu, sigma, actual):
        scored.append((mu, sigma))
        return original(mu, sigma, actual)

    with monkeypatch.context() as patched:
        patched.setattr(challengers, "_crps", capture)
        result = challengers.evaluate_matched_lead_emos(cases)
    # Each case scores its baseline, then its shadow candidate.
    return result, scored[1::2]


@pytest.mark.parametrize("lead_days", [1, 2])
def test_matched_lead_truth_cannot_change_prediction_before_it_is_available(
    monkeypatch, lead_days
):
    start = date(2026, 1, 1)
    cases = [ForecastCase("KSFO", start + timedelta(days=i), lead_days, 72.0, 2.0, 70.0)
             for i in range(14)]
    target_index = 9
    _, before = _matched_predictions(monkeypatch, cases)
    poisoned = [ForecastCase(row.station_id, row.target_date, row.lead_days,
                             row.mu, row.sigma,
                             row.actual + (1000.0 if target_index - lead_days <= i <= target_index else 0.0))
                for i, row in enumerate(cases)]
    _, after = _matched_predictions(monkeypatch, poisoned)
    assert after[target_index] == before[target_index]
    assert after[-1] != before[-1]
    available = list(cases)
    index = target_index - lead_days - 1
    row = available[index]
    available[index] = ForecastCase(row.station_id, row.target_date, row.lead_days,
                                    row.mu, row.sigma, row.actual + 1000.0)
    assert _matched_predictions(monkeypatch, available)[1][target_index] != before[target_index]


def test_matched_lead_challenger_matches_production_calendar_window_and_correction(monkeypatch):
    start = date(2026, 1, 1)
    # Irregular coverage distinguishes a 45-calendar-day window from 60 rows;
    # noisy early residuals distinguish the production deadband from a mean.
    cases = [ForecastCase("KSFO", start + timedelta(days=i), 2,
                          72.0, 2.5, 70.0 + (4.0 if i % 7 == 0 else 0.0))
             for i in range(80) if i % 4 != 0]
    result, predictions = _matched_predictions(monkeypatch, cases)
    for index, row in enumerate(cases):
        series = [(past.target_date, past.mu, past.sigma, past.actual) for past in cases[:index]]
        window = window_rows(series, row.target_date - timedelta(days=row.lead_days))
        correction = compute_correction(window, apply_bias=SERVE_RECAL_BIAS,
                                        apply_sigma=SERVE_RECAL_SIGMA)
        assert predictions[index] == pytest.approx(correction.apply(row.mu, row.sigma))
    assert result["configuration"] == {
        "window_days": TRAILING_WINDOW_DAYS,
        "shrinkage_k": SHRINKAGE_K,
        "bias_deadband_t": BIAS_DEADBAND_T,
        "apply_bias": SERVE_RECAL_BIAS,
        "apply_sigma": SERVE_RECAL_SIGMA,
        "truth_availability": "history_target_date < forecast_serve_date",
    }
    assert result["active"] is False and result["promotion_eligible"] is False


def test_matched_lead_challenger_waits_for_three_available_residuals():
    cases = [ForecastCase("KSFO", date(2026, 1, 1) + timedelta(days=i), 2,
                          72.0, 2.0, 70.0) for i in range(5)]
    result = evaluate_matched_lead_emos(cases)
    assert result["candidate_crps"] == result["baseline_crps"]


def test_matched_lead_challenger_deduplicates_identical_target_rows():
    cases = [ForecastCase("KSFO", date(2026, 1, 1) + timedelta(days=i), 1,
                          72.0, 2.0, 70.0) for i in range(40)]
    original = evaluate_matched_lead_emos(cases)
    doubled = evaluate_matched_lead_emos(case for case in cases for _copy in range(2))
    assert doubled["cases"] == original["cases"] == 40
    assert doubled["candidate_crps"] == original["candidate_crps"]
    assert doubled["duplicate_cases_dropped"] == 40


def test_matched_lead_challenger_rejects_conflicting_target_rows():
    target = date(2026, 1, 1)
    result = evaluate_matched_lead_emos([
        ForecastCase("KSFO", target, 1, 72.0, 2.0, 70.0),
        ForecastCase("KSFO", target, 1, 73.0, 2.0, 70.0),
    ])
    assert result["available"] is False
    assert result["cases"] == 0
    assert "conflicting" in " ".join(result["block_reasons"]).lower()


@pytest.mark.parametrize("field,value", [
    ("mu", float("nan")), ("mu", float("inf")), ("actual", float("-inf")),
    ("actual", float("nan")), ("sigma", float("nan")), ("sigma", float("inf")),
    ("sigma", 0.0), ("sigma", -2.0), ("mu", True),
])
def test_matched_lead_challenger_rejects_invalid_inputs_before_any_score(
    monkeypatch, field, value
):
    import sfo_kalshi_quant.forecast_challengers as challengers

    valid = ForecastCase("KSFO", date(2026, 1, 1), 1, 72.0, 2.0, 70.0)
    values = {"station_id": "KSFO", "target_date": date(2026, 1, 2),
              "lead_days": 1, "mu": 72.0, "sigma": 2.0, "actual": 70.0}
    values[field] = value

    def score_must_not_run(*args):
        pytest.fail("invalid input must invalidate the diagnostic before any scoring")

    monkeypatch.setattr(challengers, "_crps", score_must_not_run)
    with pytest.raises(ValueError, match="finite.*positive sigma"):
        evaluate_matched_lead_emos([valid, ForecastCase(**values)])


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
