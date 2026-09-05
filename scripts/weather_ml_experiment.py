"""Offline, frozen weather-only ML comparison; no runtime or trading integration.

Inputs are an explicit fixed-lead archive export and the existing EMOS paired
case CSV. No network access, database writes, hyperparameter search, or pickle.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import platform
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import scipy
import sklearn
from scipy.special import ndtr
from sklearn.ensemble import HistGradientBoostingRegressor
from threadpoolctl import threadpool_limits

# Frozen before this candidate's holdout scores were computed. Deliberately
# separate from the mutable production roster so future runs remain comparable.
ROSTER = (
    "ncep_nbm_conus", "ecmwf_ifs025", "gfs_seamless", "icon_seamless",
    "gem_global", "ecmwf_aifs025_single", "jma_seamless", "meteofrance_seamless",
)
MODEL_PARAMS = {
    "loss": "squared_error", "learning_rate": 0.05, "max_iter": 120,
    "max_leaf_nodes": 15, "min_samples_leaf": 60, "l2_regularization": 10.0,
    "max_bins": 64, "early_stopping": False, "random_state": 20260905,
}
SIGMA_FLOOR = 1.5
SIGMA_PRIOR_CASES = 30
BOOTSTRAP_SEED = 20260905
ARMS = ("ml", "ensemble", "emos", "emos_bias", "crps_emos", "crps_emos_bias")
BASELINE_COLUMNS = {
    "emos": ("baseline_mu", "baseline_sigma"),
    "emos_bias": ("baseline_corrected_mu", "baseline_sigma"),
    "crps_emos": ("challenger_mu", "challenger_sigma"),
    "crps_emos_bias": ("challenger_corrected_mu", "challenger_sigma"),
}


@dataclass(frozen=True)
class Case:
    station: str
    lead: int
    day: date
    members: tuple[float, ...]
    truth: float

    @property
    def key(self) -> tuple[str, int, date]:
        return self.station, self.lead, self.day


def load_cases(payload: dict) -> list[Case]:
    """Reject ambiguous joins; only the complete canonical roster is eligible."""
    truth = {}
    for station, day, value in payload["truth"]:
        key = str(station), date.fromisoformat(day)
        if key in truth or not math.isfinite(float(value)):
            raise ValueError("duplicate or nonfinite truth")
        truth[key] = float(value)
    members = defaultdict(dict)
    for station, day, model, lead, value, source in payload["nwp"]:
        if model not in ROSTER:
            continue
        if source != "openmeteo_previous_runs":
            raise ValueError("unexpected forecast source")
        if int(lead) != lead or int(lead) not in (1, 2):
            raise ValueError("experiment accepts only leads 1 and 2")
        key = str(station), int(lead), date.fromisoformat(day)
        if model in members[key] or not math.isfinite(float(value)):
            raise ValueError("duplicate or nonfinite member forecast")
        members[key][model] = float(value)
    return [
        Case(station, lead, day, tuple(values[m] for m in ROSTER), truth[station, day])
        for (station, lead, day), values in sorted(members.items())
        if len(values) == len(ROSTER) and (station, day) in truth
    ]


def load_pairs(path: Path) -> list[dict]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def paired_cases(cases: list[Case], pairs: list[dict], start: date, end: date) -> list[Case]:
    lookup = {c.key: c for c in cases}
    seen = set()
    selected = []
    for row in pairs:
        key = row["station"], int(row["lead"]), date.fromisoformat(row["target_date"])
        if key in seen:
            raise ValueError("duplicate paired evaluation key")
        seen.add(key)
        if not start <= key[2] <= end:
            raise ValueError("paired case outside declared holdout")
        case = lookup.get(key)
        if case is None:
            raise ValueError("paired case missing complete forecast roster or truth")
        if not math.isclose(case.truth, float(row["truth"]), abs_tol=1e-9, rel_tol=0):
            raise ValueError("paired truth differs from archive truth")
        selected.append(case)
    if not selected:
        raise ValueError("no paired evaluation cases")
    return selected


def month_before(day: date, count: int) -> date:
    index = day.year * 12 + day.month - 1 - count
    return date(index // 12, index % 12 + 1, 1)


def features(cases: list[Case], stations: tuple[str, ...]) -> tuple[np.ndarray, np.ndarray]:
    station_codes = {station: index for index, station in enumerate(stations)}
    output = []
    means = []
    for case in cases:
        if case.station not in station_codes:
            raise ValueError("evaluation station absent from frozen training")
        values = np.asarray(case.members)
        mean = float(values.mean())
        year_days = (date(case.day.year + 1, 1, 1) - date(case.day.year, 1, 1)).days
        angle = 2 * math.pi * (case.day.timetuple().tm_yday - 1) / year_days
        output.append([
            station_codes[case.station], case.lead, math.sin(angle), math.cos(angle),
            mean, float(values.std(ddof=1)), *(values - mean).tolist(),
        ])
        means.append(mean)
    return np.asarray(output, dtype=float), np.asarray(means)


def fit_at_cutoff(cases: list[Case], cutoff: date):
    train = sorted((c for c in cases if c.day <= cutoff), key=lambda c: c.key)
    if len(train) < 120:
        raise ValueError("too few complete cases before training cutoff")
    stations = tuple(sorted({c.station for c in train}))
    x, means = features(train, stations)
    model = HistGradientBoostingRegressor(
        **MODEL_PARAMS, categorical_features=[True] + [False] * (x.shape[1] - 1),
    )
    model.fit(x, np.asarray([c.truth for c in train]) - means)
    metadata = {
        "cutoff": cutoff.isoformat(), "first_truth_date": min(c.day for c in train).isoformat(),
        "last_truth_date": max(c.day for c in train).isoformat(), "n": len(train),
        "stations": list(stations), "features": x.shape[1],
    }
    return model, stations, metadata


def predict(model, stations: tuple[str, ...], cases: list[Case]) -> tuple[np.ndarray, np.ndarray]:
    x, means = features(cases, stations)
    return means + model.predict(x), means


def frozen_predictions(cases: list[Case], evaluation: list[Case], start: date):
    """Only pretest truth can affect ML means or either calibrated uncertainty.

    Three prior complete calendar months produce forward residuals, with each
    model fitted no later than its month's first target minus max lead minus one
    day. Those residuals calibrate sigma; the final point model uses all eligible
    truth through the same conservative cutoff relative to the holdout start.
    """
    max_lead = max(c.lead for c in evaluation)
    lag = timedelta(days=max_lead + 1)
    cutoff = start - lag
    calibration_rows = []
    folds = []
    for months_ago in (3, 2, 1):
        lower = month_before(start, months_ago)
        upper = month_before(start, months_ago - 1)
        # A holdout starting on day 1/2 can put the end of the prior month beyond
        # the final truth cutoff. Explicitly exclude those unavailable outcomes.
        selected = [c for c in cases if lower <= c.day < upper and c.day <= cutoff]
        if not selected:
            raise ValueError("empty pretest calibration month")
        model, stations, metadata = fit_at_cutoff(cases, lower - lag)
        mu, raw_mean = predict(model, stations, selected)
        metadata.update({"prediction_start": min(c.day for c in selected).isoformat(),
                         "prediction_end": max(c.day for c in selected).isoformat(),
                         "prediction_n": len(selected)})
        folds.append(metadata)
        for case, point, raw in zip(selected, mu, raw_mean, strict=True):
            calibration_rows.append({
                "station": case.station, "lead": case.lead, "target_date": case.day.isoformat(),
                "truth": case.truth, "ml_mu": float(point), "ensemble_mu": float(raw),
                "fit_truth_cutoff": metadata["cutoff"],
            })
    scales = {}
    for arm in ("ml", "ensemble"):
        squared_errors = defaultdict(list)
        all_squared = []
        for row in calibration_rows:
            error_squared = (row["truth"] - row[f"{arm}_mu"]) ** 2
            squared_errors[row["station"], row["lead"]].append(error_squared)
            all_squared.append(error_squared)
        pooled_mse = float(np.mean(all_squared))
        for station, lead in sorted({(c.station, c.lead) for c in evaluation}):
            errors = squared_errors[station, lead]
            if not errors:
                raise ValueError("station/lead missing pretest uncertainty evidence")
            mse = (sum(errors) + SIGMA_PRIOR_CASES * pooled_mse) / (len(errors) + SIGMA_PRIOR_CASES)
            scales[arm, station, lead] = {"sigma": max(SIGMA_FLOOR, math.sqrt(mse)),
                                        "calibration_n": len(errors), "pooled_mse": pooled_mse}
    model, stations, final_fit = fit_at_cutoff(cases, cutoff)
    mu, raw_mean = predict(model, stations, evaluation)
    output = []
    for case, point, raw in zip(evaluation, mu, raw_mean, strict=True):
        output.append({"ml_mu": float(point), "ensemble_mu": float(raw),
                       **{f"{arm}_sigma": scales[arm, case.station, case.lead]["sigma"]
                          for arm in ("ml", "ensemble")}})
    metadata = {"final_fit": final_fit, "calibration_folds": folds,
                "uncertainty": [{"arm": arm, "station": station, "lead": lead, **scale}
                                for (arm, station, lead), scale in sorted(scales.items())]}
    return output, metadata, calibration_rows


def gaussian_crps(mu, sigma, truth):
    mu, sigma, truth = np.asarray(mu), np.asarray(sigma), np.asarray(truth)
    if np.any(sigma <= 0) or not all(np.isfinite(v).all() for v in (mu, sigma, truth)):
        raise ValueError("invalid Gaussian forecast")
    z = (truth - mu) / sigma
    return sigma * (z * (2 * ndtr(z) - 1) + math.sqrt(2 / math.pi) * np.exp(-z * z / 2)
                    - 1 / math.sqrt(math.pi))


def score_cases(evaluation: list[Case], pairs: list[dict], predictions: list[dict]) -> list[dict]:
    output = []
    z80 = 1.2815515655446004
    for case, pair, prediction in zip(evaluation, pairs, predictions, strict=True):
        row = {"station": case.station, "lead": case.lead, "target_date": case.day.isoformat(),
               "truth": case.truth, **prediction}
        for arm, (mu_column, sigma_column) in BASELINE_COLUMNS.items():
            row[f"{arm}_mu"] = float(pair[mu_column])
            row[f"{arm}_sigma"] = float(pair[sigma_column])
        for arm in ARMS:
            mu, sigma = row[f"{arm}_mu"], row[f"{arm}_sigma"]
            row[f"{arm}_crps"] = float(gaussian_crps(mu, sigma, case.truth))
            row[f"{arm}_mae"] = abs(case.truth - mu)
            row[f"{arm}_covered80"] = int(abs(case.truth - mu) <= z80 * sigma)
        output.append(row)
    return output


def date_block_interval(rows: list[dict], comparator: str, metric: str, *, block_days: int,
                        replicates: int, seed: int = BOOTSTRAP_SEED) -> dict:
    """Resample dates jointly; sum/count preserves the pooled case estimand."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[date.fromisoformat(row["target_date"])].append(
            row[f"ml_{metric}"] - row[f"{comparator}_{metric}"])
    first, last = min(grouped), max(grouped)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    totals = np.asarray([sum(grouped[d]) for d in days])
    counts = np.asarray([len(grouped[d]) for d in days])
    if block_days < 1 or replicates < 1:
        raise ValueError("positive bootstrap settings required")
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, len(days), size=(replicates, math.ceil(len(days) / block_days)))
    indices = ((starts[:, :, None] + np.arange(block_days)) % len(days)).reshape(replicates, -1)
    indices = indices[:, :len(days)]
    denominator = counts[indices].sum(axis=1)
    if np.any(denominator == 0):
        raise ValueError("bootstrap resampled no cases; evaluation is too sparse")
    draws = totals[indices].sum(axis=1) / denominator
    return {"delta": float(totals.sum() / counts.sum()),
            "date_block_95_ci": np.quantile(draws, [0.025, 0.975]).tolist()}


def summarize(rows: list[dict], *, replicates: int, block_days: int = 7) -> dict:
    return {
        "n": len(rows), "days": len({r["target_date"] for r in rows}),
        "scores": {arm: {metric: float(np.mean([r[f"{arm}_{metric}"] for r in rows]))
                         for metric in ("crps", "mae", "covered80", "sigma")} for arm in ARMS},
        "ml_minus": {arm: {metric: date_block_interval(
            rows, arm, metric, block_days=block_days, replicates=replicates)
            for metric in ("crps", "mae")} for arm in ARMS if arm != "ml"},
    }


def run_experiment(payload: dict, pairs: list[dict], start: date, end: date, *, replicates=5000):
    if start > end:
        raise ValueError("holdout start must precede end")
    cases = load_cases(payload)
    evaluation = paired_cases(cases, pairs, start, end)
    with threadpool_limits(limits=2):
        predictions, fits, calibration_rows = frozen_predictions(cases, evaluation, start)
    scores = score_cases(evaluation, pairs, predictions)
    result = {
        "schema_version": 1, "exported_at": payload.get("exported_at"),
        "holdout_start": start.isoformat(), "holdout_end": end.isoformat(),
        "model_roster": list(ROSTER), "model_params": MODEL_PARAMS,
        "feature_order": ["station_category", "lead", "season_sin", "season_cos", "ensemble_mean",
                          "ensemble_sample_std", *[f"{m}_minus_mean" for m in ROSTER]],
        "sigma_floor_f": SIGMA_FLOOR, "sigma_prior_cases": SIGMA_PRIOR_CASES,
        "complete_roster_source_cases": len(cases), **fits,
        "bootstrap": {"replicates": replicates, "block_days": 7, "seed": BOOTSTRAP_SEED,
                      "estimand": "case-weighted paired difference; jointly sampled calendar-date blocks"},
        "primary": summarize(scores, replicates=replicates),
        "by_lead": [{"lead": lead, **summarize([r for r in scores if r["lead"] == lead],
                                              replicates=replicates)}
                    for lead in sorted({r["lead"] for r in scores})],
        "by_station_lead": [
            {"station": station, "lead": lead, **summarize(
                [r for r in scores if r["station"] == station and r["lead"] == lead], replicates=replicates)}
            for station, lead in sorted({(r["station"], r["lead"]) for r in scores})
        ],
        "robustness_14_day_blocks": {arm: date_block_interval(
            scores, arm, "crps", block_days=14, replicates=replicates) for arm in ARMS if arm != "ml"},
        "versions": {"python": platform.python_version(), "numpy": np.__version__,
                     "scipy": scipy.__version__, "sklearn": sklearn.__version__},
        "limitations": [
            "Fixed-lead archive is not a single historical serving vintage; hourly completeness is unavailable.",
            "Current final CLI truth and a date lag do not reconstruct publication or revision timestamps.",
            "ML is frozen before holdout; existing EMOS baselines expand and update using earlier holdout truth.",
            "One candidate with no holdout tuning; prior EMOS experiment already inspected this evaluation period.",
            "Gaussian sigma is constant by station/lead, estimated from forward pretest RMSE with pooled shrinkage.",
            "Spring calibration transferred to summer is unproven; grouped intervals are exploratory and unadjusted.",
            "No trading-side calibration, execution, fees, fills, volume, sizing, or profitability was tested.",
        ],
    }
    return result, scores, calibration_rows


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True, help="gzip JSON: nwp and truth positional rows")
    parser.add_argument("--baseline", type=Path, required=True, help="existing paired_scores.csv")
    parser.add_argument("--output-dir", type=Path, required=True, help="new local directory; refuses overwrite")
    parser.add_argument("--holdout-start", type=date.fromisoformat, default=date(2026, 6, 6))
    parser.add_argument("--holdout-end", type=date.fromisoformat, default=date(2026, 9, 3))
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error("output directory already exists; choose a new directory")
    with gzip.open(args.export, "rt") as handle:
        payload = json.load(handle)
    result, scores, calibration_rows = run_experiment(
        payload, load_pairs(args.baseline), args.holdout_start, args.holdout_end)
    result["input_sha256"] = {"export": hashlib.sha256(args.export.read_bytes()).hexdigest(),
                              "baseline": hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
                              "script": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    args.output_dir.mkdir(parents=True, exist_ok=False)
    write_csv(args.output_dir / "scored_cases.csv", scores)
    write_csv(args.output_dir / "calibration_cases.csv", calibration_rows)
    (args.output_dir / "results.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"paired_cases": len(scores), "primary": result["primary"]}, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
