from datetime import date, timedelta

from sfo_kalshi_quant.backtest import run_walk_forward_calibration_backtest
from sfo_kalshi_quant.config import StrategyConfig
from sfo_kalshi_quant.models import ForecastOutcome


def test_walk_forward_backtest_returns_calibration_buckets():
    start = date(2025, 1, 1)
    outcomes = []
    for idx in range(80):
        pred = 66.0 + (idx % 8)
        actual = pred + [-2, -1, 0, 1, 2, 3, -1, 0][idx % 8]
        outcomes.append(
            ForecastOutcome(
                local_date=start + timedelta(days=idx),
                predicted_high_f=pred,
                actual_high_f=actual,
            )
        )

    result = run_walk_forward_calibration_backtest(
        outcomes,
        config=StrategyConfig(min_conditional_samples=10),
        min_train=40,
    )

    assert result.n == 40
    assert len(result.calibration_buckets) == 10
    assert sum(bucket.count for bucket in result.calibration_buckets) > result.n
    assert result.cohorts
    assert sum(cohort.count for cohort in result.cohorts) == result.n


def test_walk_forward_backtest_reports_climatology_skill():
    # The readiness gate judges skill vs a climatological prior, so the backtest
    # must report a finite climatology Brier and a Brier Skill Score (= 1 -
    # model/clim) overall and per cohort.
    start = date(2025, 1, 1)
    outcomes = []
    for idx in range(120):
        pred = 66.0 + (idx % 8)
        actual = pred + [-2, -1, 0, 1, 2, 1, -1, 0][idx % 8]
        outcomes.append(
            ForecastOutcome(
                local_date=start + timedelta(days=idx),
                predicted_high_f=pred,
                actual_high_f=actual,
            )
        )

    result = run_walk_forward_calibration_backtest(
        outcomes,
        config=StrategyConfig(min_conditional_samples=10),
        min_train=60,
    )

    assert result.climatology_brier_score > 0.0
    # 1 - model/clim is internally consistent with the reported briers.
    expected_skill = 1.0 - result.brier_score / result.climatology_brier_score
    assert abs(result.brier_skill - expected_skill) < 1e-9
    for cohort in result.cohorts:
        assert cohort.climatology_brier_score is not None
        assert cohort.brier_skill is not None


def test_walk_forward_backtest_reports_ranked_probability_score_across_bins():
    start = date(2025, 1, 1)
    outcomes = []
    for idx in range(120):
        pred = 66.0 + (idx % 8)
        actual = pred + [-2, -1, 0, 1, 2, 1, -1, 0][idx % 8]
        outcomes.append(
            ForecastOutcome(
                local_date=start + timedelta(days=idx),
                predicted_high_f=pred,
                actual_high_f=actual,
            )
        )

    result = run_walk_forward_calibration_backtest(
        outcomes,
        config=StrategyConfig(min_conditional_samples=10),
        min_train=60,
    )

    assert result.ranked_probability_score >= 0.0
    assert result.climatology_ranked_probability_score > 0.0
    expected_skill = 1.0 - (
        result.ranked_probability_score / result.climatology_ranked_probability_score
    )
    assert abs(result.ranked_probability_skill - expected_skill) < 1e-9
    for cohort in result.cohorts:
        assert cohort.ranked_probability_score >= 0.0
        assert cohort.ranked_probability_skill is not None
