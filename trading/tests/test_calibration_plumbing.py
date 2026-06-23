"""Phase 0 plumbing guards: the calibration-edge scaffolding must be INERT.

These lock the acceptance criterion that the new config fields and the
calibrator-factory seam do not change behavior at their defaults, so the
plumbing can ship ahead of the phases that wire it.
"""

from datetime import date, timedelta

from sfo_kalshi_quant.backtest import run_walk_forward_calibration_backtest
from sfo_kalshi_quant.config import StrategyConfig, strategy_config_for_profile
from sfo_kalshi_quant.models import ForecastOutcome
from sfo_kalshi_quant.probability import ResidualCalibrator


def _synthetic_outcomes(n: int) -> list[ForecastOutcome]:
    base = date(2025, 1, 1)
    outcomes = []
    for i in range(n):
        predicted = 60.0 + float(i % 15)
        actual = predicted + (1.0 if i % 3 == 0 else -1.0)
        outcomes.append(
            ForecastOutcome(
                local_date=base + timedelta(days=i),
                predicted_high_f=predicted,
                actual_high_f=actual,
                model_name="synthetic",
            )
        )
    return outcomes


def test_calibration_edge_config_fields_are_identity_by_default():
    cfg = StrategyConfig()
    assert cfg.theta_recalibration == 1.0  # sigmoid(1*logit p) == p
    assert cfg.regime_bias_enabled is False
    assert cfg.regime_bias_shrink_k == 30.0
    assert cfg.regime_bias_cap_f == 1.5
    assert cfg.cohort_kelly_multipliers == ()
    # Neither profile may accidentally override the inert defaults.
    for profile in ("live", "research"):
        resolved = strategy_config_for_profile(profile)
        assert resolved.theta_recalibration == 1.0
        assert resolved.regime_bias_enabled is False
        assert resolved.cohort_kelly_multipliers == ()


def test_calibrator_factory_seam_is_transparent_at_default():
    # min_train must stay >= 30: ResidualCalibrator requires >=30 training outcomes.
    outcomes = _synthetic_outcomes(50)
    baseline = run_walk_forward_calibration_backtest(outcomes, min_train=35)
    seamed = run_walk_forward_calibration_backtest(
        outcomes, min_train=35, calibrator_factory=ResidualCalibrator
    )
    assert seamed.n == baseline.n
    assert seamed.brier_score == baseline.brier_score
    assert seamed.log_loss == baseline.log_loss
    assert seamed.brier_skill == baseline.brier_skill
