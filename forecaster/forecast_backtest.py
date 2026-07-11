"""Walk-forward backtest harness for the clean next-day SFO high forecast.

The "clean forecast miss" is the absolute error of a clean next-day blend
snapshot against the CLISFO settlement -- the same truth Kalshi resolves on. This
module scores *any* candidate forecaster (the production weighted blend, a learned
weight vector, or a rolling de-bias variant) over historical clean days,
out-of-sample, and emits a fail-closed acceptance verdict so a change only ships
if it provably lowers the miss without regressing the warm/hot tail.

Design choices that keep the harness honest:

* Rolling-origin: a predictor only ever sees days strictly before the day it
  scores -- history is appended *after* the prediction is made.
* CLISFO truth only: a day is scored against its CLISFO settlement; days without
  one are excluded and counted, never silently averaged against a fallback.
* Daily resolution: the unit is one independent weather day, deduped to the last
  pre-midnight clean snapshot -- exactly what the live blend committed.
* Pure standard library: no numpy/pandas/scipy, so the project test runner can
  exercise it without heavyweight deps.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from forecast_scoring import is_clean_next_day_forecast, parse_details_json
from settlement_calendar import integer_settlement_high_f
from google_weather_cache import (
    ADAPTIVE_SOURCE_COLUMNS,
    BLEND_WEIGHTS,
    ROLLING_BIAS_CAP_F,
    ROLLING_BIAS_COHORT_SHRINK_K,
    ROLLING_BIAS_WINDOW_DAYS,
    cap_magnitude,
    predicted_temperature_cohort,
)


DB_PATH = Path("weather.db")
FORECAST_DATA_PATH = Path("forecast_data.json")
RESULTS_PATH = Path("forecast_backtest_results.json")
SIGMA_FLOOR_F = 1.5
COHORTS = ("cold", "normal", "warm", "hot")
LEAD_BUCKETS = ((0.0, 24.0, "<=24h"), (24.0, 30.0, "24-30h"), (30.0, math.inf, ">30h"))
SPREAD_BUCKETS = ((0.0, 2.0, "<=2F"), (2.0, 4.0, "2-4F"), (4.0, math.inf, ">4F"))


def _finite(value) -> bool:
    return value is not None and isinstance(value, (int, float)) and math.isfinite(float(value))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


# --------------------------------------------------------------------------- #
# Row loading
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BlendRow:
    target_date: str
    fetched_at: str
    predicted_high_f: float
    raw_weighted_prediction_f: float | None
    google_high_f: float | None
    nws_high_f: float | None
    open_meteo_high_f: float | None
    history_high_f: float | None
    station_adjustment_f: float
    lead_hours: float | None
    actual_high_f: float
    truth_source: str
    details: dict = field(default_factory=dict)

    def source_highs(self) -> dict[str, float]:
        out = {}
        for key, column in ADAPTIVE_SOURCE_COLUMNS.items():
            value = getattr(self, column)
            if _finite(value):
                out[key] = float(value)
        return out

    def source_spread(self) -> float | None:
        highs = list(self.source_highs().values())
        if len(highs) < 2:
            return None
        return max(highs) - min(highs)


@dataclass
class LoadDiagnostics:
    clean_days: int = 0
    scored_days: int = 0
    excluded_no_clisfo: int = 0
    stored_truth_sources: dict = field(default_factory=dict)


def load_clean_blend_rows(
    conn: sqlite3.Connection,
) -> tuple[list[BlendRow], LoadDiagnostics]:
    """Clean, latest-per-day blend snapshots scored against CLISFO truth."""

    conn.row_factory = sqlite3.Row
    if not _table_exists(conn, "forecast_blend_daily_high"):
        return [], LoadDiagnostics()

    settlements: dict[str, float] = {}
    if _table_exists(conn, "cli_settlements"):
        final_filter = (
            "AND is_final = 1"
            if _has_column(conn, "cli_settlements", "is_final")
            else ""
        )
        truth_cursor = conn.execute(
            "SELECT local_date, max_temperature_f FROM cli_settlements "
            "WHERE station_id = 'KSFO' AND max_temperature_f IS NOT NULL "
            f"{final_filter}"
        )
    elif _table_exists(conn, "clisfo_settlements"):
        truth_cursor = conn.execute(
            "SELECT local_date, max_temperature_f FROM clisfo_settlements "
            "WHERE max_temperature_f IS NOT NULL"
        )
    else:
        truth_cursor = ()
    for local_date, max_t in truth_cursor:
        value = integer_settlement_high_f(max_t)
        if value is not None:
            settlements[local_date] = value

    has_truth_source = _has_column(conn, "forecast_blend_daily_high", "truth_source")
    truth_select = "truth_source" if has_truth_source else "NULL AS truth_source"
    raw = conn.execute(
        f"""
        SELECT fetched_at, target_date, lead_hours, predicted_high_f,
               google_high_f, nws_high_f, open_meteo_high_f, history_high_f,
               station_adjustment_f, details_json, actual_high_f, {truth_select}
        FROM forecast_blend_daily_high
        ORDER BY target_date, fetched_at
        """
    ).fetchall()

    # Clean eligibility, then keep the last pre-midnight snapshot per target day.
    latest_by_day: dict[str, sqlite3.Row] = {}
    for row in raw:
        if not is_clean_next_day_forecast(row["target_date"], row["fetched_at"], row["details_json"]):
            continue
        current = latest_by_day.get(row["target_date"])
        if current is None or row["fetched_at"] > current["fetched_at"]:
            latest_by_day[row["target_date"]] = row

    diagnostics = LoadDiagnostics(clean_days=len(latest_by_day))
    rows: list[BlendRow] = []
    for target_date in sorted(latest_by_day):
        row = latest_by_day[target_date]
        settlement = settlements.get(target_date)
        if settlement is None:
            diagnostics.excluded_no_clisfo += 1
            continue
        details = parse_details_json(row["details_json"])
        raw_pred = details.get("raw_weighted_prediction_f")
        rows.append(
            BlendRow(
                target_date=target_date,
                fetched_at=row["fetched_at"],
                predicted_high_f=float(row["predicted_high_f"]),
                raw_weighted_prediction_f=float(raw_pred) if _finite(raw_pred) else None,
                google_high_f=row["google_high_f"],
                nws_high_f=row["nws_high_f"],
                open_meteo_high_f=row["open_meteo_high_f"],
                history_high_f=row["history_high_f"],
                station_adjustment_f=float(row["station_adjustment_f"] or 0.0),
                lead_hours=row["lead_hours"],
                actual_high_f=float(settlement),
                truth_source="clisfo",
                details=details,
            )
        )
        source = row["truth_source"] or "unknown"
        diagnostics.stored_truth_sources[source] = (
            diagnostics.stored_truth_sources.get(source, 0) + 1
        )
    diagnostics.scored_days = len(rows)
    return rows, diagnostics


# --------------------------------------------------------------------------- #
# Predictors
# --------------------------------------------------------------------------- #

PredictorFn = Callable[[BlendRow, "list[BlendRow]"], "float | None"]


def _weighted_high(row: BlendRow, weights: dict[str, float]) -> float | None:
    total = 0.0
    weight_sum = 0.0
    for key, value in row.source_highs().items():
        weight = weights.get(key, 0.0)
        if weight > 0:
            total += weight * value
            weight_sum += weight
    if weight_sum <= 0:
        return None
    return total / weight_sum + row.station_adjustment_f


def _stored_raw(row: BlendRow) -> float | None:
    """The actual raw (pre-de-bias) prediction production committed for a day.

    Prefers ``raw_weighted_prediction_f`` from the snapshot details -- which bakes
    in whatever adaptive weights and station adjustment production used that day --
    so the baseline is the real production blend, not a static-weight strawman.
    """

    raw = row.raw_weighted_prediction_f
    if _finite(raw):
        return float(raw)
    base = _weighted_high(row, BLEND_WEIGHTS)
    return base if base is not None else row.predicted_high_f


def make_weighted_predictor(weights: dict[str, float]) -> PredictorFn:
    """Reconstruct the raw (pre-de-bias) weighted blend from source columns."""

    frozen = dict(weights)

    def predictor(row: BlendRow, history: list[BlendRow]) -> float | None:
        return _weighted_high(row, frozen)

    return predictor


def make_production_predictor(base_fn: "Callable[[BlendRow], float | None] | None" = None) -> PredictorFn:
    """The production blend baseline -- the raw prediction it actually committed."""

    resolved = base_fn or _stored_raw

    def predictor(row: BlendRow, history: list[BlendRow]) -> float | None:
        return resolved(row)

    return predictor


def make_debias_predictor(
    weights: dict[str, float] | None = None,
    *,
    base_fn: "Callable[[BlendRow], float | None] | None" = None,
    window: int = ROLLING_BIAS_WINDOW_DAYS,
    cap: float = ROLLING_BIAS_CAP_F,
    shrink_k: float = ROLLING_BIAS_COHORT_SHRINK_K,
    min_history_days: int = 30,
) -> PredictorFn:
    """A base prediction plus a cohort-aware rolling residual de-bias.

    The correction is learned only from prior settled days (rolling-origin), so it
    is out-of-sample by construction. Mirrors
    ``google_weather_cache.rolling_blend_residual_bias`` but learns from the live
    backtest history instead of the database. ``base_fn`` selects the prediction
    the correction is applied to; it defaults to the source-weighted blend, but
    ``run_default_report`` passes ``_stored_raw`` to de-bias the real production
    blend.
    """

    if base_fn is None:
        frozen = dict(weights) if weights is not None else dict(BLEND_WEIGHTS)
        base_fn = lambda row: _weighted_high(row, frozen)  # noqa: E731
    resolved = base_fn

    def predictor(row: BlendRow, history: list[BlendRow]) -> float | None:
        base = resolved(row)
        if base is None:
            return None
        recent = history[-window:] if window else history
        records = []
        for prior in recent:
            raw_pred = resolved(prior)
            if raw_pred is None:
                continue
            records.append(
                {
                    "cohort": predicted_temperature_cohort(raw_pred),
                    "residual": prior.actual_high_f - raw_pred,
                }
            )
        if len(records) < min_history_days:
            return base
        corrections, global_correction = _cohort_corrections(records, shrink_k, cap)
        cohort = predicted_temperature_cohort(base)
        correction = corrections.get(cohort, global_correction)
        return base + cap_magnitude(correction, cap)

    return predictor


def make_source_mos_predictor(
    weights: dict[str, float] | None = None,
    *,
    window: int = ROLLING_BIAS_WINDOW_DAYS,
    cap: float = ROLLING_BIAS_CAP_F,
    shrink_k: float = ROLLING_BIAS_COHORT_SHRINK_K,
    min_history_days: int = 30,
) -> PredictorFn:
    """Correct each source before blending, learned only from prior days.

    This is the backtest mirror of a MOS-style live blend: each available source
    receives a capped residual correction by predicted-temperature cohort, then
    the corrected sources are blended with nonnegative weights.
    """

    frozen = dict(weights) if weights is not None else dict(BLEND_WEIGHTS)

    def predictor(row: BlendRow, history: list[BlendRow]) -> float | None:
        base_sources = row.source_highs()
        if not base_sources:
            return None
        if len(history) < min_history_days:
            return _weighted_high(row, frozen)
        recent = history[-window:] if window else history
        corrections = _source_mos_corrections(recent, frozen, shrink_k=shrink_k, cap=cap)
        corrected = {}
        for key, value in base_sources.items():
            correction = _source_mos_correction_for(
                key,
                value,
                corrections,
                cap=cap,
            )
            corrected[key] = value + correction
        return _weighted_source_values(corrected, frozen, row.station_adjustment_f)

    return predictor


def _weighted_source_values(
    source_values: dict[str, float],
    weights: dict[str, float],
    station_adjustment_f: float,
) -> float | None:
    total = 0.0
    weight_sum = 0.0
    for key, value in source_values.items():
        weight = weights.get(key, 0.0)
        if weight > 0:
            total += weight * value
            weight_sum += weight
    if weight_sum <= 0:
        return None
    return total / weight_sum + station_adjustment_f


def _source_mos_corrections(
    history: list[BlendRow],
    weights: dict[str, float],
    *,
    shrink_k: float,
    cap: float,
) -> dict:
    records: list[dict] = []
    by_source: dict[str, list[dict]] = {}
    for prior in history:
        for key, value in prior.source_highs().items():
            if weights.get(key, 0.0) <= 0:
                continue
            record = {
                "source": key,
                "cohort": predicted_temperature_cohort(value),
                "residual": prior.actual_high_f - value,
            }
            records.append(record)
            by_source.setdefault(key, []).append(record)
    if not records:
        return {"global": 0.0, "sources": {}}
    global_mean = cap_magnitude(
        sum(record["residual"] for record in records) / len(records),
        cap,
    )
    source_tables = {}
    for key, source_records in by_source.items():
        source_mean = sum(record["residual"] for record in source_records) / len(source_records)
        source_mean = cap_magnitude(source_mean, cap)
        by_cohort: dict[str, list[float]] = {}
        for record in source_records:
            by_cohort.setdefault(record["cohort"], []).append(record["residual"])
        cohorts = {}
        for cohort, residuals in by_cohort.items():
            count = len(residuals)
            cohort_mean = sum(residuals) / count
            weight = count / (count + shrink_k)
            shrunk = weight * cohort_mean + (1.0 - weight) * source_mean
            cohorts[cohort] = cap_magnitude(shrunk, cap)
        source_tables[key] = {
            "global": source_mean,
            "cohorts": cohorts,
        }
    return {"global": global_mean, "sources": source_tables}


def _source_mos_correction_for(
    source: str,
    value: float,
    corrections: dict,
    *,
    cap: float,
) -> float:
    source_table = (corrections.get("sources") or {}).get(source)
    if not source_table:
        return cap_magnitude(float(corrections.get("global", 0.0)), cap)
    cohort = predicted_temperature_cohort(value)
    correction = (source_table.get("cohorts") or {}).get(cohort)
    if correction is None:
        correction = source_table.get("global", corrections.get("global", 0.0))
    return cap_magnitude(float(correction), cap)


def _cohort_corrections(records, shrink_k, cap):
    if not records:
        return {}, 0.0
    global_mean = sum(r["residual"] for r in records) / len(records)
    by_cohort: dict[str, list[float]] = {}
    for record in records:
        by_cohort.setdefault(record["cohort"], []).append(record["residual"])
    corrections = {}
    for cohort, residuals in by_cohort.items():
        count = len(residuals)
        cohort_mean = sum(residuals) / count
        weight = count / (count + shrink_k)
        shrunk = weight * cohort_mean + (1.0 - weight) * global_mean
        corrections[cohort] = cap_magnitude(shrunk, cap)
    return corrections, cap_magnitude(global_mean, cap)


# --------------------------------------------------------------------------- #
# Statistics (pure-Python)
# --------------------------------------------------------------------------- #


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = pct / 100.0 * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    frac = rank - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _normal_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def diebold_mariano(deltas: list[float]) -> dict:
    """Newey-West Diebold-Mariano test on paired loss deltas (pure-Python).

    Mirrors ``ab_test.diebold_mariano_test`` but avoids the numpy/scipy import so
    the harness stays dependency-light. Negative stat => the first arm (candidate)
    has lower loss.
    """

    values = [d for d in deltas if math.isfinite(d)]
    n = len(values)
    if n < 3:
        return {"stat": float("nan"), "p_value": float("nan"), "lags": 0}
    mean = sum(values) / n
    centered = [v - mean for v in values]
    max_lag = min(n - 1, max(0, round(n ** (1.0 / 3.0))))
    gamma0 = sum(c * c for c in centered) / n
    long_run_var = gamma0
    for lag in range(1, max_lag + 1):
        cov = sum(centered[i] * centered[i - lag] for i in range(lag, n)) / n
        weight = 1.0 - lag / (max_lag + 1.0)
        long_run_var += 2.0 * weight * cov
    if long_run_var <= 0:
        return {"stat": float("nan"), "p_value": float("nan"), "lags": max_lag}
    stat = mean / math.sqrt(long_run_var / n)
    p_value = math.erfc(abs(stat) / math.sqrt(2.0))
    return {"stat": stat, "p_value": p_value, "lags": max_lag}


def moving_block_bootstrap_ci(
    deltas: list[float], *, samples: int, seed: int, alpha: float = 0.05
) -> tuple[float, float] | None:
    """Block-bootstrap CI on the mean paired delta (preserves serial dependence)."""

    n = len(deltas)
    if n < 2:
        return None
    block = max(1, round(n ** (1.0 / 3.0)))
    # True moving-block bootstrap: blocks start only where a full block fits, no
    # circular wrap. Wrapping would treat the series as periodic and narrow the CI
    # -- anti-conservative for a gate that requires ci_high < 0 to ship.
    max_start = max(1, n - block + 1)
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(samples):
        resampled: list[float] = []
        while len(resampled) < n:
            start = rng.randrange(max_start)
            resampled.extend(deltas[start + offset] for offset in range(block))
        resampled = resampled[:n]
        means.append(sum(resampled) / n)
    return _percentile(means, 100 * alpha / 2), _percentile(means, 100 * (1 - alpha / 2))


# --------------------------------------------------------------------------- #
# Calibration (Gaussian multi-category Brier vs climatology)
# --------------------------------------------------------------------------- #


def _load_climatology(path: Path = FORECAST_DATA_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text()).get("table", {})
    except (json.JSONDecodeError, OSError):
        return {}


def _gaussian_bin_probs(mu: float, sigma: float) -> dict[int, float]:
    sigma = max(sigma, SIGMA_FLOOR_F)
    lo = int(math.floor(mu - 4 * sigma))
    hi = int(math.ceil(mu + 4 * sigma))
    probs = {}
    for b in range(lo, hi + 1):
        probs[b] = _normal_cdf((b + 0.5 - mu) / sigma) - _normal_cdf((b - 0.5 - mu) / sigma)
    return probs


def _multicat_brier(mu: float, sigma: float, realized_bin: int) -> float:
    probs = _gaussian_bin_probs(mu, sigma)
    p_realized = probs.get(realized_bin, 0.0)
    sum_sq = sum(p * p for p in probs.values())
    return 1.0 - 2.0 * p_realized + sum_sq


def _residual_sigma(signed_errors: list[float]) -> float:
    if len(signed_errors) < 2:
        return SIGMA_FLOOR_F
    mean = sum(signed_errors) / len(signed_errors)
    variance = sum((e - mean) ** 2 for e in signed_errors) / (len(signed_errors) - 1)
    return max(math.sqrt(variance), SIGMA_FLOOR_F)


def cohort_sigmas(per_day: list[dict]) -> dict[str, float]:
    """Per-cohort (and overall) residual sigma; shareable across arms for a fair
    Brier comparison so a candidate cannot look better calibrated merely because
    its own tighter errors shrink its own sigma."""

    sigmas = {
        cohort: _residual_sigma([d["signed_error"] for d in per_day if d["settled_cohort"] == cohort])
        for cohort in COHORTS
    }
    sigmas["overall"] = _residual_sigma([d["signed_error"] for d in per_day])
    return sigmas


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #


def _bucket_metrics(records: list[dict]) -> dict:
    if not records:
        return {"days": 0, "mae": None, "bias": None, "within3": None, "rmse": None}
    errors = [r["error"] for r in records]
    signed = [r["signed_error"] for r in records]
    return {
        "days": len(records),
        "mae": round(sum(errors) / len(errors), 3),
        "bias": round(sum(signed) / len(signed), 3),
        "rmse": round(math.sqrt(sum(e * e for e in errors) / len(errors)), 3),
        "within3": round(sum(e <= 3 for e in errors) / len(errors) * 100, 1),
    }


def run_forecast_backtest(
    rows: list[BlendRow],
    predictor: PredictorFn,
    *,
    label: str = "candidate",
    climatology: dict | None = None,
    shared_sigmas: dict[str, float] | None = None,
) -> dict:
    """Score a predictor over clean days, rolling-origin, against CLISFO truth."""

    climatology = climatology if climatology is not None else _load_climatology()
    ordered = sorted(rows, key=lambda r: (r.target_date, r.fetched_at))
    history: list[BlendRow] = []
    per_day: list[dict] = []
    for row in ordered:
        prediction = predictor(row, history)
        history.append(row)  # rolling-origin: only prior days were visible
        if prediction is None:
            continue
        actual = row.actual_high_f
        per_day.append(
            {
                "date": row.target_date,
                "predicted": round(prediction, 3),
                "actual": actual,
                "error": abs(prediction - actual),
                "signed_error": prediction - actual,
                "forecast_cohort": predicted_temperature_cohort(prediction),
                "settled_cohort": predicted_temperature_cohort(actual),
                "lead_hours": row.lead_hours,
                "source_spread": row.source_spread(),
            }
        )

    errors = [d["error"] for d in per_day]
    signed = [d["signed_error"] for d in per_day]
    n = len(per_day)
    headline = {
        "days": n,
        "mae": round(sum(errors) / n, 3) if n else None,
        "rmse": round(math.sqrt(sum(e * e for e in errors) / n), 3) if n else None,
        "bias": round(sum(signed) / n, 3) if n else None,
        "within2": round(sum(e <= 2 for e in errors) / n * 100, 1) if n else None,
        "within3": round(sum(e <= 3 for e in errors) / n * 100, 1) if n else None,
        "within5": round(sum(e <= 5 for e in errors) / n * 100, 1) if n else None,
    }

    by_settled_cohort = {
        cohort: _bucket_metrics([d for d in per_day if d["settled_cohort"] == cohort])
        for cohort in COHORTS
    }
    by_forecast_cohort = {
        cohort: _bucket_metrics([d for d in per_day if d["forecast_cohort"] == cohort])
        for cohort in COHORTS
    }
    by_lead = {
        bucket[2]: _bucket_metrics(
            [d for d in per_day if d["lead_hours"] is not None and bucket[0] <= d["lead_hours"] < bucket[1]]
        )
        for bucket in LEAD_BUCKETS
    }
    by_spread = {
        bucket[2]: _bucket_metrics(
            [d for d in per_day if d["source_spread"] is not None and bucket[0] <= d["source_spread"] < bucket[1]]
        )
        for bucket in SPREAD_BUCKETS
    }

    calibration = _calibration_block(per_day, climatology, shared_sigmas)

    return {
        "label": label,
        "headline": headline,
        "by_settled_cohort": by_settled_cohort,
        "by_forecast_cohort": by_forecast_cohort,
        "by_lead": by_lead,
        "by_source_disagreement": by_spread,
        "calibration": calibration,
        "per_day": per_day,
    }


def _calibration_block(
    per_day: list[dict], climatology: dict, shared_sigmas: dict[str, float] | None = None
) -> dict:
    """Per-cohort residual sigma + Gaussian multi-cat Brier and skill vs climo.

    When ``shared_sigmas`` is supplied (the production arm's sigma), both arms
    score Brier with the same sigma so a candidate cannot appear better calibrated
    purely because its tighter errors shrink its own sigma.
    """

    if not per_day:
        return {"overall": None, "by_settled_cohort": {}}

    sigmas = shared_sigmas or cohort_sigmas(per_day)
    sigma_by_cohort = {cohort: sigmas.get(cohort, SIGMA_FLOOR_F) for cohort in COHORTS}
    overall_sigma = sigmas.get("overall", _residual_sigma([d["signed_error"] for d in per_day]))

    def cohort_brier(records: list[dict]) -> dict:
        if not records:
            return {"days": 0, "brier": None, "climo_brier": None, "brier_skill": None}
        forecast_scores = []
        climo_scores = []
        for record in records:
            realized = int(round(record["actual"]))
            sigma = sigma_by_cohort.get(record["settled_cohort"], overall_sigma)
            forecast_scores.append(_multicat_brier(record["predicted"], sigma, realized))
            climo = climatology.get(record["date"][5:]) if climatology else None
            if climo and _finite(climo.get("mean")) and _finite(climo.get("std")):
                climo_scores.append(
                    _multicat_brier(float(climo["mean"]), float(climo["std"]), realized)
                )
            else:
                climo_scores.append(None)
        brier = sum(forecast_scores) / len(forecast_scores)
        paired = [(f, c) for f, c in zip(forecast_scores, climo_scores) if c is not None]
        if paired:
            f_mean = sum(f for f, _ in paired) / len(paired)
            c_mean = sum(c for _, c in paired) / len(paired)
            skill = 1.0 - f_mean / c_mean if c_mean > 0 else None
        else:
            c_mean = None
            skill = None
        return {
            "days": len(records),
            "brier": round(brier, 4),
            "climo_brier": round(c_mean, 4) if c_mean is not None else None,
            "brier_skill": round(skill, 4) if skill is not None else None,
        }

    return {
        "overall": cohort_brier(per_day),
        "overall_residual_sigma_f": round(overall_sigma, 3),
        "residual_sigma_by_cohort_f": {k: round(v, 3) for k, v in sigma_by_cohort.items()},
        "by_settled_cohort": {
            cohort: cohort_brier([d for d in per_day if d["settled_cohort"] == cohort])
            for cohort in COHORTS
        },
    }


# --------------------------------------------------------------------------- #
# Paired comparison + acceptance
# --------------------------------------------------------------------------- #


def compare_forecasters(
    production: dict, candidate: dict, *, samples: int = 2000, seed: int = 0
) -> dict:
    """Paired daily comparison: delta = candidate error - production error.

    Negative delta means the candidate is closer to settlement. Uses a moving
    block bootstrap (autocorrelated daily highs) and a Diebold-Mariano test.
    """

    prod_by_date = {d["date"]: d["error"] for d in production["per_day"]}
    deltas: list[float] = []
    paired_dates: list[str] = []
    for record in candidate["per_day"]:
        prod_error = prod_by_date.get(record["date"])
        if prod_error is None:
            continue
        deltas.append(record["error"] - prod_error)
        paired_dates.append(record["date"])

    n = len(deltas)
    mean_delta = sum(deltas) / n if n else None
    ci = moving_block_bootstrap_ci(deltas, samples=samples, seed=seed) if n >= 2 else None
    dm = diebold_mariano(deltas)
    candidate_wins = sum(1 for d in deltas if d < 0)
    return {
        "paired_days": n,
        "mean_delta_f": round(mean_delta, 4) if mean_delta is not None else None,
        "ci_low": round(ci[0], 4) if ci else None,
        "ci_high": round(ci[1], 4) if ci else None,
        "dm_stat": round(dm["stat"], 4) if math.isfinite(dm["stat"]) else None,
        "dm_p_value": round(dm["p_value"], 6) if math.isfinite(dm["p_value"]) else None,
        "candidate_win_rate": round(candidate_wins / n, 4) if n else None,
        "bootstrap_samples": samples,
        "seed": seed,
    }


@dataclass(frozen=True)
class ForecastAcceptanceThresholds:
    min_days: int = 30
    cohort_min_days: int = 10
    cohort_mae_tolerance_f: float = 0.25
    cohort_hard_regression_f: float = 0.5
    tail_cohorts: tuple[str, ...] = ("warm", "hot")
    dm_alpha: float = 0.05
    bias_tolerance_f: float = 0.1
    brier_tolerance: float = 0.02


def evaluate_acceptance(
    production: dict,
    candidate: dict,
    paired: dict,
    thresholds: ForecastAcceptanceThresholds | None = None,
) -> dict:
    """Fail-closed verdict: a change ships only if every blocking check passes."""

    t = thresholds or ForecastAcceptanceThresholds()
    checks: list[dict] = []

    def add(name, passed, detail):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    cand_n = candidate["headline"]["days"]
    add(
        "sample_sufficiency",
        cand_n >= t.min_days,
        f"{cand_n} clean CLISFO-scored days (need >= {t.min_days})",
    )

    cand_mae = candidate["headline"]["mae"]
    prod_mae = production["headline"]["mae"]
    ci_high = paired["ci_high"]
    dm_p = paired["dm_p_value"]
    dm_stat = paired.get("dm_stat")
    skill_pass = (
        cand_mae is not None
        and prod_mae is not None
        and cand_mae <= prod_mae
        and ci_high is not None
        and ci_high < 0  # whole CI below 0 => candidate strictly better
        and dm_p is not None
        and dm_p < t.dm_alpha
        and dm_stat is not None
        and dm_stat < 0  # explicit direction guard: candidate has lower loss
    )
    add(
        "aggregate_skill",
        skill_pass,
        f"candidate MAE {cand_mae} vs production {prod_mae}; "
        f"delta 95% CI high {ci_high} (<0 required); DM stat {dm_stat} p {dm_p} (<{t.dm_alpha})",
    )

    cohort_issues = []
    tail_notes = []
    for cohort in COHORTS:
        cand = candidate["by_settled_cohort"][cohort]
        prod = production["by_settled_cohort"][cohort]
        cand_days, prod_days = cand["days"], prod["days"]
        is_tail = cohort in t.tail_cohorts
        if cand["mae"] is None or prod["mae"] is None:
            # Candidate produced no prediction for this regime at all.
            if is_tail and prod_days >= t.cohort_min_days:
                cohort_issues.append(
                    f"tail {cohort}: candidate has no scored days, production has {prod_days}"
                )
            continue
        delta = cand["mae"] - prod["mae"]
        if is_tail:
            # Hard tail gate: any measurable warm/hot regression blocks. A tail too
            # thin to judge is surfaced, never silently skipped.
            if cand_days >= t.cohort_min_days:
                if delta > 0:
                    cohort_issues.append(f"tail {cohort} MAE +{delta:.2f}F (no regression allowed)")
            elif prod_days >= t.cohort_min_days:
                cohort_issues.append(
                    f"tail {cohort}: only {cand_days} candidate days (need {t.cohort_min_days})"
                )
            else:
                tail_notes.append(f"{cohort} inconclusive ({cand_days}d)")
        else:
            if cand_days < t.cohort_min_days:
                continue
            if delta > t.cohort_hard_regression_f:
                cohort_issues.append(f"{cohort} MAE +{delta:.2f}F (> {t.cohort_hard_regression_f})")
            elif delta > t.cohort_mae_tolerance_f:
                cohort_issues.append(f"{cohort} MAE +{delta:.2f}F (> {t.cohort_mae_tolerance_f})")
    cohort_detail = (
        "; ".join(cohort_issues) if cohort_issues else "no cohort regression beyond tolerance"
    )
    if tail_notes:
        cohort_detail += " | tail inconclusive: " + ", ".join(tail_notes)
    add("no_cohort_regression", not cohort_issues, cohort_detail)

    calib_issues = []
    for cohort in t.tail_cohorts:
        cand = candidate["calibration"]["by_settled_cohort"].get(cohort, {})
        prod = production["calibration"]["by_settled_cohort"].get(cohort, {})
        if cand.get("brier") is None or prod.get("brier") is None:
            continue
        if cand["brier"] > prod["brier"] + t.brier_tolerance:
            calib_issues.append(
                f"tail {cohort} Brier {cand['brier']} > {prod['brier']} + {t.brier_tolerance}"
            )
    add(
        "calibration_not_worse",
        not calib_issues,
        "; ".join(calib_issues) if calib_issues else "tail cohort Brier not worse",
    )

    cand_bias = candidate["headline"]["bias"]
    prod_bias = production["headline"]["bias"]
    bias_pass = (
        cand_bias is not None
        and prod_bias is not None
        and abs(cand_bias) <= abs(prod_bias) + t.bias_tolerance_f
    )
    add(
        "no_bias_inflation",
        bias_pass,
        f"|candidate bias| {abs(cand_bias) if cand_bias is not None else None} "
        f"vs |production bias| {abs(prod_bias) if prod_bias is not None else None}",
    )

    accepted = all(check["passed"] for check in checks)
    return {"accepted": accepted, "checks": checks}


# --------------------------------------------------------------------------- #
# Diagnostics: CLISFO vs NWS divergence + truth/predictor reconciliation
# --------------------------------------------------------------------------- #


def clisfo_nws_divergence(conn: sqlite3.Connection) -> dict:
    """Quantify the CLISFO-vs-NWS-daily divergence (not assert it)."""

    has_new = _table_exists(conn, "cli_settlements")
    if not (
        (has_new or _table_exists(conn, "clisfo_settlements"))
        and _table_exists(conn, "nws_daily_high_ground_truth")
    ):
        return {"available": False, "rows": [], "summary": {}}

    truth_table = "cli_settlements" if has_new else "clisfo_settlements"
    truth_filter = "AND c.station_id = 'KSFO'" if has_new else ""
    if has_new and _has_column(conn, "cli_settlements", "is_final"):
        truth_filter += " AND c.is_final = 1"
    rows = conn.execute(
        f"""
        SELECT c.local_date, c.max_temperature_f AS clisfo, n.high_f AS nws
        FROM {truth_table} c
        JOIN nws_daily_high_ground_truth n
          ON n.local_date = c.local_date AND n.station_id = 'KSFO' AND n.is_complete = 1
        WHERE c.max_temperature_f IS NOT NULL AND n.high_f IS NOT NULL {truth_filter}
        ORDER BY c.local_date
        """
    ).fetchall()
    detail = []
    diffs = []
    bin_flips = 0
    for local_date, clisfo, nws in rows:
        clisfo_int = integer_settlement_high_f(clisfo)
        nws_int = integer_settlement_high_f(nws)
        if clisfo_int is None or nws_int is None:
            continue
        diff = clisfo_int - nws_int
        diffs.append(diff)
        if diff != 0:
            bin_flips += 1
        detail.append(
            {"date": local_date, "clisfo": clisfo_int, "nws": nws_int, "diff": diff}
        )
    summary = {
        "days": len(diffs),
        "mean_abs_diff": round(sum(abs(d) for d in diffs) / len(diffs), 3) if diffs else None,
        "max_abs_diff": max((abs(d) for d in diffs), default=None),
        "bin_flips": bin_flips,
        "bin_flip_rate": round(bin_flips / len(diffs), 3) if diffs else None,
    }
    return {"available": True, "rows": detail, "summary": summary}


def reconcile_truth_and_predictor(rows: list[BlendRow]) -> dict:
    """Attribute forecast error to the truth source and the predictor.

    * truth: stored prediction error vs CLISFO truth (the harness already uses
      CLISFO; reported for transparency).
    * predictor: MAE of each bare source vs CLISFO and the blend vs CLISFO, so the
      "blend helps/hurts" question is answerable from stored columns alone.
    """

    if not rows:
        return {"days": 0}
    blend_errors = [abs(r.predicted_high_f - r.actual_high_f) for r in rows]
    source_errors: dict[str, list[float]] = {key: [] for key in ADAPTIVE_SOURCE_COLUMNS}
    for row in rows:
        for key, high in row.source_highs().items():
            source_errors[key].append(abs(high - row.actual_high_f))
    return {
        "days": len(rows),
        "blend_mae_vs_clisfo": round(sum(blend_errors) / len(blend_errors), 3),
        "source_mae_vs_clisfo": {
            key: round(sum(errs) / len(errs), 3) for key, errs in source_errors.items() if errs
        },
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def run_default_report(db_path: Path, *, samples: int, seed: int) -> dict:
    with sqlite3.connect(db_path) as conn:
        rows, diagnostics = load_clean_blend_rows(conn)
        divergence = clisfo_nws_divergence(conn)
    climatology = _load_climatology()

    production = run_forecast_backtest(
        rows, make_production_predictor(), label="production_blend",
        climatology=climatology,
    )
    # Score the source-level MOS candidate against production with the same
    # production sigma so calibration comparisons are fair.
    shared = cohort_sigmas(production["per_day"]) if production["per_day"] else None
    candidate = run_forecast_backtest(
        rows, make_source_mos_predictor(BLEND_WEIGHTS), label="source_mos",
        climatology=climatology, shared_sigmas=shared,
    )
    paired = compare_forecasters(production, candidate, samples=samples, seed=seed)
    verdict = evaluate_acceptance(production, candidate, paired)
    return {
        "diagnostics": {
            "clean_days": diagnostics.clean_days,
            "scored_days": diagnostics.scored_days,
            "excluded_no_clisfo": diagnostics.excluded_no_clisfo,
            "stored_truth_sources": diagnostics.stored_truth_sources,
        },
        "clisfo_nws_divergence": divergence,
        "reconciliation": reconcile_truth_and_predictor(rows),
        "production": production,
        "candidate": candidate,
        "paired": paired,
        "acceptance": verdict,
        "seed": seed,
    }


def _print_summary(report: dict) -> None:
    prod = report["production"]["headline"]
    cand = report["candidate"]["headline"]
    paired = report["paired"]
    print("=" * 64)
    print("FORECAST BACKTEST — clean next-day miss vs CLISFO")
    print("=" * 64)
    diag = report["diagnostics"]
    print(
        f"  scored days {diag['scored_days']}  "
        f"(clean {diag['clean_days']}, excluded no-CLISFO {diag['excluded_no_clisfo']})"
    )
    print(f"  truth sources {diag['stored_truth_sources']}")
    print(f"  production blend   MAE {prod['mae']}  bias {prod['bias']}  within3 {prod['within3']}%")
    print(f"  + source MOS       MAE {cand['mae']}  bias {cand['bias']}  within3 {cand['within3']}%")
    print(
        f"  paired delta {paired['mean_delta_f']} "
        f"CI [{paired['ci_low']}, {paired['ci_high']}] DM p {paired['dm_p_value']}"
    )
    div = report["clisfo_nws_divergence"].get("summary", {})
    if div:
        print(
            f"  CLISFO vs NWS: mean|diff| {div.get('mean_abs_diff')}F, "
            f"bin flips {div.get('bin_flips')}/{div.get('days')}"
        )
    verdict = report["acceptance"]
    print(f"  VERDICT: {'ACCEPT' if verdict['accepted'] else 'REJECT'}")
    for check in verdict["checks"]:
        mark = "ok" if check["passed"] else "XX"
        print(f"    [{mark}] {check['name']}: {check['detail']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--out", type=Path, default=RESULTS_PATH)
    parser.add_argument("--bootstrap", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"weather.db not found at {args.db}")
        return 1
    report = run_default_report(args.db, samples=args.bootstrap, seed=args.seed)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    _print_summary(report)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
