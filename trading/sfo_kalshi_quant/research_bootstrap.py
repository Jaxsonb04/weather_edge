"""Task 5 Step 4: deterministic day-clustered bootstrap over independent
(station_id, target_date) clusters.

Split out of ``research_evidence.py`` (Step 3's paired daily/capacity
evidence) for the same file-size-cohesion reason ``research_scoring.py``
was split out of ``research_walkforward.py``: this is a genuinely separate
concern (resampling statistics over the fold-level paired deltas
``research_evidence.py`` already builds) with its own dedicated test file
(``trading/tests/test_research_bootstrap.py``), and combining both would
push a single module close to or past the project's 800-line file cap.

Plan Task 5 Step 4, verbatim: "Resample independent (station_id,
target_date) clusters with a fixed seed and 10,000 draws. Publish
percentile 95% intervals for paired realized P&L/day, log growth/day,
ROI, CRPS, and Brier deltas." The independent resampling unit is
therefore one ``WalkForwardFold`` -- exactly the plan's own "indivisible
station/target-day test group" -- never an individual paired case/ticker
row, so a fold that happens to hold several correlated same-city-day
cases (one per scan cycle) is not overrepresented relative to a fold that
holds only one.

Sign convention (documented once, applies to every delta this module
reports): positive always means the CHALLENGER arm improved.
P&L/ROI/log-growth deltas are ``challenger - baseline`` (higher raw P&L is
better). CRPS/Brier deltas are ``baseline - challenger`` (LOWER raw score
is better, so subtracting the other way keeps "positive = challenger
improved" true for every metric this module publishes).

This module never talks to the database or the wall clock. Its one
source of randomness is a ``random.Random`` instance seeded with an
explicit, fixed integer -- never global ``random`` state -- so two calls
with the same input always produce byte-identical output.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from .research_evidence import PairedCaseRecord
from .research_policy import TARGET_POLICY

# Plan Task 5 Step 4: "a fixed seed and 10,000 draws." The seed reuses the
# same YYYYMMDD-of-design-doc convention research_goals.py's own
# ``_day_cluster_bootstrap_interval`` already established (its literal
# ``random.Random(20260717)``) -- a distinct ``random.Random`` instance
# each call, so there is no shared mutable state between the two modules.
DEFAULT_BOOTSTRAP_SEED = 20260717
DEFAULT_BOOTSTRAP_DRAWS = 10000


@dataclass(frozen=True)
class FoldPairedAggregate:
    """One (station_id, target_date) cluster's paired delta aggregate --
    the bootstrap's independent resampling unit. Built from every paired
    case in one fold (``PairedCaseRecord.fold_id``), summed/averaged so a
    fold with several cases contributes exactly one cluster value.

    Carries ``execution_model_version``/``side_scope``/``fill_scope`` (S1/
    S2) straight through from its contributing ``PairedCaseRecord``s --
    this is itself an "aggregate" in the Task 4 review's sense, so it must
    not drop the stamp on the way to a bootstrap statistic.
    """

    fold_id: str
    station_id: str
    target_date: date
    case_count: int
    baseline_pnl: float
    challenger_pnl: float
    pnl_delta: float
    roi_delta: float
    log_growth_delta: float | None
    crps_delta: float | None
    brier_delta: float | None
    execution_model_version: str
    side_scope: str
    fill_scope: str


def _paired_log_growth_delta(
    baseline_pnl: float, challenger_pnl: float, *, reference_equity: float
) -> float | None:
    """``ln(1 + challenger/equity) - ln(1 + baseline/equity)``; ``None``
    when either side's implied single-cluster equity ratio would be
    non-positive (never a fabricated growth figure over a wipeout)."""

    baseline_ratio = 1.0 + baseline_pnl / reference_equity
    challenger_ratio = 1.0 + challenger_pnl / reference_equity
    if baseline_ratio <= 0.0 or challenger_ratio <= 0.0:
        return None
    return math.log(challenger_ratio) - math.log(baseline_ratio)


def fold_paired_aggregates(
    records: Sequence[PairedCaseRecord],
    *,
    reference_equity: float = TARGET_POLICY.reference_equity,
) -> tuple[FoldPairedAggregate, ...]:
    """Group paired case records into one aggregate per (station_id,
    target_date) cluster (``fold_id``) -- the bootstrap's own resampling
    unit, never an individual case/ticker row."""

    if reference_equity <= 0 or not math.isfinite(reference_equity):
        raise ValueError("reference_equity must be finite and positive")

    by_fold: dict[str, list[PairedCaseRecord]] = {}
    for record in records:
        by_fold.setdefault(record.fold_id, []).append(record)

    aggregates: list[FoldPairedAggregate] = []
    for fold_id, fold_records in by_fold.items():
        stamp_fields = {
            (r.execution_model_version, r.side_scope, r.fill_scope) for r in fold_records
        }
        if len(stamp_fields) > 1:
            # Every record in one fold_id cluster comes from the same
            # single FoldReplayEvidence row (build_paired_case_records
            # stamps them all identically), so a divergence here means the
            # caller mixed records from different experiment runs under
            # one fold_id -- fail closed rather than silently pick one.
            raise ValueError(
                f"paired case records for fold_id={fold_id!r} disagree on "
                "execution_model_version/side_scope/fill_scope"
            )
        execution_model_version, side_scope, fill_scope = next(iter(stamp_fields))
        baseline_total = math.fsum(r.baseline_pnl for r in fold_records)
        challenger_total = math.fsum(r.challenger_pnl for r in fold_records)
        pnl_delta = challenger_total - baseline_total
        crps_pairs = [
            (r.baseline_crps, r.challenger_crps)
            for r in fold_records
            if r.baseline_crps is not None and r.challenger_crps is not None
        ]
        brier_pairs = [
            (r.baseline_brier, r.challenger_brier)
            for r in fold_records
            if r.baseline_brier is not None and r.challenger_brier is not None
        ]
        aggregates.append(
            FoldPairedAggregate(
                fold_id=fold_id,
                station_id=fold_records[0].station_id,
                target_date=fold_records[0].target_date,
                case_count=len(fold_records),
                baseline_pnl=baseline_total,
                challenger_pnl=challenger_total,
                pnl_delta=pnl_delta,
                roi_delta=pnl_delta / reference_equity,
                log_growth_delta=_paired_log_growth_delta(
                    baseline_total, challenger_total, reference_equity=reference_equity
                ),
                crps_delta=(
                    statistics.fmean(b - c for b, c in crps_pairs) if crps_pairs else None
                ),
                brier_delta=(
                    statistics.fmean(b - c for b, c in brier_pairs) if brier_pairs else None
                ),
                execution_model_version=execution_model_version,
                side_scope=side_scope,
                fill_scope=fill_scope,
            )
        )

    aggregates.sort(key=lambda a: (a.target_date, a.station_id, a.fold_id))
    return tuple(aggregates)


def _percentile(values: Sequence[float], probability: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


@dataclass(frozen=True)
class BootstrapInterval:
    """One metric's deterministic day(station-day)-clustered bootstrap
    result: the observed point estimate plus a percentile 95% interval
    over ``samples`` resampled cluster means."""

    metric: str
    samples: int
    seed: int
    n_clusters: int
    point_estimate: float | None
    lower: float | None
    upper: float | None


_METRIC_NAMES = ("realized_pnl_per_day", "roi", "log_growth_per_day", "crps", "brier")


def _cluster_values(aggregates: Sequence[FoldPairedAggregate], metric: str) -> list[float]:
    if metric == "realized_pnl_per_day":
        return [a.pnl_delta for a in aggregates]
    if metric == "roi":
        return [a.roi_delta for a in aggregates]
    if metric == "log_growth_per_day":
        return [a.log_growth_delta for a in aggregates if a.log_growth_delta is not None]
    if metric == "crps":
        return [a.crps_delta for a in aggregates if a.crps_delta is not None]
    if metric == "brier":
        return [a.brier_delta for a in aggregates if a.brier_delta is not None]
    raise ValueError(f"unknown bootstrap metric: {metric!r}")


def day_clustered_bootstrap(
    aggregates: Sequence[FoldPairedAggregate],
    *,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    draws: int = DEFAULT_BOOTSTRAP_DRAWS,
) -> dict[str, BootstrapInterval]:
    """Deterministic percentile-95% bootstrap for paired realized P&L/day,
    ROI, log growth/day, CRPS, and Brier deltas, resampled over independent
    (station_id, target_date) clusters (plan Task 5 Step 4).

    Sorts ``aggregates`` by content first (never trusts caller order), so
    the same set of clusters -- supplied in any order -- always produces
    byte-identical output for a fixed ``seed``. Each metric resamples only
    over the clusters where that metric has a value (e.g. a cluster with
    no available CRPS pairing does not contribute a "missing" draw); a
    metric with zero available clusters reports ``None`` bounds rather
    than a fabricated interval.
    """

    ordered = sorted(aggregates, key=lambda a: (a.target_date, a.station_id, a.fold_id))
    results: dict[str, BootstrapInterval] = {}
    for metric in _METRIC_NAMES:
        values = _cluster_values(ordered, metric)
        if not values:
            results[metric] = BootstrapInterval(
                metric=metric,
                samples=draws,
                seed=seed,
                n_clusters=0,
                point_estimate=None,
                lower=None,
                upper=None,
            )
            continue
        rng = random.Random(seed)
        size = len(values)
        point_estimate = statistics.fmean(values)
        draw_means = [
            statistics.fmean(rng.choice(values) for _ in range(size)) for _ in range(draws)
        ]
        results[metric] = BootstrapInterval(
            metric=metric,
            samples=draws,
            seed=seed,
            n_clusters=size,
            point_estimate=point_estimate,
            lower=_percentile(draw_means, 0.025),
            upper=_percentile(draw_means, 0.975),
        )
    return results
