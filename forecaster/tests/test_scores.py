"""Contract tests for the dependency-light probabilistic scoring primitives."""

from __future__ import annotations

import math

import pytest

from scores import (
    SIGMA_FLOOR_F,
    gaussian_crps,
    gaussian_integer_bin_probs,
    multicategory_brier,
)


def test_scores_match_the_legacy_closed_form_and_integer_bin_math():
    assert SIGMA_FLOOR_F == 1.5
    expected_at_mean = 2.0 * (2.0 / math.sqrt(2.0 * math.pi) - 1.0 / math.sqrt(math.pi))
    assert gaussian_crps(50.0, 2.0, 50.0) == pytest.approx(expected_at_mean)

    probabilities = gaussian_integer_bin_probs(70.0, 2.0)
    assert set(probabilities) == set(range(62, 79))
    assert sum(probabilities.values()) == pytest.approx(0.9999786, rel=1e-5)
    expected_brier = 1.0 - 2.0 * probabilities[70] + sum(p * p for p in probabilities.values())
    assert multicategory_brier(70.0, 2.0, 70) == pytest.approx(expected_brier)


def test_scores_enforce_the_shared_sigma_floor():
    assert gaussian_crps(70.0, 0.01, 71.0) == pytest.approx(
        gaussian_crps(70.0, SIGMA_FLOOR_F, 71.0)
    )
    assert gaussian_integer_bin_probs(70.0, 0.01) == gaussian_integer_bin_probs(
        70.0, SIGMA_FLOOR_F
    )
