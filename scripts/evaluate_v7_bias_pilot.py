#!/usr/bin/env python3
"""Offline fixed-policy comparison; reconstructed input skill, never promotion."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import gzip
import io
import json
import math
from pathlib import Path
import sys


def clock(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("a timezone is required")
    return value.astimezone(timezone.utc)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lineage-output", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.repo / "forecaster"))
    sys.path.insert(0, str(args.repo / "scripts"))
    from audit_forecast_evidence import summarize as summarize_distributions
    from cities import CITY_BY_STATION
    from emos_forecast import SERVE_RECAL_BIAS, SERVE_RECAL_SIGMA
    from emos_recalibration import (BIAS_DEADBAND_T, SHRINKAGE_K,
                                    TRAILING_WINDOW_DAYS, compute_correction, window_rows)
    from scores import SIGMA_FLOOR_F
    from settlement_calendar import utc_window_for_local_standard_date

    if not SERVE_RECAL_BIAS or SERVE_RECAL_SIGMA:
        raise ValueError("this comparison requires the existing bias-only policy")
    raw = args.export.read_bytes()
    export = json.loads(raw)
    captured = clock(export["captured_at"])
    table_names = ("nwp_model_forecasts", "cli_settlements", "forecast_emos_daily_high")
    if any(export[name].get("truncated") is not False for name in table_names):
        raise ValueError("complete, non-truncated export required")
    truth = {}
    for row in export["cli_settlements"]["rows"]:
        if row.get("is_final") != 1 or not finite(row.get("max_temperature_f")):
            continue
        if clock(row["fetched_at"]) > captured:
            raise ValueError("final truth retrieval follows capture")
        key = (row["station_id"], row["local_date"])
        if key in truth:
            raise ValueError("duplicate final station-date truth")
        truth[key] = row["max_temperature_f"]
    groups = defaultdict(dict)
    source_counts = Counter()
    excluded = Counter()
    seen = set()
    for row in export["forecast_emos_daily_high"]["rows"]:
        source_counts[row["source"]] += 1
        if row["source"] != "rolling_origin_v2":
            excluded["other_source"] += 1
            continue
        station, lead, target = row["station_id"], row["lead_days"], row["target_date"]
        key = (station, lead, target)
        if key in seen:
            raise ValueError("duplicate canonical station-lead-target")
        seen.add(key)
        if station not in CITY_BY_STATION or type(lead) is not int or lead not in (1, 2):
            raise ValueError("comparison requires registered stations and lead 1/2")
        if row["method"] != "emos_wmean":
            raise ValueError("comparison requires one canonical method: emos_wmean")
        start, end = utc_window_for_local_standard_date(
            date.fromisoformat(target), CITY_BY_STATION[station].fixed_standard_timezone())
        if clock(row["fetched_at"]) > captured:
            raise ValueError("forecast recording follows capture")
        if end > captured:
            excluded["unfinished_climate_day"] += 1
            continue
        actual = truth.get((station, target))
        if actual is None:
            excluded["no_final_station_truth"] += 1
            continue
        mu, sigma = row.get("predicted_high_f"), row.get("sigma_f")
        if not finite(mu) or not finite(sigma) or sigma <= 0:
            raise ValueError("invalid canonical distribution")
        groups[(station, lead)][target] = (mu, sigma, actual)
    stations = sorted({station for station, lead in groups})
    if len(stations) != 15 or set(groups) != {(station, lead) for station in stations for lead in (1, 2)}:
        raise ValueError("15 stations and both leads required")
    common_dates = sorted(set.intersection(*(set(rows) for rows in groups.values())))
    if not common_dates:
        raise ValueError("no shared complete dates")
    # Primary pilot requires 45 valid exported calendar targets, not merely
    # 45 rows or a window whose endpoints lie within the exported range.
    full_history_dates = []
    full_history_excluded = Counter()
    for target in common_dates:
        target_day = date.fromisoformat(target)
        if any(target_day - timedelta(days=lead + TRAILING_WINDOW_DAYS)
               < min(date.fromisoformat(day) for day in forecasts)
               for (station, lead), forecasts in groups.items()):
            full_history_excluded["left_censored_training_window_dates"] += 1
            continue
        if any(not {(target_day - timedelta(days=lead + lag)).isoformat()
                    for lag in range(1, TRAILING_WINDOW_DAYS + 1)}.issubset(forecasts)
               for (station, lead), forecasts in groups.items()):
            full_history_excluded["incomplete_training_calendar_window_dates"] += 1
            continue
        full_history_dates.append(target)
    if not full_history_dates:
        raise ValueError("no shared dates have full 45-day exported training history")

    def summarize(rows, arm):
        return summarize_distributions([
            {"date": row["target_date"], "mu": row[arm + "_mu"],
             "sigma": row["sigma"], "truth": row["actual"]}
            for row in rows])

    def compare(rows):
        baseline = summarize(rows, "baseline")
        candidate = summarize(rows, "candidate")
        return {"baseline": baseline, "candidate": candidate,
                "candidate_minus_baseline": {
                    metric: candidate[metric] - baseline[metric]
                    for metric in ("mean_error_f", "mae_f", "crps_f", "central_90pct_coverage")},
                "relative_crps_change": candidate["crps_f"] / baseline["crps_f"] - 1,
                "absolute_bias_reduced": abs(candidate["mean_error_f"]) < abs(baseline["mean_error_f"])}

    all_cases = []
    by_group = []
    for (station, lead), forecasts in sorted(groups.items()):
        series = [(date.fromisoformat(day), mu, sigma, actual)
                  for day, (mu, sigma, actual) in sorted(forecasts.items())]
        cases = []
        for target in common_dates:
            mu, sigma, actual = forecasts[target]
            serve_date = date.fromisoformat(target) - timedelta(days=lead)
            training_dates = [day for day, _, _, _ in series
                              if serve_date - timedelta(days=TRAILING_WINDOW_DAYS) <= day < serve_date]
            window = window_rows(series, serve_date, window_days=TRAILING_WINDOW_DAYS)
            assert len(window) == len(training_dates)
            assert all(day < serve_date for day in training_dates)
            correction = compute_correction(window, k=SHRINKAGE_K,
                apply_bias=SERVE_RECAL_BIAS, apply_sigma=SERVE_RECAL_SIGMA,
                bias_deadband_t=BIAS_DEADBAND_T)
            candidate_mu, candidate_sigma = correction.apply(mu, sigma)
            if not finite(candidate_mu) or candidate_sigma != sigma:
                raise ValueError("correction must be finite and leave sigma unchanged")
            cases.append({"station": station, "lead": lead, "target_date": target,
                          "simulated_serve_date": serve_date.isoformat(),
                          "baseline_mu": mu, "candidate_mu": candidate_mu,
                          "sigma": sigma, "actual": actual, "bias_correction_f": correction.bias_f,
                          "training_cases": len(window),
                          "last_training_target": max(training_dates).isoformat() if training_dates else None})
        all_cases.extend(cases)
        by_group.append({"station": station, "lead": lead, **compare(cases),
                         "training_case_min": min(r["training_cases"] for r in cases),
                         "training_case_max": max(r["training_cases"] for r in cases),
                         "nonzero_bias_correction_cases": sum(r["bias_correction_f"] != 0 for r in cases)})
    assert len(all_cases) == len(common_dates) * 30
    by_date = []
    for target in common_dates:
        paired = [r for r in all_cases if r["target_date"] == target]
        assert len(paired) == 30
        by_date.append({"target_date": target, **compare(paired)})
    # The bounded export cannot recover corrections before its left edge.
    # Keep the all-date result and a deterministic suffix with every group's
    # entire requested calendar window lying inside the exported date range.
    non_censored_start = max(
        min(date.fromisoformat(day) for day in forecasts)
        + timedelta(days=TRAILING_WINDOW_DAYS + lead)
        for (station, lead), forecasts in groups.items())
    non_censored_cases = [r for r in all_cases if date.fromisoformat(r["target_date"]) >= non_censored_start]
    non_censored = {
        "selection": "each station/lead's 45-day training calendar window starts on or after its exported history start",
        "target_start": min(r["target_date"] for r in non_censored_cases),
        "target_end": max(r["target_date"] for r in non_censored_cases),
        **compare(non_censored_cases),
        "by_station_lead": [{"station": station, "lead": lead, **compare(
            [r for r in non_censored_cases if (r["station"], r["lead"]) == (station, lead)])}
            for station, lead in sorted(groups)],
    }
    pilot_cases = [row for row in all_cases if row["target_date"] in set(full_history_dates)]
    assert len(pilot_cases) == len(full_history_dates) * 30
    assert all(row["training_cases"] == TRAILING_WINDOW_DAYS for row in pilot_cases)
    pilot_groups = [{"station": station, "lead": lead, **compare(
        [row for row in pilot_cases if (row["station"], row["lead"]) == (station, lead)])}
        for station, lead in sorted(groups)]
    primary_pilot = {
        "eligibility": "all 30 station/lead groups have every one of the 45 pre-serve calendar targets exported, finite, canonical and matched to final truth",
        "common_calendar_targets": full_history_dates,
        "target_start": full_history_dates[0], "target_end": full_history_dates[-1],
        "paired_case_count": len(pilot_cases), "distinct_calendar_targets": len(full_history_dates),
        "excluded_common_dates": dict(full_history_excluded),
        "excluded_common_cases": {reason: count * 30 for reason, count in full_history_excluded.items()},
        "training_cases_per_prediction": TRAILING_WINDOW_DAYS,
        **compare(pilot_cases), "by_station_lead": pilot_groups,
        "ranking_by_observed_relative_crps_change": [
            {"station": row["station"], "lead": row["lead"], "relative_crps_change": row["relative_crps_change"]}
            for row in sorted(pilot_groups, key=lambda row: row["relative_crps_change"])],
        "group_counts": {
            "crps_improved": sum(row["relative_crps_change"] < 0 for row in pilot_groups),
            "crps_worse": sum(row["relative_crps_change"] > 0 for row in pilot_groups),
            "crps_worse_over_2pct": sum(row["relative_crps_change"] > .02 for row in pilot_groups)},
    }
    result = {
        "schema_version": 1,
        "evidence_scope": "exploratory_reconstructed_weather_skill_not_issued_forecasts_or_v7_profit",
        "captured_at": export["captured_at"],
        "source_export_sha256": hashlib.sha256(raw).hexdigest(),
        "source": "rolling_origin_v2", "method": "emos_wmean",
        "export_row_counts": {name: len(export[name]["rows"]) for name in table_names},
        "archive_source_counts": dict(source_counts), "excluded": dict(excluded),
        "available_source_matched_cases": sum(len(rows) for rows in groups.values()),
        "cases_outside_all_group_date_intersection": sum(len(rows) for rows in groups.values()) - len(all_cases),
        "stations": stations, "station_count": len(stations), "lead_days": [1, 2],
        "paired_case_count": len(all_cases), "distinct_calendar_targets": len(common_dates),
        "paired_target_start": common_dates[0], "paired_target_end": common_dates[-1],
        "common_calendar_targets": common_dates,
        "primary_pilot": primary_pilot,
        "primary_result_key": "primary_pilot",
        "supplemental_result_note": "top-level all-date and non-left-censored suffix summaries are retained as explicitly supplemental exploratory diagnostics",
        "configuration": {"window_days": TRAILING_WINDOW_DAYS, "shrinkage_k": SHRINKAGE_K,
            "bias_deadband_t": BIAS_DEADBAND_T, "apply_bias": SERVE_RECAL_BIAS,
            "apply_sigma": SERVE_RECAL_SIGMA, "sigma_scoring_floor_f": SIGMA_FLOOR_F,
            "truth_date_cutoff": "target_date < simulated_serve_date; simulated_serve_date = evaluation_target - lead",
            "training_selection": "all valid final-truth canonical station/lead cases within the calendar window",
            "evaluation_selection": "intersection of available target dates across all 30 station/lead groups"},
        "overall": compare(all_cases), "by_station_lead": by_group,
        "non_left_censored_training_window_subset": non_censored,
        "ranking_by_observed_relative_crps_change": [
            {"station": r["station"], "lead": r["lead"], "relative_crps_change": r["relative_crps_change"]}
            for r in sorted(by_group, key=lambda r: r["relative_crps_change"])],
        "group_counts": {
            "crps_improved": sum(r["relative_crps_change"] < 0 for r in by_group),
            "crps_worse": sum(r["relative_crps_change"] > 0 for r in by_group),
            "crps_worse_over_2pct": sum(r["relative_crps_change"] > .02 for r in by_group)},
        "paired_daily_metrics": by_date,
        "paired_predictions_and_lineage": all_cases,
        "implementation_sha256": {"runner": sha(Path(__file__)), **{
            path: sha(args.repo / path) for path in (
                "forecaster/emos_recalibration.py", "forecaster/emos_forecast.py", "forecaster/scores.py",
                "forecaster/settlement_calendar.py", "forecaster/cities.py", "scripts/audit_forecast_evidence.py")}},
        "active": False, "promotion_eligible": False,
        "limitations": [
            "A fixed pre-existing bias-only policy is explored without parameter searches or model retraining.",
            "The policy's historical selection may overlap this archive; this is not untouched held-out validation.",
            "Constituent forecast initialization, original hourly completeness, and decision-time availability are unestablished.",
            "Target-date truth exclusion reproduces the serving rule but original CLI publication timestamps and revisions are unestablished.",
            "All scores are paired on common dates; training retains each station/lead's own available final-truth calendar history.",
            "The all-date analysis has left-censored early training windows because no forecasts preceding the bounded export can be recovered; a fixed non-left-censored suffix is separately reported.",
            "The primary pilot is stricter: each prior calendar target must have a valid canonical forecast and final truth in every group; this reduces the dates scored and cannot establish performance on excluded dates.",
            "Integer final CLI highs are point-scored; sigma is unchanged and no outcome-cohort sigma is used.",
            "Station and lead cases share weather dates; 30 cases per date are not independent observations.",
            "Observed group ranks are exploratory, unadjusted for multiple comparisons, and do not authorize city selection or promotion.",
            "No quotes, fees, liquidity, fills, capacity, ROI, daily dollars, or future profits are inferred.",
        ],
    }
    lineage_path = args.lineage_output or args.output.with_suffix(".lineage.jsonl.gz")
    raw_lineage = "".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in result.pop("paired_predictions_and_lineage")).encode()
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as compressed:
        compressed.write(raw_lineage)
    lineage_path.write_bytes(buffer.getvalue())
    result["paired_lineage"] = {
        "file": lineage_path.name, "format": "gzip JSONL",
        "rows": len(all_cases), "uncompressed_sha256": hashlib.sha256(raw_lineage).hexdigest(),
        "compressed_sha256": sha(lineage_path),
        "scope": "all paired cases; primary eligibility remains the primary_pilot date set",
    }
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({"captured_at": result["captured_at"],
                      "primary_pilot": {key: value for key, value in primary_pilot.items()
                                        if key not in ("by_station_lead", "common_calendar_targets")},
                      "supplemental_all_date": {"paired_cases": len(all_cases),
                        "distinct_dates": len(common_dates), "overall": result["overall"],
                        "group_counts": result["group_counts"]}}, indent=2))


if __name__ == "__main__":
    main()
