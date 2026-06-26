"""Network-free tests for the EMOS/analog post-processors (Phase 1)."""

from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import fmean

from postproc_models import (
    _simple_ols,
    analog_ensemble_predictions,
    apply_emos,
    blend_gaussian_predictions,
    emos_ngr_predictions,
    fit_emos,
    make_lookup_predictor,
)


def _synthetic_series(n: int = 200):
    """A smooth seasonal truth with three biased models (warm/cold/unbiased)."""

    base = date(2024, 1, 1)
    truth: dict[str, float] = {}
    nwp: dict[str, dict[str, float]] = {}
    for i in range(n):
        day = (base + timedelta(days=i)).isoformat()
        t = 70.0 + 8.0 * math.sin(i / 25.0)
        truth[day] = float(round(t))
        nwp[day] = {"m_warm": t + 3.0, "m_cold": t - 2.0, "m_zero": t}
    return sorted(nwp), truth, nwp


def test_simple_ols_recovers_line_and_handles_degenerate():
    intercept, slope = _simple_ols([0.0, 1.0, 2.0, 3.0], [1.0, 3.0, 5.0, 7.0])  # y = 1 + 2x
    assert abs(intercept - 1.0) < 1e-9 and abs(slope - 2.0) < 1e-9
    flat_intercept, flat_slope = _simple_ols([5.0, 5.0, 5.0], [1.0, 2.0, 3.0])  # zero x-variance
    assert abs(flat_intercept - 2.0) < 1e-9 and flat_slope == 0.0


def test_emos_corrects_per_model_bias_and_beats_raw_mean():
    dates, truth, nwp = _synthetic_series()
    preds = emos_ngr_predictions(dates, truth, nwp, min_train=40)
    assert len(preds) > 100

    emos_err = fmean([abs(mu - truth[d]) for d, (mu, _s) in preds.items()])
    raw_err = fmean([abs(fmean(list(nwp[d].values())) - truth[d]) for d in preds])
    assert emos_err < raw_err  # debiasing removes the net +0.33F ensemble bias

    signed = fmean([mu - truth[d] for d, (mu, _s) in preds.items()])
    assert abs(signed) < 0.5  # predictions are ~unbiased
    assert all(s >= 1.5 for _mu, s in preds.values())  # sigma respects the floor


def test_emos_is_rolling_origin_no_leakage():
    # Predictions for the first 120 days must be IDENTICAL whether or not the
    # future days exist -- proving each fit uses only strictly-prior history.
    dates, truth, nwp = _synthetic_series()
    full = emos_ngr_predictions(dates, truth, nwp, min_train=40)

    prefix_dates = dates[:120]
    prefix_nwp = {d: nwp[d] for d in prefix_dates}
    prefix_truth = {d: truth[d] for d in prefix_dates}
    prefix = emos_ngr_predictions(sorted(prefix_nwp), prefix_truth, prefix_nwp, min_train=40)

    assert prefix  # there are predictions to compare
    for day in prefix_dates:
        if day in prefix:
            assert day in full
            assert abs(full[day][0] - prefix[day][0]) < 1e-9
            assert abs(full[day][1] - prefix[day][1]) < 1e-9


def test_analog_predicts_near_truth_and_is_rolling_origin():
    dates, truth, nwp = _synthetic_series()
    preds = analog_ensemble_predictions(dates, truth, nwp, k=25, min_train=120)
    assert len(preds) > 50
    err = fmean([abs(mu - truth[d]) for d, (mu, _s) in preds.items()])
    assert err < 3.0  # analogs of similar forecast-days carry similar outcomes

    prefix_dates = dates[:150]
    prefix_nwp = {d: nwp[d] for d in prefix_dates}
    prefix_truth = {d: truth[d] for d in prefix_dates}
    prefix = analog_ensemble_predictions(sorted(prefix_nwp), prefix_truth, prefix_nwp, k=25, min_train=120)
    assert prefix
    for day, (mu, sigma) in prefix.items():
        assert day in preds  # an early-day prediction must exist in the full run too
        assert abs(preds[day][0] - mu) < 1e-9 and abs(preds[day][1] - sigma) < 1e-9


def test_emos_does_not_train_on_its_own_truth():
    # Same-day self-leak guard (distinct from the future-leak test): mutating a
    # day's OWN truth must not change that day's OWN prediction -- it is fit on
    # strictly-prior days. Catches an append-before-predict regression that the
    # future-truth test (which only checks earlier days) cannot see.
    dates, truth, nwp = _synthetic_series()
    base = emos_ngr_predictions(dates, truth, nwp, min_train=40)
    target = dates[180]
    assert target in base
    mutated = dict(truth)
    mutated[target] = mutated[target] + 100.0  # extreme, so a self-leak is unmissable
    after = emos_ngr_predictions(dates, mutated, nwp, min_train=40)
    assert abs(after[target][0] - base[target][0]) < 1e-9
    assert abs(after[target][1] - base[target][1]) < 1e-9
    # strictly-later days DO change (the mutated truth enters their training history)
    assert any(
        abs(after[d][0] - base[d][0]) > 1e-6 for d in dates[181:] if d in base and d in after
    )


def test_emos_returns_nothing_before_warmup():
    dates, truth, nwp = _synthetic_series(n=30)
    assert emos_ngr_predictions(dates, truth, nwp, min_train=60) == {}


def test_make_lookup_predictor():
    predict = make_lookup_predictor({"2024-06-01": (70.0, 2.0)})
    assert predict("2024-06-01", None) == (70.0, 2.0)
    assert predict("2024-06-02", None) is None


def test_inv_var_weighting_downweights_noisy_model():
    base = date(2024, 1, 1)
    truth = {}
    nwp = {}
    for i in range(200):
        day = (base + timedelta(days=i)).isoformat()
        t = 70.0 + 5.0 * math.sin(i / 20.0)
        truth[day] = float(round(t))
        noisy = t + (8.0 if i % 2 == 0 else -8.0)  # big alternating error, ~0 mean bias
        nwp[day] = {"clean": t + 0.2, "tight": t - 0.2, "noisy": noisy}

    history = [(nwp[d], truth[d]) for d in sorted(nwp)]
    params_equal = fit_emos(history, weight_mode="equal")
    params_wmean = fit_emos(history, weight_mode="inv_var")
    assert params_equal.weights == {}  # equal mode carries no per-model weights
    assert params_wmean.weights["noisy"] < params_wmean.weights["clean"]  # noisy down-weighted

    equal = emos_ngr_predictions(sorted(nwp), truth, nwp, min_train=40, weight_mode="equal")
    wmean = emos_ngr_predictions(sorted(nwp), truth, nwp, min_train=40, weight_mode="inv_var")
    assert any(abs(equal[d][0] - wmean[d][0]) > 1e-6 for d in equal if d in wmean)


def test_inv_var_weighting_stays_rolling_origin():
    # Same self-leak guard for the weighted variant: a day's own truth must not
    # change its own prediction.
    dates, truth, nwp = _synthetic_series()
    base = emos_ngr_predictions(dates, truth, nwp, min_train=40, weight_mode="inv_var")
    target = dates[180]
    mutated = dict(truth)
    mutated[target] = mutated[target] + 100.0
    after = emos_ngr_predictions(dates, mutated, nwp, min_train=40, weight_mode="inv_var")
    assert abs(after[target][0] - base[target][0]) < 1e-9


def test_blend_gaussian_predictions_mixture_moments():
    a = {"d1": (70.0, 2.0), "d2": (60.0, 3.0)}
    b = {"d1": (74.0, 2.0)}  # only d1 is shared
    out = blend_gaussian_predictions(a, b, weight=0.5)
    assert set(out) == {"d1"}
    mu, sigma = out["d1"]
    assert abs(mu - 72.0) < 1e-9  # 0.5*70 + 0.5*74
    # mixture var = 0.5*4 + 0.5*4 + 0.5*(70-72)^2 + 0.5*(74-72)^2 = 8
    assert abs(sigma - math.sqrt(8.0)) < 1e-9
    assert sigma > 2.0  # disagreement widens beyond either component


def _hetero_history(error_tracks_spread: bool):
    """History where the cross-model spread either tracks the error (hetero) or
    anti-correlates with it (forcing the d>=0 clip + homoscedastic fallback)."""

    history = []
    for i in range(200):
        base = 50.0 + (i % 40)
        low_spread = i % 2 == 0
        spread_models = (
            {"a": base - 0.5, "b": base, "c": base + 0.5} if low_spread
            else {"a": base - 5.0, "b": base, "c": base + 5.0}
        )
        big_error = low_spread != error_tracks_spread  # XOR
        err = (5.0 if i % 4 < 2 else -5.0) if big_error else (0.3 if i % 4 < 2 else -0.3)
        history.append((spread_models, base + err))
    return history


def test_emos_sigma_grows_with_spread_when_spread_predicts_error():
    params = fit_emos(_hetero_history(error_tracks_spread=True))
    assert params.var_d > 0.0  # disagreement predicts error
    _mu_lo, sigma_lo = apply_emos(params, {"a": 69.5, "b": 70.0, "c": 70.5})  # tight
    _mu_hi, sigma_hi = apply_emos(params, {"a": 65.0, "b": 70.0, "c": 75.0})  # wide
    assert sigma_hi > sigma_lo + 1.0  # wide ensemble -> wider predictive distribution


def test_emos_variance_clips_and_falls_back_when_spread_anticorrelates():
    params = fit_emos(_hetero_history(error_tracks_spread=False))
    assert params.var_d == 0.0  # clipped: more disagreement must never lower sigma
    _m1, sigma_tight = apply_emos(params, {"a": 69.5, "b": 70.0, "c": 70.5})
    _m2, sigma_wide = apply_emos(params, {"a": 65.0, "b": 70.0, "c": 75.0})
    assert abs(sigma_tight - sigma_wide) < 1e-9  # homoscedastic fallback -> sigma constant


def test_emos_prediction_insensitive_to_future_truth():
    # The decisive leakage guard: poisoning a LATE day's truth must not perturb
    # any earlier prediction (and MUST perturb later ones, proving propagation).
    dates, truth, nwp = _synthetic_series()
    base = emos_ngr_predictions(dates, truth, nwp, min_train=40)
    poisoned_truth = dict(truth)
    poisoned_truth[dates[180]] = poisoned_truth[dates[180]] + 100.0
    poisoned = emos_ngr_predictions(dates, poisoned_truth, nwp, min_train=40)

    for day in dates[:180]:
        if day in base:
            assert abs(base[day][0] - poisoned[day][0]) < 1e-9
            assert abs(base[day][1] - poisoned[day][1]) < 1e-9
    assert any(
        day in base and abs(base[day][0] - poisoned[day][0]) > 1e-6 for day in dates[181:]
    )
