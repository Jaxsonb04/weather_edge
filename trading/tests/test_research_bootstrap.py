"""Task 5 Step 4: deterministic day-clustered bootstrap.

Covers plan Task 5 Step 1's named regressions that are specific to
``research_bootstrap.py`` (paired-daily/capacity coverage lives in
``test_research_evidence.py``, a separate sibling per that module's own
docstring):

- weather-day clustering: bootstrap resampling by independent station-day
  (``FoldPairedAggregate``/``fold_id``) rather than trade/case row;
- complete metric coverage (P&L/day, ROI, log growth/day, CRPS, Brier);
- determinism (fixed seed, order-invariant, repeatable).
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from sfo_kalshi_quant.research_bootstrap import (
    DEFAULT_BOOTSTRAP_DRAWS,
    DEFAULT_BOOTSTRAP_SEED,
    BootstrapInterval,
    FoldPairedAggregate,
    day_clustered_bootstrap,
    fold_paired_aggregates,
)
from sfo_kalshi_quant.research_evidence import PairedCaseRecord
from sfo_kalshi_quant.research_policy import TARGET_POLICY


def _record(
    *, fold_id: str, station_id: str = "KSFO", target_date: date = date(2026, 6, 20),
    source_context_hash: str, baseline_pnl: float = 0.0, challenger_pnl: float = 0.0,
    baseline_crps: float | None = None, challenger_crps: float | None = None,
    baseline_brier: float | None = None, challenger_brier: float | None = None,
) -> PairedCaseRecord:
    return PairedCaseRecord(
        fold_id=fold_id, station_id=station_id, target_date=target_date,
        pacific_day=target_date, source_context_hash=source_context_hash,
        baseline_pnl=baseline_pnl, challenger_pnl=challenger_pnl,
        baseline_filled=True, challenger_filled=True,
        baseline_contracts=1.0, challenger_contracts=1.0,
        baseline_dollars_at_risk=1.0, challenger_dollars_at_risk=1.0,
        baseline_rejections=(), challenger_rejections=(),
        baseline_crps=baseline_crps, challenger_crps=challenger_crps,
        baseline_brier=baseline_brier, challenger_brier=challenger_brier,
        execution_model_version="exec-v4-test", side_scope="yes_only",
        fill_scope="taker_only_no_tape",
    )


def _agg(
    *, fold_id: str = "KSFO:2026-06-20", station_id: str = "KSFO",
    target_date: date = date(2026, 6, 20), case_count: int = 1,
    baseline_pnl: float = 0.0, challenger_pnl: float = 0.0, pnl_delta: float = 0.0,
    roi_delta: float = 0.0, log_growth_delta: float | None = 0.0,
    crps_delta: float | None = None, brier_delta: float | None = None,
    execution_model_version: str = "exec-v4-test", side_scope: str = "yes_only",
    fill_scope: str = "taker_only_no_tape",
) -> FoldPairedAggregate:
    return FoldPairedAggregate(
        fold_id=fold_id, station_id=station_id, target_date=target_date, case_count=case_count,
        baseline_pnl=baseline_pnl, challenger_pnl=challenger_pnl, pnl_delta=pnl_delta,
        roi_delta=roi_delta, log_growth_delta=log_growth_delta,
        crps_delta=crps_delta, brier_delta=brier_delta,
        execution_model_version=execution_model_version, side_scope=side_scope, fill_scope=fill_scope,
    )


# ---------------------------------------------------------------------------
# fold_paired_aggregates: one row per (station_id, target_date) cluster
# ---------------------------------------------------------------------------


def test_fold_paired_aggregates_groups_multiple_cases_in_one_fold_into_one_cluster() -> None:
    records = [
        _record(fold_id="KSFO:2026-06-20", source_context_hash="h1", baseline_pnl=5.0, challenger_pnl=8.0),
        _record(fold_id="KSFO:2026-06-20", source_context_hash="h2", baseline_pnl=3.0, challenger_pnl=1.0),
        _record(fold_id="KLAX:2026-06-20", station_id="KLAX", source_context_hash="h3",
                baseline_pnl=10.0, challenger_pnl=10.0),
    ]

    aggregates = fold_paired_aggregates(records)

    # Two cases sharing one fold_id collapse into ONE cluster, not two.
    assert len(aggregates) == 2
    ksfo = next(a for a in aggregates if a.fold_id == "KSFO:2026-06-20")
    assert ksfo.case_count == 2
    assert ksfo.baseline_pnl == pytest.approx(8.0)
    assert ksfo.challenger_pnl == pytest.approx(9.0)


def test_fold_paired_aggregates_pnl_and_roi_delta_is_challenger_minus_baseline() -> None:
    records = [_record(fold_id="KSFO:2026-06-20", source_context_hash="h1", baseline_pnl=10.0, challenger_pnl=25.0)]

    aggregates = fold_paired_aggregates(records, reference_equity=1000.0)

    assert aggregates[0].pnl_delta == pytest.approx(15.0)
    assert aggregates[0].roi_delta == pytest.approx(15.0 / 1000.0)


def test_fold_paired_aggregates_crps_and_brier_delta_is_baseline_minus_challenger() -> None:
    records = [
        _record(
            fold_id="KSFO:2026-06-20", source_context_hash="h1",
            baseline_crps=2.0, challenger_crps=1.0, baseline_brier=0.3, challenger_brier=0.1,
        )
    ]

    aggregates = fold_paired_aggregates(records)

    # Lower CRPS/Brier is better; a challenger that scores LOWER must
    # produce a POSITIVE delta here (same "positive = challenger improved"
    # convention as every other paired delta this module publishes).
    assert aggregates[0].crps_delta == pytest.approx(1.0)
    assert aggregates[0].brier_delta == pytest.approx(0.2)


def test_fold_paired_aggregates_crps_delta_averages_only_paired_available_cases() -> None:
    records = [
        _record(fold_id="KSFO:2026-06-20", source_context_hash="h1", baseline_crps=2.0, challenger_crps=1.0),
        _record(fold_id="KSFO:2026-06-20", source_context_hash="h2", baseline_crps=None, challenger_crps=None),
        _record(fold_id="KSFO:2026-06-20", source_context_hash="h3", baseline_crps=4.0, challenger_crps=2.0),
    ]

    aggregates = fold_paired_aggregates(records)

    # h2 contributes no CRPS pairing; mean over h1 (delta=1.0) and h3
    # (delta=2.0) is 1.5, not diluted by a missing third value.
    assert aggregates[0].crps_delta == pytest.approx(1.5)


def test_fold_paired_aggregates_log_growth_delta_is_none_when_equity_would_go_non_positive() -> None:
    records = [_record(fold_id="KSFO:2026-06-20", source_context_hash="h1", baseline_pnl=-1500.0, challenger_pnl=10.0)]

    aggregates = fold_paired_aggregates(records, reference_equity=1000.0)

    assert aggregates[0].log_growth_delta is None


def test_fold_paired_aggregates_log_growth_delta_hand_computed() -> None:
    records = [_record(fold_id="KSFO:2026-06-20", source_context_hash="h1", baseline_pnl=50.0, challenger_pnl=100.0)]

    aggregates = fold_paired_aggregates(records, reference_equity=1000.0)

    expected = math.log(1100.0 / 1000.0) - math.log(1050.0 / 1000.0)
    assert aggregates[0].log_growth_delta == pytest.approx(expected)


def test_fold_paired_aggregates_rejects_non_positive_reference_equity() -> None:
    with pytest.raises(ValueError):
        fold_paired_aggregates(
            [_record(fold_id="KSFO:2026-06-20", source_context_hash="h1")], reference_equity=0.0
        )


def test_fold_paired_aggregates_surfaces_engine_version_and_scope_labels() -> None:
    # S1/S2: this "aggregate" must not drop the stamp its contributing
    # PairedCaseRecords carried -- Task 6's gate needs it here too, not
    # only on the pre-aggregation records.
    records = [_record(fold_id="KSFO:2026-06-20", source_context_hash="h1", baseline_pnl=1.0, challenger_pnl=2.0)]

    aggregates = fold_paired_aggregates(records)

    assert aggregates[0].execution_model_version == "exec-v4-test"
    assert aggregates[0].side_scope == "yes_only"
    assert aggregates[0].fill_scope == "taker_only_no_tape"


def test_fold_paired_aggregates_rejects_a_cluster_with_disagreeing_stamps() -> None:
    mismatched = PairedCaseRecord(
        fold_id="KSFO:2026-06-20", station_id="KSFO", target_date=date(2026, 6, 20),
        pacific_day=date(2026, 6, 20), source_context_hash="h2",
        baseline_pnl=0.0, challenger_pnl=0.0, baseline_filled=True, challenger_filled=True,
        baseline_contracts=1.0, challenger_contracts=1.0,
        baseline_dollars_at_risk=1.0, challenger_dollars_at_risk=1.0,
        baseline_rejections=(), challenger_rejections=(),
        baseline_crps=None, challenger_crps=None, baseline_brier=None, challenger_brier=None,
        execution_model_version="exec-v3-different", side_scope="yes_only",
        fill_scope="taker_only_no_tape",
    )
    records = [
        _record(fold_id="KSFO:2026-06-20", source_context_hash="h1"),
        mismatched,
    ]

    with pytest.raises(ValueError, match="disagree"):
        fold_paired_aggregates(records)


# ---------------------------------------------------------------------------
# day_clustered_bootstrap: metric coverage, clustering, determinism
# ---------------------------------------------------------------------------


def test_day_clustered_bootstrap_default_seed_and_draws_match_the_plan() -> None:
    results = day_clustered_bootstrap([_agg(pnl_delta=10.0, roi_delta=0.01)])

    assert results["realized_pnl_per_day"].seed == DEFAULT_BOOTSTRAP_SEED == 20260717
    assert results["realized_pnl_per_day"].samples == DEFAULT_BOOTSTRAP_DRAWS == 10000


def test_day_clustered_bootstrap_publishes_all_five_named_metrics() -> None:
    results = day_clustered_bootstrap(
        [_agg(pnl_delta=10.0, roi_delta=0.01, log_growth_delta=0.01, crps_delta=1.0, brier_delta=0.1)]
    )

    assert set(results) == {"realized_pnl_per_day", "roi", "log_growth_per_day", "crps", "brier"}
    for interval in results.values():
        assert isinstance(interval, BootstrapInterval)


def test_day_clustered_bootstrap_constant_cluster_values_collapse_to_an_exact_interval() -> None:
    # Every cluster has IDENTICAL pnl_delta -- every bootstrap resample is
    # therefore also exactly that value, so the interval must collapse to
    # a point: an exactly hand-computable case, independent of the RNG.
    aggregates = [
        _agg(fold_id=f"KSFO:2026-06-{20 + i}", target_date=date(2026, 6, 20 + i), pnl_delta=7.5, roi_delta=0.0075)
        for i in range(5)
    ]

    results = day_clustered_bootstrap(aggregates, draws=200)

    interval = results["realized_pnl_per_day"]
    assert interval.point_estimate == pytest.approx(7.5)
    assert interval.lower == pytest.approx(7.5)
    assert interval.upper == pytest.approx(7.5)
    assert interval.n_clusters == 5


def test_day_clustered_bootstrap_resamples_clusters_not_paired_case_rows() -> None:
    # Fold A has FIVE paired cases with pnl_delta effectively concentrated
    # in one cluster; fold B has ONE. If the bootstrap resampled individual
    # case/trade rows, fold A would dominate the resampled distribution
    # five-to-one. Resampling clusters means each fold contributes exactly
    # one point regardless of how many cases built it.
    records = [
        *[
            _record(fold_id="KSFO:2026-06-20", source_context_hash=f"h{i}", baseline_pnl=0.0, challenger_pnl=100.0)
            for i in range(5)
        ],
        _record(fold_id="KLAX:2026-06-20", station_id="KLAX", source_context_hash="hz", baseline_pnl=0.0, challenger_pnl=0.0),
    ]
    aggregates = fold_paired_aggregates(records)

    assert len(aggregates) == 2  # exactly one row per cluster, not per case

    results = day_clustered_bootstrap(aggregates, draws=500)

    # Point estimate is the mean of the two CLUSTER deltas (500 and 0),
    # i.e. 250 -- not the mean of the six underlying rows, which would
    # weight the KSFO cluster 5x more heavily.
    assert results["realized_pnl_per_day"].n_clusters == 2
    assert results["realized_pnl_per_day"].point_estimate == pytest.approx(250.0)


def test_day_clustered_bootstrap_crps_metric_only_resamples_available_clusters() -> None:
    aggregates = [
        _agg(fold_id="A", target_date=date(2026, 6, 20), crps_delta=1.0),
        _agg(fold_id="B", target_date=date(2026, 6, 21), crps_delta=None),
        _agg(fold_id="C", target_date=date(2026, 6, 22), crps_delta=3.0),
    ]

    results = day_clustered_bootstrap(aggregates, draws=300)

    assert results["crps"].n_clusters == 2
    assert results["crps"].point_estimate == pytest.approx(2.0)


def test_day_clustered_bootstrap_metric_with_zero_available_clusters_reports_none_bounds() -> None:
    aggregates = [_agg(fold_id="A", target_date=date(2026, 6, 20), crps_delta=None, brier_delta=None)]

    results = day_clustered_bootstrap(aggregates, draws=100)

    assert results["crps"].n_clusters == 0
    assert results["crps"].point_estimate is None
    assert results["crps"].lower is None
    assert results["crps"].upper is None


def test_day_clustered_bootstrap_empty_aggregates_returns_none_bounds_for_every_metric() -> None:
    results = day_clustered_bootstrap([])

    assert set(results) == {"realized_pnl_per_day", "roi", "log_growth_per_day", "crps", "brier"}
    for interval in results.values():
        assert interval.n_clusters == 0
        assert interval.point_estimate is None
        assert interval.lower is None
        assert interval.upper is None


def test_day_clustered_bootstrap_is_deterministic_across_repeated_calls() -> None:
    aggregates = [
        _agg(fold_id="A", target_date=date(2026, 6, 20), pnl_delta=10.0, roi_delta=0.01,
             log_growth_delta=0.01, crps_delta=1.0, brier_delta=0.1),
        _agg(fold_id="B", target_date=date(2026, 6, 21), pnl_delta=-4.0, roi_delta=-0.004,
             log_growth_delta=-0.004, crps_delta=0.5, brier_delta=0.2),
        _agg(fold_id="C", target_date=date(2026, 6, 22), pnl_delta=8.0, roi_delta=0.008,
             log_growth_delta=0.008, crps_delta=1.5, brier_delta=0.05),
    ]

    first = day_clustered_bootstrap(aggregates, draws=500)
    second = day_clustered_bootstrap(aggregates, draws=500)

    assert first == second


def test_day_clustered_bootstrap_is_invariant_to_input_order() -> None:
    aggregates = [
        _agg(fold_id="A", target_date=date(2026, 6, 20), pnl_delta=10.0, roi_delta=0.01, crps_delta=1.0),
        _agg(fold_id="B", target_date=date(2026, 6, 21), pnl_delta=-4.0, roi_delta=-0.004, crps_delta=0.5),
        _agg(fold_id="C", target_date=date(2026, 6, 22), pnl_delta=8.0, roi_delta=0.008, crps_delta=1.5),
    ]

    forward = day_clustered_bootstrap(aggregates, draws=500)
    reversed_order = day_clustered_bootstrap(list(reversed(aggregates)), draws=500)

    assert forward == reversed_order


def test_day_clustered_bootstrap_respects_explicit_seed_and_draws() -> None:
    aggregates = [_agg(fold_id="A", target_date=date(2026, 6, 20), pnl_delta=10.0)]

    results = day_clustered_bootstrap(aggregates, seed=1, draws=50)

    assert results["realized_pnl_per_day"].seed == 1
    assert results["realized_pnl_per_day"].samples == 50


def test_day_clustered_bootstrap_different_seeds_can_diverge_but_stay_deterministic() -> None:
    aggregates = [
        _agg(fold_id="A", target_date=date(2026, 6, 20), pnl_delta=10.0),
        _agg(fold_id="B", target_date=date(2026, 6, 21), pnl_delta=-30.0),
        _agg(fold_id="C", target_date=date(2026, 6, 22), pnl_delta=25.0),
    ]

    seed_1a = day_clustered_bootstrap(aggregates, seed=1, draws=500)
    seed_1b = day_clustered_bootstrap(aggregates, seed=1, draws=500)
    seed_2 = day_clustered_bootstrap(aggregates, seed=2, draws=500)

    assert seed_1a == seed_1b
    # Not asserting inequality with seed_2 (a different seed COULD
    # coincidentally land on the same percentile draw for a tiny cluster
    # count) -- only that re-using one seed is exactly reproducible.
    assert seed_1a["realized_pnl_per_day"].seed == 1
    assert seed_2["realized_pnl_per_day"].seed == 2
