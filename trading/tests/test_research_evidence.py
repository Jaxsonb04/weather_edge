"""Task 5: paired daily and capacity evidence.

Covers plan Task 5 Step 1's named regressions for ``research_evidence.py``
(the day-clustered bootstrap itself lives in ``research_bootstrap.py`` /
``test_research_bootstrap.py``, a separate sibling per that module's own
docstring):

- same-day-same-case pairing, including mixed-coverage exclusion (S3);
- each required KPI/capacity field (plan Task 5 Step 3's named list);
- Pacific-day aggregation across a UTC seam;
- retention of zero-fill days in daily statistics;
- drawdown/log-growth math verified against hand-computed values;
- scope labels + engine stamp surfaced on every aggregate (S1/S2);
- determinism (pure functions; no clock/random reads at all in this file).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

from sfo_kalshi_quant.research_candidates import FoldCandidateEvidence
from sfo_kalshi_quant.research_evidence import (
    ArmDailyKpis,
    CaseCoverageExclusion,
    DailyPairedEvidence,
    PairedCaseRecord,
    arm_daily_kpis,
    arm_kpi_delta,
    build_paired_case_records,
    build_paired_evidence_report,
    build_paired_records_for_experiment,
    capacity_evidence,
    daily_paired_evidence,
    sleeve_capacity_evidence,
)
from sfo_kalshi_quant.research_policy import MOTION_POLICY, TARGET_POLICY
from sfo_kalshi_quant.research_replay import FoldReplayEvidence
from sfo_kalshi_quant.research_walkforward import ResearchCase, WalkForwardFold

# ---------------------------------------------------------------------------
# Fixtures: direct construction, matching the sibling test files' own
# convention (test_research_replay.py/test_research_candidates.py build
# ResearchCase/WalkForwardFold/CandidateDistribution directly rather than
# running the full loader/replay pipeline for every unit test).
# ---------------------------------------------------------------------------


def _stamp(
    *,
    execution_model_version: str = "exec-v4-test",
    side_scope: str = "yes_only",
    fill_scope: str = "taker_only_no_tape",
) -> dict[str, object]:
    return {
        "execution_model_version": execution_model_version,
        "reference_equity": 1000.0,
        "max_position_risk_pct": 0.03,
        "policy_fingerprint": "fp-test",
        "order_ttl_minutes": 15,
        "side_scope": side_scope,
        "fill_scope": fill_scope,
    }


def _ticker(*, status: str = "filled", contracts: float = 10.0, limit_price: float = 0.20,
            realized_pnl: float = 5.0) -> dict[str, object]:
    return {
        "ticker": "KX-TEST",
        "status": status,
        "detail": "",
        "side": "YES" if status == "filled" else None,
        "would_cross": status == "filled",
        "limit_price": limit_price,
        "contracts": contracts if status == "filled" else None,
        "fee_per_contract": 0.01,
        "queue_ahead": 0.0,
        "probability": 0.5,
        "edge": 0.02,
        "edge_lcb": 0.02,
        "realized_pnl": realized_pnl if status == "filled" else 0.0,
    }


def _case_payload(
    *, available: bool = True, skip_reason: str = "", realized_pnl: float = 0.0,
    filled_count: int = 0, tickers=(), stamp: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "candidate_key": "candidate",
        "available": available,
        "skip_reason": skip_reason,
        "realized_pnl": realized_pnl,
        "filled_count": filled_count,
        "stamp": stamp if stamp is not None else _stamp(),
        "tickers": list(tickers),
    }


def _score_payload(*, available: bool = True, crps: float = 1.0, bracket_brier: float = 0.1):
    return {
        "candidate_key": "candidate",
        "candidate_version": "v1",
        "hypothesis_family": "family",
        "available": available,
        "mu": 70.0,
        "sigma": 3.0,
        "unavailable_reason": "" if available else "unavailable",
        "crps": crps if available else None,
        "ranked_probability_score": None,
        "log_score": None,
        "pit": None,
        "point_error": None,
        "interval_80_covered": None,
        "bracket_brier": bracket_brier if available else None,
    }


def _research_case(
    *, station_id: str = "KSFO", target_date: date = date(2026, 6, 20),
    settled_at: datetime, source_context_hash: str, lead_days: int = 1,
    decision_at: datetime | None = None,
) -> ResearchCase:
    if decision_at is None:
        decision_at = datetime(
            target_date.year, target_date.month, target_date.day, 20, 0, 0, tzinfo=timezone.utc
        ) - timedelta(days=lead_days)
    return ResearchCase(
        station_id=station_id,
        target_date=target_date,
        decision_at=decision_at,
        settled_at=settled_at,
        lead_days=lead_days,
        source_context_hash=source_context_hash,
        baseline_mu=70.0,
        baseline_sigma=3.0,
        actual_high_f=75.0,
    )


def _fold(fold_id: str, test_cases: tuple[ResearchCase, ...], train=()) -> WalkForwardFold:
    return WalkForwardFold(
        fold_id=fold_id,
        decision_at=min(c.decision_at for c in test_cases),
        train=train,
        test=test_cases,
    )


def _replay_evidence(
    *, fold_id: str, station_id: str = "KSFO", target_date: date = date(2026, 6, 20),
    challenger_key: str = "gaussian-pit-station-lead-v1", baseline_cases, challenger_cases,
    baseline_stamp: dict[str, object] | None = None, challenger_stamp: dict[str, object] | None = None,
) -> FoldReplayEvidence:
    return FoldReplayEvidence(
        fold_id=fold_id,
        station_id=station_id,
        target_date=target_date,
        challenger_candidate_key=challenger_key,
        baseline={"cases": baseline_cases, "stamp": baseline_stamp if baseline_stamp is not None else _stamp()},
        challenger={"cases": challenger_cases, "stamp": challenger_stamp if challenger_stamp is not None else _stamp()},
        promotion_eligible=True,
    )


def _candidate_evidence(
    *, fold_id: str, station_id: str = "KSFO", target_date: date = date(2026, 6, 20),
    challenger_key: str = "gaussian-pit-station-lead-v1", baseline_cases, challenger_cases,
) -> FoldCandidateEvidence:
    return FoldCandidateEvidence(
        fold_id=fold_id,
        station_id=station_id,
        target_date=target_date,
        evaluated_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc),
        challenger_candidate_key=challenger_key,
        baseline={"cases": baseline_cases},
        challenger={"cases": challenger_cases},
    )


def _record(
    *, fold_id: str = "KSFO:2026-06-20", station_id: str = "KSFO",
    target_date: date = date(2026, 6, 20), pacific_day: date = date(2026, 6, 20),
    source_context_hash: str = "hash-1", baseline_pnl: float = 0.0, challenger_pnl: float = 0.0,
    baseline_filled: bool = False, challenger_filled: bool = False,
    baseline_contracts: float = 0.0, challenger_contracts: float = 0.0,
    baseline_dollars_at_risk: float = 0.0, challenger_dollars_at_risk: float = 0.0,
    baseline_rejections: tuple[str, ...] = (), challenger_rejections: tuple[str, ...] = (),
    baseline_crps: float | None = None, challenger_crps: float | None = None,
    baseline_brier: float | None = None, challenger_brier: float | None = None,
    execution_model_version: str = "exec-v4-test", side_scope: str = "yes_only",
    fill_scope: str = "taker_only_no_tape",
) -> PairedCaseRecord:
    return PairedCaseRecord(
        fold_id=fold_id, station_id=station_id, target_date=target_date, pacific_day=pacific_day,
        source_context_hash=source_context_hash, baseline_pnl=baseline_pnl, challenger_pnl=challenger_pnl,
        baseline_filled=baseline_filled, challenger_filled=challenger_filled,
        baseline_contracts=baseline_contracts, challenger_contracts=challenger_contracts,
        baseline_dollars_at_risk=baseline_dollars_at_risk, challenger_dollars_at_risk=challenger_dollars_at_risk,
        baseline_rejections=baseline_rejections, challenger_rejections=challenger_rejections,
        baseline_crps=baseline_crps, challenger_crps=challenger_crps,
        baseline_brier=baseline_brier, challenger_brier=challenger_brier,
        execution_model_version=execution_model_version, side_scope=side_scope, fill_scope=fill_scope,
    )


def _day(
    *, pacific_day: date, case_count: int = 1, baseline_pnl: float = 0.0, challenger_pnl: float = 0.0,
    baseline_filled_count: int = 0, challenger_filled_count: int = 0,
    baseline_contracts: float = 0.0, challenger_contracts: float = 0.0,
    baseline_dollars_at_risk: float = 0.0, challenger_dollars_at_risk: float = 0.0,
    baseline_rejection_counts=None, challenger_rejection_counts=None,
) -> DailyPairedEvidence:
    return DailyPairedEvidence(
        pacific_day=pacific_day, case_count=case_count, baseline_pnl=baseline_pnl, challenger_pnl=challenger_pnl,
        baseline_filled_count=baseline_filled_count, challenger_filled_count=challenger_filled_count,
        baseline_contracts=baseline_contracts, challenger_contracts=challenger_contracts,
        baseline_dollars_at_risk=baseline_dollars_at_risk, challenger_dollars_at_risk=challenger_dollars_at_risk,
        baseline_rejection_counts=baseline_rejection_counts or {}, challenger_rejection_counts=challenger_rejection_counts or {},
    )


# ---------------------------------------------------------------------------
# build_paired_case_records: Pacific-day derivation, pairing, coverage (S3)
# ---------------------------------------------------------------------------


def test_pacific_day_derived_from_settled_at_crosses_utc_seam() -> None:
    # 2026-01-15T07:30:00Z is 2026-01-14T23:30:00-08:00 in Pacific
    # (January -> PST, UTC-8) -- a day EARLIER than the naive UTC date, and
    # different from the station's own fixed-standard target_date below.
    settled_at = datetime(2026, 1, 15, 7, 30, 0, tzinfo=timezone.utc)
    case = _research_case(
        target_date=date(2026, 1, 15), settled_at=settled_at, source_context_hash="h1"
    )
    fold = _fold("KSFO:2026-01-15", (case,))
    replay = _replay_evidence(
        fold_id=fold.fold_id,
        target_date=date(2026, 1, 15),
        baseline_cases={"h1": _case_payload(realized_pnl=1.0, tickers=[_ticker()])},
        challenger_cases={"h1": _case_payload(realized_pnl=2.0, tickers=[_ticker()])},
    )

    records, exclusions = build_paired_case_records(fold, replay)

    assert exclusions == ()
    assert len(records) == 1
    assert records[0].pacific_day == date(2026, 1, 14)
    assert records[0].pacific_day != records[0].target_date


def test_case_excluded_when_challenger_unavailable_is_recorded_as_coverage_exclusion() -> None:
    case = _research_case(
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc), source_context_hash="h1"
    )
    fold = _fold("KSFO:2026-06-20", (case,))
    replay = _replay_evidence(
        fold_id=fold.fold_id,
        baseline_cases={"h1": _case_payload(realized_pnl=5.0, tickers=[_ticker()])},
        challenger_cases={
            "h1": _case_payload(
                available=False, skip_reason="candidate_distribution_unavailable:no_pooled_training_history"
            )
        },
    )

    records, exclusions = build_paired_case_records(fold, replay)

    assert records == ()
    assert len(exclusions) == 1
    assert exclusions[0].reason == "challenger_unavailable"
    assert "no_pooled_training_history" in exclusions[0].challenger_skip_reason
    assert exclusions[0].fold_id == fold.fold_id
    assert exclusions[0].source_context_hash == "h1"


def test_case_excluded_when_baseline_unavailable_is_recorded_as_coverage_exclusion() -> None:
    case = _research_case(
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc), source_context_hash="h1"
    )
    fold = _fold("KSFO:2026-06-20", (case,))
    replay = _replay_evidence(
        fold_id=fold.fold_id,
        baseline_cases={"h1": _case_payload(available=False, skip_reason="missing_market_snapshot")},
        challenger_cases={"h1": _case_payload(realized_pnl=5.0, tickers=[_ticker()])},
    )

    records, exclusions = build_paired_case_records(fold, replay)

    assert records == ()
    assert exclusions[0].reason == "baseline_unavailable"
    assert "missing_market_snapshot" in exclusions[0].baseline_skip_reason


def test_case_excluded_when_both_arms_unavailable_reports_both_unavailable() -> None:
    case = _research_case(
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc), source_context_hash="h1"
    )
    fold = _fold("KSFO:2026-06-20", (case,))
    replay = _replay_evidence(
        fold_id=fold.fold_id,
        baseline_cases={"h1": _case_payload(available=False, skip_reason="a")},
        challenger_cases={"h1": _case_payload(available=False, skip_reason="b")},
    )

    _, exclusions = build_paired_case_records(fold, replay)

    assert exclusions[0].reason == "both_unavailable"


def test_paired_case_record_derives_contracts_dollars_and_rejections_from_tickers() -> None:
    case = _research_case(
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc), source_context_hash="h1"
    )
    fold = _fold("KSFO:2026-06-20", (case,))
    tickers = [
        _ticker(status="filled", contracts=10.0, limit_price=0.20, realized_pnl=3.0),
        _ticker(status="no_trade"),
    ]
    replay = _replay_evidence(
        fold_id=fold.fold_id,
        baseline_cases={"h1": _case_payload(realized_pnl=3.0, filled_count=1, tickers=tickers)},
        challenger_cases={"h1": _case_payload(realized_pnl=3.0, filled_count=1, tickers=tickers)},
    )

    records, exclusions = build_paired_case_records(fold, replay)

    assert exclusions == ()
    record = records[0]
    assert record.baseline_pnl == 3.0
    assert record.baseline_filled is True
    assert record.baseline_contracts == 10.0
    assert record.baseline_dollars_at_risk == pytest.approx(2.0)  # 10 contracts * $0.20
    assert record.baseline_rejections == ("no_trade",)


def test_build_paired_case_records_rejects_mismatched_fold_id() -> None:
    case = _research_case(
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc), source_context_hash="h1"
    )
    fold = _fold("KSFO:2026-06-20", (case,))
    replay = _replay_evidence(
        fold_id="KSFO:2026-06-21",
        baseline_cases={"h1": _case_payload(tickers=[_ticker()])},
        challenger_cases={"h1": _case_payload(tickers=[_ticker()])},
    )

    with pytest.raises(ValueError, match="fold_id"):
        build_paired_case_records(fold, replay)


def test_build_paired_case_records_rejects_mismatched_candidate_evidence_fold_id() -> None:
    case = _research_case(
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc), source_context_hash="h1"
    )
    fold = _fold("KSFO:2026-06-20", (case,))
    replay = _replay_evidence(
        fold_id=fold.fold_id,
        baseline_cases={"h1": _case_payload(tickers=[_ticker()])},
        challenger_cases={"h1": _case_payload(tickers=[_ticker()])},
    )
    candidate = _candidate_evidence(
        fold_id="KSFO:2026-06-21",
        baseline_cases={"h1": _score_payload()},
        challenger_cases={"h1": _score_payload()},
    )

    with pytest.raises(ValueError, match="fold_id"):
        build_paired_case_records(fold, replay, candidate)


def test_build_paired_case_records_raises_when_baseline_and_challenger_stamps_disagree() -> None:
    case = _research_case(
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc), source_context_hash="h1"
    )
    fold = _fold("KSFO:2026-06-20", (case,))
    replay = _replay_evidence(
        fold_id=fold.fold_id,
        baseline_cases={"h1": _case_payload(tickers=[_ticker()])},
        challenger_cases={"h1": _case_payload(tickers=[_ticker()])},
        baseline_stamp=_stamp(execution_model_version="v1"),
        challenger_stamp=_stamp(execution_model_version="v2"),
    )

    with pytest.raises(ValueError, match="stamp"):
        build_paired_case_records(fold, replay)


def test_paired_case_record_surfaces_engine_version_and_scope_labels() -> None:
    case = _research_case(
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc), source_context_hash="h1"
    )
    fold = _fold("KSFO:2026-06-20", (case,))
    stamp = _stamp(execution_model_version="exec-v4-2026-07-17", side_scope="yes_only", fill_scope="taker_only_no_tape")
    replay = _replay_evidence(
        fold_id=fold.fold_id,
        baseline_cases={"h1": _case_payload(tickers=[_ticker()], stamp=stamp)},
        challenger_cases={"h1": _case_payload(tickers=[_ticker()], stamp=stamp)},
        baseline_stamp=stamp,
        challenger_stamp=stamp,
    )

    records, _ = build_paired_case_records(fold, replay)

    assert records[0].execution_model_version == "exec-v4-2026-07-17"
    assert records[0].side_scope == "yes_only"
    assert records[0].fill_scope == "taker_only_no_tape"


def test_paired_case_record_carries_crps_and_brier_when_candidate_evidence_supplied() -> None:
    case = _research_case(
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc), source_context_hash="h1"
    )
    fold = _fold("KSFO:2026-06-20", (case,))
    replay = _replay_evidence(
        fold_id=fold.fold_id,
        baseline_cases={"h1": _case_payload(tickers=[_ticker()])},
        challenger_cases={"h1": _case_payload(tickers=[_ticker()])},
    )
    candidate = _candidate_evidence(
        fold_id=fold.fold_id,
        baseline_cases={"h1": _score_payload(crps=2.0, bracket_brier=0.3)},
        challenger_cases={"h1": _score_payload(crps=1.0, bracket_brier=0.1)},
    )

    records, _ = build_paired_case_records(fold, replay, candidate)

    assert records[0].baseline_crps == 2.0
    assert records[0].challenger_crps == 1.0
    assert records[0].baseline_brier == 0.3
    assert records[0].challenger_brier == 0.1


def test_paired_case_record_crps_is_none_without_candidate_evidence() -> None:
    case = _research_case(
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc), source_context_hash="h1"
    )
    fold = _fold("KSFO:2026-06-20", (case,))
    replay = _replay_evidence(
        fold_id=fold.fold_id,
        baseline_cases={"h1": _case_payload(tickers=[_ticker()])},
        challenger_cases={"h1": _case_payload(tickers=[_ticker()])},
    )

    records, _ = build_paired_case_records(fold, replay)

    assert records[0].baseline_crps is None
    assert records[0].challenger_crps is None


def test_paired_case_record_crps_is_none_when_score_itself_unavailable() -> None:
    case = _research_case(
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc), source_context_hash="h1"
    )
    fold = _fold("KSFO:2026-06-20", (case,))
    replay = _replay_evidence(
        fold_id=fold.fold_id,
        baseline_cases={"h1": _case_payload(tickers=[_ticker()])},
        challenger_cases={"h1": _case_payload(tickers=[_ticker()])},
    )
    candidate = _candidate_evidence(
        fold_id=fold.fold_id,
        baseline_cases={"h1": _score_payload(available=False)},
        challenger_cases={"h1": _score_payload(available=False)},
    )

    records, _ = build_paired_case_records(fold, replay, candidate)

    assert records[0].baseline_crps is None
    assert records[0].baseline_brier is None


def test_missing_case_from_one_side_is_a_coverage_exclusion_not_a_crash() -> None:
    case = _research_case(
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc), source_context_hash="h1"
    )
    fold = _fold("KSFO:2026-06-20", (case,))
    replay = _replay_evidence(
        fold_id=fold.fold_id,
        baseline_cases={"h1": _case_payload(tickers=[_ticker()])},
        challenger_cases={},  # hand-built, deliberately missing "h1"
    )

    records, exclusions = build_paired_case_records(fold, replay)

    assert records == ()
    assert exclusions[0].reason == "case_missing_from_fold_or_replay_evidence"


# ---------------------------------------------------------------------------
# build_paired_records_for_experiment: multi-fold plumbing, challenger filter
# ---------------------------------------------------------------------------


def test_build_paired_records_for_experiment_combines_every_fold_for_one_challenger() -> None:
    case1 = _research_case(
        target_date=date(2026, 6, 20),
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc),
        source_context_hash="h1",
    )
    fold1 = _fold("KSFO:2026-06-20", (case1,))
    case2 = _research_case(
        target_date=date(2026, 6, 21),
        settled_at=datetime(2026, 6, 22, 4, 0, tzinfo=timezone.utc),
        source_context_hash="h2",
    )
    fold2 = _fold("KSFO:2026-06-21", (case2,))

    replay1 = _replay_evidence(
        fold_id=fold1.fold_id, target_date=date(2026, 6, 20),
        baseline_cases={"h1": _case_payload(tickers=[_ticker()])},
        challenger_cases={"h1": _case_payload(tickers=[_ticker()])},
    )
    replay2 = _replay_evidence(
        fold_id=fold2.fold_id, target_date=date(2026, 6, 21),
        baseline_cases={"h2": _case_payload(tickers=[_ticker()])},
        challenger_cases={"h2": _case_payload(tickers=[_ticker()])},
    )
    # A different challenger's row for fold1 must be ignored, not merged in.
    other_replay = _replay_evidence(
        fold_id=fold1.fold_id, target_date=date(2026, 6, 20),
        challenger_key="google-runtime-fixed-v1",
        baseline_cases={"h1": _case_payload(tickers=[_ticker()])},
        challenger_cases={"h1": _case_payload(tickers=[_ticker()])},
    )

    records, exclusions = build_paired_records_for_experiment(
        [fold1, fold2],
        [replay1, replay2, other_replay],
        challenger_candidate_key="gaussian-pit-station-lead-v1",
    )

    assert exclusions == ()
    assert {r.fold_id for r in records} == {fold1.fold_id, fold2.fold_id}
    assert len(records) == 2


def test_build_paired_records_for_experiment_raises_without_matching_fold() -> None:
    replay = _replay_evidence(
        fold_id="KSFO:2026-06-20",
        baseline_cases={"h1": _case_payload(tickers=[_ticker()])},
        challenger_cases={"h1": _case_payload(tickers=[_ticker()])},
    )

    with pytest.raises(ValueError, match="fold_id"):
        build_paired_records_for_experiment(
            [], [replay], challenger_candidate_key="gaussian-pit-station-lead-v1"
        )


# ---------------------------------------------------------------------------
# daily_paired_evidence: Pacific-day bucketing, zero-fill retention
# ---------------------------------------------------------------------------


def test_daily_paired_evidence_retains_zero_fill_days_between_first_and_last_activity() -> None:
    records = [
        _record(pacific_day=date(2026, 1, 1), baseline_pnl=10.0, challenger_pnl=12.0),
        _record(pacific_day=date(2026, 1, 4), baseline_pnl=-5.0, challenger_pnl=-3.0),
    ]

    days = daily_paired_evidence(records)

    assert [d.pacific_day for d in days] == [
        date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4),
    ]
    zero_day = days[1]
    assert zero_day.case_count == 0
    assert zero_day.baseline_pnl == 0.0
    assert zero_day.challenger_pnl == 0.0
    assert zero_day.baseline_rejection_counts == {}
    assert zero_day.challenger_rejection_counts == {}


def test_daily_paired_evidence_aggregates_multiple_folds_on_the_same_pacific_day() -> None:
    records = [
        _record(fold_id="A:2026-06-20", pacific_day=date(2026, 6, 20), baseline_pnl=5.0, challenger_pnl=6.0),
        _record(fold_id="B:2026-06-20", pacific_day=date(2026, 6, 20), baseline_pnl=7.0, challenger_pnl=9.0),
    ]

    days = daily_paired_evidence(records)

    assert len(days) == 1
    assert days[0].case_count == 2
    assert days[0].baseline_pnl == pytest.approx(12.0)
    assert days[0].challenger_pnl == pytest.approx(15.0)


def test_daily_paired_evidence_empty_records_without_day_range_is_empty() -> None:
    assert daily_paired_evidence(()) == ()


def test_daily_paired_evidence_empty_records_with_explicit_day_range_fills_zero_days() -> None:
    days = daily_paired_evidence((), start_day=date(2026, 1, 1), end_day=date(2026, 1, 3))

    assert [d.pacific_day for d in days] == [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
    assert all(d.case_count == 0 for d in days)


def test_daily_paired_evidence_rejects_start_day_after_end_day() -> None:
    with pytest.raises(ValueError):
        daily_paired_evidence((), start_day=date(2026, 1, 5), end_day=date(2026, 1, 1))


def test_daily_paired_evidence_aggregates_rejection_counts_and_contracts() -> None:
    records = [
        _record(
            pacific_day=date(2026, 6, 20), baseline_filled=True, baseline_contracts=10.0,
            baseline_dollars_at_risk=2.0, baseline_rejections=(),
            challenger_rejections=("no_trade",),
        ),
        _record(
            fold_id="B:2026-06-20", pacific_day=date(2026, 6, 20), baseline_rejections=("no_trade",),
            challenger_rejections=("no_trade", "unfilled_expired"),
        ),
    ]

    days = daily_paired_evidence(records)

    assert days[0].baseline_filled_count == 1
    assert days[0].baseline_contracts == 10.0
    assert days[0].baseline_dollars_at_risk == pytest.approx(2.0)
    assert days[0].baseline_rejection_counts == {"no_trade": 1}
    assert days[0].challenger_rejection_counts == {"no_trade": 2, "unfilled_expired": 1}


# ---------------------------------------------------------------------------
# arm_daily_kpis: every required metric, hand-computed
# ---------------------------------------------------------------------------


def test_arm_daily_kpis_mean_median_stdev_positive_day_rate_and_hit_rate() -> None:
    days = [
        _day(pacific_day=date(2026, 1, 1), baseline_pnl=100.0),
        _day(pacific_day=date(2026, 1, 2), baseline_pnl=-50.0),
        _day(pacific_day=date(2026, 1, 3), baseline_pnl=60.0),
    ]

    kpis = arm_daily_kpis(days, arm="baseline", reference_equity=1000.0, target_pnl=50.0)

    assert kpis.observed_days == 3
    assert kpis.mean_daily_pnl == pytest.approx((100.0 - 50.0 + 60.0) / 3.0)
    assert kpis.median_daily_pnl == pytest.approx(60.0)
    import statistics as _stats
    assert kpis.stdev_daily_pnl == pytest.approx(_stats.pstdev([100.0, -50.0, 60.0]))
    # Positive days: Jan 1 (100) and Jan 3 (60) -> 2/3.
    assert kpis.positive_day_rate == pytest.approx(2.0 / 3.0)
    # $50 hit-rate: Jan 1 (100>=50) and Jan 3 (60>=50) -> 2/3; Jan 2 misses.
    assert kpis.target_hit_rate == pytest.approx(2.0 / 3.0)


def test_arm_daily_kpis_after_fee_roi_is_total_pnl_over_reference_equity() -> None:
    days = [
        _day(pacific_day=date(2026, 1, 1), baseline_pnl=40.0),
        _day(pacific_day=date(2026, 1, 2), baseline_pnl=-10.0),
    ]

    kpis = arm_daily_kpis(days, arm="baseline", reference_equity=1000.0, target_pnl=50.0)

    assert kpis.realized_pnl_total == pytest.approx(30.0)
    assert kpis.after_fee_roi == pytest.approx(30.0 / 1000.0)


def test_arm_daily_kpis_drawdown_and_log_growth_match_hand_computed_values() -> None:
    daily_pnls = [100.0, -50.0, 30.0]
    days = [
        _day(pacific_day=date(2026, 1, 1), baseline_pnl=daily_pnls[0]),
        _day(pacific_day=date(2026, 1, 2), baseline_pnl=daily_pnls[1]),
        _day(pacific_day=date(2026, 1, 3), baseline_pnl=daily_pnls[2]),
    ]

    kpis = arm_daily_kpis(days, arm="baseline", reference_equity=1000.0, target_pnl=50.0)

    # Hand-computed equity path: 1000 -> 1100 (peak 1100) -> 1050
    # (drawdown $50 / 4.5454...%) -> 1080 (still under the $1100 peak,
    # drawdown $20 / 1.818...%). Maximum drawdown is the $50/4.5454...% step.
    expected_max_dd_dollars = 50.0
    expected_max_dd_pct = 50.0 / 1100.0
    expected_log_growth = math.log(1100.0 / 1000.0) + math.log(1050.0 / 1100.0) + math.log(1080.0 / 1050.0)

    assert kpis.maximum_drawdown_dollars == pytest.approx(expected_max_dd_dollars)
    assert kpis.maximum_drawdown_pct == pytest.approx(expected_max_dd_pct)
    assert kpis.log_growth == pytest.approx(expected_log_growth)
    assert kpis.log_growth_per_day == pytest.approx(expected_log_growth / 3.0)


def test_arm_daily_kpis_log_growth_is_none_when_equity_goes_non_positive() -> None:
    days = [_day(pacific_day=date(2026, 1, 1), baseline_pnl=-1500.0)]

    kpis = arm_daily_kpis(days, arm="baseline", reference_equity=1000.0, target_pnl=50.0)

    assert kpis.log_growth is None
    assert kpis.log_growth_per_day is None


def test_arm_daily_kpis_turnover_ratio_is_dollars_at_risk_over_reference_equity() -> None:
    days = [
        _day(pacific_day=date(2026, 1, 1), baseline_dollars_at_risk=100.0),
        _day(pacific_day=date(2026, 1, 2), baseline_dollars_at_risk=150.0),
    ]

    kpis = arm_daily_kpis(days, arm="baseline", reference_equity=1000.0, target_pnl=50.0)

    assert kpis.dollars_at_risk == pytest.approx(250.0)
    assert kpis.turnover_ratio == pytest.approx(0.25)


def test_arm_daily_kpis_fills_contracts_and_rejection_counts_sum_across_days() -> None:
    days = [
        _day(pacific_day=date(2026, 1, 1), baseline_filled_count=2, baseline_contracts=20.0,
             baseline_rejection_counts={"no_trade": 1}),
        _day(pacific_day=date(2026, 1, 2), baseline_filled_count=1, baseline_contracts=5.0,
             baseline_rejection_counts={"no_trade": 2, "unfilled_expired": 1}),
    ]

    kpis = arm_daily_kpis(days, arm="baseline", reference_equity=1000.0, target_pnl=50.0)

    assert kpis.fills == 3
    assert kpis.contracts == pytest.approx(25.0)
    assert kpis.rejection_counts == {"no_trade": 3, "unfilled_expired": 1}


def test_arm_daily_kpis_zero_activity_days_counted_from_zero_pnl() -> None:
    days = [
        _day(pacific_day=date(2026, 1, 1), baseline_pnl=0.0),
        _day(pacific_day=date(2026, 1, 2), baseline_pnl=5.0),
    ]

    kpis = arm_daily_kpis(days, arm="baseline", reference_equity=1000.0, target_pnl=50.0)

    assert kpis.zero_activity_days == 1


def test_arm_daily_kpis_empty_day_range_returns_none_rates_not_a_crash() -> None:
    kpis = arm_daily_kpis((), arm="baseline", reference_equity=1000.0, target_pnl=50.0)

    assert kpis.observed_days == 0
    assert kpis.mean_daily_pnl is None
    assert kpis.positive_day_rate is None
    assert kpis.target_hit_rate is None


def test_arm_daily_kpis_rejects_non_positive_reference_equity() -> None:
    with pytest.raises(ValueError):
        arm_daily_kpis((), arm="baseline", reference_equity=0.0, target_pnl=50.0)


# ---------------------------------------------------------------------------
# arm_kpi_delta: "positive always means the challenger arm improved"
# ---------------------------------------------------------------------------


def test_arm_kpi_delta_is_challenger_minus_baseline_for_pnl_like_metrics() -> None:
    baseline = arm_daily_kpis(
        [_day(pacific_day=date(2026, 1, 1), baseline_pnl=10.0)],
        arm="baseline", reference_equity=1000.0, target_pnl=50.0,
    )
    challenger = arm_daily_kpis(
        [_day(pacific_day=date(2026, 1, 1), challenger_pnl=25.0)],
        arm="challenger", reference_equity=1000.0, target_pnl=50.0,
    )

    delta = arm_kpi_delta(baseline, challenger)

    assert delta.mean_daily_pnl == pytest.approx(15.0)
    assert delta.after_fee_roi == pytest.approx(15.0 / 1000.0)


def test_arm_kpi_delta_drawdown_is_baseline_minus_challenger_so_positive_is_still_improvement() -> None:
    baseline = arm_daily_kpis(
        [_day(pacific_day=date(2026, 1, 1), baseline_pnl=-100.0)],
        arm="baseline", reference_equity=1000.0, target_pnl=50.0,
    )
    challenger = arm_daily_kpis(
        [_day(pacific_day=date(2026, 1, 1), challenger_pnl=-20.0)],
        arm="challenger", reference_equity=1000.0, target_pnl=50.0,
    )

    delta = arm_kpi_delta(baseline, challenger)

    # Baseline drew down $100, challenger only $20: challenger drew down
    # LESS, which must read as a positive ("improved") delta.
    assert delta.maximum_drawdown_dollars == pytest.approx(80.0)


# ---------------------------------------------------------------------------
# capacity: hand-computed target/motion envelope utilization
# ---------------------------------------------------------------------------


def test_sleeve_capacity_evidence_target_hand_computed() -> None:
    evidence = sleeve_capacity_evidence(100.0, TARGET_POLICY)

    # TARGET_POLICY: reference_equity=1000, max_aggregate_risk_pct=0.25.
    assert evidence.capacity_dollars == pytest.approx(250.0)
    assert evidence.utilization_pct == pytest.approx(0.4)
    assert evidence.capacity_remaining_dollars == pytest.approx(150.0)


def test_sleeve_capacity_evidence_motion_hand_computed() -> None:
    evidence = sleeve_capacity_evidence(100.0, MOTION_POLICY)

    # MOTION_POLICY: reference_equity=1000, max_aggregate_risk_pct=0.10.
    assert evidence.capacity_dollars == pytest.approx(100.0)
    assert evidence.utilization_pct == pytest.approx(1.0)
    assert evidence.capacity_remaining_dollars == pytest.approx(0.0)


def test_sleeve_capacity_evidence_can_exceed_full_utilization() -> None:
    evidence = sleeve_capacity_evidence(500.0, MOTION_POLICY)

    assert evidence.utilization_pct == pytest.approx(5.0)
    assert evidence.capacity_remaining_dollars == 0.0


def test_capacity_evidence_returns_both_declared_sleeves() -> None:
    evidence = capacity_evidence(50.0)

    assert set(evidence) == {"target", "motion"}
    assert evidence["target"].policy_version == TARGET_POLICY.policy_version
    assert evidence["motion"].policy_version == MOTION_POLICY.policy_version


# ---------------------------------------------------------------------------
# build_paired_evidence_report: end-to-end shape, S1/S2 surfacing
# ---------------------------------------------------------------------------


def test_build_paired_evidence_report_end_to_end_shape() -> None:
    case1 = _research_case(
        target_date=date(2026, 6, 20),
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc),
        source_context_hash="h1",
    )
    fold1 = _fold("KSFO:2026-06-20", (case1,))
    replay1 = _replay_evidence(
        fold_id=fold1.fold_id, target_date=date(2026, 6, 20),
        baseline_cases={"h1": _case_payload(realized_pnl=10.0, tickers=[_ticker(realized_pnl=10.0)])},
        challenger_cases={"h1": _case_payload(realized_pnl=15.0, tickers=[_ticker(realized_pnl=15.0)])},
    )
    candidate1 = _candidate_evidence(
        fold_id=fold1.fold_id, target_date=date(2026, 6, 20),
        baseline_cases={"h1": _score_payload(crps=2.0, bracket_brier=0.3)},
        challenger_cases={"h1": _score_payload(crps=1.0, bracket_brier=0.1)},
    )

    records, exclusions = build_paired_records_for_experiment(
        [fold1], [replay1], [candidate1], challenger_candidate_key="gaussian-pit-station-lead-v1"
    )
    report = build_paired_evidence_report(
        records, exclusions, challenger_candidate_key="gaussian-pit-station-lead-v1"
    )

    assert report.challenger_candidate_key == "gaussian-pit-station-lead-v1"
    assert report.paired_case_count == 1
    assert len(report.days) == 1
    assert report.baseline_kpis.realized_pnl_total == pytest.approx(10.0)
    assert report.challenger_kpis.realized_pnl_total == pytest.approx(15.0)
    assert report.kpi_delta.mean_daily_pnl == pytest.approx(5.0)
    assert report.baseline_capacity["target"].dollars_at_risk == report.baseline_kpis.dollars_at_risk
    assert report.challenger_capacity["motion"].dollars_at_risk == report.challenger_kpis.dollars_at_risk
    assert report.coverage_exclusions == ()
    # S1/S2: engine version and scope labels surfaced on the report.
    assert report.execution_model_versions == ("exec-v4-test",)
    assert report.side_scopes == ("yes_only",)
    assert report.fill_scopes == ("taker_only_no_tape",)


def test_build_paired_evidence_report_records_distinct_engine_versions_across_folds() -> None:
    case1 = _research_case(
        target_date=date(2026, 6, 20),
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc),
        source_context_hash="h1",
    )
    fold1 = _fold("KSFO:2026-06-20", (case1,))
    stamp_v1 = _stamp(execution_model_version="exec-v3")
    replay1 = _replay_evidence(
        fold_id=fold1.fold_id, target_date=date(2026, 6, 20),
        baseline_cases={"h1": _case_payload(tickers=[_ticker()], stamp=stamp_v1)},
        challenger_cases={"h1": _case_payload(tickers=[_ticker()], stamp=stamp_v1)},
        baseline_stamp=stamp_v1, challenger_stamp=stamp_v1,
    )

    case2 = _research_case(
        target_date=date(2026, 6, 21),
        settled_at=datetime(2026, 6, 22, 4, 0, tzinfo=timezone.utc),
        source_context_hash="h2",
    )
    fold2 = _fold("KSFO:2026-06-21", (case2,))
    stamp_v2 = _stamp(execution_model_version="exec-v4-2026-07-17")
    replay2 = _replay_evidence(
        fold_id=fold2.fold_id, target_date=date(2026, 6, 21),
        baseline_cases={"h2": _case_payload(tickers=[_ticker()], stamp=stamp_v2)},
        challenger_cases={"h2": _case_payload(tickers=[_ticker()], stamp=stamp_v2)},
        baseline_stamp=stamp_v2, challenger_stamp=stamp_v2,
    )

    records, exclusions = build_paired_records_for_experiment(
        [fold1, fold2], [replay1, replay2], challenger_candidate_key="gaussian-pit-station-lead-v1"
    )
    report = build_paired_evidence_report(
        records, exclusions, challenger_candidate_key="gaussian-pit-station-lead-v1"
    )

    assert report.execution_model_versions == ("exec-v3", "exec-v4-2026-07-17")


def test_build_paired_evidence_report_surfaces_coverage_exclusions_not_silently_dropped() -> None:
    case1 = _research_case(
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc), source_context_hash="h1"
    )
    fold1 = _fold("KSFO:2026-06-20", (case1,))
    replay1 = _replay_evidence(
        fold_id=fold1.fold_id,
        baseline_cases={"h1": _case_payload(tickers=[_ticker()])},
        challenger_cases={"h1": _case_payload(available=False, skip_reason="google_corroboration_blocked")},
    )

    records, exclusions = build_paired_records_for_experiment(
        [fold1], [replay1], challenger_candidate_key="gaussian-pit-station-lead-v1"
    )
    report = build_paired_evidence_report(
        records, exclusions, challenger_candidate_key="gaussian-pit-station-lead-v1"
    )

    assert report.paired_case_count == 0
    assert len(report.coverage_exclusions) == 1
    assert report.coverage_exclusions[0].reason == "challenger_unavailable"
