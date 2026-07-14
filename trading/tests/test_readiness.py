"""Real-money readiness verdict: the documented go-live gate as a single
percentage + per-check breakdown computed from the live rescore + calibration.

The gate judges forecast SKILL (Brier Skill Score > 0, model beats climatology)
per traded cohort -- not a flat absolute Brier -- and scopes per-cohort/side
checks to the FORECAST regime the live block actually gated on, with each traded
cohort and side required to show its OWN positive after-fee ROI."""

from sfo_kalshi_quant.backtest_rescore import (
    ReadinessThresholds,
    compute_real_money_readiness,
)

_TRADED_COHORTS = {"cold_below_60f", "normal_60_69f"}


def _passing_rescore():
    """A live rescore that clears every threshold."""
    return {
        "evidence_kind": "chronological_account_replay",
        "promotion_eligible": True,
        "config_basis": "paper-realism, not real-money validated",
        "counts": {"settled_decisions": 45, "independent_days": 32},
        "candidate": {
            "roi_ci95_day_clustered": [0.02, 0.15],
            "log_growth_per_independent_day": 0.012,
            "max_drawdown_pct": 0.08,
        },
        # Keyed by the forecast-time regime the live block gated on.
        "by_forecast_cohort": {
            "cold_below_60f": {"trades": 20, "independent_days": 16, "roi": 0.06},
            "normal_60_69f": {"trades": 25, "independent_days": 16, "roi": 0.05},
        },
        "by_cohort": {
            "cold_below_60f": {"trades": 20, "independent_days": 16, "roi": 0.06},
            "normal_60_69f": {"trades": 25, "independent_days": 16, "roi": 0.05},
        },
        "by_side": {"NO": {"trades": 45, "independent_days": 32, "roi": 0.055}},
    }


def _good_calibration():
    # Low absolute Brier -- retained for display/detail context only.
    return {c: 0.05 for c in _TRADED_COHORTS}


def _good_skill():
    # Positive Brier Skill Score: the model beats climatology on every traded cohort.
    return {c: 0.30 for c in _TRADED_COHORTS}


def test_ready_when_every_threshold_is_cleared():
    result = compute_real_money_readiness(
        _passing_rescore(),
        calibration_cohort_brier=_good_calibration(),
        calibration_cohort_brier_skill=_good_skill(),
        max_abs_calibration_gap=0.04,
    )
    assert result["ready"] is True
    assert result["verdict"] == "READY"
    assert result["readiness_pct"] == 100.0
    assert result["checks_passed"] == result["checks_total"]


def test_snapshot_rescore_is_diagnostic_only_and_requires_replay():
    rescore = _passing_rescore()
    rescore["evidence_kind"] = "snapshot_rescore"
    rescore["promotion_eligible"] = False
    result = compute_real_money_readiness(
        rescore,
        calibration_cohort_brier=_good_calibration(),
        calibration_cohort_brier_skill=_good_skill(),
        max_abs_calibration_gap=0.04,
    )

    assert result["ready"] is False
    assert result["status"] == "REPLAY_REQUIRED"
    assert result["verdict"] == "REPLAY REQUIRED"


def test_not_ready_and_percentage_reflects_partial_progress():
    rescore = _passing_rescore()
    rescore["counts"]["independent_days"] = 15  # half of 30
    rescore["counts"]["settled_decisions"] = 15  # half of 30
    result = compute_real_money_readiness(
        rescore,
        calibration_cohort_brier=_good_calibration(),
        calibration_cohort_brier_skill=_good_skill(),
        max_abs_calibration_gap=0.04,
    )
    assert result["ready"] is False
    assert result["verdict"] == "NOT READY"
    # Two count checks at 50%, the rest passing -> below 100 but well above 0.
    assert 0.0 < result["readiness_pct"] < 100.0
    days_check = next(c for c in result["checks"] if c["name"] == "independent_days")
    assert days_check["passed"] is False
    assert abs(days_check["progress"] - 0.5) < 1e-9


def test_drawdown_above_ten_percent_blocks_readiness():
    rescore = _passing_rescore()
    rescore["candidate"]["max_drawdown_pct"] = 0.1001
    result = compute_real_money_readiness(
        rescore,
        calibration_cohort_brier=_good_calibration(),
        calibration_cohort_brier_skill=_good_skill(),
        max_abs_calibration_gap=0.04,
    )
    assert result["ready"] is False
    check = next(c for c in result["checks"] if c["name"] == "max_drawdown")
    assert check["passed"] is False


def test_readiness_percentage_climbs_with_more_days():
    low = _passing_rescore()
    low["counts"]["independent_days"] = 6
    high = _passing_rescore()
    high["counts"]["independent_days"] = 24
    pct_low = compute_real_money_readiness(
        low,
        calibration_cohort_brier=_good_calibration(),
        calibration_cohort_brier_skill=_good_skill(),
        max_abs_calibration_gap=0.04,
    )["readiness_pct"]
    pct_high = compute_real_money_readiness(
        high,
        calibration_cohort_brier=_good_calibration(),
        calibration_cohort_brier_skill=_good_skill(),
        max_abs_calibration_gap=0.04,
    )["readiness_pct"]
    assert pct_high > pct_low


def test_fails_closed_when_calibration_is_unavailable():
    # No cohort skill and no calibration gap provided -> those checks cannot
    # pass, so a data-complete book is still NOT READY.
    result = compute_real_money_readiness(_passing_rescore())
    assert result["ready"] is False
    skill_checks = [c for c in result["checks"] if c["name"].startswith("cohort_skill")]
    assert skill_checks and all(c["passed"] is False for c in skill_checks)
    gap_check = next(c for c in result["checks"] if c["name"] == "calibration_gap")
    assert gap_check["passed"] is False


def test_no_skill_cohort_blocks_readiness():
    # The model must BEAT climatology (skill > 0). A cohort with non-positive
    # skill -- even with a low absolute Brier -- blocks the verdict.
    result = compute_real_money_readiness(
        _passing_rescore(),
        calibration_cohort_brier={"cold_below_60f": 0.05, "normal_60_69f": 0.54},
        calibration_cohort_brier_skill={"cold_below_60f": 0.6, "normal_60_69f": -0.05},
        max_abs_calibration_gap=0.04,
    )
    assert result["ready"] is False
    bad = next(c for c in result["checks"] if c["name"] == "cohort_skill:normal_60_69f")
    assert bad["passed"] is False


def test_blocked_regime_is_not_demanded_when_never_forecast_traded():
    # A day forecast NORMAL but that SETTLES warm shows up in by_cohort (settled)
    # but not in by_forecast_cohort. Readiness scopes by forecast cohort, so the
    # warm regime the live block sits out is never demanded -> READY stays
    # reachable. (The cohort-key-mismatch fix.)
    rescore = _passing_rescore()
    rescore["by_cohort"]["warm_70_79f"] = {"trades": 3, "independent_days": 2, "roi": -0.4}
    result = compute_real_money_readiness(
        rescore,
        calibration_cohort_brier=_good_calibration(),
        calibration_cohort_brier_skill=_good_skill(),
        max_abs_calibration_gap=0.04,
    )
    assert result["ready"] is True
    names = {c["name"] for c in result["checks"]}
    assert "cohort_skill:warm_70_79f" not in names
    assert "cohort_roi:warm_70_79f" not in names


def test_losing_cohort_blocks_readiness():
    # Each traded cohort must carry its OWN positive after-fee ROI; a quietly
    # losing cohort blocks the verdict even when the portfolio aggregate is green.
    rescore = _passing_rescore()
    rescore["by_forecast_cohort"]["normal_60_69f"]["roi"] = -0.02
    result = compute_real_money_readiness(
        rescore,
        calibration_cohort_brier=_good_calibration(),
        calibration_cohort_brier_skill=_good_skill(),
        max_abs_calibration_gap=0.04,
    )
    assert result["ready"] is False
    bad = next(c for c in result["checks"] if c["name"] == "cohort_roi:normal_60_69f")
    assert bad["passed"] is False


def test_losing_side_blocks_readiness():
    rescore = _passing_rescore()
    rescore["by_side"]["NO"]["roi"] = -0.01
    result = compute_real_money_readiness(
        rescore,
        calibration_cohort_brier=_good_calibration(),
        calibration_cohort_brier_skill=_good_skill(),
        max_abs_calibration_gap=0.04,
    )
    assert result["ready"] is False
    bad = next(c for c in result["checks"] if c["name"] == "side_roi:NO")
    assert bad["passed"] is False


def test_negative_roi_lower_ci_blocks_readiness():
    rescore = _passing_rescore()
    rescore["candidate"]["roi_ci95_day_clustered"] = [-0.03, 0.10]
    result = compute_real_money_readiness(
        rescore,
        calibration_cohort_brier=_good_calibration(),
        calibration_cohort_brier_skill=_good_skill(),
        max_abs_calibration_gap=0.04,
    )
    assert result["ready"] is False
    roi_check = next(c for c in result["checks"] if c["name"] == "after_fee_roi_lower_ci_positive")
    assert roi_check["passed"] is False


def test_custom_thresholds_are_honored():
    rescore = _passing_rescore()
    rescore["counts"]["independent_days"] = 10
    rescore["counts"]["settled_decisions"] = 10
    relaxed = ReadinessThresholds(min_independent_days=10, min_settled_decisions=10)
    result = compute_real_money_readiness(
        rescore,
        calibration_cohort_brier=_good_calibration(),
        calibration_cohort_brier_skill=_good_skill(),
        max_abs_calibration_gap=0.04,
        thresholds=relaxed,
    )
    assert result["ready"] is True
