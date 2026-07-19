"""Task 6: gate paper-target promotion and control repeated experiments.

Sits on top of every prior task's frozen, reviewed surfaces and adds
nothing to any of them:

- ``research_walkforward.WalkForwardFold``/``UnavailableFold`` (Task 2) --
  the fold inventory this module reconciles paired evidence against (G1).
- ``research_replay.FoldReplayEvidence`` (Task 4) -- in particular its own
  ``promotion_eligible``/``promotion_block_reasons`` fields, which Task 4's
  own spec (plan Task 4 Step 3) already defines as covering "missing
  initial quote, missing side depth, time-traveling events, missing
  settlement, unknown partial-fill ordering, or non-flat replay end". This
  module trusts that signal as-is rather than re-deriving "flat end state"
  from scratch (Task 4 is a consumed, reviewed surface).
- ``research_candidates.FoldCandidateEvidence``/``candidate_calibration_gap``
  (Task 3) -- pooled PIT calibration-gap evidence for the regression check.
- ``research_evidence.build_paired_records_for_experiment``/
  ``build_paired_evidence_report`` (Task 5) -- paired KPI/capacity/coverage
  evidence and the ``DELTA_DIRECTIONS`` sign convention.
- ``research_bootstrap.fold_paired_aggregates``/``day_clustered_bootstrap``
  (Task 5) -- day-clustered paired deltas and their 95% CIs.
- ``research_significance`` (this task, split out alongside this module for
  the same file-size-cohesion reason Task 5 split ``research_bootstrap.py``
  out of ``research_evidence.py`` -- see that module's docstring) -- the
  Holm-Bonferroni family-wise correction and its one-sided bootstrap
  p-value.

Plan Task 6 Step 3, verbatim, is "Require all of": at least 30 independent
confirmatory target days; lower 95% paired after-fee ROI interval above
zero; lower 95% paired log-growth/day interval above zero; no worse
maximum drawdown than the declared tolerance; no CRPS, Brier, or maximum
calibration-gap regression beyond declared tolerances; complete exec-v3
replay evidence and a flat end state; Holm-adjusted significance within
the predeclared hypothesis family. The $50/day hit rate (``target_hit_rate``
on ``ArmDailyKpis``) is reported on every ``PromotionDecision`` but is
NEVER read by any gate condition below -- plan Step 3's own last sentence.

Binding-condition design notes (review-blocking if missed, so recorded
here rather than only in the implementer's own notes):

- G1: ``reconcile_fold_inventory`` independently re-derives, from ``folds``
  alone, every settled test case's ``(fold_id, source_context_hash)`` key
  and checks it appears in EXACTLY ONE of ``records`` (paired) or
  ``exclusions`` (excluded-with-reason) -- never trusting that whatever
  built those two sequences did so correctly. A case belonging to an
  ``UnavailableFold`` group never reaches ``folds`` at all (Task 2's own
  partition), so it is accounted for at the coarser "fold-unavailable"
  granularity via ``fold_unavailable_count`` instead of a per-case key;
  ``UnavailableFold`` carries no case-level detail to reconcile against
  (a genuine Task 2 API limitation, not something this module can recover).
- G2: ``evaluate_promotion`` calls ``build_paired_evidence_report`` with
  NO ``start_day``/``end_day`` -- the full observed-window default is the
  only honest form for a promotion verdict, and Task 5's own F2 guard
  raises (uncaught, deliberately) if that invariant is ever violated.
- G3: every threshold below that reads an ``ArmKpiDelta``-shaped or
  bootstrap-delta figure only ever thresholds a "positive_is_better"
  field (ROI, log-growth/day, CRPS "positive=improved", Brier
  "positive=improved", drawdown against an absolute declared ceiling).
  Nothing here thresholds ``stdev_daily_pnl``, ``turnover_ratio``,
  ``fills``, ``contracts``, or ``dollars_at_risk`` -- see
  ``research_evidence.DELTA_DIRECTIONS``.
- G4: ``max_daily_capacity_utilization_pct`` on ``PromotionDecision`` is
  sourced from ``SleeveCapacityEvidence.max_daily_utilization_pct`` (the
  concurrency-meaningful figure), never
  ``window_total_utilization_pct`` (F5 in ``research_evidence.py``).
- G5: uniformity is enforced on ALL THREE of ``execution_model_versions``/
  ``side_scopes``/``fill_scopes`` -- each must be a length-1 tuple.
  Length 0 (a fully-censored report with zero paired records) is REJECTED
  exactly like length > 1 (mixed evidence); see
  ``_uniform_scope_block_reasons``.
- G6: ``predicted_edge_scope`` on ``ChallengerDeclaration`` is this
  module's own new field (neither Task 1's ``research_experiments`` schema
  nor Task 3's ``declared_research_candidates()`` carry it) -- introduced
  here because ``research_replay.py``'s module docstring establishes that
  EVERY replay in this project only ever prices/sizes the YES side via an
  immediate/crossing taker match (``_SIDE = "YES"``, no public trade tape
  to resolve a resting maker fill). A challenger declared with
  ``PREDICTED_EDGE_SCOPE_NO_SIDE_OR_MAKER`` can therefore never be
  confirmed OR falsified by this pipeline's evidence -- a "no significant
  delta" result for one is classified ``insufficient_instrument_coverage``,
  never ``no_effect`` (see ``_effect_classification``).
- G7: ``MIN_INDEPENDENT_CONFIRMATORY_DAYS = 30`` (plan Task 6 Step 1 and
  spec Sec 8, both verbatim "30 independent ... days"). Repeated
  experiments: a ``candidate_version`` already present in
  ``prior_family_attempts`` for the same ``hypothesis_family`` raises
  ``ValueError`` (a silent re-run is a caller/integration bug, not a
  legitimate gate outcome -- mirrors Task 1's own immutable-declaration
  trigger, which raises rather than degrading gracefully). A genuinely NEW
  ``candidate_version`` is welcome, but its Holm-adjusted significance is
  computed against the WHOLE family's p-value history, not in isolation.

HARD CONSTRAINTS: this module never imports ``config``, ``live_execution``,
or ``db`` -- it has no way to read or write ``LIVE_PROFILE_OVERRIDES``,
``LIVE_ORDERS_ENABLED``, ``SFO_LIVE_TRADING_ENABLED``, any live fingerprint,
or any dry-run flag. ``PromotionDecision.live_activation_allowed`` is never
assigned anywhere in this file -- it only ever takes its dataclass default
(``False``), by construction, for every input this module can be called
with. Nothing here touches the database, the wall clock, or unseeded
random state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .research_bootstrap import (
    DEFAULT_BOOTSTRAP_DRAWS,
    DEFAULT_BOOTSTRAP_SEED,
    fold_paired_aggregates,
    day_clustered_bootstrap,
)
from .research_candidates import IDENTITY_CANDIDATE_KEY, FoldCandidateEvidence, candidate_calibration_gap
from .research_evidence import (
    CaseCoverageExclusion,
    PairedCaseRecord,
    PairedEvidenceReport,
    build_paired_evidence_report,
    build_paired_records_for_experiment,
)
from .research_replay import FoldReplayEvidence
from .research_significance import holm_bonferroni_significant, one_sided_bootstrap_p_value
from .research_walkforward import UnavailableFold, WalkForwardFold

# Plan Task 6 Step 1 / spec Sec 8: "at least 30 independent ... days".
MIN_INDEPENDENT_CONFIRMATORY_DAYS = 30

CONFIRMATORY_EVIDENCE_ROLE = "confirmatory"
EXPLORATORY_EVIDENCE_ROLE = "exploratory"
_VALID_EVIDENCE_ROLES = (EXPLORATORY_EVIDENCE_ROLE, CONFIRMATORY_EVIDENCE_ROLE)

# G6: what side/fill instrument this hypothesis predicts its edge shows up
# on. This project's replay evidence is ALWAYS yes-side/taker-only (see
# module docstring) -- "no_side_or_maker" therefore always falls outside
# what this pipeline's evidence can speak to, regardless of what the
# observed report's own (uniform) side_scope/fill_scope happen to be.
PREDICTED_EDGE_SCOPE_YES_SIDE_TAKER = "yes_side_taker"
PREDICTED_EDGE_SCOPE_NO_SIDE_OR_MAKER = "no_side_or_maker"
_VALID_PREDICTED_EDGE_SCOPES = (
    PREDICTED_EDGE_SCOPE_YES_SIDE_TAKER,
    PREDICTED_EDGE_SCOPE_NO_SIDE_OR_MAKER,
)

EFFECT_FOUND = "effect_found"
NO_EFFECT = "no_effect"
INSUFFICIENT_INSTRUMENT_COVERAGE = "insufficient_instrument_coverage"

REASON_NOT_CONFIRMATORY_EVIDENCE = "motion_or_exploratory_evidence_is_proposal_only"
REASON_FOLD_INVENTORY_MISMATCH = "fold_inventory_reconciliation_failed"
REASON_EXECUTION_MODEL_VERSION_NOT_UNIFORM = "execution_model_version_not_uniform"
REASON_SIDE_SCOPE_NOT_UNIFORM = "side_scope_not_uniform"
REASON_FILL_SCOPE_NOT_UNIFORM = "fill_scope_not_uniform"
REASON_COVERAGE_EXCLUSIONS_PRESENT = "incomplete_replay_evidence_coverage_exclusions_present"
REASON_FOLD_NOT_PROMOTION_ELIGIBLE = "incomplete_replay_evidence_promotion_ineligible_fold"
REASON_INSUFFICIENT_DAYS = "insufficient_independent_confirmatory_days"
REASON_ROI_LOWER_BOUND = "roi_lower_confidence_bound_not_above_zero"
REASON_LOG_GROWTH_LOWER_BOUND = "log_growth_lower_confidence_bound_not_above_zero"
REASON_DRAWDOWN_TOLERANCE = "maximum_drawdown_exceeds_declared_tolerance"
REASON_CRPS_UNAVAILABLE = "crps_regression_evidence_unavailable"
REASON_CRPS_REGRESSION = "crps_regression_exceeds_declared_tolerance"
REASON_BRIER_UNAVAILABLE = "brier_regression_evidence_unavailable"
REASON_BRIER_REGRESSION = "brier_regression_exceeds_declared_tolerance"
REASON_CALIBRATION_GAP_UNAVAILABLE = "calibration_gap_evidence_unavailable"
REASON_CALIBRATION_GAP_REGRESSION = "calibration_gap_regression_exceeds_declared_tolerance"
REASON_HOLM_NOT_SIGNIFICANT = "holm_adjusted_significance_not_reached"
REASON_INSUFFICIENT_INSTRUMENT_COVERAGE = "insufficient_instrument_coverage_for_declared_hypothesis"


@dataclass(frozen=True)
class ChallengerDeclaration:
    """One challenger's immutable, predeclared identity plus the
    regression tolerances it must be evaluated against -- ALL declared
    before this module ever sees paired evidence, matching Task 1's own
    "immutable after first evidence row" guarantee for
    ``research_experiments``.

    ``hypothesis_family``/``candidate_key``/``candidate_version``/
    ``evidence_role`` mirror ``research_experiments``' own schema columns
    (Task 1) exactly. ``predicted_edge_scope`` is new to this module (G6).
    Every tolerance is a non-negative, absolute ceiling declared once, up
    front -- never fit or adjusted after seeing this challenger's own
    evidence.
    """

    experiment_id: str
    hypothesis_family: str
    candidate_key: str
    candidate_version: str
    evidence_role: str
    predicted_edge_scope: str
    max_drawdown_tolerance_pct: float
    crps_regression_tolerance: float
    brier_regression_tolerance: float
    calibration_gap_regression_tolerance: float

    def __post_init__(self) -> None:
        if not self.experiment_id or not self.hypothesis_family:
            raise ValueError("experiment_id and hypothesis_family must be non-empty")
        if not self.candidate_key or not self.candidate_version:
            raise ValueError("candidate_key and candidate_version must be non-empty")
        if self.evidence_role not in _VALID_EVIDENCE_ROLES:
            raise ValueError(f"evidence_role must be one of {_VALID_EVIDENCE_ROLES}")
        if self.predicted_edge_scope not in _VALID_PREDICTED_EDGE_SCOPES:
            raise ValueError(f"predicted_edge_scope must be one of {_VALID_PREDICTED_EDGE_SCOPES}")
        for name in (
            "max_drawdown_tolerance_pct",
            "crps_regression_tolerance",
            "brier_regression_tolerance",
            "calibration_gap_regression_tolerance",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True)
class FamilyAttempt:
    """One earlier declared-and-evaluated attempt within the same
    ``hypothesis_family``, needed so a repeated experiment's
    Holm-adjusted significance accounts for the WHOLE family's history,
    not just its own single look (G7, plan Task 6 Step 1: "Holm adjustment
    blocks marginal repeated hypotheses"). Persisting this history across
    calls is a later task's (Task 7's) job -- this module only ever
    consumes it as a plain argument."""

    hypothesis_family: str
    candidate_version: str
    p_value: float


@dataclass(frozen=True)
class FoldInventoryMismatch:
    """One settled test case that G1 reconciliation could not account for
    exactly once across paired records and coverage exclusions."""

    fold_id: str
    source_context_hash: str
    reason: str


@dataclass(frozen=True)
class PromotionDecision:
    """Task 6's one output: a deterministic, fail-closed, structured
    verdict. Only ``experiment_id``/``eligible_for_target_paper``/
    ``block_reasons``/``live_activation_allowed`` are the plan's own Step 4
    sketch; every field after them is additional structured evidence this
    module's binding conditions require the payload to carry.

    ``live_activation_allowed`` is NEVER set to ``True`` anywhere in this
    module -- see the module docstring's HARD CONSTRAINTS paragraph."""

    experiment_id: str
    eligible_for_target_paper: bool
    block_reasons: tuple[str, ...]
    live_activation_allowed: bool = False
    effect_classification: str = NO_EFFECT
    instrument_scope_statement: str = ""
    independent_confirmatory_days: int = 0
    holm_p_value: float | None = None
    holm_adjusted_significant: bool = False
    target_hit_rate_reported: float | None = None
    fold_unavailable_count: int = 0
    coverage_exclusion_count: int = 0
    max_daily_capacity_utilization_pct: float | None = None


def reconcile_fold_inventory(
    folds: Sequence[WalkForwardFold],
    records: Sequence[PairedCaseRecord],
    exclusions: Sequence[CaseCoverageExclusion],
) -> tuple[FoldInventoryMismatch, ...]:
    """G1: independently re-verify every settled test case in ``folds``
    (Task 2's own fold inventory) is accounted for in EXACTLY ONE of
    ``records`` (paired) or ``exclusions`` (excluded-with-reason) --
    never trusts that whatever assembled ``records``/``exclusions`` did so
    correctly. A case present in neither (silently scrubbed) or in BOTH
    (double-counted) is reported as a ``FoldInventoryMismatch``; an empty
    return means full reconciliation."""

    paired_keys = {(r.fold_id, r.source_context_hash) for r in records}
    excluded_keys = {(e.fold_id, e.source_context_hash) for e in exclusions}

    mismatches: list[FoldInventoryMismatch] = []
    for fold in folds:
        for case in fold.test:
            key = (fold.fold_id, case.source_context_hash)
            in_paired = key in paired_keys
            in_excluded = key in excluded_keys
            if in_paired and in_excluded:
                mismatches.append(
                    FoldInventoryMismatch(
                        fold_id=fold.fold_id,
                        source_context_hash=case.source_context_hash,
                        reason="case_double_counted_as_both_paired_and_excluded",
                    )
                )
            elif not in_paired and not in_excluded:
                mismatches.append(
                    FoldInventoryMismatch(
                        fold_id=fold.fold_id,
                        source_context_hash=case.source_context_hash,
                        reason="case_not_accounted_for",
                    )
                )
    mismatches.sort(key=lambda m: (m.fold_id, m.source_context_hash))
    return tuple(mismatches)


def _uniform_scope_block_reasons(report: PairedEvidenceReport) -> list[str]:
    """G5: EXACTLY ONE value required for each of ``execution_model_versions``/
    ``side_scopes``/``fill_scopes`` -- rejects BOTH multi-value (mixed
    evidence) and empty-tuple (a fully-censored report with zero paired
    records), never just the multi-value case."""

    reasons: list[str] = []
    if len(report.execution_model_versions) != 1:
        reasons.append(REASON_EXECUTION_MODEL_VERSION_NOT_UNIFORM)
    if len(report.side_scopes) != 1:
        reasons.append(REASON_SIDE_SCOPE_NOT_UNIFORM)
    if len(report.fill_scopes) != 1:
        reasons.append(REASON_FILL_SCOPE_NOT_UNIFORM)
    return reasons


def _instrument_scope_matches(predicted_edge_scope: str, side_scope: str, fill_scope: str) -> bool:
    """G6: does this pipeline's ACTUAL replayed instrument scope cover
    what ``predicted_edge_scope`` claims the challenger's edge shows up
    on? ``research_replay.py`` only ever replays the YES side via an
    immediate/crossing taker match (see module docstring) -- a
    "no_side_or_maker" prediction can therefore never be matched by any
    observed scope this pipeline could ever produce."""

    if predicted_edge_scope == PREDICTED_EDGE_SCOPE_NO_SIDE_OR_MAKER:
        return False
    return side_scope == "yes_only" and fill_scope == "taker_only_no_tape"


def _instrument_scope_statement(report: PairedEvidenceReport) -> str:
    if len(report.side_scopes) != 1 or len(report.fill_scopes) != 1:
        return "instrument scope not uniform across evidence -- cannot state a single coverage scope"
    return (
        "YES-side, taker-only, no post-decision tape "
        f"(side_scope={report.side_scopes[0]!r}, fill_scope={report.fill_scopes[0]!r}) -- "
        "not full-opportunity coverage"
    )


def _effect_classification(
    *,
    roi_ok: bool,
    log_growth_ok: bool,
    declaration: ChallengerDeclaration,
    report: PairedEvidenceReport,
) -> tuple[str, list[str]]:
    """Returns ``(classification, extra_block_reasons)``. G6: a "no
    significant delta" result is only ever labeled ``no_effect`` when the
    declared hypothesis predicts an edge this pipeline's uniform,
    observed instrument scope could actually have shown; otherwise it is
    ``insufficient_instrument_coverage`` (a distinct, non-overridable
    block reason), never conflated with a genuine null result."""

    if roi_ok and log_growth_ok:
        return EFFECT_FOUND, []

    uniform_scope = len(report.side_scopes) == 1 and len(report.fill_scopes) == 1
    if uniform_scope and not _instrument_scope_matches(
        declaration.predicted_edge_scope, report.side_scopes[0], report.fill_scopes[0]
    ):
        return INSUFFICIENT_INSTRUMENT_COVERAGE, [REASON_INSUFFICIENT_INSTRUMENT_COVERAGE]
    return NO_EFFECT, []


def evaluate_promotion(
    declaration: ChallengerDeclaration,
    *,
    folds: Sequence[WalkForwardFold],
    unavailable_folds: Sequence[UnavailableFold] = (),
    replay_evidence: Sequence[FoldReplayEvidence],
    candidate_evidence: Sequence[FoldCandidateEvidence] = (),
    prior_family_attempts: Sequence[FamilyAttempt] = (),
) -> PromotionDecision:
    """Task 6's one gate entry point. Deterministic and fail-closed: every
    condition below is independently evaluated and accumulated into
    ``block_reasons`` (never short-circuited), so a caller always sees the
    FULL set of reasons a candidate is not yet promotable, not just the
    first one found.

    Raises ``ValueError`` (does not return a soft-failing
    ``PromotionDecision``) for a caller/integration bug that is not a
    legitimate evidentiary gate outcome: a ``prior_family_attempts`` entry
    for a different ``hypothesis_family``, or one that reuses
    ``declaration.candidate_version`` -- a silent repeated-experiment
    re-run (G7).
    """

    for attempt in prior_family_attempts:
        if attempt.hypothesis_family != declaration.hypothesis_family:
            raise ValueError(
                "prior_family_attempts entry for hypothesis_family "
                f"{attempt.hypothesis_family!r} does not match declaration's "
                f"{declaration.hypothesis_family!r}"
            )
        if attempt.candidate_version == declaration.candidate_version:
            raise ValueError(
                f"candidate_version {declaration.candidate_version!r} for hypothesis_family "
                f"{declaration.hypothesis_family!r} was already declared and evaluated in "
                "prior_family_attempts -- a repeated experiment must declare a NEW "
                "candidate_version, never silently re-run an existing one"
            )

    block_reasons: list[str] = []
    if declaration.evidence_role != CONFIRMATORY_EVIDENCE_ROLE:
        block_reasons.append(REASON_NOT_CONFIRMATORY_EVIDENCE)

    records, exclusions = build_paired_records_for_experiment(
        folds,
        replay_evidence,
        candidate_evidence,
        challenger_candidate_key=declaration.candidate_key,
    )

    if reconcile_fold_inventory(folds, records, exclusions):
        block_reasons.append(REASON_FOLD_INVENTORY_MISMATCH)

    # G2: full-window default only -- no start_day/end_day passed here.
    report = build_paired_evidence_report(
        records,
        exclusions,
        challenger_candidate_key=declaration.candidate_key,
    )

    block_reasons.extend(_uniform_scope_block_reasons(report))

    if report.coverage_exclusions:
        block_reasons.append(REASON_COVERAGE_EXCLUSIONS_PRESENT)

    relevant_replay_rows = [
        row for row in replay_evidence if row.challenger_candidate_key == declaration.candidate_key
    ]
    if any(not row.promotion_eligible for row in relevant_replay_rows):
        block_reasons.append(REASON_FOLD_NOT_PROMOTION_ELIGIBLE)

    aggregates = fold_paired_aggregates(records)
    independent_days = len(aggregates)
    if independent_days < MIN_INDEPENDENT_CONFIRMATORY_DAYS:
        block_reasons.append(REASON_INSUFFICIENT_DAYS)

    bootstrap_results = day_clustered_bootstrap(aggregates)
    roi_interval = bootstrap_results["roi"]
    log_growth_interval = bootstrap_results["log_growth_per_day"]
    roi_ok = roi_interval.lower is not None and roi_interval.lower > 0.0
    log_growth_ok = log_growth_interval.lower is not None and log_growth_interval.lower > 0.0
    if not roi_ok:
        block_reasons.append(REASON_ROI_LOWER_BOUND)
    if not log_growth_ok:
        block_reasons.append(REASON_LOG_GROWTH_LOWER_BOUND)

    if report.challenger_kpis.maximum_drawdown_pct > declaration.max_drawdown_tolerance_pct:
        block_reasons.append(REASON_DRAWDOWN_TOLERANCE)

    crps_interval = bootstrap_results["crps"]
    if crps_interval.point_estimate is None:
        block_reasons.append(REASON_CRPS_UNAVAILABLE)
    elif crps_interval.point_estimate < -declaration.crps_regression_tolerance:
        block_reasons.append(REASON_CRPS_REGRESSION)

    brier_interval = bootstrap_results["brier"]
    if brier_interval.point_estimate is None:
        block_reasons.append(REASON_BRIER_UNAVAILABLE)
    elif brier_interval.point_estimate < -declaration.brier_regression_tolerance:
        block_reasons.append(REASON_BRIER_REGRESSION)

    baseline_gap = candidate_calibration_gap(candidate_evidence, candidate_key=IDENTITY_CANDIDATE_KEY)
    challenger_gap = candidate_calibration_gap(candidate_evidence, candidate_key=declaration.candidate_key)
    if baseline_gap is None or challenger_gap is None:
        block_reasons.append(REASON_CALIBRATION_GAP_UNAVAILABLE)
    elif (challenger_gap - baseline_gap) > declaration.calibration_gap_regression_tolerance:
        block_reasons.append(REASON_CALIBRATION_GAP_REGRESSION)

    current_p_value = one_sided_bootstrap_p_value(
        [a.roi_delta for a in aggregates],
        seed=DEFAULT_BOOTSTRAP_SEED,
        draws=DEFAULT_BOOTSTRAP_DRAWS,
    )
    if current_p_value is None:
        holm_significant = False
        block_reasons.append(REASON_HOLM_NOT_SIGNIFICANT)
    else:
        family_p_values = [attempt.p_value for attempt in prior_family_attempts] + [current_p_value]
        holm_significant = holm_bonferroni_significant(family_p_values)[-1]
        if not holm_significant:
            block_reasons.append(REASON_HOLM_NOT_SIGNIFICANT)

    effect_classification, extra_reasons = _effect_classification(
        roi_ok=roi_ok, log_growth_ok=log_growth_ok, declaration=declaration, report=report
    )
    block_reasons.extend(extra_reasons)

    eligible = effect_classification == EFFECT_FOUND and not block_reasons

    target_capacity = report.challenger_capacity["target"]

    return PromotionDecision(
        experiment_id=declaration.experiment_id,
        eligible_for_target_paper=eligible,
        block_reasons=tuple(sorted(set(block_reasons))),
        effect_classification=effect_classification,
        instrument_scope_statement=_instrument_scope_statement(report),
        independent_confirmatory_days=independent_days,
        holm_p_value=current_p_value,
        holm_adjusted_significant=holm_significant,
        target_hit_rate_reported=report.challenger_kpis.target_hit_rate,
        fold_unavailable_count=len(unavailable_folds),
        coverage_exclusion_count=len(report.coverage_exclusions),
        max_daily_capacity_utilization_pct=target_capacity.max_daily_utilization_pct,
    )
