"""Tests for the per-cohort Gaussian recalibration core (Phase 1a)."""

import random

from pytest import approx

from sfo_kalshi_quant.recalibration import (
    GaussianRecalibration,
    fit_by_cohort,
    fit_recalibration,
)


def test_identity_leaves_distribution_unchanged():
    ident = GaussianRecalibration.identity()
    assert ident.is_identity
    assert ident.apply(72.0, 2.5) == (72.0, 2.5)


def test_apply_shifts_mean_and_scales_sigma():
    recal = GaussianRecalibration(bias_f=1.5, sigma_scale=1.4, n=100)
    mu, sigma = recal.apply(70.0, 2.0)
    assert mu == approx(71.5)
    assert sigma == approx(2.8)


def test_apply_is_safe_for_nonpositive_sigma():
    recal = GaussianRecalibration(bias_f=1.0, sigma_scale=2.0, n=100)
    mu, sigma = recal.apply(70.0, 0.0)
    assert mu == approx(71.0)
    assert sigma == 0.0  # not scaled when non-positive


def test_empty_sample_fits_identity():
    assert fit_recalibration([]).is_identity


def test_fit_recovers_a_known_bias_with_enough_data():
    # Forecasts systematically 3F too low; sigma correctly sized. With a large n
    # the shrinkage is near 1 so the fitted bias approaches the true +3F.
    rng = random.Random(0)
    samples = []
    for _ in range(2000):
        y = 70.0 + rng.gauss(0.0, 2.0)
        mu = y - 3.0  # forecast is 3F below realized on average
        samples.append((mu, 2.0, y))
    recal = fit_recalibration(samples, shrinkage_k=40.0)
    assert recal.bias_f == approx(3.0, abs=0.3)
    assert recal.sigma_scale == approx(1.0, abs=0.15)


def test_fit_detects_under_dispersion():
    # Predictive sigma is 1.0 but realized scatter is ~2.0 -> the forecast is
    # over-confident and the fitted sigma_scale must inflate above 1.
    rng = random.Random(1)
    samples = [(70.0, 1.0, 70.0 + rng.gauss(0.0, 2.0)) for _ in range(2000)]
    recal = fit_recalibration(samples, shrinkage_k=40.0)
    assert recal.sigma_scale > 1.5


def test_shrinkage_pulls_small_samples_toward_identity():
    # A tiny but wildly biased sample must barely move after shrinkage.
    samples = [(70.0, 2.0, 80.0)] * 3  # 3 points, +10F bias, no spread
    recal = fit_recalibration(samples, shrinkage_k=40.0)
    lam = 3 / (3 + 40.0)
    assert recal.bias_f == approx(lam * 10.0, abs=1e-6)  # heavily shrunk
    assert recal.bias_f < 1.0
    # Same data with a huge n behind it would move much further; shrinkage is the
    # only thing holding it back.
    assert recal.n == 3


def test_guardrails_bound_the_applied_map():
    # A large, absurd bias sample is capped at the guardrail even at full weight.
    samples = [(70.0, 2.0, 90.0)] * 5000  # +20F systematic
    recal = fit_recalibration(samples, shrinkage_k=40.0)
    assert recal.bias_f == approx(5.0)  # _MAX_ABS_BIAS_F cap


def test_fit_by_cohort_fits_each_key_independently():
    warm = [(72.0, 2.0, 75.0)] * 500  # +3F bias
    cool = [(60.0, 2.0, 60.0)] * 500  # unbiased
    fits = fit_by_cohort({"warm": warm, "cool": cool}, shrinkage_k=40.0)
    assert fits["warm"].bias_f > 2.0
    assert fits["cool"].bias_f == approx(0.0, abs=0.2)
