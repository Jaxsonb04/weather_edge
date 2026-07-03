"""Per-cohort recalibration of the served predictive distribution (Phase 1a).

Phase 0 showed the EMOS Gaussian already beats the blend but leaves *residual*
miscalibration in the warm/hot cohorts (shared-sigma Brier ~0.905 warm, ~1.014
hot -- still near the no-skill line), which is exactly what keeps those cohorts
blocked. This module fits a light, cohort-conditional correction on top of the
served Gaussian and applies it before the bins are integrated, so every
downstream bin probability stays coherent (they still sum to one after the
existing normalization).

Design choice -- parametric PIT recalibration, not per-bin isotonic:

* The bins are integrals of a Gaussian CDF. Recalibrating the *distribution*
  (a bias shift + a dispersion scale) keeps the bin probabilities coherent;
  isotonic maps fit per bin would break sum-to-one and need far more data.
* For a Gaussian, the probability integral transform is uniform iff the
  standardized residuals ``(y - mu) / sigma`` are ~N(0, 1). So the calibrated
  correction is: shift the mean by the residual bias, and scale sigma by the
  standard deviation of the standardized residuals (spread calibration).
* With only months of data every correction is shrunk toward identity by
  ``n / (n + k)``; a cohort with little history barely moves. Full
  non-parametric isotonic-CDF recalibration is deferred until the archive is
  large enough to fit it without overfitting (Phase 3 refit cadence).

Fitting consumes ``(mu, sigma, realized_high)`` triples from the forecast
archive; application is a pure ``(mu, sigma) -> (mu', sigma')`` map. Both are DB
free and fully unit-tested; wiring (where the triples come from and where the
map is applied) lives in the forecaster/trading layers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Guardrails so a small or pathological sample can never produce a wild map even
# before shrinkage. Dispersion is never collapsed below half nor inflated beyond
# 3x; the mean shift is capped at a few degrees F.
_MIN_SIGMA_SCALE = 0.5
_MAX_SIGMA_SCALE = 3.0
_MAX_ABS_BIAS_F = 5.0
_DEFAULT_SHRINKAGE_K = 40.0


@dataclass(frozen=True)
class GaussianRecalibration:
    """A shift+scale correction for a Gaussian predictive distribution."""

    bias_f: float  # added to mu (corrects systematic over/under-forecasting)
    sigma_scale: float  # multiplies sigma (corrects over/under-dispersion)
    n: int  # training sample size behind this correction

    @classmethod
    def identity(cls) -> "GaussianRecalibration":
        return cls(bias_f=0.0, sigma_scale=1.0, n=0)

    @property
    def is_identity(self) -> bool:
        return self.bias_f == 0.0 and self.sigma_scale == 1.0

    def apply(self, mu: float, sigma: float) -> tuple[float, float]:
        """Return the recalibrated ``(mu, sigma)``; safe for any sigma."""

        if sigma <= 0.0:
            return mu + self.bias_f, sigma
        return mu + self.bias_f, sigma * self.sigma_scale


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _std(values: list[float], *, mean: float) -> float:
    if len(values) < 2:
        return 0.0
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(max(0.0, var))


def fit_recalibration(
    samples: list[tuple[float, float, float]],
    *,
    shrinkage_k: float = _DEFAULT_SHRINKAGE_K,
) -> GaussianRecalibration:
    """Fit a shrunk shift+scale from ``(mu, sigma, realized_high)`` triples.

    * ``bias`` is the mean residual ``realized - mu`` (systematic error).
    * ``sigma_scale`` is the standard deviation of the standardized residuals
      ``(realized - mu) / sigma`` -- the multiplier that makes the predictive
      spread match the realized spread (PIT-uniform for a Gaussian).
    * Both are shrunk toward identity by ``lambda = n / (n + k)`` so a short
      record barely moves the served distribution.
    """

    usable = [(mu, sigma, y) for mu, sigma, y in samples if sigma and sigma > 0.0]
    n = len(usable)
    if n == 0:
        return GaussianRecalibration.identity()

    residuals = [y - mu for mu, _, y in usable]
    standardized = [(y - mu) / sigma for mu, sigma, y in usable]

    raw_bias = _mean(residuals)
    # Spread of the standardized residuals about zero (a biased forecast that is
    # otherwise sharp should still be judged under-dispersed only by its scatter,
    # so measure scale about the standardized MEAN, then correct the mean separately).
    z_mean = _mean(standardized)
    raw_scale = _std(standardized, mean=z_mean) if n >= 2 else 1.0
    if raw_scale <= 0.0:
        raw_scale = 1.0

    lam = n / (n + shrinkage_k)
    bias = lam * raw_bias
    sigma_scale = 1.0 + lam * (raw_scale - 1.0)

    # Clamp AFTER shrinkage so the guardrails bound the actually-applied map.
    bias = max(-_MAX_ABS_BIAS_F, min(_MAX_ABS_BIAS_F, bias))
    sigma_scale = max(_MIN_SIGMA_SCALE, min(_MAX_SIGMA_SCALE, sigma_scale))
    return GaussianRecalibration(bias_f=bias, sigma_scale=sigma_scale, n=n)


def fit_by_cohort(
    samples_by_cohort: dict[str, list[tuple[float, float, float]]],
    *,
    shrinkage_k: float = _DEFAULT_SHRINKAGE_K,
) -> dict[str, GaussianRecalibration]:
    """Fit one shrunk recalibration per cohort key."""

    return {
        cohort: fit_recalibration(samples, shrinkage_k=shrinkage_k)
        for cohort, samples in samples_by_cohort.items()
    }
