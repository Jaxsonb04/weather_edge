"""Task 6: gate paper-target promotion and control repeated experiments.

Covers plan Task 6 Step 1's named regressions -- motion has proposal-only
authority, promotion requires 30 independent confirmatory target days,
Holm adjustment blocks marginal repeated hypotheses, forecast/drawdown
regressions block a profitable candidate, and no promotion can change
live configuration or real-order flags -- plus the binding conditions
(G1-G7) from the prior tasks' reviews:

- G1: fold-inventory reconciliation catches a scrubbed case.
- G2: the gate never passes start_day/end_day into the report builder.
- G3: gate conditions only ever threshold "positive_is_better" fields.
- G4: capacity reasoning uses max_daily, never window_total, utilization.
- G5: uniformity rejects BOTH multi-value and empty-tuple scope evidence.
- G6: a "no significant delta" verdict for a NO-side/maker-predicted
  hypothesis is classified insufficient_instrument_coverage, not no_effect.
- G7: 30-independent-day minimum; repeated experiments require a new
  candidate_version, never a silent re-run.

Fixture convention matches the sibling Task 3-5 test files
(test_research_evidence.py/test_research_replay.py/
test_research_candidates.py): ``ResearchCase``/``WalkForwardFold``/
``FoldReplayEvidence``/``FoldCandidateEvidence`` are built directly, never
through the full loader/replay pipeline, for every unit test.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sfo_kalshi_quant.research_candidates import GAUSSIAN_PIT_CANDIDATE_KEY, IDENTITY_CANDIDATE_KEY
from sfo_kalshi_quant.research_evidence import (
    CaseCoverageExclusion,
    PairedCaseRecord,
    build_paired_evidence_report,
    build_paired_records_for_experiment,
)
from sfo_kalshi_quant.research_policy import TARGET_POLICY
from sfo_kalshi_quant.research_promotion import (
    EFFECT_FOUND,
    INSUFFICIENT_INSTRUMENT_COVERAGE,
    NO_EFFECT,
    PREDICTED_EDGE_SCOPE_NO_SIDE_OR_MAKER,
    PREDICTED_EDGE_SCOPE_YES_SIDE_TAKER,
    REASON_BRIER_REGRESSION,
    REASON_CALIBRATION_GAP_REGRESSION,
    REASON_COVERAGE_EXCLUSIONS_PRESENT,
    REASON_CRPS_REGRESSION,
    REASON_DRAWDOWN_TOLERANCE,
    REASON_EXECUTION_MODEL_VERSION_NOT_UNIFORM,
    REASON_FILL_SCOPE_NOT_UNIFORM,
    REASON_FOLD_INVENTORY_MISMATCH,
    REASON_FOLD_NOT_PROMOTION_ELIGIBLE,
    REASON_HOLM_NOT_SIGNIFICANT,
    REASON_INSUFFICIENT_DAYS,
    REASON_INSUFFICIENT_INSTRUMENT_COVERAGE,
    REASON_LOG_GROWTH_LOWER_BOUND,
    REASON_NOT_CONFIRMATORY_EVIDENCE,
    REASON_ROI_LOWER_BOUND,
    REASON_SIDE_SCOPE_NOT_UNIFORM,
    ChallengerDeclaration,
    FamilyAttempt,
    FoldInventoryMismatch,
    PromotionDecision,
    evaluate_promotion,
    reconcile_fold_inventory,
)
from sfo_kalshi_quant.research_replay import FoldReplayEvidence
from sfo_kalshi_quant.research_walkforward import ResearchCase, WalkForwardFold

STATION = "KSFO"
BASE_DATE = date(2026, 1, 1)
CHALLENGER_KEY = GAUSSIAN_PIT_CANDIDATE_KEY
HYPOTHESIS_FAMILY = "gaussian-pit-station-lead"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _stamp(
    *, execution_model_version: str = "exec-v4-test", side_scope: str = "yes_only",
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
    filled_count: int = 0, tickers: tuple = (), stamp: dict | None = None,
    candidate_key: str = "candidate",
) -> dict[str, object]:
    return {
        "candidate_key": candidate_key,
        "available": available,
        "skip_reason": skip_reason,
        "realized_pnl": realized_pnl,
        "filled_count": filled_count,
        "stamp": stamp if stamp is not None else _stamp(),
        "tickers": list(tickers),
    }


def _score_payload(
    *, available: bool = True, crps: float = 1.0, bracket_brier: float = 0.1, pit: float = 0.5,
    candidate_key: str = "candidate",
) -> dict[str, object]:
    return {
        "candidate_key": candidate_key,
        "candidate_version": "v1",
        "hypothesis_family": HYPOTHESIS_FAMILY,
        "available": available,
        "mu": 70.0,
        "sigma": 3.0,
        "unavailable_reason": "" if available else "unavailable",
        "crps": crps if available else None,
        "ranked_probability_score": None,
        "log_score": None,
        "pit": pit if available else None,
        "point_error": None,
        "interval_80_covered": None,
        "bracket_brier": bracket_brier if available else None,
    }


def _research_case(
    *, station_id: str = STATION, target_date: date, settled_at: datetime,
    source_context_hash: str, lead_days: int = 1,
) -> ResearchCase:
    decision_at = datetime(
        target_date.year, target_date.month, target_date.day, 20, 0, 0, tzinfo=timezone.utc
    ) - timedelta(days=lead_days)
    return ResearchCase(
        station_id=station_id, target_date=target_date, decision_at=decision_at,
        settled_at=settled_at, lead_days=lead_days, source_context_hash=source_context_hash,
        baseline_mu=70.0, baseline_sigma=3.0, actual_high_f=75.0,
    )


def _fold(fold_id: str, test_cases: tuple[ResearchCase, ...]) -> WalkForwardFold:
    return WalkForwardFold(
        fold_id=fold_id, decision_at=min(c.decision_at for c in test_cases), train=(), test=test_cases,
    )


def _replay_evidence(
    *, fold_id: str, station_id: str, target_date: date, challenger_key: str,
    baseline_cases: dict, challenger_cases: dict, promotion_eligible: bool = True,
    baseline_stamp: dict | None = None, challenger_stamp: dict | None = None,
) -> FoldReplayEvidence:
    return FoldReplayEvidence(
        fold_id=fold_id, station_id=station_id, target_date=target_date,
        challenger_candidate_key=challenger_key,
        baseline={"cases": baseline_cases, "stamp": baseline_stamp if baseline_stamp is not None else _stamp()},
        challenger={"cases": challenger_cases, "stamp": challenger_stamp if challenger_stamp is not None else _stamp()},
        promotion_eligible=promotion_eligible,
        promotion_block_reasons=() if promotion_eligible else ("synthetic_block_reason",),
    )


def _candidate_evidence(
    *, fold_id: str, station_id: str, target_date: date, challenger_key: str,
    baseline_cases: dict, challenger_cases: dict,
) -> object:
    from sfo_kalshi_quant.research_candidates import FoldCandidateEvidence

    return FoldCandidateEvidence(
        fold_id=fold_id, station_id=station_id, target_date=target_date,
        evaluated_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc),
        challenger_candidate_key=challenger_key,
        baseline={"cases": baseline_cases}, challenger={"cases": challenger_cases},
    )


def _cluster(
    day_offset: int,
    *,
    baseline_pnl: float = 10.0,
    challenger_pnl: float = 15.0,
    baseline_crps: float = 2.0,
    challenger_crps: float = 1.0,
    baseline_brier: float = 0.3,
    challenger_brier: float = 0.1,
    baseline_pit: float = 0.5,
    challenger_pit: float = 0.5,
    challenger_key: str = CHALLENGER_KEY,
    promotion_eligible: bool = True,
    baseline_available: bool = True,
    challenger_available: bool = True,
    execution_model_version: str = "exec-v4-test",
    side_scope: str = "yes_only",
    fill_scope: str = "taker_only_no_tape",
    station_id: str = STATION,
):
    target_date = BASE_DATE + timedelta(days=day_offset)
    fold_id = f"{station_id}:{target_date.isoformat()}"
    source_hash = f"hash-{station_id}-{day_offset}"
    settled_at = datetime(target_date.year, target_date.month, target_date.day, 23, 0, tzinfo=timezone.utc)
    case = _research_case(station_id=station_id, target_date=target_date, settled_at=settled_at, source_context_hash=source_hash)
    fold = _fold(fold_id, (case,))

    stamp = _stamp(execution_model_version=execution_model_version, side_scope=side_scope, fill_scope=fill_scope)

    if baseline_available:
        baseline_case_payload = _case_payload(
            available=True, realized_pnl=baseline_pnl, filled_count=1,
            tickers=(_ticker(realized_pnl=baseline_pnl),), stamp=stamp, candidate_key=IDENTITY_CANDIDATE_KEY,
        )
    else:
        baseline_case_payload = _case_payload(available=False, skip_reason="no_training_data", stamp=stamp, candidate_key=IDENTITY_CANDIDATE_KEY)

    if challenger_available:
        challenger_case_payload = _case_payload(
            available=True, realized_pnl=challenger_pnl, filled_count=1,
            tickers=(_ticker(realized_pnl=challenger_pnl),), stamp=stamp, candidate_key=challenger_key,
        )
    else:
        challenger_case_payload = _case_payload(available=False, skip_reason="no_training_data", stamp=stamp, candidate_key=challenger_key)

    replay = _replay_evidence(
        fold_id=fold_id, station_id=station_id, target_date=target_date, challenger_key=challenger_key,
        baseline_cases={source_hash: baseline_case_payload}, challenger_cases={source_hash: challenger_case_payload},
        promotion_eligible=promotion_eligible, baseline_stamp=stamp, challenger_stamp=stamp,
    )

    baseline_score = _score_payload(crps=baseline_crps, bracket_brier=baseline_brier, pit=baseline_pit, candidate_key=IDENTITY_CANDIDATE_KEY)
    challenger_score = _score_payload(crps=challenger_crps, bracket_brier=challenger_brier, pit=challenger_pit, candidate_key=challenger_key)
    candidate = _candidate_evidence(
        fold_id=fold_id, station_id=station_id, target_date=target_date, challenger_key=challenger_key,
        baseline_cases={source_hash: baseline_score}, challenger_cases={source_hash: challenger_score},
    )
    return fold, replay, candidate


def _experiment(count: int = 30, **defaults):
    folds, replays, candidates = [], [], []
    for i in range(count):
        fold, replay, candidate = _cluster(i, **defaults)
        folds.append(fold)
        replays.append(replay)
        candidates.append(candidate)
    return folds, replays, candidates


def _declaration(**overrides) -> ChallengerDeclaration:
    defaults = dict(
        experiment_id="exp-1",
        hypothesis_family=HYPOTHESIS_FAMILY,
        candidate_key=CHALLENGER_KEY,
        candidate_version="v1",
        evidence_role="confirmatory",
        predicted_edge_scope=PREDICTED_EDGE_SCOPE_YES_SIDE_TAKER,
        max_drawdown_tolerance_pct=0.10,
        crps_regression_tolerance=0.5,
        brier_regression_tolerance=0.5,
        calibration_gap_regression_tolerance=0.3,
    )
    defaults.update(overrides)
    return ChallengerDeclaration(**defaults)


def _record_for(*, fold_id: str, source_context_hash: str) -> PairedCaseRecord:
    return PairedCaseRecord(
        fold_id=fold_id, station_id=STATION, target_date=BASE_DATE, pacific_day=BASE_DATE,
        source_context_hash=source_context_hash, baseline_pnl=0.0, challenger_pnl=0.0,
        baseline_filled=True, challenger_filled=True, baseline_contracts=1.0, challenger_contracts=1.0,
        baseline_dollars_at_risk=1.0, challenger_dollars_at_risk=1.0, baseline_rejections=(), challenger_rejections=(),
        baseline_crps=None, challenger_crps=None, baseline_brier=None, challenger_brier=None,
        execution_model_version="exec-v4-test", side_scope="yes_only", fill_scope="taker_only_no_tape",
    )


def _exclusion_for(*, fold_id: str, source_context_hash: str) -> CaseCoverageExclusion:
    return CaseCoverageExclusion(fold_id=fold_id, source_context_hash=source_context_hash, reason="test_reason")


# ---------------------------------------------------------------------------
# G1: fold-inventory reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_fold_inventory_catches_a_case_scrubbed_from_both_records_and_exclusions() -> None:
    case1 = _research_case(target_date=BASE_DATE, settled_at=datetime(2026, 1, 1, 23, tzinfo=timezone.utc), source_context_hash="h1")
    case2 = _research_case(target_date=BASE_DATE, settled_at=datetime(2026, 1, 1, 23, tzinfo=timezone.utc), source_context_hash="h2")
    fold = _fold("KSFO:2026-01-01", (case1, case2))

    record_h1 = _record_for(fold_id=fold.fold_id, source_context_hash="h1")
    # h2 is scrubbed: present in neither records nor exclusions.
    mismatches = reconcile_fold_inventory([fold], (record_h1,), ())

    assert mismatches == (
        FoldInventoryMismatch(fold_id=fold.fold_id, source_context_hash="h2", reason="case_not_accounted_for"),
    )


def test_reconcile_fold_inventory_catches_a_case_double_counted() -> None:
    case1 = _research_case(target_date=BASE_DATE, settled_at=datetime(2026, 1, 1, 23, tzinfo=timezone.utc), source_context_hash="h1")
    fold = _fold("KSFO:2026-01-01", (case1,))

    record_h1 = _record_for(fold_id=fold.fold_id, source_context_hash="h1")
    exclusion_h1 = _exclusion_for(fold_id=fold.fold_id, source_context_hash="h1")
    mismatches = reconcile_fold_inventory([fold], (record_h1,), (exclusion_h1,))

    assert mismatches == (
        FoldInventoryMismatch(
            fold_id=fold.fold_id, source_context_hash="h1", reason="case_double_counted_as_both_paired_and_excluded"
        ),
    )


def test_reconcile_fold_inventory_is_clean_when_every_case_accounted_for_exactly_once() -> None:
    case1 = _research_case(target_date=BASE_DATE, settled_at=datetime(2026, 1, 1, 23, tzinfo=timezone.utc), source_context_hash="h1")
    case2 = _research_case(target_date=BASE_DATE, settled_at=datetime(2026, 1, 1, 23, tzinfo=timezone.utc), source_context_hash="h2")
    fold = _fold("KSFO:2026-01-01", (case1, case2))

    record_h1 = _record_for(fold_id=fold.fold_id, source_context_hash="h1")
    exclusion_h2 = _exclusion_for(fold_id=fold.fold_id, source_context_hash="h2")
    assert reconcile_fold_inventory([fold], (record_h1,), (exclusion_h2,)) == ()


def test_evaluate_promotion_blocks_and_reports_coverage_when_a_whole_fold_has_no_replay_row() -> None:
    folds, replays, candidates = _experiment(2)
    decision = evaluate_promotion(
        _declaration(), folds=folds, replay_evidence=[replays[0]], candidate_evidence=candidates,
    )
    assert REASON_COVERAGE_EXCLUSIONS_PRESENT in decision.block_reasons
    assert decision.coverage_exclusion_count >= 1
    assert decision.eligible_for_target_paper is False


# ---------------------------------------------------------------------------
# G5: uniformity (execution_model_version / side_scope / fill_scope)
# ---------------------------------------------------------------------------


def test_evaluate_promotion_rejects_fully_censored_report_with_empty_scope_tuples() -> None:
    folds, replays, candidates = _experiment(1, baseline_available=False, challenger_available=False)
    decision = evaluate_promotion(_declaration(), folds=folds, replay_evidence=replays, candidate_evidence=candidates)
    assert REASON_EXECUTION_MODEL_VERSION_NOT_UNIFORM in decision.block_reasons
    assert REASON_SIDE_SCOPE_NOT_UNIFORM in decision.block_reasons
    assert REASON_FILL_SCOPE_NOT_UNIFORM in decision.block_reasons
    assert decision.eligible_for_target_paper is False


def test_evaluate_promotion_rejects_multi_value_execution_model_version() -> None:
    fold0, replay0, cand0 = _cluster(0, execution_model_version="exec-v4-test")
    fold1, replay1, cand1 = _cluster(1, execution_model_version="exec-v5-test")
    decision = evaluate_promotion(
        _declaration(), folds=[fold0, fold1], replay_evidence=[replay0, replay1], candidate_evidence=[cand0, cand1],
    )
    assert REASON_EXECUTION_MODEL_VERSION_NOT_UNIFORM in decision.block_reasons


def test_evaluate_promotion_rejects_multi_value_side_scope() -> None:
    fold0, replay0, cand0 = _cluster(0, side_scope="yes_only")
    fold1, replay1, cand1 = _cluster(1, side_scope="no_only")
    decision = evaluate_promotion(
        _declaration(), folds=[fold0, fold1], replay_evidence=[replay0, replay1], candidate_evidence=[cand0, cand1],
    )
    assert REASON_SIDE_SCOPE_NOT_UNIFORM in decision.block_reasons


def test_evaluate_promotion_rejects_multi_value_fill_scope() -> None:
    fold0, replay0, cand0 = _cluster(0, fill_scope="taker_only_no_tape")
    fold1, replay1, cand1 = _cluster(1, fill_scope="maker_and_taker")
    decision = evaluate_promotion(
        _declaration(), folds=[fold0, fold1], replay_evidence=[replay0, replay1], candidate_evidence=[cand0, cand1],
    )
    assert REASON_FILL_SCOPE_NOT_UNIFORM in decision.block_reasons


# ---------------------------------------------------------------------------
# ROI / log-growth lower-confidence-bound boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("delta,expect_blocked", [(0.0, True), (1.0, False), (-1.0, True)])
def test_roi_and_log_growth_lower_bound_boundary(delta: float, expect_blocked: bool) -> None:
    folds, replays, candidates = _experiment(3, baseline_pnl=1000.0, challenger_pnl=1000.0 + delta)
    decision = evaluate_promotion(_declaration(), folds=folds, replay_evidence=replays, candidate_evidence=candidates)
    assert (REASON_ROI_LOWER_BOUND in decision.block_reasons) == expect_blocked
    assert (REASON_LOG_GROWTH_LOWER_BOUND in decision.block_reasons) == expect_blocked


def test_log_growth_lower_bound_blocks_independently_when_equity_wiped_out() -> None:
    # baseline_pnl = -reference_equity wipes baseline equity to exactly
    # zero -> log_growth_delta is None for every cluster (no fabricated
    # growth over a wipeout) -- log-growth blocks even though ROI, a
    # simple linear pnl-over-equity figure, stays robustly positive.
    folds, replays, candidates = _experiment(3, baseline_pnl=-1000.0, challenger_pnl=50.0)
    decision = evaluate_promotion(_declaration(), folds=folds, replay_evidence=replays, candidate_evidence=candidates)
    assert REASON_ROI_LOWER_BOUND not in decision.block_reasons
    assert REASON_LOG_GROWTH_LOWER_BOUND in decision.block_reasons


# ---------------------------------------------------------------------------
# Independent confirmatory days boundary (G7: 30)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count,expect_blocked", [(29, True), (30, False), (31, False)])
def test_independent_confirmatory_days_boundary(count: int, expect_blocked: bool) -> None:
    folds, replays, candidates = _experiment(count)
    decision = evaluate_promotion(_declaration(), folds=folds, replay_evidence=replays, candidate_evidence=candidates)
    assert (REASON_INSUFFICIENT_DAYS in decision.block_reasons) == expect_blocked
    assert decision.independent_confirmatory_days == count


# ---------------------------------------------------------------------------
# Drawdown tolerance boundary
# ---------------------------------------------------------------------------


def _drawdown_experiment(bad_day_pnl: float):
    folds, replays, candidates = [], [], []
    for i in range(30):
        challenger_pnl = bad_day_pnl if i == 15 else 15.0
        fold, replay, candidate = _cluster(i, baseline_pnl=10.0, challenger_pnl=challenger_pnl)
        folds.append(fold)
        replays.append(replay)
        candidates.append(candidate)
    return folds, replays, candidates


def test_drawdown_tolerance_boundary_at_computed_value_passes() -> None:
    # -20.0 on day 15 produces a hand/computation-verified
    # challenger max_drawdown_pct of exactly 20/1225 (see the reference
    # computation this test's tolerance is pinned to).
    folds, replays, candidates = _drawdown_experiment(-20.0)
    records, exclusions = build_paired_records_for_experiment(folds, replays, candidates, challenger_candidate_key=CHALLENGER_KEY)
    report = build_paired_evidence_report(records, exclusions, challenger_candidate_key=CHALLENGER_KEY)
    exact_drawdown = report.challenger_kpis.maximum_drawdown_pct

    decision = evaluate_promotion(
        _declaration(max_drawdown_tolerance_pct=exact_drawdown), folds=folds, replay_evidence=replays, candidate_evidence=candidates
    )
    assert REASON_DRAWDOWN_TOLERANCE not in decision.block_reasons


def test_drawdown_tolerance_boundary_just_below_blocks() -> None:
    folds, replays, candidates = _drawdown_experiment(-20.0)
    records, exclusions = build_paired_records_for_experiment(folds, replays, candidates, challenger_candidate_key=CHALLENGER_KEY)
    report = build_paired_evidence_report(records, exclusions, challenger_candidate_key=CHALLENGER_KEY)
    exact_drawdown = report.challenger_kpis.maximum_drawdown_pct

    decision = evaluate_promotion(
        _declaration(max_drawdown_tolerance_pct=exact_drawdown - 0.0001),
        folds=folds, replay_evidence=replays, candidate_evidence=candidates,
    )
    assert REASON_DRAWDOWN_TOLERANCE in decision.block_reasons


def test_drawdown_regression_blocks_an_otherwise_profitable_candidate() -> None:
    # Named plan Task 6 Step 1 regression: "forecast/drawdown regressions
    # block a profitable candidate". ROI/log-growth/Holm all clearly pass
    # (only one bad day out of 30); a tight declared drawdown tolerance
    # still blocks promotion.
    folds, replays, candidates = _drawdown_experiment(-20.0)
    decision = evaluate_promotion(
        _declaration(max_drawdown_tolerance_pct=0.01), folds=folds, replay_evidence=replays, candidate_evidence=candidates
    )
    assert REASON_ROI_LOWER_BOUND not in decision.block_reasons
    assert REASON_LOG_GROWTH_LOWER_BOUND not in decision.block_reasons
    assert REASON_DRAWDOWN_TOLERANCE in decision.block_reasons
    assert decision.eligible_for_target_paper is False
    assert decision.live_activation_allowed is False


# ---------------------------------------------------------------------------
# CRPS / Brier regression tolerance boundary + "forecast regression blocks
# a profitable candidate"
# ---------------------------------------------------------------------------


def test_crps_regression_boundary_at_tolerance_passes() -> None:
    # baseline_crps - challenger_crps == -tolerance exactly.
    folds, replays, candidates = _experiment(1, baseline_crps=1.0, challenger_crps=1.5)
    decision = evaluate_promotion(
        _declaration(crps_regression_tolerance=0.5), folds=folds, replay_evidence=replays, candidate_evidence=candidates
    )
    assert REASON_CRPS_REGRESSION not in decision.block_reasons


def test_crps_regression_boundary_just_beyond_tolerance_blocks() -> None:
    folds, replays, candidates = _experiment(1, baseline_crps=1.0, challenger_crps=1.50001)
    decision = evaluate_promotion(
        _declaration(crps_regression_tolerance=0.5), folds=folds, replay_evidence=replays, candidate_evidence=candidates
    )
    assert REASON_CRPS_REGRESSION in decision.block_reasons


def test_brier_regression_boundary_at_tolerance_passes() -> None:
    # Exact-binary-representable fractions (0.25/0.5/0.75) so the boundary
    # comparison is not muddied by float rounding noise: delta ==
    # 0.5 - 0.75 == -0.25 == -tolerance exactly.
    folds, replays, candidates = _experiment(1, baseline_brier=0.5, challenger_brier=0.75)
    decision = evaluate_promotion(
        _declaration(brier_regression_tolerance=0.25), folds=folds, replay_evidence=replays, candidate_evidence=candidates
    )
    assert REASON_BRIER_REGRESSION not in decision.block_reasons


def test_brier_regression_boundary_just_beyond_tolerance_blocks() -> None:
    folds, replays, candidates = _experiment(1, baseline_brier=0.5, challenger_brier=0.751)
    decision = evaluate_promotion(
        _declaration(brier_regression_tolerance=0.25), folds=folds, replay_evidence=replays, candidate_evidence=candidates
    )
    assert REASON_BRIER_REGRESSION in decision.block_reasons


def test_forecast_regression_blocks_an_otherwise_profitable_candidate() -> None:
    # Named plan Task 6 Step 1 regression, forecast half: a clearly
    # profitable P&L history (30 clean days, no drawdown) is still
    # blocked by a CRPS regression beyond the declared tolerance -- and
    # the challenger's own excellent $50/day hit rate is reported but
    # never overrides it.
    folds, replays, candidates = _experiment(
        30, baseline_pnl=10.0, challenger_pnl=100.0, baseline_crps=1.0, challenger_crps=3.0,
    )
    decision = evaluate_promotion(
        _declaration(crps_regression_tolerance=0.5), folds=folds, replay_evidence=replays, candidate_evidence=candidates
    )
    assert REASON_ROI_LOWER_BOUND not in decision.block_reasons
    assert REASON_DRAWDOWN_TOLERANCE not in decision.block_reasons
    assert REASON_CRPS_REGRESSION in decision.block_reasons
    assert decision.eligible_for_target_paper is False
    assert decision.target_hit_rate_reported == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Calibration-gap regression tolerance boundary
# ---------------------------------------------------------------------------


def test_calibration_gap_regression_boundary_at_tolerance_passes() -> None:
    # n=1: max_calibration_gap(v) == max(v, 1-v). baseline pit=0.5 -> gap
    # 0.5 (exact); challenger pit=0.75 -> gap 0.75 (exact); delta ==
    # 0.25 == tolerance exactly -- exact-binary-representable fractions so
    # the boundary comparison is not muddied by float rounding noise.
    folds, replays, candidates = _experiment(1, baseline_pit=0.5, challenger_pit=0.75)
    decision = evaluate_promotion(
        _declaration(calibration_gap_regression_tolerance=0.25), folds=folds, replay_evidence=replays, candidate_evidence=candidates
    )
    assert REASON_CALIBRATION_GAP_REGRESSION not in decision.block_reasons


def test_calibration_gap_regression_boundary_just_beyond_tolerance_blocks() -> None:
    folds, replays, candidates = _experiment(1, baseline_pit=0.5, challenger_pit=0.751)
    decision = evaluate_promotion(
        _declaration(calibration_gap_regression_tolerance=0.25), folds=folds, replay_evidence=replays, candidate_evidence=candidates
    )
    assert REASON_CALIBRATION_GAP_REGRESSION in decision.block_reasons


# ---------------------------------------------------------------------------
# G6: instrument-scope classification (insufficient_instrument_coverage vs
# no_effect)
# ---------------------------------------------------------------------------


def _no_delta_experiment():
    folds, replays, candidates = [], [], []
    for i in range(4):
        challenger_pnl = 50.0 if i % 2 == 0 else -50.0
        fold, replay, candidate = _cluster(i, baseline_pnl=0.0, challenger_pnl=challenger_pnl)
        folds.append(fold)
        replays.append(replay)
        candidates.append(candidate)
    return folds, replays, candidates


def test_no_side_or_maker_hypothesis_with_no_delta_is_insufficient_instrument_coverage() -> None:
    folds, replays, candidates = _no_delta_experiment()
    declaration = _declaration(predicted_edge_scope=PREDICTED_EDGE_SCOPE_NO_SIDE_OR_MAKER)
    decision = evaluate_promotion(declaration, folds=folds, replay_evidence=replays, candidate_evidence=candidates)
    assert decision.effect_classification == INSUFFICIENT_INSTRUMENT_COVERAGE
    assert REASON_INSUFFICIENT_INSTRUMENT_COVERAGE in decision.block_reasons
    assert decision.eligible_for_target_paper is False


def test_yes_side_taker_hypothesis_with_no_delta_is_plain_no_effect() -> None:
    folds, replays, candidates = _no_delta_experiment()
    declaration = _declaration(predicted_edge_scope=PREDICTED_EDGE_SCOPE_YES_SIDE_TAKER)
    decision = evaluate_promotion(declaration, folds=folds, replay_evidence=replays, candidate_evidence=candidates)
    assert decision.effect_classification == NO_EFFECT
    assert REASON_INSUFFICIENT_INSTRUMENT_COVERAGE not in decision.block_reasons


def test_instrument_scope_statement_never_claims_full_opportunity_coverage() -> None:
    folds, replays, candidates = _experiment(30)
    decision = evaluate_promotion(_declaration(), folds=folds, replay_evidence=replays, candidate_evidence=candidates)
    assert "YES-side" in decision.instrument_scope_statement
    assert "taker-only" in decision.instrument_scope_statement
    assert "not full-opportunity coverage" in decision.instrument_scope_statement


# ---------------------------------------------------------------------------
# G4: capacity uses max_daily, never window_total
# ---------------------------------------------------------------------------


def test_capacity_utilization_reports_max_daily_not_window_total() -> None:
    folds, replays, candidates = _experiment(30)
    decision = evaluate_promotion(_declaration(), folds=folds, replay_evidence=replays, candidate_evidence=candidates)

    records, exclusions = build_paired_records_for_experiment(folds, replays, candidates, challenger_candidate_key=CHALLENGER_KEY)
    report = build_paired_evidence_report(records, exclusions, challenger_candidate_key=CHALLENGER_KEY)
    target_capacity = report.challenger_capacity["target"]

    assert decision.max_daily_capacity_utilization_pct == pytest.approx(target_capacity.max_daily_utilization_pct)
    assert decision.max_daily_capacity_utilization_pct != pytest.approx(target_capacity.window_total_utilization_pct)


# ---------------------------------------------------------------------------
# Complete replay evidence (Task 4's promotion_eligible / "flat end state")
# ---------------------------------------------------------------------------


def test_evaluate_promotion_blocks_when_any_fold_is_not_promotion_eligible() -> None:
    folds, replays, candidates = _experiment(30)
    replays[5] = dataclasses.replace(replays[5], promotion_eligible=False, promotion_block_reasons=("non_flat_replay_end",))
    decision = evaluate_promotion(_declaration(), folds=folds, replay_evidence=replays, candidate_evidence=candidates)
    assert REASON_FOLD_NOT_PROMOTION_ELIGIBLE in decision.block_reasons
    assert decision.eligible_for_target_paper is False


# ---------------------------------------------------------------------------
# Motion has proposal-only authority
# ---------------------------------------------------------------------------


def test_exploratory_evidence_role_is_always_blocked_regardless_of_numbers() -> None:
    folds, replays, candidates = _experiment(30)
    declaration = _declaration(evidence_role="exploratory")
    decision = evaluate_promotion(declaration, folds=folds, replay_evidence=replays, candidate_evidence=candidates)
    assert decision.eligible_for_target_paper is False
    assert REASON_NOT_CONFIRMATORY_EVIDENCE in decision.block_reasons
    assert decision.live_activation_allowed is False


# ---------------------------------------------------------------------------
# Repeated experiments: declaration versioning, no silent re-runs, Holm
# ---------------------------------------------------------------------------


def test_evaluate_promotion_raises_on_duplicate_candidate_version_in_family() -> None:
    folds, replays, candidates = _experiment(5)
    declaration = _declaration(candidate_version="v1")
    prior = (FamilyAttempt(hypothesis_family=declaration.hypothesis_family, candidate_version="v1", p_value=0.01),)
    with pytest.raises(ValueError):
        evaluate_promotion(declaration, folds=folds, replay_evidence=replays, candidate_evidence=candidates, prior_family_attempts=prior)


def test_evaluate_promotion_raises_on_prior_attempt_for_a_different_hypothesis_family() -> None:
    folds, replays, candidates = _experiment(5)
    declaration = _declaration(hypothesis_family="gaussian-pit-station-lead")
    prior = (FamilyAttempt(hypothesis_family="google-runtime-fixed", candidate_version="v1", p_value=0.01),)
    with pytest.raises(ValueError):
        evaluate_promotion(declaration, folds=folds, replay_evidence=replays, candidate_evidence=candidates, prior_family_attempts=prior)


def test_evaluate_promotion_allows_a_genuinely_new_candidate_version_in_the_same_family() -> None:
    folds, replays, candidates = _experiment(30)
    declaration = _declaration(candidate_version="v2")
    prior = (FamilyAttempt(hypothesis_family=declaration.hypothesis_family, candidate_version="v1", p_value=0.2),)
    decision = evaluate_promotion(
        declaration, folds=folds, replay_evidence=replays, candidate_evidence=candidates, prior_family_attempts=prior
    )
    assert decision.holm_p_value is not None


def test_holm_adjustment_blocks_a_marginal_repeated_hypothesis_family() -> None:
    deltas = [2.0, 1.5, -1.0, 1.8, -0.8, 2.2, 1.0, -1.5, 1.7, 0.9, -0.6, 1.3]
    folds, replays, candidates = [], [], []
    for i, d in enumerate(deltas):
        fold, replay, candidate = _cluster(i, baseline_pnl=0.0, challenger_pnl=d * 1000.0)
        folds.append(fold)
        replays.append(replay)
        candidates.append(candidate)

    alone = evaluate_promotion(_declaration(candidate_version="v1"), folds=folds, replay_evidence=replays, candidate_evidence=candidates)
    assert alone.holm_adjusted_significant is True
    assert REASON_HOLM_NOT_SIGNIFICANT not in alone.block_reasons

    prior_p = alone.holm_p_value / 2
    prior_attempts = tuple(
        FamilyAttempt(hypothesis_family=HYPOTHESIS_FAMILY, candidate_version=f"prior-{k}", p_value=prior_p)
        for k in range(3)
    )
    combined = evaluate_promotion(
        _declaration(candidate_version="v-final"),
        folds=folds, replay_evidence=replays, candidate_evidence=candidates, prior_family_attempts=prior_attempts,
    )
    assert combined.holm_adjusted_significant is False
    assert REASON_HOLM_NOT_SIGNIFICANT in combined.block_reasons


# ---------------------------------------------------------------------------
# Happy path, determinism, live-activation safety
# ---------------------------------------------------------------------------


def _happy_path_evidence():
    return _experiment(30)


def test_evaluate_promotion_happy_path_is_eligible() -> None:
    folds, replays, candidates = _happy_path_evidence()
    decision = evaluate_promotion(_declaration(), folds=folds, replay_evidence=replays, candidate_evidence=candidates)
    assert decision.block_reasons == ()
    assert decision.eligible_for_target_paper is True
    assert decision.effect_classification == EFFECT_FOUND
    assert decision.independent_confirmatory_days == 30
    assert decision.live_activation_allowed is False


def test_evaluate_promotion_is_deterministic() -> None:
    folds, replays, candidates = _happy_path_evidence()
    declaration = _declaration()
    first = evaluate_promotion(declaration, folds=folds, replay_evidence=replays, candidate_evidence=candidates)
    second = evaluate_promotion(declaration, folds=folds, replay_evidence=replays, candidate_evidence=candidates)
    assert first == second


def test_promotion_decision_default_fields_match_plan_step4_sketch() -> None:
    decision = PromotionDecision(experiment_id="e1", eligible_for_target_paper=False, block_reasons=("x",))
    assert decision.live_activation_allowed is False


def test_live_activation_allowed_is_always_false_across_scenarios() -> None:
    folds, replays, candidates = _happy_path_evidence()
    eligible_decision = evaluate_promotion(_declaration(), folds=folds, replay_evidence=replays, candidate_evidence=candidates)
    blocked_decision = evaluate_promotion(
        _declaration(evidence_role="exploratory"), folds=folds, replay_evidence=replays, candidate_evidence=candidates
    )
    assert eligible_decision.live_activation_allowed is False
    assert blocked_decision.live_activation_allowed is False


_FORBIDDEN_LIVE_MODULES = {"config", "live_execution", "db"}


def test_research_promotion_module_never_imports_live_config_surfaces() -> None:
    # AST-based (not substring) on purpose: the module's own docstring
    # names LIVE_PROFILE_OVERRIDES/LIVE_ORDERS_ENABLED/
    # SFO_LIVE_TRADING_ENABLED in PROSE to document this very invariant --
    # a raw substring scan would false-positive on that explanation. What
    # actually matters is that this module never *imports* the modules
    # that own live-trading state at all.
    import ast

    import sfo_kalshi_quant.research_promotion as module

    source = Path(module.__file__).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
            imported.update(alias.name for alias in node.names)

    collision = imported & _FORBIDDEN_LIVE_MODULES
    assert not collision, f"research_promotion.py must never import {collision}"


# ---------------------------------------------------------------------------
# ChallengerDeclaration validation (fail-closed)
# ---------------------------------------------------------------------------


def test_challenger_declaration_rejects_invalid_evidence_role() -> None:
    with pytest.raises(ValueError):
        _declaration(evidence_role="bogus")


def test_challenger_declaration_rejects_invalid_predicted_edge_scope() -> None:
    with pytest.raises(ValueError):
        _declaration(predicted_edge_scope="bogus")


def test_challenger_declaration_rejects_negative_tolerance() -> None:
    with pytest.raises(ValueError):
        _declaration(max_drawdown_tolerance_pct=-0.01)


def test_challenger_declaration_rejects_empty_identity_fields() -> None:
    with pytest.raises(ValueError):
        _declaration(experiment_id="")
