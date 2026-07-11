#!/usr/bin/env python3
"""Blend learners and walk-forward promotion gates."""

from __future__ import annotations

import math

from blend_archive import latest_scored_blend_rows
from forecast_scoring import parse_details_json
from google_api import finite
from weather_cache_config import (
    ADAPTIVE_SOURCE_COLUMNS,
    ADAPTIVE_WEIGHT_HOLDOUT_MIN_DAYS,
    ADAPTIVE_WEIGHT_MAX_LEARNED_SHARE,
    ADAPTIVE_WEIGHT_MIN_SCORED_DAYS,
    BLEND_WEIGHTS,
    ENABLE_ROLLING_BLEND_BIAS,
    ENABLE_SOURCE_MOS_CORRECTION,
    ROLLING_BIAS_CAP_F,
    ROLLING_BIAS_COHORT_HOLDOUT_MIN_SAMPLES,
    ROLLING_BIAS_COHORT_REGRESSION_TOL_F,
    ROLLING_BIAS_COHORT_SHRINK_K,
    ROLLING_BIAS_HOLDOUT_MIN_DAYS,
    ROLLING_BIAS_MIN_SCORED_DAYS,
    ROLLING_BIAS_TAIL_COHORTS,
    ROLLING_BIAS_WINDOW_DAYS,
    SOURCE_MOS_CAP_F,
    SOURCE_MOS_COHORT_SHRINK_K,
    SOURCE_MOS_HOLDOUT_MIN_DAYS,
    SOURCE_MOS_MIN_SCORED_DAYS,
)

def normalize_weights(weights):
    total = sum(value for value in weights.values() if finite(value) and value > 0)
    if total <= 0:
        return dict(BLEND_WEIGHTS)
    return {
        key: (float(value) / total if finite(value) and value > 0 else 0.0)
        for key, value in weights.items()
    }




def adaptive_blend_weights():
    cached = getattr(adaptive_blend_weights, "_cached", None)
    if cached is not None:
        return cached

    base = dict(BLEND_WEIGHTS)
    rows = latest_scored_blend_rows()
    scored_days = len({row["target_date"] for row in rows})
    truth_source_counts = {}
    for row in rows:
        key = (row["effective_truth_source"] if "effective_truth_source" in row.keys() else None) or "unknown"
        truth_source_counts[key] = truth_source_counts.get(key, 0) + 1
    metadata = {
        "mode": "base",
        "reason": (
            f"collecting clean next-day scored blend days; need {ADAPTIVE_WEIGHT_MIN_SCORED_DAYS}, "
            f"have {scored_days}"
        ),
        "scored_days": scored_days,
        "eligibility": "last pre-midnight SFO snapshot from the day before target; excludes observed lock/floor rows",
        "truth_source_counts": truth_source_counts,
        "base_weights": base,
        "weights": base,
        "source_mae_f": {},
        "source_counts": {},
        "learned_share": 0.0,
    }

    if scored_days < ADAPTIVE_WEIGHT_MIN_SCORED_DAYS:
        result = (base, metadata)
        adaptive_blend_weights._cached = result
        return result

    # Walk-forward gate: weights learned from the older days must beat the
    # base blend on the most recent days before they get any live share.
    ordered_days = sorted({row["target_date"] for row in rows})
    holdout_count = max(ADAPTIVE_WEIGHT_HOLDOUT_MIN_DAYS, len(ordered_days) // 3)
    holdout_days = set(ordered_days[-holdout_count:])
    train_rows = [row for row in rows if row["target_date"] not in holdout_days]
    holdout_rows = [row for row in rows if row["target_date"] in holdout_days]

    learned_share = min(
        ADAPTIVE_WEIGHT_MAX_LEARNED_SHARE,
        0.25 + (scored_days - ADAPTIVE_WEIGHT_MIN_SCORED_DAYS) * 0.02,
    )

    train_learned, train_mae, train_counts = learned_source_weights(train_rows, base)
    if train_learned is None:
        metadata.update(
            {
                "reason": "not enough per-source scored samples to learn weights safely",
                "source_mae_f": {key: round(value, 2) for key, value in train_mae.items()},
                "source_counts": train_counts,
            }
        )
        result = (base, metadata)
        adaptive_blend_weights._cached = result
        return result

    candidate = normalize_weights(
        {
            key: base[key] * (1 - learned_share) + train_learned[key] * learned_share
            for key in base
        }
    )
    base_holdout_mae = blended_mae(holdout_rows, base)
    candidate_holdout_mae = blended_mae(holdout_rows, candidate)
    holdout_report = {
        "holdout_days": holdout_count,
        "base_mae_f": None if base_holdout_mae is None else round(base_holdout_mae, 3),
        "candidate_mae_f": None if candidate_holdout_mae is None else round(candidate_holdout_mae, 3),
    }
    if (
        base_holdout_mae is None
        or candidate_holdout_mae is None
        or candidate_holdout_mae >= base_holdout_mae
    ):
        metadata.update(
            {
                "reason": (
                    "learned weights did not improve walk-forward holdout blend error; "
                    "keeping base weights"
                ),
                "source_mae_f": {key: round(value, 2) for key, value in train_mae.items()},
                "source_counts": train_counts,
                "holdout": holdout_report,
            }
        )
        result = (base, metadata)
        adaptive_blend_weights._cached = result
        return result

    # Methodology survived the holdout; refit on all scored days for the
    # weights that actually go live.
    learned, source_mae, source_counts = learned_source_weights(rows, base)
    if learned is None:  # pragma: no cover - train superset cannot lose sources
        result = (base, metadata)
        adaptive_blend_weights._cached = result
        return result
    mixed = normalize_weights(
        {
            key: base[key] * (1 - learned_share) + learned[key] * learned_share
            for key in base
        }
    )
    metadata.update(
        {
            "mode": "adaptive",
            "reason": (
                "weights nudged toward lower-MAE sources after beating the base "
                "blend on a walk-forward holdout"
            ),
            "source_mae_f": {key: round(value, 2) for key, value in source_mae.items()},
            "source_counts": source_counts,
            "learned_share": round(learned_share, 3),
            "learned_weights": {key: round(value, 4) for key, value in learned.items()},
            "weights": {key: round(value, 4) for key, value in mixed.items()},
            "holdout": holdout_report,
        }
    )
    result = (mixed, metadata)
    adaptive_blend_weights._cached = result
    return result


def learned_source_weights(rows, base):
    """Inverse-MAE weights from scored rows, or (None, mae, counts) if unsafe."""

    source_errors = {key: [] for key in ADAPTIVE_SOURCE_COLUMNS}
    for row in rows:
        actual = row["actual_high_f"]
        if not finite(actual):
            continue
        for key, column in ADAPTIVE_SOURCE_COLUMNS.items():
            value = row[column]
            if finite(value):
                source_errors[key].append(abs(float(value) - float(actual)))

    scored_days = len(rows)
    min_source_samples = max(3, min(ADAPTIVE_WEIGHT_MIN_SCORED_DAYS, scored_days // 2))
    source_mae = {
        key: sum(errors) / len(errors)
        for key, errors in source_errors.items()
        if len(errors) >= min_source_samples
    }
    source_counts = {key: len(errors) for key, errors in source_errors.items()}
    if len(source_mae) < 2:
        return None, source_mae, source_counts

    inverse_scores = {key: 1 / max(mae, 0.5) for key, mae in source_mae.items()}
    learned_for_scored = normalize_weights(inverse_scores)
    missing_mass = sum(base[key] for key in base if key not in learned_for_scored)
    learned = {}
    for key in base:
        if key in learned_for_scored:
            learned[key] = learned_for_scored[key] * max(0.0, 1 - missing_mass)
        else:
            learned[key] = base[key]
    return normalize_weights(learned), source_mae, source_counts


def blended_mae(rows, weights):
    """MAE of the weighted source blend over scored rows, or None if empty."""

    errors = []
    for row in rows:
        actual = row["actual_high_f"]
        if not finite(actual):
            continue
        total = 0.0
        weight_sum = 0.0
        for key, column in ADAPTIVE_SOURCE_COLUMNS.items():
            value = row[column]
            weight = weights.get(key, 0.0)
            if finite(value) and weight > 0:
                total += weight * float(value)
                weight_sum += weight
        if weight_sum <= 0:
            continue
        errors.append(abs(total / weight_sum - float(actual)))
    if not errors:
        return None
    return sum(errors) / len(errors)


def source_mos_corrections():
    cached = getattr(source_mos_corrections, "_cached", None)
    if cached is not None:
        return cached

    metadata = {
        "mode": "disabled",
        "reason": "",
        "scored_days": 0,
        "cap_f": SOURCE_MOS_CAP_F,
        "eligibility": "clean next-day scored blend rows only",
    }

    def _disabled(reason):
        metadata["reason"] = reason
        result = ({}, dict(metadata))
        source_mos_corrections._cached = result
        return result

    if not ENABLE_SOURCE_MOS_CORRECTION:
        return _disabled("source MOS correction disabled via ENABLE_SOURCE_MOS_CORRECTION")

    # Source MOS is an optional enhancement; a failure here must never take
    # down the refresh (a crash in this path cost days of blend outage).
    try:
        return _compute_source_mos_corrections(metadata, _disabled)
    except Exception as exc:
        return _disabled(f"source MOS correction failed: {type(exc).__name__}: {exc}")


def _compute_source_mos_corrections(metadata, _disabled):
    rows = latest_scored_blend_rows()
    scored_days = len({row["target_date"] for row in rows})
    metadata["scored_days"] = scored_days
    if scored_days < SOURCE_MOS_MIN_SCORED_DAYS:
        return _disabled(
            f"collecting clean next-day scored blend days; need {SOURCE_MOS_MIN_SCORED_DAYS}, "
            f"have {scored_days}"
        )

    ordered_days = sorted({row["target_date"] for row in rows})
    holdout_count = max(SOURCE_MOS_HOLDOUT_MIN_DAYS, len(ordered_days) // 3)
    holdout_days = set(ordered_days[-holdout_count:])
    train = [row for row in rows if row["target_date"] not in holdout_days]
    holdout = [row for row in rows if row["target_date"] in holdout_days]
    train_corrections = _learn_source_mos_corrections(train)
    raw_mae, corrected_mae = _source_mos_holdout_mae(holdout, train_corrections)
    metadata["holdout"] = {
        "holdout_days": holdout_count,
        "raw_mae_f": None if raw_mae is None else round(raw_mae, 3),
        "corrected_mae_f": None if corrected_mae is None else round(corrected_mae, 3),
    }
    if raw_mae is None or corrected_mae is None or corrected_mae >= raw_mae:
        return _disabled("source MOS correction did not improve holdout MAE")

    corrections = _learn_source_mos_corrections(rows)
    metadata.update(
        {
            "mode": "adaptive",
            "reason": "source MOS correction improved clean holdout MAE",
            "source_corrections_f": _round_source_mos_corrections(corrections),
        }
    )
    result = (corrections, dict(metadata))
    source_mos_corrections._cached = result
    return result


def _learn_source_mos_corrections(rows):
    by_source = {}
    all_residuals = []
    for row in rows:
        actual = row["actual_high_f"]
        if not finite(actual):
            continue
        for source_key, column in ADAPTIVE_SOURCE_COLUMNS.items():
            value = row[column]
            if not finite(value):
                continue
            residual = float(actual) - float(value)
            all_residuals.append(residual)
            cohort = predicted_temperature_cohort(value)
            by_source.setdefault(source_key, {}).setdefault(cohort, []).append(residual)
    global_mean = (
        cap_magnitude(sum(all_residuals) / len(all_residuals), SOURCE_MOS_CAP_F)
        if all_residuals
        else 0.0
    )
    corrections = {"global": global_mean, "sources": {}}
    for source_key, cohorts in by_source.items():
        source_residuals = [value for values in cohorts.values() for value in values]
        source_mean = cap_magnitude(
            sum(source_residuals) / len(source_residuals),
            SOURCE_MOS_CAP_F,
        )
        table = {"global": source_mean, "cohorts": {}}
        for cohort, values in cohorts.items():
            count = len(values)
            cohort_mean = sum(values) / count
            weight = count / (count + SOURCE_MOS_COHORT_SHRINK_K)
            table["cohorts"][cohort] = cap_magnitude(
                weight * cohort_mean + (1.0 - weight) * source_mean,
                SOURCE_MOS_CAP_F,
            )
        corrections["sources"][source_key] = table
    return corrections


def _source_mos_holdout_mae(rows, corrections):
    raw_errors = []
    corrected_errors = []
    for row in rows:
        actual = row["actual_high_f"]
        if not finite(actual):
            continue
        raw_pred = _weighted_sources_for_row(row, corrections=None)
        corrected_pred = _weighted_sources_for_row(row, corrections=corrections)
        if raw_pred is None or corrected_pred is None:
            continue
        raw_errors.append(abs(raw_pred - float(actual)))
        corrected_errors.append(abs(corrected_pred - float(actual)))
    if not raw_errors:
        return None, None
    return sum(raw_errors) / len(raw_errors), sum(corrected_errors) / len(corrected_errors)


def _weighted_sources_for_row(row, corrections):
    total = 0.0
    weight_sum = 0.0
    for source_key, column in ADAPTIVE_SOURCE_COLUMNS.items():
        value = row[column]
        weight = BLEND_WEIGHTS.get(source_key, 0.0)
        if not finite(value) or weight <= 0:
            continue
        adjusted = float(value)
        if corrections:
            adjusted += _source_mos_correction_for(source_key, adjusted, corrections)
        total += adjusted * weight
        weight_sum += weight
    if weight_sum <= 0:
        return None
    return total / weight_sum + float(row["station_adjustment_f"] or 0.0)


def _source_mos_correction_for(source_key, value, corrections):
    source_table = (corrections.get("sources") or {}).get(source_key)
    if not source_table:
        return cap_magnitude(float(corrections.get("global", 0.0)), SOURCE_MOS_CAP_F)
    cohort = predicted_temperature_cohort(value)
    correction = (source_table.get("cohorts") or {}).get(cohort)
    if correction is None:
        correction = source_table.get("global", corrections.get("global", 0.0))
    return cap_magnitude(float(correction), SOURCE_MOS_CAP_F)


def apply_source_mos(sources, corrections):
    corrected = {}
    report = {"enabled": bool(corrections), "corrected_sources": {}, "cap_f": SOURCE_MOS_CAP_F}
    for key, row in sources.items():
        corrected_row = dict(row)
        value = row.get("highF")
        correction = 0.0
        if corrections and finite(value):
            correction = _source_mos_correction_for(key, float(value), corrections)
            corrected_row["highF"] = round(float(value) + correction, 2)
            if finite(corrected_row.get("lockHighF")):
                corrected_row["lockHighF"] = round(float(corrected_row["lockHighF"]) + correction, 2)
        corrected[key] = corrected_row
        report["corrected_sources"][key] = {
            "raw_high_f": round(float(value), 2) if finite(value) else None,
            "correction_f": round(correction, 3),
            "corrected_high_f": corrected_row.get("highF"),
        }
    return corrected, report


def _round_source_mos_corrections(corrections):
    rounded = {}
    for key, table in (corrections.get("sources") or {}).items():
        rounded[key] = {
            "global": round(table.get("global", 0.0), 3),
            "cohorts": {
                cohort: round(value, 3)
                for cohort, value in (table.get("cohorts") or {}).items()
            },
        }
    return rounded


def cap_magnitude(value: float, cap: float) -> float:
    return max(-cap, min(cap, value))


def predicted_temperature_cohort(high_f: object) -> str:
    """Temperature band of a predicted high, matching the trading cohorts.

    The blend is anti-calibrated on warm/hot SFO days, so the de-bias is learned
    per band and a sparse band (hot) shrinks toward the global correction.
    """

    if high_f is None or not finite(high_f):
        return "unknown"
    high_f = float(high_f)
    if high_f < 60.0:
        return "cold"
    if high_f < 70.0:
        return "normal"
    if high_f < 80.0:
        return "warm"
    return "hot"


def _bias_residual_records(rows):
    """Signed residuals of the *raw* (pre-de-bias) blend over scored rows.

    Learning against the raw weighted prediction (stored as
    ``raw_weighted_prediction_f``) keeps the estimator stable once the de-bias
    ships -- otherwise it would chase its own correction.
    """

    records = []
    for row in rows:
        actual = row["actual_high_f"]
        if not finite(actual):
            continue
        details = parse_details_json(row["details_json"])
        raw_pred = details.get("raw_weighted_prediction_f")
        if not finite(raw_pred):
            raw_pred = row["predicted_high_f"]
        if not finite(raw_pred):
            continue
        raw_pred = float(raw_pred)
        actual = float(actual)
        records.append(
            {
                "target_date": row["target_date"],
                "raw_pred": raw_pred,
                "actual": actual,
                "residual": actual - raw_pred,
                "cohort": predicted_temperature_cohort(raw_pred),
            }
        )
    return records


def _cohort_bias_corrections(records):
    """Per-cohort mean residual shrunk toward the global mean residual."""

    if not records:
        return {}, 0.0
    global_mean = sum(r["residual"] for r in records) / len(records)
    by_cohort = {}
    for record in records:
        by_cohort.setdefault(record["cohort"], []).append(record["residual"])
    corrections = {}
    for cohort, residuals in by_cohort.items():
        count = len(residuals)
        cohort_mean = sum(residuals) / count
        weight = count / (count + ROLLING_BIAS_COHORT_SHRINK_K)
        shrunk = weight * cohort_mean + (1.0 - weight) * global_mean
        corrections[cohort] = cap_magnitude(shrunk, ROLLING_BIAS_CAP_F)
    return corrections, cap_magnitude(global_mean, ROLLING_BIAS_CAP_F)


def _correction_for(cohort, corrections, global_correction):
    value = corrections.get(cohort)
    if value is None:
        value = global_correction
    return cap_magnitude(value, ROLLING_BIAS_CAP_F)


def _bias_holdout_mae(records, corrections, global_correction):
    """(raw MAE, corrected MAE) on holdout records, or (None, None) if empty."""

    raw_errors = []
    corrected_errors = []
    for record in records:
        raw_errors.append(abs(record["actual"] - record["raw_pred"]))
        correction = _correction_for(record["cohort"], corrections, global_correction)
        corrected_errors.append(abs(record["actual"] - (record["raw_pred"] + correction)))
    if not raw_errors:
        return None, None
    return (
        sum(raw_errors) / len(raw_errors),
        sum(corrected_errors) / len(corrected_errors),
    )


def _bias_holdout_cohort_regressions(records, corrections, global_correction):
    """Cohorts whose holdout MAE the correction would WORSEN beyond tolerance.

    Tail cohorts (warm/hot) get zero tolerance; others a small one. Cohorts with
    too few holdout days to judge are skipped (returned via ``inconclusive``).
    Mirrors the backtest harness's per-cohort no-regression gate inside the live
    activation path, so the de-bias can never ship a tail-regressing correction.
    """

    by_cohort = {}
    for record in records:
        by_cohort.setdefault(record["cohort"], []).append(record)
    regressions = []
    inconclusive = []
    for cohort, cohort_records in by_cohort.items():
        if len(cohort_records) < ROLLING_BIAS_COHORT_HOLDOUT_MIN_SAMPLES:
            inconclusive.append(cohort)
            continue
        raw = sum(abs(r["actual"] - r["raw_pred"]) for r in cohort_records) / len(cohort_records)
        correction = _correction_for(cohort, corrections, global_correction)
        corrected = sum(
            abs(r["actual"] - (r["raw_pred"] + correction)) for r in cohort_records
        ) / len(cohort_records)
        tol = 0.0 if cohort in ROLLING_BIAS_TAIL_COHORTS else ROLLING_BIAS_COHORT_REGRESSION_TOL_F
        if corrected > raw + tol:
            regressions.append(cohort)
    return regressions, inconclusive


DISABLED_BIAS_TABLE = {"global_correction": 0.0, "cohort_corrections": {}, "enabled": False}


def rolling_blend_residual_bias() -> tuple[dict, dict]:
    """Cohort-aware rolling de-bias for the final blend, gated on a holdout.

    Returns ``(bias_table, metadata)``. ``bias_table`` feeds ``blend_bias_for``;
    it stays disabled (zero correction) until there are enough clean CLISFO-scored
    days and the correction beats the raw blend on a walk-forward holdout.
    """

    cached = getattr(rolling_blend_residual_bias, "_cached", None)
    if cached is not None:
        return cached

    metadata = {
        "mode": "disabled",
        "reason": "",
        "scored_days": 0,
        "window_days": ROLLING_BIAS_WINDOW_DAYS,
        "cap_f": ROLLING_BIAS_CAP_F,
    }

    def _disabled(reason):
        metadata["reason"] = reason
        result = (dict(DISABLED_BIAS_TABLE), dict(metadata))
        rolling_blend_residual_bias._cached = result
        return result

    if not ENABLE_ROLLING_BLEND_BIAS:
        return _disabled("rolling blend de-bias disabled via ENABLE_ROLLING_BLEND_BIAS")

    records = _bias_residual_records(latest_scored_blend_rows())
    scored_days = len({record["target_date"] for record in records})
    metadata["scored_days"] = scored_days
    if scored_days < ROLLING_BIAS_MIN_SCORED_DAYS:
        return _disabled(
            f"collecting clean next-day scored blend days; need {ROLLING_BIAS_MIN_SCORED_DAYS}, "
            f"have {scored_days}"
        )

    records.sort(key=lambda record: record["target_date"])
    window = records[-ROLLING_BIAS_WINDOW_DAYS:]
    ordered_days = sorted({record["target_date"] for record in window})
    holdout_count = max(ROLLING_BIAS_HOLDOUT_MIN_DAYS, len(ordered_days) // 3)
    holdout_days = set(ordered_days[-holdout_count:])
    train = [record for record in window if record["target_date"] not in holdout_days]
    holdout = [record for record in window if record["target_date"] in holdout_days]

    train_corrections, train_global = _cohort_bias_corrections(train)
    raw_mae, corrected_mae = _bias_holdout_mae(holdout, train_corrections, train_global)
    holdout_report = {
        "holdout_days": holdout_count,
        "raw_mae_f": None if raw_mae is None else round(raw_mae, 3),
        "corrected_mae_f": None if corrected_mae is None else round(corrected_mae, 3),
    }
    metadata["holdout"] = holdout_report
    if raw_mae is None or corrected_mae is None or corrected_mae >= raw_mae:
        metadata["mode"] = "base"
        return _disabled(
            "rolling de-bias did not beat the raw blend on a walk-forward holdout; "
            "applying zero correction"
        )

    # Overall holdout improved -- but never ship a correction that regresses a
    # cohort's holdout MAE (zero tolerance on the warm/hot tail).
    cohort_regressions, cohort_inconclusive = _bias_holdout_cohort_regressions(
        holdout, train_corrections, train_global
    )
    holdout_report["cohort_regressions"] = cohort_regressions
    holdout_report["cohort_inconclusive"] = cohort_inconclusive
    if cohort_regressions:
        metadata["mode"] = "base"
        return _disabled(
            "rolling de-bias regressed cohort(s) on the holdout: "
            + ", ".join(cohort_regressions)
        )

    # Survived the holdout; refit on the full window for the live corrections.
    corrections, global_correction = _cohort_bias_corrections(window)
    cohort_counts = {}
    for record in window:
        cohort_counts[record["cohort"]] = cohort_counts.get(record["cohort"], 0) + 1
    table = {
        "global_correction": global_correction,
        "cohort_corrections": corrections,
        "enabled": True,
    }
    metadata.update(
        {
            "mode": "adaptive",
            "reason": "rolling residual de-bias beat the raw blend on a walk-forward holdout",
            "window_days_used": len(window),
            "cohort_counts": cohort_counts,
            "cohort_corrections_f": {key: round(value, 3) for key, value in corrections.items()},
            "global_correction_f": round(global_correction, 3),
        }
    )
    result = (table, dict(metadata))
    rolling_blend_residual_bias._cached = result
    return result


def blend_bias_for(raw_predicted: object, bias_table: dict) -> float:
    """Capped rolling de-bias to add to a raw blended prediction (0 if disabled)."""

    if not bias_table or not bias_table.get("enabled") or not finite(raw_predicted):
        return 0.0
    cohort = predicted_temperature_cohort(float(raw_predicted))
    corrections = bias_table.get("cohort_corrections") or {}
    value = corrections.get(cohort)
    if value is None:
        value = bias_table.get("global_correction", 0.0)
    return cap_magnitude(float(value), ROLLING_BIAS_CAP_F)
