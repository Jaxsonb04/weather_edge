"""Probabilistic scoreboard for SFO daily-high forecasts (Phase 0).

The trade engine consumes a *distribution*, so the right yardstick is a proper
score on distributions -- not point MAE. This harness scores any predictor by
the Continuous Ranked Probability Score (CRPS, the headline) and a Gaussian
multi-category Brier (calibration cross-check) against the CLISFO settlement --
the same integer high Kalshi resolves on -- with a Diebold-Mariano + moving-block
bootstrap gate for head-to-head significance.

Phase 0 ships three predictors so the board is meaningful immediately and the
later phases have a baseline to beat:

* ``climatology``     -- day-of-year mean/std from CLISFO history; the skill floor.
* ``baseline_blend``  -- the live production point-blend (the incumbent), scored
                         on the days it actually committed a clean snapshot.
* ``nwp_consensus``   -- the mean of the archived multi-model day-ahead highs,
                         with the cross-model spread as sigma. A naive smoke test
                         that the new ``nwp_model_forecasts`` archive carries
                         skill; Phase 1 replaces it with a trained EMOS/analog.

Honesty notes carried over from ``forecast_backtest.py``: CLISFO-truth only (days
without a settlement are excluded and counted, never averaged against a
fallback), pure standard library, and a *shared* sigma set for the Brier
comparison so a candidate cannot look better calibrated merely because its own
tighter errors shrink its own sigma. CRPS, by contrast, uses each predictor's
own honest per-day sigma -- rewarding genuine sharpness.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from statistics import fmean, pstdev, stdev

from forecast_backtest import (
    COHORTS,
    _residual_sigma,
    cohort_sigmas,
    diebold_mariano,
    load_clean_blend_rows,
    moving_block_bootstrap_ci,
)
from google_weather_cache import predicted_temperature_cohort
from postproc_models import (
    analog_ensemble_predictions,
    blend_gaussian_predictions,
    emos_ngr_predictions,
    make_lookup_predictor,
)
from postproc_recalibration import fit_by_cohort
from scores import (
    SIGMA_FLOOR_F,
    gaussian_crps,
    multicategory_brier as _multicat_brier,
)
from settlement_calendar import integer_settlement_high_f
from truth_store import load_clisfo_truth, load_nwp_forecasts

DB_PATH = Path(__file__).resolve().parent / "weather.db"
CLIMATOLOGY_WINDOW_DAYS = 15
CLIMATOLOGY_MIN_SAMPLES = 5
BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_SEED = 20260625
# A head-to-head gate on fewer shared days than this is reported as inconclusive:
# a verdict from a tiny, single-season overlap (e.g. the blend's ~17 June days)
# is not a basis for a ship/kill decision, however the bootstrap CI lands.
UNDERPOWERED_N = 30
# A "consensus" must rest on enough models to have a real cross-model spread.
# Below this, the day is excluded-and-counted (never averaged against a borrowed
# sigma) -- the same honesty rule this module applies to missing CLISFO truth.
MIN_CONSENSUS_MODELS = 3


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def load_blend_predictions(conn: sqlite3.Connection) -> tuple[dict[str, float], float]:
    """Incumbent: clean next-day blend point forecast per day + its residual sigma."""

    rows, _ = load_clean_blend_rows(conn)
    preds = {row.target_date: float(row.predicted_high_f) for row in rows}
    residuals = [row.predicted_high_f - row.actual_high_f for row in rows]
    sigma = _residual_sigma(residuals) if len(residuals) >= 2 else SIGMA_FLOOR_F
    return preds, sigma


# --------------------------------------------------------------------------- #
# Climatology reference
# --------------------------------------------------------------------------- #

def build_climatology(
    truth: dict[str, float],
    window: int = CLIMATOLOGY_WINDOW_DAYS,
) -> dict[int, tuple[float, float]]:
    """Per day-of-year (mu, sigma) from a cyclic +/- window of CLISFO history.

    A fixed reference, not an OOS claim -- it deliberately uses all years (the
    standard climatological baseline), so it answers "how hard is this day to
    beat" rather than "what will happen".
    """

    highs_by_doy: dict[int, list[float]] = {}
    for date_str, high in truth.items():
        doy = date.fromisoformat(date_str).timetuple().tm_yday
        highs_by_doy.setdefault(doy, []).append(high)

    climatology: dict[int, tuple[float, float]] = {}
    for target_doy in range(1, 367):
        values: list[float] = []
        for offset in range(-window, window + 1):
            doy = ((target_doy - 1 + offset) % 366) + 1
            values.extend(highs_by_doy.get(doy, ()))
        if len(values) >= CLIMATOLOGY_MIN_SAMPLES:
            mu = fmean(values)
            sigma = pstdev(values) if len(values) > 1 else SIGMA_FLOOR_F
            climatology[target_doy] = (mu, max(sigma, SIGMA_FLOOR_F))
    return climatology


def climatology_for(climatology: dict[int, tuple[float, float]], date_str: str):
    doy = date.fromisoformat(date_str).timetuple().tm_yday
    return climatology.get(doy)


# --------------------------------------------------------------------------- #
# Predictors: date_str -> (mu, sigma) | None
# --------------------------------------------------------------------------- #

def make_climatology_predictor(climatology):
    def predict(date_str, _models):
        return climatology_for(climatology, date_str)
    return predict


def make_blend_predictor(blend_preds, blend_sigma):
    def predict(date_str, _models):
        mu = blend_preds.get(date_str)
        return None if mu is None else (mu, blend_sigma)
    return predict


def make_nwp_consensus_predictor(nwp_by_date, fallback_sigma):
    """Mean of the multi-model day-ahead highs; cross-model spread as sigma.

    The spread-as-sigma is the whole point of carrying many models: on days the
    models agree the distribution is sharp, on days they diverge it widens --
    a leakage-free, per-day uncertainty signal the bolted-on residual Gaussian
    never had.
    """

    def predict(date_str, _models):
        highs = list((nwp_by_date.get(date_str) or {}).values())
        if len(highs) < MIN_CONSENSUS_MODELS:
            # Too few models for an honest cross-model spread -> exclude the day
            # (counted in coverage) rather than score a degenerate 1-2 model
            # "consensus" whose sigma would be borrowed from an unrelated arm.
            return None
        mu = fmean(highs)
        sigma = stdev(highs)  # sample (n-1) spread = unbiased predictive sigma
        return mu, max(sigma, SIGMA_FLOOR_F)

    predict.fallback_sigma = fallback_sigma  # retained for API stability
    return predict


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

@dataclass
class DayScore:
    date: str
    mu: float
    sigma: float
    truth: float
    error: float
    signed_error: float
    crps: float
    settled_cohort: str


@dataclass
class PredictorScore:
    name: str
    per_day: list[DayScore] = field(default_factory=list)

    @property
    def days(self) -> int:
        return len(self.per_day)

    def aggregate(self) -> dict:
        if not self.per_day:
            return {"days": 0, "mae": None, "rmse": None, "within3": None, "crps": None}
        errors = [d.error for d in self.per_day]
        return {
            "days": self.days,
            "mae": round(fmean(errors), 3),
            "rmse": round(math.sqrt(fmean([e * e for e in errors])), 3),
            "within3": round(100.0 * fmean([1.0 if e <= 3 else 0.0 for e in errors]), 1),
            "crps": round(fmean([d.crps for d in self.per_day]), 4),
        }

    def crps_by_cohort(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for cohort in COHORTS:
            vals = [d.crps for d in self.per_day if d.settled_cohort == cohort]
            out[cohort] = round(fmean(vals), 4) if vals else None
        return out


def score_predictor(name: str, predictor, truth: dict[str, float]) -> PredictorScore:
    score = PredictorScore(name=name)
    for date_str in sorted(truth):
        prediction = predictor(date_str, None)
        if prediction is None:
            continue
        mu, sigma = prediction
        actual = truth[date_str]
        error = abs(mu - actual)
        score.per_day.append(
            DayScore(
                date=date_str,
                mu=mu,
                sigma=sigma,
                truth=actual,
                error=error,
                signed_error=mu - actual,
                crps=gaussian_crps(mu, sigma, actual),
                settled_cohort=predicted_temperature_cohort(actual),
            )
        )
    return score


def brier_with_shared_sigma(score: PredictorScore, shared_sigmas: dict[str, float]) -> float | None:
    """Mean multi-category Brier using a shared per-cohort sigma (calibration
    isolated from sharpness, so a fair location-only comparison across arms)."""

    if not score.per_day:
        return None
    total = 0.0
    for day in score.per_day:
        sigma = shared_sigmas.get(day.settled_cohort) or shared_sigmas.get("overall") or SIGMA_FLOOR_F
        total += _multicat_brier(day.mu, sigma, int(round(day.truth)))
    return round(total / score.days, 4)


def brier_by_cohort(
    score: PredictorScore, shared_sigmas: dict[str, float]
) -> dict[str, float | None]:
    """Per-settled-cohort mean multi-category Brier (shared sigma, like the
    aggregate ``brier_with_shared_sigma``).

    The warm/hot trade block is justified by cohort *Brier* (~0.96), not CRPS, so
    the cohort Brier row is what tells us whether a post-processor actually earns
    a blocked cohort back -- a sharper CRPS with an uncalibrated Brier would not.
    """

    out: dict[str, float | None] = {}
    for cohort in COHORTS:
        days = [d for d in score.per_day if d.settled_cohort == cohort]
        if not days:
            out[cohort] = None
            continue
        sigma = shared_sigmas.get(cohort) or shared_sigmas.get("overall") or SIGMA_FLOOR_F
        total = sum(_multicat_brier(d.mu, sigma, int(round(d.truth))) for d in days)
        out[cohort] = round(total / len(days), 4)
    return out


def crps_gate(candidate: PredictorScore, reference: PredictorScore) -> dict:
    """DM + block-bootstrap on per-day (candidate - reference) CRPS deltas.

    Negative mean delta / ``ci_high`` < 0 => candidate has lower CRPS (better).
    Only days both arms scored are compared.
    """

    ref_by_date = {d.date: d.crps for d in reference.per_day}
    deltas = [d.crps - ref_by_date[d.date] for d in candidate.per_day if d.date in ref_by_date]
    if len(deltas) < 3:
        return {"n": len(deltas), "mean_delta": None, "dm": None, "dm_stat": None, "ci": None}
    ci = moving_block_bootstrap_ci(deltas, samples=BOOTSTRAP_SAMPLES, seed=BOOTSTRAP_SEED)
    dm = diebold_mariano(deltas)
    return {
        "n": len(deltas),
        "mean_delta": round(fmean(deltas), 4),
        "dm": dm,
        "dm_stat": dm.get("stat"),  # <0 => candidate has lower CRPS (better)
        "ci": (round(ci[0], 4), round(ci[1], 4)) if ci else None,
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def recalibrated_lookup_predictions(
    base_preds: dict[str, tuple[float, float]],
    truth: dict[str, float],
    *,
    shrinkage_k: float = 40.0,
    min_train: int = 60,
) -> dict[str, tuple[float, float]]:
    """Rolling-origin per-cohort recalibration of a ``(mu, sigma)`` lookup.

    For each date, fit the per-cohort shift+scale correction (Phase 1a) ONLY on
    strictly-earlier dates that already have settled truth -- leakage-safe, the
    same discipline the EMOS arms use -- then apply the correction for that
    date's *predicted* cohort. Days before ``min_train`` settled history pass
    through unchanged. This is the honest OOS test of whether the recalibration
    earns the blocked warm/hot cohorts back before it is wired into the engine.
    """

    out: dict[str, tuple[float, float]] = {}
    history: list[tuple[float, float, float, str]] = []  # (mu, sigma, realized, cohort)
    for date_str in sorted(base_preds):
        mu, sigma = base_preds[date_str]
        cohort = predicted_temperature_cohort(mu)
        if len(history) >= min_train:
            by_cohort: dict[str, list[tuple[float, float, float]]] = {}
            for hmu, hsig, hy, hc in history:
                by_cohort.setdefault(hc, []).append((hmu, hsig, hy))
            recal = fit_by_cohort(by_cohort, shrinkage_k=shrinkage_k).get(cohort)
            if recal is not None:
                mu, sigma = recal.apply(mu, sigma)
        out[date_str] = (mu, sigma)
        # Record truth AFTER predicting so date_str never trains on its own outcome.
        if date_str in truth:
            base_mu, base_sigma = base_preds[date_str]
            history.append((base_mu, base_sigma, truth[date_str], cohort))
    return out


def evaluate(conn: sqlite3.Connection, lead_days: int, reference_name: str) -> dict:
    truth = load_clisfo_truth(conn)
    nwp_by_date = load_nwp_forecasts(conn, lead_days)
    blend_preds, blend_sigma = load_blend_predictions(conn)
    climatology = build_climatology(truth)

    # Trained post-processors (rolling-origin, leakage-safe) over the NWP archive.
    nwp_dates = sorted(nwp_by_date)
    emos_preds = emos_ngr_predictions(nwp_dates, truth, nwp_by_date)
    analog_preds = analog_ensemble_predictions(nwp_dates, truth, nwp_by_date)
    # Phase 4 candidate upgrades, gated against emos_ngr (ship only if they win OOS).
    emos_wmean_preds = emos_ngr_predictions(nwp_dates, truth, nwp_by_date, weight_mode="inv_var")
    emos_anen_blend = blend_gaussian_predictions(emos_preds, analog_preds)
    # Phase 1a: per-cohort recalibration on top of the winning emos_wmean arm.
    emos_wmean_recal = recalibrated_lookup_predictions(emos_wmean_preds, truth)

    predictors = {
        "climatology": make_climatology_predictor(climatology),
        "baseline_blend": make_blend_predictor(blend_preds, blend_sigma),
        "nwp_consensus": make_nwp_consensus_predictor(nwp_by_date, blend_sigma),
        "emos_ngr": make_lookup_predictor(emos_preds),
        "analog_ens": make_lookup_predictor(analog_preds),
        "emos_wmean": make_lookup_predictor(emos_wmean_preds),
        "emos_anen_blend": make_lookup_predictor(emos_anen_blend),
        "emos_wmean_recal": make_lookup_predictor(emos_wmean_recal),
    }
    scores = {name: score_predictor(name, fn, truth) for name, fn in predictors.items()}

    reference = scores.get(reference_name)
    if reference is None or not reference.per_day:
        # Fall back to climatology when the requested reference has no coverage.
        reference_name = "climatology"
        reference = scores["climatology"]
    shared_sigmas = cohort_sigmas(
        [{"signed_error": d.signed_error, "settled_cohort": d.settled_cohort} for d in reference.per_day]
    )

    # Audit the consensus arm: how many models actually backed each scored day,
    # so a thin/ragged consensus subset is visible before any verdict is trusted.
    consensus_model_counts: dict[int, int] = {}
    for day in scores["nwp_consensus"].per_day:
        n_models = len(nwp_by_date.get(day.date, {}))
        consensus_model_counts[n_models] = consensus_model_counts.get(n_models, 0) + 1

    return {
        "lead_days": lead_days,
        "reference": reference_name,
        "truth_days": len(truth),
        "nwp_days": len(nwp_by_date),
        "scores": scores,
        "shared_sigmas": shared_sigmas,
        "brier": {name: brier_with_shared_sigma(s, shared_sigmas) for name, s in scores.items()},
        "brier_by_cohort": {name: brier_by_cohort(s, shared_sigmas) for name, s in scores.items()},
        "consensus_model_counts": consensus_model_counts,
        "gates": {
            name: crps_gate(s, reference)
            for name, s in scores.items()
            if name != reference_name
        },
    }


def _print_report(result: dict) -> None:
    print(f"\n=== Probabilistic scoreboard (lead {result['lead_days']}d) ===")
    print(f"CLISFO truth days: {result['truth_days']}   NWP-archive days: {result['nwp_days']}")
    print(f"reference (skill baseline): {result['reference']}\n")

    print("CRPS is the headline (date-matched gate below = the ship decision).")
    print("MAE/RMSE/CRPS/Brier columns are each over that arm's OWN scored days, so")
    print("cross-arm columns are NOT head-to-head -- only the gate is.\n")
    print(f"{'predictor':16s} {'days':>5s} {'MAE':>6s} {'RMSE':>6s} {'<=3F%':>6s} {'CRPS':>7s} {'Brier*':>7s}")
    for name, score in result["scores"].items():
        agg = score.aggregate()
        brier = result["brier"].get(name)
        if agg["days"] == 0:
            print(f"{name:16s} {'0':>5s}  (no overlapping days)")
            continue
        print(
            f"{name:16s} {agg['days']:>5d} {agg['mae']:>6.2f} {agg['rmse']:>6.2f} "
            f"{agg['within3']:>6.1f} {agg['crps']:>7.3f} {brier if brier is not None else float('nan'):>7.3f}"
        )
    print("  *Brier uses a shared per-cohort sigma (calibration isolated from sharpness).")

    counts = result.get("consensus_model_counts") or {}
    if counts:
        dist = ", ".join(f"{k}mdl:{n}" for k, n in sorted(counts.items()))
        print(f"\nnwp_consensus scored-day model coverage ( >= {3} models required ): {dist}")

    print("\nCRPS by settled cohort (warm = 70-79F, the known anti-calibration target):")
    print(f"{'predictor':16s} " + " ".join(f"{c:>8s}" for c in COHORTS))
    for name, score in result["scores"].items():
        if not score.per_day:
            continue
        by_cohort = score.crps_by_cohort()
        cells = " ".join(f"{(by_cohort[c] if by_cohort[c] is not None else float('nan')):>8.3f}" for c in COHORTS)
        print(f"{name:16s} {cells}")

    print("\nBrier by settled cohort (shared sigma; the warm/hot trade block's own metric):")
    print(f"{'predictor':16s} " + " ".join(f"{c:>8s}" for c in COHORTS))
    brier_cohorts = result.get("brier_by_cohort") or {}
    for name, score in result["scores"].items():
        if not score.per_day:
            continue
        by_cohort = brier_cohorts.get(name) or {}
        cells = " ".join(
            f"{(by_cohort.get(c) if by_cohort.get(c) is not None else float('nan')):>8.3f}"
            for c in COHORTS
        )
        print(f"{name:16s} {cells}")

    if result["reference"] == "climatology":
        print(
            "\nNote: climatology is an in-sample (all-years) reference, so it is a "
            "slightly\nstrong -- i.e. conservative -- baseline; it cannot manufacture a false win."
        )
    print(
        f"\nHead-to-head CRPS gate vs '{result['reference']}' "
        "(WINS needs DM_stat<0 AND ci_high<0; date-matched per day):"
    )
    for name, gate in result["gates"].items():
        if gate["mean_delta"] is None:
            print(f"  {name:16s} n={gate['n']} (insufficient overlap, <3 shared days)")
            continue
        ci = gate["ci"]
        dm_stat = gate.get("dm_stat")
        dm_p = (gate["dm"] or {}).get("p_value", float("nan"))
        if not ci or dm_stat is None:
            print(f"  {name:16s} n={gate['n']} mean_delta={gate['mean_delta']:+.4f} (no CI)")
            continue
        wins = ci[1] < 0 and math.isfinite(dm_stat) and dm_stat < 0
        loses = ci[0] > 0
        if gate["n"] < UNDERPOWERED_N:
            verdict = f"inconclusive (underpowered, n<{UNDERPOWERED_N})"
        else:
            verdict = "WINS" if wins else ("loses" if loses else "tie")
        print(
            f"  {name:16s} n={gate['n']:>3d} mean_delta={gate['mean_delta']:+.4f} "
            f"DM_stat={dm_stat:+.2f} p={dm_p:.3f} ci=[{ci[0]:+.4f}, {ci[1]:+.4f}] -> {verdict}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH), help="weather.db path")
    parser.add_argument("--lead", type=int, default=1, help="lead horizon in days (1/2/3)")
    parser.add_argument(
        "--baseline",
        default="baseline_blend",
        choices=(
            "baseline_blend",
            "climatology",
            "nwp_consensus",
            "emos_ngr",
            "analog_ens",
            "emos_wmean",
            "emos_wmean_recal",
        ),
        help="reference arm for the CRPS gate",
    )
    args = parser.parse_args(argv)
    with sqlite3.connect(args.db) as conn:
        result = evaluate(conn, args.lead, args.baseline)
    _print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
