#!/usr/bin/env python3
"""Bounded offline V7 challengers; reconstructed skill never authorizes trading.

Default uses only the standard library. ``--with-boosting`` additionally uses
the workstation's existing sklearn installation, never installs on AWS. Fits
freeze before each forward block. No production database or policy is written.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import statistics
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "forecaster"))
from cities import CITY_BY_STATION
from emos_recalibration import compute_correction, window_rows
from scores import SIGMA_FLOOR_F, gaussian_crps
from settlement_calendar import utc_window_for_local_standard_date

CONFIG = {
    "training_window_days": 90,
    "minimum_training_dates_per_station_lead": 45,
    "forward_block_days": 28,
    "minimum_baseline_history_dates": 45,
    "baseline_window_days": 45,
    "location_grid_f": [-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5],
    "scale_grid": [0.75, 0.875, 1.0, 1.125, 1.25, 1.5],
    "shrinkage_pseudodates": 30,
    "quantiles": [0.05, 0.5, 0.95],
    "boosting_trees_per_quantile": 24,
    "boosting_tree_depth": 2,
    "boosting_minimum_leaf_rows": 30,
    "boosting_learning_rate": 0.05,
    "seed": 20261005,
    "bootstrap_replicates": 1000,
    "bootstrap_calendar_block_days": 7,
    "maximum_rows_per_table": 250000,
    "maximum_export_bytes": 100000000,
    "maximum_runtime_seconds": 300,
}
Z90 = 1.6448536269514722


def clock(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("evidence clocks require a timezone")
    return result.astimezone(timezone.utc)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def load_cases(export, check_budget=lambda: None):
    """Source/station/lead-separated diagnostics with explicit availability clocks.

    The legacy reconstructed table cannot establish original issued vintages.
    Even an early storage clock cannot turn its rows into prospective evidence.
    Truth comes exclusively from final CLI, never cached actual_high_f.
    """
    captured = clock(export["captured_at"])
    for name in ("nwp_model_forecasts", "forecast_emos_daily_high", "cli_settlements"):
        table = export[name]
        if table.get("truncated") is not False or len(table["rows"]) > CONFIG["maximum_rows_per_table"]:
            raise ValueError("a complete bounded export is required")
    truth = {}
    for row in export["cli_settlements"]["rows"]:
        check_budget()
        if row.get("is_final") != 1 or not finite(row.get("max_temperature_f")):
            continue
        fetched = clock(row["fetched_at"])
        if fetched > captured:
            raise ValueError("truth recording follows capture")
        key = row["station_id"], row["local_date"]
        if key in truth:
            raise ValueError("duplicate final station-date truth")
        truth[key] = row["max_temperature_f"], fetched
    groups = defaultdict(list)
    excluded = Counter()
    seen = set()
    for row in export["forecast_emos_daily_high"]["rows"]:
        check_budget()
        if row.get("source") != "rolling_origin_v2":
            excluded["other_source"] += 1
            continue
        station, target, lead = row["station_id"], date.fromisoformat(row["target_date"]), row["lead_days"]
        if station not in CITY_BY_STATION or type(lead) is not int or lead not in (1, 2):
            excluded["unknown_station_or_lead"] += 1
            continue
        if row.get("method") != "emos_wmean":
            excluded["other_method"] += 1
            continue
        key = station, target, lead
        if key in seen:
            raise ValueError("duplicate canonical station-target-lead")
        seen.add(key)
        fetched = clock(row["fetched_at"])
        if fetched > captured:
            raise ValueError("forecast recording follows capture")
        start, end = utc_window_for_local_standard_date(target, CITY_BY_STATION[station].fixed_standard_timezone())
        if end > captured:
            excluded["unfinished_climate_day"] += 1
            continue
        actual = truth.get((station, target.isoformat()))
        if actual is None:
            excluded["no_final_station_truth"] += 1
            continue
        mu, sigma = row.get("predicted_high_f"), row.get("sigma_f")
        if not finite(mu) or not finite(sigma) or sigma <= 0:
            excluded["invalid_distribution"] += 1
            continue
        serve = start - timedelta(days=lead)
        groups[(station, lead)].append({
            "station": station, "lead": lead, "target_date": target.isoformat(),
            "serve_date": (target - timedelta(days=lead)).isoformat(),
            "serve_clock": serve.isoformat(), "forecast_recorded_at": fetched.isoformat(),
            "truth_recorded_at": actual[1].isoformat(), "mu": mu,
            "sigma": max(sigma, SIGMA_FLOOR_F), "truth": actual[0],
            "model_spread_f": row.get("model_spread_f"), "n_models": row.get("n_models"),
            "stored_by_nominal_serve": fetched <= serve,
        })
    corrected = []
    for group, rows in sorted(groups.items()):
        check_budget()
        rows.sort(key=lambda r: r["target_date"])
        series = [(date.fromisoformat(r["target_date"]), r["mu"], r["sigma"], r["truth"]) for r in rows]
        for row in rows:
            check_budget()
            serve = date.fromisoformat(row["serve_date"])
            window = window_rows(series, serve, window_days=CONFIG["baseline_window_days"])
            # Compare against the already implemented bias-only policy, with
            # exactly the same finite scoring floor for every Gaussian arm.
            correction = compute_correction(window, apply_bias=True, apply_sigma=False)
            row["baseline_mu"], row["baseline_sigma"] = correction.apply(row["mu"], row["sigma"])
            row["baseline_training_dates"] = len(window)
            if len(window) < CONFIG["minimum_baseline_history_dates"]:
                excluded["incomplete_baseline_training_calendar"] += 1
                continue
            corrected.append(row)
    return corrected, dict(excluded)


def training_for_fold(cases, serve_date, lead):
    start = (serve_date - timedelta(days=CONFIG["training_window_days"])).isoformat()
    end = serve_date.isoformat()
    rows = [r for r in cases if r["lead"] == lead and start <= r["target_date"] < end]
    # Both date and source lineage are frozen before the first serve of the
    # block. Training targets equal to serve_date are never included.
    return rows


def fit_location_scale(rows):
    """Minimum-CRPS finite location/scale search, shrunk toward the current arm.

    These are workstation resource/regularization choices, not coefficients
    claimed optimal by a paper. No held-out outcomes enter the search.
    """
    if not rows:
        return {"location_f": 0.0, "sigma_factor": 1.0}
    def loss(location, scale):
        return statistics.fmean(gaussian_crps(r["baseline_mu"] + location,
            max(SIGMA_FLOOR_F, r["baseline_sigma"] * scale), r["truth"]) for r in rows)
    choices = [(location, scale) for location in CONFIG["location_grid_f"] for scale in CONFIG["scale_grid"]]
    location, scale = min(choices, key=lambda pair: (loss(*pair), abs(pair[0]), abs(math.log(pair[1]))))
    weight = len({r["target_date"] for r in rows}) / (len({r["target_date"] for r in rows}) + CONFIG["shrinkage_pseudodates"])
    return {"location_f": location * weight, "sigma_factor": math.exp(math.log(scale) * weight),
            "raw_location_f": location, "raw_sigma_factor": scale, "shrinkage_weight": weight}


def feature_vector(row, stations):
    target = date.fromisoformat(row["target_date"])
    angle = 2 * math.pi * (target.timetuple().tm_yday - 1) / 365.25
    # Missing optional features use a deterministic prior and an explicit
    # missingness bit; no future outcome or held-out station statistic is used.
    spread = row.get("model_spread_f")
    members = row.get("n_models")
    return [row["baseline_mu"], row["baseline_sigma"],
            spread if finite(spread) and spread >= 0 else 0.0,
            int(not finite(spread) or spread < 0),
            members if finite(members) and members > 0 else 0.0,
            int(not finite(members) or members <= 0), math.sin(angle), math.cos(angle),
            *[int(row["station"] == station) for station in stations]]


def fit_quantile_boosting(rows):
    # Prevent BLAS/OpenMP from silently consuming all laptop cores. The outer
    # worker also enforces duty-cycle, memory, thermal and AC-power bounds.
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[variable] = "1"
    from sklearn.ensemble import GradientBoostingRegressor
    from threadpoolctl import threadpool_limits
    stations = sorted({r["station"] for r in rows})
    x = [feature_vector(r, stations) for r in rows]
    residual = [r["truth"] - r["baseline_mu"] for r in rows]
    models = []
    with threadpool_limits(limits=1):
        for quantile in CONFIG["quantiles"]:
            model = GradientBoostingRegressor(loss="quantile", alpha=quantile,
                n_estimators=CONFIG["boosting_trees_per_quantile"],
                max_depth=CONFIG["boosting_tree_depth"],
                min_samples_leaf=CONFIG["boosting_minimum_leaf_rows"],
                learning_rate=CONFIG["boosting_learning_rate"],
                random_state=CONFIG["seed"])
            model.fit(x, residual)
            models.append(model)
    return models, stations


def pinball(predicted, actual, quantile):
    error = actual - predicted
    return max(quantile * error, (quantile - 1) * error)


def quantile_scores(values, truth):
    lower, median, upper = values
    return {
        "mae_f": abs(median - truth),
        "mean_pinball_f": statistics.fmean(pinball(q, truth, alpha) for q, alpha in zip(values, CONFIG["quantiles"])),
        "central_90pct_coverage": float(lower <= truth <= upper),
        "interval_width_f": upper - lower,
        "interval_score_90_f": upper - lower + 20 * max(lower - truth, 0) + 20 * max(truth - upper, 0),
    }


def gaussian_scores(mu, sigma, truth, *, exact_sigma=False):
    crps = gaussian_crps(mu, sigma, truth)
    if exact_sigma:
        if not finite(sigma) or sigma <= 0:
            raise ValueError("exact issued scoring requires a finite positive sigma")
        delta = truth - mu
        z = delta / sigma
        # Avoid inf * 0 for legitimate tiny positive stored sigmas. The tail
        # expression has negligible omitted normal mass once |z| exceeds eight.
        if not math.isfinite(z) or abs(z) > 8:
            crps = abs(delta) - sigma / math.sqrt(math.pi)
        else:
            cdf = .5 * math.erfc(-z / math.sqrt(2))
            pdf = math.exp(-.5 * z * z) / math.sqrt(2 * math.pi)
            crps = sigma * (z * (2 * cdf - 1) + 2 * pdf - 1 / math.sqrt(math.pi))
    return {**quantile_scores([mu - Z90 * sigma, mu, mu + Z90 * sigma], truth), "crps_f": crps}


def calendar_block_interval(daily, metric, arm, check_budget=lambda: None):
    """Paired date-equal average differences; blocks preserve all city/lead rows.

    A block never bridges a missing calendar day. Intervals remain exploratory;
    overlapping fits, inspected archives and multiple arms forbid confirmation.
    """
    ordered = sorted(daily)
    if len(ordered) < 14:
        return None
    differences = {day: daily[day][arm][metric] - daily[day]["existing_bias_only"][metric] for day in ordered}
    size = CONFIG["bootstrap_calendar_block_days"]
    blocks = []
    supported_dates = set()
    for start in ordered:
        block = [(date.fromisoformat(start) + timedelta(days=i)).isoformat() for i in range(size)]
        if all(day in differences for day in block):
            blocks.append([differences[day] for day in block])
            supported_dates.update(block)
    # Never give isolated dates a point-estimate weight while silently dropping
    # them from resampling. That can reverse the apparent sign of improvement.
    if not blocks or supported_dates != set(ordered):
        return None
    rng = random.Random(CONFIG["seed"])
    samples = []
    for _ in range(CONFIG["bootstrap_replicates"]):
        check_budget()
        selected = []
        while len(selected) < len(ordered):
            selected.extend(rng.choice(blocks))
        samples.append(statistics.fmean(selected[:len(ordered)]))
    samples.sort()
    return {"lower_95": samples[int(.025 * len(samples))], "upper_95": samples[int(.975 * len(samples))],
            "calendar_dates": len(ordered), "complete_calendar_blocks": len(blocks),
            "method": "paired seven-calendar-day moving-block bootstrap; date-equal weighting; exploratory"}


def summarize_predictions(predictions, check_budget=lambda: None):
    if not predictions:
        return {"cases": 0, "distinct_calendar_targets": 0, "arms": {}}
    arms = list(predictions[0]["scores"])
    by_date = defaultdict(list)
    by_group = defaultdict(list)
    for row in predictions:
        check_budget()
        by_date[row["target_date"]].append(row)
        by_group[(row["station"], row["lead"])].append(row)
    def mean_scores(rows):
        return {arm: {metric: statistics.fmean(r["scores"][arm][metric] for r in rows)
                      for metric in rows[0]["scores"][arm]} for arm in arms}
    daily = {day: mean_scores(rows) for day, rows in by_date.items()}
    result = {
        "cases": len(predictions), "distinct_calendar_targets": len(by_date),
        "target_start": min(by_date), "target_end": max(by_date),
        "arms": mean_scores(predictions),
        "date_equal_weighted_arms": {arm: {metric: statistics.fmean(daily[d][arm][metric] for d in daily)
                for metric in predictions[0]["scores"][arm]} for arm in arms},
        "station_lead_results": [{"station": station, "lead": lead, "cases": len(rows),
            "arms": mean_scores(rows)} for (station, lead), rows in sorted(by_group.items())],
        "paired_daily_scores": [{"target_date": day, "cases": len(by_date[day]), "arms": daily[day]} for day in sorted(daily)],
    }
    result["candidate_differences_from_existing_bias_only"] = {
        arm: {metric: {"case_weighted_difference": result["arms"][arm][metric] - result["arms"]["existing_bias_only"][metric],
                       "date_equal_difference": result["date_equal_weighted_arms"][arm][metric] - result["date_equal_weighted_arms"]["existing_bias_only"][metric],
                       "exploratory_interval": calendar_block_interval(daily, metric, arm, check_budget)}
              for metric in ("mae_f", "mean_pinball_f", "interval_score_90_f", "crps_f")
              if metric in predictions[0]["scores"][arm]}
        for arm in arms if arm not in ("raw_existing_emos", "existing_bias_only")}
    return result


VINTAGE_FIELDS = (
    "station_id", "target_date", "recorded_at", "retrieved_at", "provider_initialized_at", "source",
    "window_start_utc", "window_end_utc", "standard_utc_offset_hours", "lead_days", "fit_lead_days",
    "legacy_lead_days", "predicted_high_f", "sigma_f", "method", "training_days", "training_truth_end",
    "training_digest_sha256", "fit_params_json", "serve_policy_json")
MEMBER_FIELDS = ("model", "predicted_high_f", "used_in_fit", "complete_hour_count", "aggregation_basis")


def audit_issued_vintages(export, check_budget=lambda: None):
    """Score actual preserved outputs, without claiming new challengers were issued.

    Unknown initialization/completeness remain unknown. Verified retrieval of an
    actual served daily-max response supports as-issued weather scoring, while a
    training digest alone does not establish every training truth's availability
    or a preregistered prospective strategy identity. Promotion stays separate.
    """
    tables = ("forecast_emos_live_vintages", "nwp_live_forecast_members")
    available = all(name in export and export[name].get("available", True) for name in tables)
    if not available:
        return {"available": False, "cases": 0,
                "reason": "append-only issued distributions and member evidence were not available in this export",
                "new_challengers_evaluated_prospectively": False, "promotion_eligible": False}
    for name in tables:
        if export[name].get("truncated") is not False or len(export[name]["rows"]) > CONFIG["maximum_rows_per_table"]:
            raise ValueError("complete bounded issued-vintage tables required")
    captured = clock(export["captured_at"])
    shadow_metadata = {}
    shadow_table = export.get("shadow_forecast_lineage")
    if shadow_table and shadow_table.get("available", True):
        if shadow_table.get("truncated") is not False or len(shadow_table["rows"]) > CONFIG["maximum_rows_per_table"]:
            raise ValueError("complete bounded shadow lineage required")
        for row in shadow_table["rows"]:
            check_budget()
            fields = {name: row[name] for name in ("snapshot_id", "research_identity", "execution_location", "model_policy_fingerprint_sha256")}
            fingerprint = fields["model_policy_fingerprint_sha256"]
            if (row["lineage_sha256"] != digest(fields) or len(fingerprint) != 64
                    or any(char not in "0123456789abcdef" for char in fingerprint)
                    or fields["execution_location"] != "local_mac" or fields["research_identity"] != "local-shadow-v7-v1"):
                raise ValueError("invalid shadow model/policy provenance")
            if fields["snapshot_id"] in shadow_metadata:
                raise ValueError("duplicate shadow snapshot provenance")
            shadow_metadata[fields["snapshot_id"]] = fields
    members = defaultdict(list)
    member_keys = set()
    for member in export["nwp_live_forecast_members"]["rows"]:
        check_budget()
        key = member["snapshot_id"], member["model"]
        if key in member_keys:
            raise ValueError("duplicate issued snapshot/model evidence")
        member_keys.add(key)
        members[member["snapshot_id"]].append({name: member[name] for name in MEMBER_FIELDS})
    truth = {(r["station_id"], r["local_date"]): r for r in export["cli_settlements"]["rows"]
             if r.get("is_final") == 1 and finite(r.get("max_temperature_f"))}
    selected = {}
    excluded = Counter()
    snapshot_ids = set()
    for row in export["forecast_emos_live_vintages"]["rows"]:
        check_budget()
        snapshot = row["snapshot_id"]
        if snapshot in snapshot_ids:
            raise ValueError("duplicate issued snapshot identity")
        snapshot_ids.add(snapshot)
        named_members = sorted(members[snapshot], key=lambda r: r["model"])
        canonical = {name: row[name] for name in VINTAGE_FIELDS}
        if digest({**canonical, "members": named_members}) != snapshot:
            raise ValueError("issued snapshot/member content hash mismatch")
        station = row["station_id"]
        if station not in CITY_BY_STATION:
            excluded["unknown_station"] += 1
            continue
        city = CITY_BY_STATION[station]
        target = date.fromisoformat(row["target_date"])
        start, end = utc_window_for_local_standard_date(target, city.fixed_standard_timezone())
        recorded = clock(row["recorded_at"])
        if recorded > captured:
            raise ValueError("issued recording follows capture")
        if (clock(row["window_start_utc"]) != start or clock(row["window_end_utc"]) != end
                or row["standard_utc_offset_hours"] != city.standard_utc_offset_hours):
            raise ValueError("issued snapshot station settlement-window mismatch")
        observed_day = recorded.astimezone(city.fixed_standard_timezone()).date()
        actual_lead = (target - observed_day).days
        if type(row["lead_days"]) is not int or row["lead_days"] != actual_lead:
            raise ValueError("issued snapshot lead disagrees with station issuance clock")
        if actual_lead not in (0, 1, 2) or recorded >= end or end > captured:
            excluded["unsupported_lead_or_unfinished_target"] += 1
            continue
        mu, sigma = row["predicted_high_f"], row["sigma_f"]
        if not finite(mu) or not finite(sigma) or sigma <= 0:
            raise ValueError("invalid issued distribution")
        retrieved = row["retrieved_at"]
        if row["source"] != "openmeteo_current_forecast" or not retrieved:
            excluded["unverified_issued_input_retrieval"] += 1
            continue
        retrieved = clock(retrieved)
        if retrieved > recorded:
            raise ValueError("issued retrieval follows recording")
        initialized = row["provider_initialized_at"]
        if initialized is not None and clock(initialized) > retrieved:
            raise ValueError("provider initialization follows retrieval")
        if (type(row["training_days"]) is not int or row["training_days"] <= 0
            or date.fromisoformat(row["training_truth_end"]) >= observed_day):
            excluded["training_target_not_strictly_prior_to_issue_day"] += 1
            continue
        training_digest = row["training_digest_sha256"]
        if len(training_digest) != 64 or any(char not in "0123456789abcdef" for char in training_digest):
            raise ValueError("invalid issued training digest")
        if not isinstance(json.loads(row["fit_params_json"]), dict) or not isinstance(json.loads(row["serve_policy_json"]), dict):
            raise ValueError("issued fit/policy metadata must be mappings")
        used = [member for member in named_members if member["used_in_fit"] == 1]
        if (len(used) < 3 or any(not finite(member["predicted_high_f"]) or member["used_in_fit"] not in (0, 1)
                or member["aggregation_basis"] != "provider_daily_max" for member in named_members)):
            excluded["unverified_or_insufficient_named_member_inputs"] += 1
            continue
        if any(member["complete_hour_count"] is not None and
            (type(member["complete_hour_count"]) is not int or member["complete_hour_count"] <= 0) for member in named_members):
            raise ValueError("invalid issued member hour-count evidence")
        actual = truth.get((station, target.isoformat()))
        if actual is None:
            excluded["no_final_station_truth"] += 1
            continue
        truth_retrieved = clock(actual["fetched_at"])
        if truth_retrieved > captured or truth_retrieved < end:
            raise ValueError("final issued-target truth clock violates completed-day/capture boundary")
        case = {"station": station, "lead": actual_lead, "target_date": target.isoformat(),
            "snapshot_id": snapshot, "recorded_at": recorded.isoformat(), "retrieved_at": retrieved.isoformat(),
            "training_digest_sha256": training_digest, "training_truth_end": row["training_truth_end"],
            "provider_initialized_at": initialized, "used_member_hour_counts": {m["model"]: m["complete_hour_count"] for m in used},
            "mu": mu, "sigma": sigma, "truth": actual["max_temperature_f"],
            "scores": gaussian_scores(mu, sigma, actual["max_temperature_f"], exact_sigma=True)}
        provenance = shadow_metadata.get(snapshot)
        case["cohort"] = "local_shadow" if provenance else "producer_identity_unestablished"
        case["research_identity"] = provenance["research_identity"] if provenance else None
        case["model_policy_fingerprint_sha256"] = provenance["model_policy_fingerprint_sha256"] if provenance else None
        key = (case["cohort"], case["research_identity"] or "", case["model_policy_fingerprint_sha256"] or "",
               station, target.isoformat(), actual_lead)
        # Select by an observed clock and content identity, never realized
        # outcome or minimum error; intraday repeats do not inflate the sample.
        if key not in selected or (case["recorded_at"], snapshot) < (selected[key]["recorded_at"], selected[key]["snapshot_id"]):
            selected[key] = case
    if set(members) - snapshot_ids:
        raise ValueError("orphan issued member evidence")
    if set(shadow_metadata) - snapshot_ids:
        raise ValueError("orphan shadow model/policy provenance")
    rows = [selected[key] for key in sorted(selected)]
    groups = defaultdict(list)
    cohorts = defaultdict(list)
    for row in rows:
        groups[(row["station"], row["lead"])].append(row)
        cohorts[(row["cohort"], row["research_identity"] or "", row["model_policy_fingerprint_sha256"] or "")].append(row)
    def summary(items):
        return {"cases": len(items), "distinct_calendar_targets": len({r["target_date"] for r in items}),
            "scores": {name: statistics.fmean(r["scores"][name] for r in items) for name in items[0]["scores"]}} if items else {"cases": 0}
    return {"available": True, **summary(rows), "excluded": dict(excluded),
        "scoring_convention": "exact preserved issued Gaussian sigma; no serving/scoring-floor restatement",
        "exported_vintages": len(snapshot_ids), "deduplication": "earliest valid issuance per producer/model identity, station, target and actual lead; never latest-write or outcome selection",
        "known_provider_initialization_cases": sum(r["provider_initialized_at"] is not None for r in rows),
        "known_all_used_member_hour_counts_cases": sum(all(v is not None for v in r["used_member_hour_counts"].values()) for r in rows),
        "cohort_case_counts": dict(Counter(r["cohort"] for r in rows)),
        "by_producer_identity": [{"cohort": cohort, "research_identity": identity or None,
            "model_policy_fingerprint_sha256": fingerprint or None, **summary(items)}
            for (cohort, identity, fingerprint), items in sorted(cohorts.items())],
        "by_station_lead": [{"station": station, "lead": lead, **summary(items)} for (station, lead), items in sorted(groups.items())],
        "issued_predictions_and_lineage": rows, "new_challengers_evaluated_prospectively": False,
        "training_truth_availability_receipts_established": False, "promotion_eligible": False,
        "limitations": ["Scores concern actually preserved served distributions, not the retrospective challengers fitted by this run.",
            "NULL provider initialization and member hour counts remain unknown; verified retrieval does not infer them.",
            "Training target/date/digest checks do not prove every training truth's issue-time availability or a preregistered strategy identity.",
            "Day-ahead and same-day forecasts share outcomes; distinct calendar days, not intraday rows, define statistical evidence.",
            "Weather scores alone do not establish executable market edge or permit real-money activation."]}


CAPITAL_LIMITS = {
    "minimum_independent_completed_days": 30,
    "daily_loss_fraction": 0.01,
    "peak_open_cost_fraction": 0.05,
    "maximum_drawdown_fraction": 0.10,
    "maximum_empirical_breach_probability": 0.05,
    "calendar_block_days": 7,
    "bootstrap_replicates": 1000,
    "capital_multipliers": [1.0, 1.25, 1.5, 2.0, 3.0, 5.0, 10.0],
}


def capital_frontier(daily_fills, *, identity_verified=False, as_of=None, calendar_timezone=None,
                     check_budget=lambda: None):
    """Research-only capital efficiency for fixed verified filled dollar exposure.

    This never scales fills/liquidity with bankroll, enables real money or claims
    globally optimal capital. Limits are declared engineering assumptions and
    empirical resampling uncertainty is not a guaranteed future risk bound.
    """
    blocked = {"live_allocation_usd": 0, "initial_bankroll_recommendation_usd": None,
        "capital_study_candidate_usd": None, "limits": CAPITAL_LIMITS, "frontier": []}
    if identity_verified is not True or len(daily_fills) < CAPITAL_LIMITS["minimum_independent_completed_days"]:
        return {**blocked, "status": "insufficient_independent_verified_prospective_fill_days"}
    if as_of is None or calendar_timezone is None:
        return {**blocked, "status": "missing_explicit_capture_clock_or_calendar_timezone"}
    captured = clock(as_of)
    local_zone = ZoneInfo(calendar_timezone)
    rows = sorted(daily_fills, key=lambda r: r["date"])
    dates = [date.fromisoformat(r["date"]) for r in rows]
    if len(set(dates)) != len(dates):
        raise ValueError("duplicate capital calendar day")
    if dates != [dates[0] + timedelta(days=i) for i in range(len(dates))]:
        return {**blocked, "status": "missing_calendar_days_include_zero_and_outage_days"}
    if any(datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=local_zone).astimezone(timezone.utc) > captured for day in dates):
        return {**blocked, "status": "unfinished_or_future_calendar_day"}
    account_keys = [r.get("economic_account_key") for r in rows]
    fingerprints = [r.get("strategy_policy_fingerprint_sha256") for r in rows]
    if (any(type(key) is not str or not key.strip() for key in account_keys)
            or any(type(value) is not str or len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in fingerprints)):
        return {**blocked, "status": "missing_or_invalid_economic_account_and_policy_lineage"}
    if len(set(account_keys)) != 1 or len(set(fingerprints)) != 1:
        return {**blocked, "status": "mixed_economic_accounts_or_strategy_policy_identities"}
    if any(r.get("prospective") is not True or r.get("net_fills_verified") is not True
           or r.get("fees_and_slippage_included") is not True or not finite(r.get("net_pnl_usd"))
           or not finite(r.get("peak_open_cost_usd")) or r["peak_open_cost_usd"] < 0
           or not finite(r.get("realized_loss_usd")) or r["realized_loss_usd"] < max(-r["net_pnl_usd"], 0) for r in rows):
        return {**blocked, "status": "unverified_net_fills_exposure_or_costs"}
    size = CAPITAL_LIMITS["calendar_block_days"]
    blocks = [rows[start:start + size] for start in range(len(rows) - size + 1)]
    rng = random.Random(CONFIG["seed"])
    trajectories = []
    for _ in range(CAPITAL_LIMITS["bootstrap_replicates"]):
        check_budget()
        trajectory = []
        while len(trajectory) < len(rows):
            trajectory.extend(rng.choice(blocks))
        trajectories.append(trajectory[:len(rows)])
    means = sorted(statistics.fmean(r["net_pnl_usd"] for r in trajectory) for trajectory in trajectories)
    lower = means[int(.025 * len(means))]
    if lower <= 0:
        return {**blocked, "status": "net_filled_daily_pnl_lower_bound_not_positive",
                "exploratory_daily_pnl_lower_95_usd": lower}
    starting_bound = max(1.0,
        max(r["peak_open_cost_usd"] for r in rows) / CAPITAL_LIMITS["peak_open_cost_fraction"],
        max(r["realized_loss_usd"] for r in rows) / CAPITAL_LIMITS["daily_loss_fraction"])
    frontier = []
    for multiplier in CAPITAL_LIMITS["capital_multipliers"]:
        check_budget()
        capital = math.ceil(starting_bound * multiplier)
        breaches = 0
        drawdowns = []
        for trajectory in trajectories:
            check_budget()
            wealth, peak, drawdown = capital, capital, 0.0
            budget_breached = False
            for row in trajectory:
                if (wealth <= 0 or row["peak_open_cost_usd"] > CAPITAL_LIMITS["peak_open_cost_fraction"] * wealth
                        or row["realized_loss_usd"] > CAPITAL_LIMITS["daily_loss_fraction"] * wealth):
                    budget_breached = True
                # Do not hide losing slices behind net profitable closes. This
                # conservative day envelope places realized losses before gains
                # and treats observed peak open cost as fully at risk.
                worst_intraday_wealth = wealth - row["realized_loss_usd"] - row["peak_open_cost_usd"]
                drawdown = max(drawdown, (peak - worst_intraday_wealth) / peak)
                wealth += row["net_pnl_usd"]
                peak = max(peak, wealth)
                drawdown = max(drawdown, (peak - wealth) / peak)
            drawdowns.append(drawdown)
            breaches += budget_breached or drawdown > CAPITAL_LIMITS["maximum_drawdown_fraction"]
        probability = breaches / len(trajectories)
        frontier.append({"capital_usd": capital, "empirical_budget_or_drawdown_breach_probability": probability,
            "simulated_drawdown_95_fraction": sorted(drawdowns)[int(.95 * len(drawdowns))],
            "feasible_under_declared_assumptions": probability <= CAPITAL_LIMITS["maximum_empirical_breach_probability"]})
    feasible = [row["capital_usd"] for row in frontier if row["feasible_under_declared_assumptions"]]
    return {**blocked, "status": "experimental_fixed_fill_capital_frontier" if feasible else "no_feasible_capital_in_bounded_grid",
        "capital_study_candidate_usd": min(feasible) if feasible else None,
        "feasible_capital_range_usd": [min(feasible), max(feasible)] if feasible else None,
        "exploratory_daily_pnl_lower_95_usd": lower, "frontier": frontier,
        "objective": "smallest simulated feasible capital for the same observed nominal fills, without liquidity scaling",
        "limitations": ["Exploratory historical block resampling, not a guarantee or optimal live bankroll.",
            "Stress-regime sensitivity and operational readiness still require independent validation.",
            "Explicit user real-money authorization is always required; this function allocates nothing."]}


def capital_gate():
    # Weather skill, paper win-rate and assumed dollar targets cannot determine
    # an optimal dollar bankroll. No missing proof is replaced with a number.
    return {
        "live_allocation_usd": 0,
        "initial_bankroll_recommendation_usd": None,
        "status": "blocked_user_authorization_and_independent_net_fill_evidence",
        "required_evidence": ["explicit user authorization for real money",
            "prospective immutable model/strategy identities and available-vintage forecasts",
            "independent date-block confidence bound for net realized filled P&L after fees, depth, latency and slippage",
            "joint station/day tail-risk and drawdown simulation under explicit proposed risk limits and stress assumptions",
            "operational readiness, fresh analysis, resolved station truth and rollback receipt"],
        "no_calendar_deadline_can_override_evidence": True,
        "fixed_fill_capital_frontier": capital_frontier([], identity_verified=False),
    }


def evaluate(export, *, source_hash, with_boosting=False, deadline=None):
    if deadline is None:
        deadline = time.monotonic() + CONFIG["maximum_runtime_seconds"]
    def check_budget():
        if time.monotonic() > deadline:
            raise TimeoutError("bounded ML runtime exceeded; retain previous successful evidence")
    check_budget()
    cases, excluded = load_cases(export, check_budget)
    issued = audit_issued_vintages(export, check_budget)
    predictions, folds = [], []
    dates = sorted({r["target_date"] for r in cases})
    if dates:
        anchor = date.fromisoformat(dates[0])
        blocks = defaultdict(list)
        for row in cases:
            index = (date.fromisoformat(row["target_date"]) - anchor).days // CONFIG["forward_block_days"]
            blocks[(index, row["lead"])].append(row)
        for (index, lead), holdout in sorted(blocks.items()):
            check_budget()
            block_target_start = anchor + timedelta(days=index * CONFIG["forward_block_days"])
            serve_date = block_target_start - timedelta(days=lead)
            training = training_for_fold(cases, serve_date, lead)
            by_station = defaultdict(list)
            for row in training:
                by_station[row["station"]].append(row)
            eligible_stations = {station for station, rows in by_station.items()
                if len({r["target_date"] for r in rows}) >= CONFIG["minimum_training_dates_per_station_lead"]}
            training = [r for r in training if r["station"] in eligible_stations]
            selected = [r for r in holdout if r["station"] in eligible_stations]
            excluded["insufficient_prior_station_training"] = excluded.get("insufficient_prior_station_training", 0) + len(holdout) - len(selected)
            if not selected:
                continue
            parameters = {station: fit_location_scale(by_station[station]) for station in sorted(eligible_stations)}
            training_ids = [(r["station"], r["lead"], r["target_date"], r["forecast_recorded_at"], r["truth_recorded_at"],
                             r["baseline_mu"], r["baseline_sigma"], r["truth"]) for r in training]
            fold_id = digest({"cutoff": serve_date.isoformat(), "training": training_ids, "configuration": CONFIG})
            fold = {"fold_id": fold_id, "lead": lead,
                "fit_before_serve_date": serve_date.isoformat(), "first_target_date": block_target_start.isoformat(),
                "last_training_target": max(r["target_date"] for r in training),
                "training_rows": len(training), "station_training_dates": {station: len(by_station[station]) for station in sorted(eligible_stations)},
                "training_digest_sha256": digest(training_ids), "parameters": parameters,
                "training_truth_rows_available_by_fold_clock": sum(clock(r["truth_recorded_at"]) <=
                    utc_window_for_local_standard_date(serve_date, CITY_BY_STATION[r["station"]].fixed_standard_timezone())[0] for r in training),
                "strict_original_vintage_eligible": False}
            quantile_predictions = None
            if with_boosting:
                models, stations = fit_quantile_boosting(training)
                x = [feature_vector(r, stations) for r in selected]
                quantile_predictions = [model.predict(x) for model in models]
            check_budget()
            for position, row in enumerate(selected):
                parameters_for_station = parameters[row["station"]]
                candidate_mu = row["baseline_mu"] + parameters_for_station["location_f"]
                candidate_sigma = max(SIGMA_FLOOR_F, row["baseline_sigma"] * parameters_for_station["sigma_factor"])
                scores = {"raw_existing_emos": gaussian_scores(row["mu"], row["sigma"], row["truth"]),
                    "existing_bias_only": gaussian_scores(row["baseline_mu"], row["baseline_sigma"], row["truth"]),
                    "minimum_crps_location_scale": gaussian_scores(candidate_mu, candidate_sigma, row["truth"])}
                prediction = {"station": row["station"], "lead": lead, "target_date": row["target_date"],
                    "serve_date": row["serve_date"], "fold_id": fold_id, "truth": row["truth"],
                    "baseline_mu": row["baseline_mu"], "baseline_sigma": row["baseline_sigma"],
                    "candidate_mu": candidate_mu, "candidate_sigma": candidate_sigma,
                    "forecast_recorded_at": row["forecast_recorded_at"], "truth_recorded_at": row["truth_recorded_at"],
                    "strict_original_vintage_eligible": False, "scores": scores}
                if quantile_predictions is not None:
                    unsorted = [row["baseline_mu"] + float(values[position]) for values in quantile_predictions]
                    ordered = sorted(unsorted)  # outcome-independent rearrangement
                    prediction["boosting_quantiles_f"] = ordered
                    prediction["quantile_crossing_before_rearrangement"] = unsorted != ordered
                    scores["gradient_boosted_quantiles"] = quantile_scores(ordered, row["truth"])
                if not all(finite(value) for arm in scores.values() for value in arm.values()):
                    raise ValueError("nonfinite candidate scoring")
                predictions.append(prediction)
            folds.append(fold)
    result = {
        "schema_version": 1, "evidence_scope": "exploratory_reconstructed_walk_forward_weather_skill",
        "captured_at": export["captured_at"], "source_export_sha256": source_hash,
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "implementation_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
            ("forecaster/emos_recalibration.py", "forecaster/scores.py", "forecaster/cities.py", "forecaster/settlement_calendar.py")},
        "configuration": CONFIG, "with_boosting": with_boosting,
        "source": "rolling_origin_v2", "baseline": "existing inverse-variance emos_wmean plus existing bias-only correction",
        "excluded": excluded, "eligible_baseline_cases": len(cases), "folds": folds,
        "retrospective": summarize_predictions(predictions, check_budget),
        "issued_vintage_weather_scores": issued,
        "strict_prequential": {"cases": 0, "promotion_eligible": False,
            "necessary_storage_clock_passes": sum(r["stored_by_nominal_serve"] for r in cases),
            "reason": "legacy rolling_origin_v2 is reconstructed; original issued inputs, provider initialization, complete constituent hours and training-truth availability are unestablished"},
        "active": False, "promotion_eligible": False, "capital_gate": capital_gate(),
        "limitations": ["Calendar order prevents direct future-target training, but source reconstruction and delayed retrieval prevent original-vintage claims.",
            "The archive and older policy were already inspected; this is exploratory, not an untouched confirmatory set.",
            "Scoring unit is the station/lead/calendar target; date-block intervals preserve concurrent-city dependence and remain approximate.",
            "Hyperparameters are bounded resource/regularization assumptions, not proven optimal.",
            "Three boosted quantiles do not define a full CDF or executable prediction-market bucket probabilities; CRPS is intentionally absent for this arm.",
            "No observed prices, fills, fees, executable returns or optimal dollar capital can be inferred from weather scores.",
            "No model, risk control, paper account, live-order flag or AWS setting is promoted or modified."],
        "paired_predictions_and_lineage": predictions,
    }
    if with_boosting:
        import sklearn
        result["sklearn_version"] = sklearn.__version__
        result["quantile_crossings_rearranged"] = sum(r["quantile_crossing_before_rearrangement"] for r in predictions)
    return result


def write_outputs(result, output, lineage_output=None):
    result = dict(result)
    rows = result.pop("paired_predictions_and_lineage")
    if lineage_output:
        payload = "".join(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n" for row in rows).encode()
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as stream:
            stream.write(payload)
        lineage_output.parent.mkdir(parents=True, exist_ok=True)
        lineage_output.write_bytes(buffer.getvalue())
        result["lineage_sha256"] = hashlib.sha256(buffer.getvalue()).hexdigest()
        result["lineage_prediction_count"] = len(rows)
    else:
        result["paired_predictions_and_lineage"] = rows
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lineage-output", type=Path)
    parser.add_argument("--with-boosting", action="store_true")
    args = parser.parse_args()
    deadline = time.monotonic() + CONFIG["maximum_runtime_seconds"]
    if args.export.stat().st_size > CONFIG["maximum_export_bytes"]:
        raise ValueError("bounded export byte limit exceeded")
    raw = args.export.read_bytes()
    result = evaluate(json.loads(raw), source_hash=hashlib.sha256(raw).hexdigest(), with_boosting=args.with_boosting, deadline=deadline)
    write_outputs(result, args.output, args.lineage_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
