#!/usr/bin/env python3
"""Publish a small, dated, allowlisted offline research summary for the SPA.

The full report remains reviewable in docs. Optional paired private receipts
contribute only dated collection counts, resource measurements and their digests.
Private paths, account IDs, configuration and source maps never enter the site.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path


def _number(value, *, positive=False, integer=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or (integer and type(value) is not int) or value < 0 or (positive and value == 0)):
        raise ValueError("invalid local collection measurement or guard limit")
    return value


def _utc_now():
    return datetime.now(timezone.utc)


def _clock(value):
    if not isinstance(value, str):
        raise ValueError("local collection requires explicit dated receipt clocks")
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError("local collection clocks require timezones")
    stamp = stamp.astimezone(timezone.utc)
    # Receipts and publication are produced on the same Mac. A future clock
    # has no justified tolerance and cannot become dated public evidence.
    if stamp > _utc_now():
        raise ValueError("public research evidence clocks cannot be in the future")
    return stamp


def local_collection(worker_raw: bytes, collector_raw: bytes) -> dict:
    """Allowlist the paired receipt evidence, never copy arbitrary receipt fields."""
    if not isinstance(worker_raw, bytes) or not isinstance(collector_raw, bytes):
        raise ValueError("local collection requires exact receipt bytes")
    try:
        worker, collector = json.loads(worker_raw), json.loads(collector_raw)
    except (ValueError, UnicodeError) as error:
        raise ValueError("invalid local collection receipt JSON") from error
    if not isinstance(worker, dict) or not isinstance(collector, dict):
        raise ValueError("local collection receipts must be objects")
    if (type(worker.get('schema_version')) is not int or worker.get('schema_version') != 2
            or worker.get('status') != 'complete'
            or worker.get('evidence_mode') != 'retained_aws_paper_and_local_prospective_weather'
            or worker.get('live_orders_enabled') is not False
            or ('promotion_eligible' in worker and worker['promotion_eligible'] is not False)):
        raise ValueError("local collection requires a completed research-only worker")
    collector_hash = hashlib.sha256(collector_raw).hexdigest()
    if worker.get('local_shadow_receipt_sha256') != collector_hash:
        raise ValueError("collector receipt does not match the completed worker digest")
    if (type(collector.get('schema_version')) is not int or collector.get('schema_version') != 1
            or collector.get('status') != 'complete'
            or collector.get('research_identity') != 'local-shadow-v7-v1'
            or collector.get('execution_location') != 'local_mac'
            or collector.get('live_orders_enabled') is not False
            or collector.get('promotion_eligible') is not False):
        raise ValueError("local collection requires completed unpromoted Mac shadow evidence")
    resources = worker.get('resources')
    if not isinstance(resources, dict) or not isinstance(resources.get('limits'), dict):
        raise ValueError("local collection requires resource budget evidence")
    limits = resources['limits']
    ceilings = {'wall_seconds': 1200, 'cpu_seconds': 600, 'cpu_fraction': .5,
                'rss_bytes': 2 * 1024**3, 'maximum_load_per_cpu': .25}
    for name, ceiling in ceilings.items():
        if _number(limits.get(name), positive=True) > ceiling:
            raise ValueError("local collection guard exceeds its reviewed ceiling")
    headroom = _number(limits.get('minimum_free_memory_percent'), positive=True)
    if not 25 <= headroom <= 100 or _number(limits.get('minimum_free_disk_bytes'), positive=True) < 20 * 1024**3:
        raise ValueError("local collection resource headroom was weakened")
    elapsed = _number(resources.get('elapsed_seconds'))
    cpu = _number(resources.get('observed_cpu_seconds'))
    rss = _number(resources.get('maximum_group_rss_bytes'), integer=True)
    if elapsed > limits['wall_seconds'] or cpu > limits['cpu_seconds'] or rss > limits['rss_bytes']:
        raise ValueError("completed local collection exceeded its resource budget")
    requests = _number(collector.get('http_requests'), positive=True, integer=True)
    maximum_requests = _number(collector.get('maximum_http_requests'), positive=True, integer=True)
    maximum_runtime = _number(collector.get('maximum_runtime_seconds'), positive=True)
    forecasts = _number(collector.get('new_issued_vintages'), positive=True, integer=True)
    if not requests <= maximum_requests <= 8 or maximum_runtime > 300 or forecasts > 12:
        raise ValueError("local collection exceeded the reviewed city/request/runtime batch")
    worker_start = worker.get('started_at')
    if not isinstance(worker_start, str):
        raise ValueError("completed worker is missing its UTC start clock")
    started = datetime.strptime(worker_start, '%Y%m%dT%H%M%S%fZ').replace(tzinfo=timezone.utc)
    if started > _utc_now():
        raise ValueError("completed worker start clock cannot be in the future")
    collected_start, finished = _clock(collector.get('started_at')), _clock(collector.get('finished_at'))
    worker_finished = _clock(worker.get('finished_at'))
    if not started <= collected_start <= finished <= worker_finished:
        raise ValueError("dated collection batch is outside its worker run")
    if (finished - collected_start).total_seconds() > maximum_runtime:
        raise ValueError("local collection exceeded its declared runtime ceiling")
    return {'finished_at': finished.isoformat(), 'new_issued_forecasts': forecasts,
            'http_requests': requests, 'elapsed_seconds': elapsed,
            'observed_cpu_seconds': cpu, 'maximum_group_rss_bytes': rss,
            'worker_receipt_sha256': hashlib.sha256(worker_raw).hexdigest(),
            'collector_receipt_sha256': collector_hash}


def summary(report: dict, report_hash: str, *, worker_receipt_raw=None, collector_receipt_raw=None) -> dict:
    if (worker_receipt_raw is None) != (collector_receipt_raw is None):
        raise ValueError("worker and collector receipts must be supplied together")
    if (type(report_hash) is not str or len(report_hash) != 64
            or any(character not in '0123456789abcdef' for character in report_hash)):
        raise ValueError("report digest must be a lowercase SHA-256 hex string")
    if (type(report.get("schema_version")) is not int or report.get("schema_version") != 1
            or report.get("active") is not False or report.get("promotion_eligible") is not False):
        raise ValueError("only explicitly unpromoted offline diagnostics may be published")
    captured = _clock(report.get('captured_at'))
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
    if scores["distinct_calendar_targets"] > scores["cases"]:
        raise ValueError("distinct target dates cannot exceed case count")
    # Prospective diagnostics live separately; this summary concerns only the
    # reconstructed cohort and never borrows cases from a future issued cohort.
    result = {
        "schema_version": 1,
        "captured_at": captured.isoformat(),
        "evaluation_kind": "retrospective_diagnostic",
        "report_sha256": report_hash,
        "cases": scores["cases"],
        "distinct_target_dates": scores["distinct_calendar_targets"],
        "qualified_original_vintage_cases": 0,
        "active": False,
        "promotion_eligible": False,
        "candidates": candidates,
    }
    if worker_receipt_raw is not None:
        result['local_collection'] = local_collection(worker_receipt_raw, collector_receipt_raw)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--worker-receipt", type=Path)
    parser.add_argument("--collector-receipt", type=Path)
    args = parser.parse_args()
    if (args.worker_receipt is None) != (args.collector_receipt is None):
        parser.error("--worker-receipt and --collector-receipt must be supplied together")
    raw = args.report.read_bytes()
    result = summary(json.loads(raw), hashlib.sha256(raw).hexdigest(),
                     worker_receipt_raw=args.worker_receipt.read_bytes() if args.worker_receipt else None,
                     collector_receipt_raw=args.collector_receipt.read_bytes() if args.collector_receipt else None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    staged = args.output.with_suffix(".json.tmp")
    staged.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    staged.replace(args.output)


if __name__ == "__main__":
    main()
