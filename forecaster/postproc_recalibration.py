"""Forecaster-local Gaussian recalibration for offline post-processing research."""

from __future__ import annotations

import math
from dataclasses import dataclass

_MIN_SIGMA_SCALE = 0.5
_MAX_SIGMA_SCALE = 3.0
_MAX_ABS_BIAS_F = 5.0
_DEFAULT_SHRINKAGE_K = 40.0


@dataclass(frozen=True)
class GaussianRecalibration:
    bias_f: float
    sigma_scale: float
    n: int

    @classmethod
    def identity(cls) -> "GaussianRecalibration":
        return cls(bias_f=0.0, sigma_scale=1.0, n=0)

    @property
    def is_identity(self) -> bool:
        return self.bias_f == 0.0 and self.sigma_scale == 1.0

    def apply(self, mu: float, sigma: float) -> tuple[float, float]:
        if sigma <= 0.0:
            return mu + self.bias_f, sigma
        return mu + self.bias_f, sigma * self.sigma_scale


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _std(values: list[float], *, mean: float) -> float:
    if len(values) < 2:
        return 0.0
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(max(0.0, variance))


def fit_recalibration(
    samples: list[tuple[float, float, float]],
    *,
    shrinkage_k: float = _DEFAULT_SHRINKAGE_K,
) -> GaussianRecalibration:
    """Fit the same guarded, shrunk shift/scale map used by trading."""

    usable = [(mu, sigma, y) for mu, sigma, y in samples if sigma and sigma > 0.0]
    n = len(usable)
    if n == 0:
        return GaussianRecalibration.identity()

    residuals = [y - mu for mu, _, y in usable]
    standardized = [(y - mu) / sigma for mu, sigma, y in usable]
    raw_bias = _mean(residuals)
    standardized_mean = _mean(standardized)
    raw_scale = _std(standardized, mean=standardized_mean) if n >= 2 else 1.0
    if raw_scale <= 0.0:
        raw_scale = 1.0

    weight = n / (n + shrinkage_k)
    bias = weight * raw_bias
    sigma_scale = 1.0 + weight * (raw_scale - 1.0)
    bias = max(-_MAX_ABS_BIAS_F, min(_MAX_ABS_BIAS_F, bias))
    sigma_scale = max(_MIN_SIGMA_SCALE, min(_MAX_SIGMA_SCALE, sigma_scale))
    return GaussianRecalibration(bias_f=bias, sigma_scale=sigma_scale, n=n)


def fit_by_cohort(
    samples_by_cohort: dict[str, list[tuple[float, float, float]]],
    *,
    shrinkage_k: float = _DEFAULT_SHRINKAGE_K,
) -> dict[str, GaussianRecalibration]:
    """Fit one independent recalibration for every cohort key."""

    return {
        cohort: fit_recalibration(samples, shrinkage_k=shrinkage_k)
        for cohort, samples in samples_by_cohort.items()
    }
