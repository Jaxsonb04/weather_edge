"""Task 3: pure Gaussian distributional scoring primitives.

Split out of ``research_walkforward.py`` to keep that module focused on
fold construction and candidate fitting (coding-style file-size guidance),
and because these functions are genuinely a separate concern: every
function here operates on plain ``(mu, sigma, actual)`` floats (or a
sequence of PIT floats for ``max_calibration_gap``) with zero dependency
on ``ResearchCase``/``WalkForwardFold``/candidate types. Nothing in this
module reads or writes the database, the clock, or any random state --
every score here is a deterministic, pure function of its inputs.

Metrics implemented, matching plan Task 3 Step 4's named list:

- ``gaussian_crps`` -- continuous ranked probability score (closed form).
- ``ranked_probability_score`` -- discrete RPS over 1F integer thresholds.
- ``gaussian_log_score`` -- negative log-likelihood under the fitted Gaussian.
- ``interval_covered`` -- whether ``actual`` falls in the fixed 80% interval.
- ``pit_value`` -- probability integral transform, ``Phi((actual-mu)/sigma)``.
- ``bracket_brier`` -- Brier score over Kalshi's own 2F bracket-edge grid.
- ``max_calibration_gap`` -- exact (non-bucketed) KS-style calibration gap
  over a collection of PIT values.
- ``point_error`` -- signed residual ``actual - mu``.
"""

from __future__ import annotations

import math
from typing import Sequence

_SQRT_2PI = math.sqrt(2.0 * math.pi)
_MIN_SIGMA = 0.1
# 80% central-interval z-score (matches forecast_scorecards.py's own fixed
# _INTERVAL_Z table). One nominal level keeps "interval coverage" a single,
# well-defined per-case field rather than an open-ended set of levels.
_INTERVAL_80_Z = 1.28155157
# Kalshi's own bracket convention (cities.py: "list 6 bins at 2F spacing").
# A ResearchCase does not carry any one specific market's floor/cap strikes
# (folded away by source-neutral dedup across possibly several brackets per
# scan), so bracket Brier is scored against this fixed, documented 2F
# canonical grid instead of a per-case market structure that no longer
# exists post-fold.
_BRACKET_WIDTH_F = 2
_THRESHOLD_LO_F = -50
_THRESHOLD_HI_F = 140


def _normal_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def _normal_pdf(z: float) -> float:
    return math.exp(-0.5 * z * z) / _SQRT_2PI


def gaussian_crps(mu: float, sigma: float, actual: float) -> float:
    """Closed-form CRPS for a Gaussian predictive distribution."""

    sigma = max(float(sigma), _MIN_SIGMA)
    z = (actual - mu) / sigma
    phi = _normal_pdf(z)
    return sigma * (z * (2.0 * _normal_cdf(z) - 1.0) + 2.0 * phi - 1.0 / math.sqrt(math.pi))


def gaussian_log_score(mu: float, sigma: float, actual: float) -> float:
    sigma = max(float(sigma), _MIN_SIGMA)
    z = (actual - mu) / sigma
    return math.log(sigma * _SQRT_2PI) + 0.5 * z * z


def pit_value(mu: float, sigma: float, actual: float) -> float:
    """Probability integral transform: ``Phi((actual - mu) / sigma)``.

    Well-calibrated over many cases iff these values are ~Uniform(0, 1);
    ``max_calibration_gap`` measures that directly.
    """

    sigma = max(float(sigma), _MIN_SIGMA)
    return _normal_cdf((actual - mu) / sigma)


def point_error(mu: float, actual: float) -> float:
    """Signed residual, matching ``recalibration.py``'s own bias convention."""

    return actual - mu


def interval_covered(mu: float, sigma: float, actual: float) -> bool:
    """Whether ``actual`` falls inside the fixed 80% central interval."""

    sigma = max(float(sigma), _MIN_SIGMA)
    half_width = _INTERVAL_80_Z * sigma
    return (mu - half_width) <= actual <= (mu + half_width)


def _threshold_score(mu: float, sigma: float, actual: float, *, step: int) -> float:
    sigma = max(float(sigma), _MIN_SIGMA)
    total = 0.0
    count = 0
    for threshold in range(_THRESHOLD_LO_F, _THRESHOLD_HI_F + 1, step):
        probability = _normal_cdf((threshold + 0.5 - mu) / sigma)
        observed = 1.0 if actual <= threshold else 0.0
        total += (probability - observed) ** 2
        count += 1
    return total / count


def ranked_probability_score(mu: float, sigma: float, actual: float) -> float:
    """Discrete RPS: mean squared CDF-vs-indicator error over 1F thresholds."""

    return _threshold_score(mu, sigma, actual, step=1)


def bracket_brier(mu: float, sigma: float, actual: float) -> float:
    """Brier score over Kalshi's own 2F bracket-edge grid (see module note)."""

    return _threshold_score(mu, sigma, actual, step=_BRACKET_WIDTH_F)


def max_calibration_gap(pit_values: Sequence[float]) -> float:
    """Two-sided Kolmogorov-Smirnov gap between PIT values and Uniform(0, 1).

    The maximum absolute distance, over every sample, between the ideal
    uniform CDF and the empirical CDF approached from either side -- an
    exact (not bucketed) "maximum calibration-bucket gap": 0.0 means
    perfect calibration, larger means worse. Callers group PIT values by
    whatever scope they want calibration measured over (one fold, one
    candidate across every fold, etc.) -- this function itself is scope-
    agnostic.
    """

    if not pit_values:
        raise ValueError("max_calibration_gap requires at least one PIT value")
    ordered = sorted(float(v) for v in pit_values)
    n = len(ordered)
    max_gap = 0.0
    for index, value in enumerate(ordered):
        max_gap = max(max_gap, abs(value - index / n), abs(value - (index + 1) / n))
    return max_gap
