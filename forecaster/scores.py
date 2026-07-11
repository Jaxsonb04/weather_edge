"""Dependency-light probabilistic scoring primitives for daily-high forecasts."""

from __future__ import annotations

import math

SIGMA_FLOOR_F = 1.5


def _normal_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _normal_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def gaussian_crps(mu: float, sigma: float, y: float) -> float:
    """Closed-form CRPS of a Gaussian predictive distribution."""

    sigma = max(sigma, SIGMA_FLOOR_F)
    z = (y - mu) / sigma
    return sigma * (
        z * (2.0 * _normal_cdf(z) - 1.0)
        + 2.0 * _normal_pdf(z)
        - 1.0 / math.sqrt(math.pi)
    )


def gaussian_integer_bin_probs(mu: float, sigma: float) -> dict[int, float]:
    """Gaussian mass in unit-width integer settlement bins within four sigma."""

    sigma = max(sigma, SIGMA_FLOOR_F)
    lo = int(math.floor(mu - 4.0 * sigma))
    hi = int(math.ceil(mu + 4.0 * sigma))
    return {
        value: _normal_cdf((value + 0.5 - mu) / sigma)
        - _normal_cdf((value - 0.5 - mu) / sigma)
        for value in range(lo, hi + 1)
    }


def multicategory_brier(mu: float, sigma: float, realized_bin: int) -> float:
    """Multi-category Brier score for an integer daily-high settlement."""

    probabilities = gaussian_integer_bin_probs(mu, sigma)
    return (
        1.0
        - 2.0 * probabilities.get(realized_bin, 0.0)
        + sum(probability * probability for probability in probabilities.values())
    )
