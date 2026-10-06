#!/usr/bin/env python3
"""Publish a small, dated, allowlisted offline research summary for the SPA.

The full report remains reviewable in docs. This never imports private worker
receipts, account IDs, access configuration, or model parameters into the site.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path


def summary(report: dict, report_hash: str) -> dict:
    if report.get("schema_version") != 1 or report.get("active") is not False or report.get("promotion_eligible") is not False:
        raise ValueError("only explicitly unpromoted offline diagnostics may be published")
    if report.get("evidence_scope") != "exploratory_reconstructed_walk_forward_weather_skill":
        raise ValueError("unrecognized research scope")
    scores = report["retrospective"]
    baseline = scores["arms"]["existing_bias_only"]
    candidates = []
    for key, name, metric, label in (
        ("minimum_crps_location_scale", "Minimum-CRPS calibration", "crps_f", "CRPS"),
        ("gradient_boosted_quantiles", "Boosted temperature quantiles", "mean_pinball_f", "mean pinball loss"),
    ):
        if key not in scores["arms"]:
            continue
        value, base = scores["arms"][key][metric], baseline[metric]
        if not all(type(n) in (int, float) and math.isfinite(n) and n >= 0 for n in (value, base)):
            raise ValueError("invalid score")
        difference = value - base
        state = "Retained offline · no promotion; original-vintage and profitable-fill evidence remain unestablished"
        candidates.append({
            "name": name,
            "state": state,
            "comparison": f"{label} {value:.4f}°F vs {base:.4f}°F baseline ({abs(difference):.4f}°F {'worse' if difference > 0 else 'better' if difference < 0 else 'equal'}; lower is better)",
        })
    for value in (scores["cases"], scores["distinct_calendar_targets"]):
        if type(value) is not int or value < 0:
            raise ValueError("invalid case/date count")
    # Prospective diagnostics live separately; this summary concerns only the
    # reconstructed cohort and never borrows cases from a future issued cohort.
    return {
        "schema_version": 1,
        "captured_at": report["captured_at"],
        "evaluation_kind": "retrospective_diagnostic",
        "report_sha256": report_hash,
        "cases": scores["cases"],
        "distinct_target_dates": scores["distinct_calendar_targets"],
        "qualified_original_vintage_cases": 0,
        "active": False,
        "promotion_eligible": False,
        "candidates": candidates,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raw = args.report.read_bytes()
    result = summary(json.loads(raw), hashlib.sha256(raw).hexdigest())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    staged = args.output.with_suffix(".json.tmp")
    staged.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    staged.replace(args.output)


if __name__ == "__main__":
    main()
