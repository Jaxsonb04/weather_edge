#!/usr/bin/env python3
"""Audit a fresh read-only forecast export; reconstructed skill is not promotion."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "forecaster"))
from cities import CITY_BY_STATION
from scores import SIGMA_FLOOR_F, gaussian_crps
from settlement_calendar import utc_window_for_local_standard_date


def finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _clock(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("export clocks require a timezone")
    return result.astimezone(timezone.utc)


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"cases": 0}
    residuals = [row["mu"] - row["truth"] for row in rows]
    return {
        "cases": len(rows),
        "distinct_calendar_targets": len({row["date"] for row in rows}),
        "target_start": min(row["date"] for row in rows),
        "target_end": max(row["date"] for row in rows),
        "mean_error_f": statistics.mean(residuals),
        "mae_f": statistics.mean(abs(value) for value in residuals),
        "crps_f": statistics.mean(gaussian_crps(row["mu"], row["sigma"], row["truth"]) for row in rows),
        "central_90pct_coverage": statistics.mean(abs(row["truth"] - row["mu"]) <= 1.6448536269514722 * max(row["sigma"], SIGMA_FLOOR_F) for row in rows),
        "mean_sigma_f": statistics.mean(row["sigma"] for row in rows),
        "scoring_floor_cases": sum(row["sigma"] < SIGMA_FLOOR_F for row in rows),
    }


def audit(export: dict, *, source_hash: str) -> dict:
    captured = _clock(export["captured_at"])
    tables = ("nwp_model_forecasts", "cli_settlements", "forecast_emos_daily_high")
    if any(export[name].get("truncated") is not False for name in tables):
        raise ValueError("a complete bounded export is required")
    counts = {name: len(export[name]["rows"]) for name in tables}
    final_truth = {}
    for row in export["cli_settlements"]["rows"]:
        if row.get("is_final") != 1 or not finite(row.get("max_temperature_f")):
            continue
        if _clock(row["fetched_at"]) > captured:
            raise ValueError("truth retrieval follows export clock")
        key = (row["station_id"], row["local_date"])
        if key in final_truth and final_truth[key] != row["max_temperature_f"]:
            raise ValueError("conflicting station-day final truth")
        final_truth[key] = row["max_temperature_f"]
    cohorts = defaultdict(list)
    rejected = Counter()
    seen = set()
    source_counts = Counter()
    recorded_by_nominal_serve = 0
    for row in export["forecast_emos_daily_high"]["rows"]:
        source_counts[row["source"]] += 1
        if row["source"] != "rolling_origin_v2":
            rejected["other_source"] += 1
            continue
        station, target, lead = row["station_id"], row["target_date"], row["lead_days"]
        key = (station, target, lead)
        if key in seen:
            raise ValueError("duplicate canonical station-target-lead")
        seen.add(key)
        city = CITY_BY_STATION.get(station)
        if city is None or type(lead) is not int or lead < 1:
            rejected["unknown_station_or_lead"] += 1
            continue
        start, end = utc_window_for_local_standard_date(date.fromisoformat(target), city.fixed_standard_timezone())
        fetched = _clock(row["fetched_at"])
        if fetched > captured:
            raise ValueError("forecast recording follows export clock")
        if end > captured:
            rejected["unfinished_climate_day"] += 1
            continue
        truth = final_truth.get((station, target))
        mu, sigma = row.get("predicted_high_f"), row.get("sigma_f")
        if truth is None:
            rejected["no_final_station_truth"] += 1
            continue
        if not finite(mu) or not finite(sigma) or sigma <= 0:
            rejected["invalid_distribution"] += 1
            continue
        # This is only a necessary storage-clock condition. Reconstructed rows
        # passing it still do not prove provider initialization or issued inputs.
        recorded_by_nominal_serve += fetched <= start - timedelta(days=lead)
        cohorts[(station, lead)].append({"date": target, "mu": mu, "sigma": sigma, "truth": truth})
    all_rows = [row for rows in cohorts.values() for row in rows]
    return {
        "schema_version": 1, "captured_at": export["captured_at"],
        "source_export_sha256": source_hash, "export_row_counts": counts,
        "archive_source_counts": dict(source_counts), "excluded": dict(rejected),
        "evaluation_source": "rolling_origin_v2", "overall": summarize(all_rows),
        "by_station_lead": [{"station": station, "lead": lead, **summarize(rows)} for (station, lead), rows in sorted(cohorts.items())],
        "records_stored_by_nominal_serve_clock": recorded_by_nominal_serve,
        "original_vintage_qualification": "unestablished",
        "scoring_sigma_floor_f": SIGMA_FLOOR_F,
        "limitations": [
            "Retained reconstructed distributions, not original decision-time forecasts or a V7 backtest.",
            "Source-separated archive diagnostics do not prove complete constituent hours for old NWP inputs.",
            "Stored retrieval clocks cannot certify provider initialization or training-truth availability.",
            "Coverage treats the integer settlement high as a point; no market prices, fills, fees or profit are inferred.",
            "Cases share weather dates and leads; counts are not independent observations or promotion evidence.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raw = args.export.read_bytes()
    result = audit(json.loads(raw), source_hash=hashlib.sha256(raw).hexdigest())
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
