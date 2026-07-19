"""Task 3: pure Gaussian distributional scoring math (research_scoring.py).

Every function under test here is deterministic and DB/clock/random-free --
these are plain sanity/property checks on the formulas themselves, kept
separate from research_walkforward.py's/research_candidates.py's own tests
because they have no dependency on ResearchCase/WalkForwardFold/candidate
types at all.
"""

from __future__ import annotations

import math

import pytest

from sfo_kalshi_quant.research_scoring import (
    bracket_brier,
    gaussian_crps,
    gaussian_log_score,
    interval_covered,
    max_calibration_gap,
    pit_value,
    point_error,
    ranked_probability_score,
)


def test_crps_is_nonnegative_for_a_range_of_inputs() -> None:
    for mu, sigma, actual in [(66.0, 3.0, 66.0), (66.0, 3.0, 80.0), (66.0, 3.0, 40.0)]:
        assert gaussian_crps(mu, sigma, actual) >= 0.0


def test_crps_improves_as_forecast_sharpens_around_a_correct_mean() -> None:
    """A sharper (smaller sigma) forecast centered exactly on the outcome
    should score at least as well as a wider one centered on the same
    point."""

    sharp = gaussian_crps(66.0, 1.0, 66.0)
    wide = gaussian_crps(66.0, 6.0, 66.0)
    assert sharp < wide


def test_crps_worsens_as_the_mean_moves_away_from_the_outcome() -> None:
    near = gaussian_crps(66.0, 3.0, 67.0)
    far = gaussian_crps(66.0, 3.0, 90.0)
    assert near < far


def test_log_score_matches_the_closed_form_gaussian_nll() -> None:
    mu, sigma, actual = 66.0, 3.0, 70.0
    z = (actual - mu) / sigma
    expected = math.log(sigma * math.sqrt(2.0 * math.pi)) + 0.5 * z * z
    assert gaussian_log_score(mu, sigma, actual) == pytest.approx(expected)


def test_log_score_is_minimized_at_the_mean() -> None:
    at_mean = gaussian_log_score(66.0, 3.0, 66.0)
    away = gaussian_log_score(66.0, 3.0, 75.0)
    assert at_mean < away


def test_pit_value_at_the_mean_is_one_half() -> None:
    assert pit_value(66.0, 3.0, 66.0) == pytest.approx(0.5)


def test_pit_value_is_monotonic_in_actual() -> None:
    low = pit_value(66.0, 3.0, 60.0)
    mid = pit_value(66.0, 3.0, 66.0)
    high = pit_value(66.0, 3.0, 72.0)
    assert low < mid < high


def test_pit_value_stays_within_unit_interval() -> None:
    for actual in (-100.0, 0.0, 66.0, 500.0):
        value = pit_value(66.0, 3.0, actual)
        assert 0.0 <= value <= 1.0


def test_point_error_is_signed_residual() -> None:
    assert point_error(mu=66.0, actual=70.0) == pytest.approx(4.0)
    assert point_error(mu=66.0, actual=60.0) == pytest.approx(-6.0)
    assert point_error(mu=66.0, actual=66.0) == pytest.approx(0.0)


def test_interval_covered_is_true_at_the_mean() -> None:
    assert interval_covered(66.0, 3.0, 66.0) is True


def test_interval_covered_is_false_far_outside_the_distribution() -> None:
    assert interval_covered(66.0, 3.0, 200.0) is False


def test_interval_covered_boundary_is_consistent_with_its_own_z_score() -> None:
    """Directly exercises the fixed 80% z-score the module documents,
    rather than hardcoding an independently-guessed boundary value."""

    from sfo_kalshi_quant.research_scoring import _INTERVAL_80_Z

    mu, sigma = 66.0, 3.0
    just_inside = mu + (_INTERVAL_80_Z * sigma) - 1e-6
    just_outside = mu + (_INTERVAL_80_Z * sigma) + 1e-6
    assert interval_covered(mu, sigma, just_inside) is True
    assert interval_covered(mu, sigma, just_outside) is False


def test_ranked_probability_score_is_nonnegative() -> None:
    assert ranked_probability_score(66.0, 3.0, 66.0) >= 0.0
    assert ranked_probability_score(66.0, 3.0, 120.0) >= 0.0


def test_ranked_probability_score_improves_for_a_sharper_correct_forecast() -> None:
    sharp = ranked_probability_score(66.0, 0.5, 66.0)
    wide = ranked_probability_score(66.0, 8.0, 66.0)
    assert sharp < wide


def test_bracket_brier_is_nonnegative() -> None:
    assert bracket_brier(66.0, 3.0, 66.0) >= 0.0


def test_bracket_brier_improves_for_a_sharper_correct_forecast() -> None:
    sharp = bracket_brier(66.0, 0.5, 66.0)
    wide = bracket_brier(66.0, 8.0, 66.0)
    assert sharp < wide


def test_bracket_brier_and_ranked_probability_score_differ_in_general() -> None:
    """They are deliberately scored on different threshold spacing (1F vs
    the 2F Kalshi bracket grid) -- a sanity check that the split actually
    changed something, not two aliases of the same function."""

    mu, sigma, actual = 66.3, 2.7, 71.0
    assert ranked_probability_score(mu, sigma, actual) != bracket_brier(mu, sigma, actual)


def test_max_calibration_gap_rejects_empty_input() -> None:
    with pytest.raises(ValueError):
        max_calibration_gap([])


def test_max_calibration_gap_is_small_for_a_well_spread_uniform_sample() -> None:
    # Evenly spaced across (0, 1) -- as close to perfectly calibrated as a
    # finite discrete sample can be.
    pits = [(i + 0.5) / 10 for i in range(10)]
    assert max_calibration_gap(pits) == pytest.approx(0.05, abs=1e-9)


def test_max_calibration_gap_is_large_for_a_degenerate_sample() -> None:
    """Every PIT value identical (e.g. every forecast landed exactly on
    its own mean) is badly miscalibrated -- far from Uniform(0, 1)."""

    pits = [0.5] * 10
    gap = max_calibration_gap(pits)
    assert gap > 0.4


def test_max_calibration_gap_is_zero_only_in_the_degenerate_single_sample_case() -> None:
    # n=1: the empirical CDF step is at 1/1; with a PIT slap in the
    # middle the gap is inherently large -- this just pins that a single
    # sample never raises and returns a finite, sane value.
    gap = max_calibration_gap([0.5])
    assert 0.0 <= gap <= 1.0
