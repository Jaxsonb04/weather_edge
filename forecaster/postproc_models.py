"""Trained probabilistic post-processors for the SFO daily high (Phase 1).

Two leakage-safe, rolling-origin predictors that turn the multi-model NWP archive
into a calibrated predictive distribution -- the object the trade engine needs:

* **EMOS / NGR** (Ensemble Model Output Statistics, a.k.a. non-homogeneous
  Gaussian regression; Gneiting et al. 2005). Per-model rolling bias-correction
  (each NWP model has a systematic, learnable offset -- ICON runs cold at the
  coast, NBM warm), then a Gaussian whose mean is an affine fit of the debiased
  ensemble mean and whose variance is a regression on the cross-model spread
  (agreement -> sharp, disagreement -> wide). All fits are closed-form
  (ordinary least squares), so they are robust on the small samples this project
  has and need no scipy.

* **Analog Ensemble** (AnEn; Delle Monache et al. 2013). Non-parametric: for the
  target day, find the K most similar past forecast days and use *their realized
  CLISFO highs* as the predictive ensemble. No distributional assumption, and a
  strong, hard-to-beat baseline.

Leakage discipline (the whole point -- two prior interventions in this repo died
out-of-sample on subtle leakage):

* Strict rolling-origin: a prediction for day D is fit using ONLY days strictly
  before D. History is appended *after* the prediction is made.
* Every standardisation/normalisation statistic the analog distance uses is
  computed from the training history alone, never the target day or the future.
* A warm-up minimum-training guard: early days return no prediction (excluded and
  counted by the scoreboard) rather than being fit on too little history.

Pure standard library; depends only on the small scoring primitives module so
there is no import cycle with either scoreboard that consumes these.
"""

from __future__ import annotations

import math
from statistics import fmean, pstdev, stdev

from scores import SIGMA_FLOOR_F

# A predictive day needs at least this many models for an honest ensemble
# mean/spread, matching the scoreboard's consensus floor.
MIN_MODELS = 3
EMOS_MIN_TRAIN = 60
ANALOG_MIN_TRAIN = 120
ANALOG_K = 25


# --------------------------------------------------------------------------- #
# Small numerics (closed-form, stdlib only)
# --------------------------------------------------------------------------- #

def _simple_ols(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Ordinary least squares for y = intercept + slope * x (2 parameters).

    Degenerate inputs (n < 2 or zero x-variance) fall back to the mean of y with
    zero slope -- a safe, well-defined estimator rather than a divide-by-zero.
    """

    n = len(xs)
    if n < 2:
        return (fmean(ys) if ys else 0.0, 0.0)
    sx = sum(xs)
    sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-12:
        return (sy / n, 0.0)
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return intercept, slope


def _spread(values: list[float]) -> float:
    return stdev(values) if len(values) >= 2 else 0.0


def day_mean_spread(models_for_day: dict[str, float] | None):
    """(raw ensemble mean, cross-model spread, present-model dict) or None."""

    if not models_for_day or len(models_for_day) < MIN_MODELS:
        return None
    values = list(models_for_day.values())
    return fmean(values), _spread(values), dict(models_for_day)


# --------------------------------------------------------------------------- #
# EMOS / NGR
# --------------------------------------------------------------------------- #

class EmosParams:
    __slots__ = ("biases", "mu_a", "mu_b", "var_c", "var_d", "weights")

    def __init__(self, biases, mu_a, mu_b, var_c, var_d, weights=None):
        self.biases = biases
        self.mu_a = mu_a
        self.mu_b = mu_b
        self.var_c = var_c
        self.var_d = var_d
        self.weights = weights or {}  # per-model weight; empty => equal weight


def _weighted_debiased_mean(models_for_day, biases, weights) -> float:
    """Debiased per-model values combined by per-model weight (equal when empty)."""

    numerator = 0.0
    denominator = 0.0
    for model, value in models_for_day.items():
        weight = weights.get(model, 1.0)
        numerator += weight * (value - biases.get(model, 0.0))
        denominator += weight
    if denominator <= 0.0:
        return fmean([value - biases.get(model, 0.0) for model, value in models_for_day.items()])
    return numerator / denominator


def fit_emos(
    history: list[tuple[dict[str, float], float]],
    *,
    weight_mode: str = "equal",
) -> EmosParams | None:
    """Fit EMOS from past (models_for_day, truth) pairs only.

    Step 1: per-model bias = mean(model - truth) over the days the model appears.
    Step 1b (weight_mode='inv_var'): per-model weight = 1 / debiased-error-variance,
            so a chronically noisy model (e.g. coarse-grid ICON near the coast) is
            down-weighted beyond just bias-correction. 'equal' keeps the robust
            equal-weight mean.
    Step 2: mu = a + b * weighted_debiased_mean   (OLS of truth on that mean).
    Step 3: var = c + d * spread^2                 (OLS of squared residual on
            squared spread; d clipped >= 0 so more disagreement never *lowers*
            predicted uncertainty, falling back to homoscedastic otherwise).
    """

    if len(history) < 2:
        return None

    # Step 1: per-model systematic bias.
    sums: dict[str, list[float]] = {}
    for models_for_day, truth in history:
        for model, value in models_for_day.items():
            acc = sums.setdefault(model, [0.0, 0.0])
            acc[0] += value - truth
            acc[1] += 1.0
    biases = {model: total / count for model, (total, count) in sums.items() if count}

    # Step 1b: optional inverse-error-variance weighting (debiased residuals).
    weights: dict[str, float] = {}
    if weight_mode == "inv_var":
        err_sq: dict[str, list[float]] = {}
        for models_for_day, truth in history:
            for model, value in models_for_day.items():
                err = (value - biases.get(model, 0.0)) - truth
                acc = err_sq.setdefault(model, [0.0, 0.0])
                acc[0] += err * err
                acc[1] += 1.0
        weights = {model: 1.0 / max(total / count, 0.25) for model, (total, count) in err_sq.items() if count}

    means = [_weighted_debiased_mean(m, biases, weights) for m, _ in history]
    truths = [t for _, t in history]
    mu_a, mu_b = _simple_ols(means, truths)

    # Step 3: variance (spread -> error) regression.
    residual_sq = []
    spread_sq = []
    for (models_for_day, truth), mean in zip(history, means):
        resid = truth - (mu_a + mu_b * mean)
        residual_sq.append(resid * resid)
        spread = _spread(list(models_for_day.values()))
        spread_sq.append(spread * spread)
    var_c, var_d = _simple_ols(spread_sq, residual_sq)
    if var_d < 0.0:  # disagreement must not reduce predicted uncertainty
        var_d = 0.0
        var_c = fmean(residual_sq)

    return EmosParams(biases, mu_a, mu_b, var_c, var_d, weights)


def apply_emos(params: EmosParams, models_for_day: dict[str, float]) -> tuple[float, float]:
    mean = _weighted_debiased_mean(models_for_day, params.biases, params.weights)
    spread = _spread(list(models_for_day.values()))
    mu = params.mu_a + params.mu_b * mean
    variance = params.var_c + params.var_d * spread * spread
    sigma = math.sqrt(max(variance, SIGMA_FLOOR_F * SIGMA_FLOOR_F))
    return mu, sigma


def emos_ngr_predictions(
    dates_sorted: list[str],
    truth: dict[str, float],
    nwp_by_date: dict[str, dict[str, float]],
    *,
    min_train: int = EMOS_MIN_TRAIN,
    weight_mode: str = "equal",
) -> dict[str, tuple[float, float]]:
    """Rolling-origin EMOS predictions: each day fit on strictly-prior days."""

    preds: dict[str, tuple[float, float]] = {}
    history: list[tuple[dict[str, float], float]] = []
    for date_str in dates_sorted:
        models_for_day = nwp_by_date.get(date_str)
        usable = bool(models_for_day) and len(models_for_day) >= MIN_MODELS
        if usable and len(history) >= min_train:
            params = fit_emos(history, weight_mode=weight_mode)
            if params is not None:
                preds[date_str] = apply_emos(params, models_for_day)
        # Append AFTER predicting so the target day never trains itself.
        if usable and date_str in truth:
            history.append((dict(models_for_day), truth[date_str]))
    return preds


# --------------------------------------------------------------------------- #
# Analog Ensemble
# --------------------------------------------------------------------------- #

def analog_ensemble_predictions(
    dates_sorted: list[str],
    truth: dict[str, float],
    nwp_by_date: dict[str, dict[str, float]],
    *,
    k: int = ANALOG_K,
    min_train: int = ANALOG_MIN_TRAIN,
) -> dict[str, tuple[float, float]]:
    """Rolling-origin analog ensemble.

    Each past day is summarised by (ensemble mean, spread). For the target day,
    find the K nearest past days in that 2-D feature space -- standardised by the
    *history's* own feature scales (leakage-safe) -- and use their realised CLISFO
    highs as the predictive ensemble.
    """

    preds: dict[str, tuple[float, float]] = {}
    history: list[tuple[float, float, float]] = []  # (mean, spread, truth)
    for date_str in dates_sorted:
        stats = day_mean_spread(nwp_by_date.get(date_str))
        if stats is not None and len(history) >= min_train:
            target_mean, target_spread, _ = stats
            means = [h[0] for h in history]
            spreads = [h[1] for h in history]
            mean_scale = pstdev(means) or 1.0
            spread_scale = pstdev(spreads) or 1.0
            scored = sorted(
                history,
                key=lambda h: (
                    ((h[0] - target_mean) / mean_scale) ** 2
                    + ((h[1] - target_spread) / spread_scale) ** 2
                ),
            )
            analog_truths = [h[2] for h in scored[:k]]
            if len(analog_truths) >= 2:
                mu = fmean(analog_truths)
                sigma = max(stdev(analog_truths), SIGMA_FLOOR_F)
                preds[date_str] = (mu, sigma)
        if stats is not None and date_str in truth:
            history.append((stats[0], stats[1], truth[date_str]))
    return preds


def make_lookup_predictor(predictions: dict[str, tuple[float, float]]):
    """Wrap a precomputed {date: (mu, sigma)} map as a scoreboard predictor."""

    def predict(date_str, _models):
        return predictions.get(date_str)

    return predict


def blend_gaussian_predictions(
    a: dict[str, tuple[float, float]],
    b: dict[str, tuple[float, float]],
    *,
    weight: float = 0.5,
) -> dict[str, tuple[float, float]]:
    """Combine two Gaussian forecasts into the moment-matched Gaussian of their
    mixture: mu = w*mu_a + (1-w)*mu_b, var = mixture variance (component variances
    plus the spread between component means). Forecast combination often beats
    either component; only days both predict are blended.
    """

    out: dict[str, tuple[float, float]] = {}
    for date_str, (mu_a, sigma_a) in a.items():
        if date_str not in b:
            continue
        mu_b, sigma_b = b[date_str]
        mu = weight * mu_a + (1.0 - weight) * mu_b
        variance = (
            weight * sigma_a * sigma_a
            + (1.0 - weight) * sigma_b * sigma_b
            + weight * (mu_a - mu) ** 2
            + (1.0 - weight) * (mu_b - mu) ** 2
        )
        out[date_str] = (mu, max(math.sqrt(variance), SIGMA_FLOOR_F))
    return out
