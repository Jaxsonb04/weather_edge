"""Task 5: paired daily and capacity evidence.

Plan deviation (documented, mirrors the Task 3/4 precedent): the plan names
``trading/sfo_kalshi_quant/research_walkforward.py`` as the file to modify
for Task 5. That module is already 677 lines and, per Task 3/4's own
decision, ``research_candidates.py``/``research_replay.py``/
``research_scoring.py`` were each split into their own sibling module (and
matching test file) rather than grown past the project's 800-line file cap.
This task follows the same convention: paired daily/capacity evidence gets
its own module (this file) and its own test file
(``trading/tests/test_research_evidence.py``), rather than adding ~500
lines to an already near-cap ``research_walkforward.py``.

Sits on top of three frozen, reviewed surfaces from Tasks 2-4:

- ``research_walkforward.WalkForwardFold``/``ResearchCase`` -- fold
  structure and each test case's own ``settled_at`` (needed here to derive
  the Pacific calendar day money actually realizes on).
- ``research_candidates.FoldCandidateEvidence`` -- fold-grained CRPS/Brier
  distributional scores, per case, keyed by ``source_context_hash``.
- ``research_replay.FoldReplayEvidence`` -- fold-grained exec-replay P&L,
  fills, and rejections, per case, keyed by ``source_context_hash``, each
  case payload carrying a ``stamp`` (``execution_model_version``,
  ``side_scope``, ``fill_scope``, ...).

Three guarantees this module is responsible for (binding conditions from
the Task 4 final review):

- **S1**: every aggregate/report this module produces surfaces the
  replay stamp's ``side_scope``/``fill_scope`` labels -- never a report
  that reads as full-opportunity or full-mechanics coverage. See
  ``PairedCaseRecord.side_scope``/``fill_scope`` and
  ``PairedEvidenceReport.side_scopes``/``fill_scopes``.
- **S2**: every aggregate carries ``stamp.execution_model_version`` so a
  later gate (Task 6) can check uniformity across an experiment's
  evidence rows. See ``PairedCaseRecord.execution_model_version`` and
  ``PairedEvidenceReport.execution_model_versions`` (the sorted set of
  distinct versions actually observed -- this module records what it
  sees, it does not itself enforce uniformity, which is Task 6's job).
- **S3**: every paired delta is same-day-same-case paired -- computed only
  from cases where BOTH the baseline and the challenger arm replayed as
  ``available`` (see ``build_paired_case_records``). A case where either
  arm is unavailable (e.g. a Google fail-closed corroboration block, or a
  missing market snapshot) is excluded from every paired statistic and
  recorded separately as a ``CaseCoverageExclusion`` -- never silently
  dropped, and never compared across mismatched case coverage.

This module never talks to the database, the clock, or unseeded random
state: every function is a pure function of its own arguments, and the one
place randomness appears (``day_clustered_bootstrap``, in
``research_bootstrap.py``) uses a fixed, explicit seed.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Mapping, Sequence

from .research_candidates import FoldCandidateEvidence
from .research_policy import MOTION_POLICY, RESEARCH_OBJECTIVE_TZ, TARGET_POLICY, ResearchSleevePolicy
from .research_replay import FoldReplayEvidence
from .research_walkforward import WalkForwardFold

# Every non-"filled" TickerReplayStatus (research_replay.py) is a rejection
# reason for KPI purposes; duplicated here (not imported) because
# TickerReplayStatus is a Literal type alias, not a runtime value this
# module can iterate -- this set is the closed, exhaustive complement of
# "filled" and is parity-tested against research_replay.py's own Literal.
_FILLED_STATUS = "filled"


@dataclass(frozen=True)
class CaseCoverageExclusion:
    """One case excluded from every paired statistic, and why (S3)."""

    fold_id: str
    source_context_hash: str
    reason: str
    baseline_skip_reason: str = ""
    challenger_skip_reason: str = ""


@dataclass(frozen=True)
class PairedCaseRecord:
    """One same-day-same-case paired baseline/challenger observation (S3).

    Only ever built for a case where both arms replayed as ``available``
    -- see ``build_paired_case_records``. ``pacific_day`` is derived from
    the case's own ``settled_at`` (the Pacific civil day money actually
    realizes on, per spec Sec 6), never from ``target_date`` (the
    station's fixed-standard settlement day, a different clock).
    """

    fold_id: str
    station_id: str
    target_date: date
    pacific_day: date
    source_context_hash: str
    baseline_pnl: float
    challenger_pnl: float
    baseline_filled: bool
    challenger_filled: bool
    baseline_contracts: float
    challenger_contracts: float
    baseline_dollars_at_risk: float
    challenger_dollars_at_risk: float
    baseline_rejections: tuple[str, ...]
    challenger_rejections: tuple[str, ...]
    baseline_crps: float | None
    challenger_crps: float | None
    baseline_brier: float | None
    challenger_brier: float | None
    execution_model_version: str
    side_scope: str
    fill_scope: str


def _pacific_day(moment: datetime) -> date:
    return moment.astimezone(RESEARCH_OBJECTIVE_TZ).date()


def _case_replay_metrics(
    payload: Mapping[str, object]
) -> tuple[float, bool, float, float, tuple[str, ...]]:
    """(pnl, filled, contracts, dollars_at_risk, rejections) from one
    already-available ``case_replay_payload`` dict."""

    pnl = float(payload["realized_pnl"])  # type: ignore[arg-type]
    tickers = payload.get("tickers") or []
    contracts = 0.0
    dollars_at_risk = 0.0
    rejections: list[str] = []
    filled = False
    for ticker in tickers:  # type: ignore[union-attr]
        status = str(ticker.get("status"))
        if status == _FILLED_STATUS:
            filled = True
            contracts += float(ticker.get("contracts") or 0.0)
            dollars_at_risk += float(ticker.get("contracts") or 0.0) * float(
                ticker.get("limit_price") or 0.0
            )
        else:
            rejections.append(status)
    return pnl, filled, contracts, dollars_at_risk, tuple(sorted(rejections))


def build_paired_case_records(
    fold: WalkForwardFold,
    replay_evidence: FoldReplayEvidence,
    candidate_evidence: FoldCandidateEvidence | None = None,
) -> tuple[tuple[PairedCaseRecord, ...], tuple[CaseCoverageExclusion, ...]]:
    """Pair one fold's baseline/challenger replay (plus optional CRPS/Brier
    scores) into same-case records, excluding any case where either arm is
    unavailable (S3).

    ``candidate_evidence``, when given, must be the SAME fold's
    ``FoldCandidateEvidence`` for the SAME ``challenger_candidate_key`` --
    checked, not assumed. CRPS/Brier stay ``None`` on a record when no
    candidate evidence was supplied, or a case's score there is itself
    unavailable; this never widens or narrows the P&L pairing gate, which
    is governed by ``replay_evidence`` alone.
    """

    if replay_evidence.fold_id != fold.fold_id:
        raise ValueError("replay_evidence.fold_id does not match fold.fold_id")
    if candidate_evidence is not None and candidate_evidence.fold_id != fold.fold_id:
        raise ValueError("candidate_evidence.fold_id does not match fold.fold_id")

    baseline_stamp = replay_evidence.baseline.get("stamp") or {}
    challenger_stamp = replay_evidence.challenger.get("stamp") or {}
    if baseline_stamp != challenger_stamp:
        raise ValueError(
            "baseline and challenger replay stamps disagree within one "
            f"FoldReplayEvidence row (fold_id={fold.fold_id!r}) -- a single "
            "replay_fold_candidates call always stamps both arms "
            "identically, so this indicates corrupted or hand-built evidence"
        )
    execution_model_version = str(baseline_stamp.get("execution_model_version") or "")
    side_scope = str(baseline_stamp.get("side_scope") or "")
    fill_scope = str(baseline_stamp.get("fill_scope") or "")

    settled_by_hash = {case.source_context_hash: case.settled_at for case in fold.test}
    baseline_cases = replay_evidence.baseline.get("cases") or {}
    challenger_cases = replay_evidence.challenger.get("cases") or {}
    candidate_baseline_cases = (
        (candidate_evidence.baseline.get("cases") or {}) if candidate_evidence else {}
    )
    candidate_challenger_cases = (
        (candidate_evidence.challenger.get("cases") or {}) if candidate_evidence else {}
    )

    records: list[PairedCaseRecord] = []
    exclusions: list[CaseCoverageExclusion] = []
    for source_hash in sorted(set(baseline_cases) | set(challenger_cases)):
        baseline_payload = baseline_cases.get(source_hash)
        challenger_payload = challenger_cases.get(source_hash)
        settled_at = settled_by_hash.get(source_hash)
        if baseline_payload is None or challenger_payload is None or settled_at is None:
            exclusions.append(
                CaseCoverageExclusion(
                    fold_id=fold.fold_id,
                    source_context_hash=source_hash,
                    reason="case_missing_from_fold_or_replay_evidence",
                )
            )
            continue

        baseline_available = bool(baseline_payload.get("available"))
        challenger_available = bool(challenger_payload.get("available"))
        if not baseline_available or not challenger_available:
            if not baseline_available and not challenger_available:
                reason = "both_unavailable"
            elif not baseline_available:
                reason = "baseline_unavailable"
            else:
                reason = "challenger_unavailable"
            exclusions.append(
                CaseCoverageExclusion(
                    fold_id=fold.fold_id,
                    source_context_hash=source_hash,
                    reason=reason,
                    baseline_skip_reason=str(baseline_payload.get("skip_reason") or ""),
                    challenger_skip_reason=str(challenger_payload.get("skip_reason") or ""),
                )
            )
            continue

        b_pnl, b_filled, b_contracts, b_dollars, b_rejections = _case_replay_metrics(
            baseline_payload
        )
        c_pnl, c_filled, c_contracts, c_dollars, c_rejections = _case_replay_metrics(
            challenger_payload
        )

        b_crps = b_brier = c_crps = c_brier = None
        b_score = candidate_baseline_cases.get(source_hash)
        c_score = candidate_challenger_cases.get(source_hash)
        if b_score is not None and b_score.get("available"):
            b_crps = b_score.get("crps")
            b_brier = b_score.get("bracket_brier")
        if c_score is not None and c_score.get("available"):
            c_crps = c_score.get("crps")
            c_brier = c_score.get("bracket_brier")

        records.append(
            PairedCaseRecord(
                fold_id=fold.fold_id,
                station_id=replay_evidence.station_id,
                target_date=replay_evidence.target_date,
                pacific_day=_pacific_day(settled_at),
                source_context_hash=source_hash,
                baseline_pnl=b_pnl,
                challenger_pnl=c_pnl,
                baseline_filled=b_filled,
                challenger_filled=c_filled,
                baseline_contracts=b_contracts,
                challenger_contracts=c_contracts,
                baseline_dollars_at_risk=b_dollars,
                challenger_dollars_at_risk=c_dollars,
                baseline_rejections=b_rejections,
                challenger_rejections=c_rejections,
                baseline_crps=b_crps,
                challenger_crps=c_crps,
                baseline_brier=b_brier,
                challenger_brier=c_brier,
                execution_model_version=execution_model_version,
                side_scope=side_scope,
                fill_scope=fill_scope,
            )
        )

    records.sort(key=lambda r: (r.pacific_day, r.fold_id, r.source_context_hash))
    exclusions.sort(key=lambda e: (e.fold_id, e.source_context_hash))
    return tuple(records), tuple(exclusions)


def build_paired_records_for_experiment(
    folds: Sequence[WalkForwardFold],
    replay_evidence: Sequence[FoldReplayEvidence],
    candidate_evidence: Sequence[FoldCandidateEvidence] = (),
    *,
    challenger_candidate_key: str,
) -> tuple[tuple[PairedCaseRecord, ...], tuple[CaseCoverageExclusion, ...]]:
    """Build paired case records across every fold, for ONE declared
    challenger. Rows for any other ``challenger_candidate_key`` present in
    ``replay_evidence``/``candidate_evidence`` are ignored, not mixed in."""

    folds_by_id = {f.fold_id: f for f in folds}
    replay_by_fold = {
        row.fold_id: row
        for row in replay_evidence
        if row.challenger_candidate_key == challenger_candidate_key
    }
    candidate_by_fold = {
        row.fold_id: row
        for row in candidate_evidence
        if row.challenger_candidate_key == challenger_candidate_key
    }

    all_records: list[PairedCaseRecord] = []
    all_exclusions: list[CaseCoverageExclusion] = []
    for fold_id in sorted(replay_by_fold):
        fold = folds_by_id.get(fold_id)
        if fold is None:
            raise ValueError(f"no WalkForwardFold supplied for fold_id={fold_id!r}")
        records, exclusions = build_paired_case_records(
            fold, replay_by_fold[fold_id], candidate_by_fold.get(fold_id)
        )
        all_records.extend(records)
        all_exclusions.extend(exclusions)

    all_records.sort(key=lambda r: (r.pacific_day, r.fold_id, r.source_context_hash))
    all_exclusions.sort(key=lambda e: (e.fold_id, e.source_context_hash))
    return tuple(all_records), tuple(all_exclusions)


@dataclass(frozen=True)
class DailyPairedEvidence:
    """One Pacific calendar day's paired baseline/challenger evidence.

    Every day strictly between the earliest and latest observed
    ``pacific_day`` is retained even when no case settled on it (zero
    P&L, zero fills, empty rejection counts) -- plan Task 5 Step 1's
    "retention of zero-fill days in daily statistics".
    """

    pacific_day: date
    case_count: int
    baseline_pnl: float
    challenger_pnl: float
    baseline_filled_count: int
    challenger_filled_count: int
    baseline_contracts: float
    challenger_contracts: float
    baseline_dollars_at_risk: float
    challenger_dollars_at_risk: float
    baseline_rejection_counts: dict[str, int]
    challenger_rejection_counts: dict[str, int]


def _rejection_counts(records: Sequence[PairedCaseRecord], *, arm: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    attr = "baseline_rejections" if arm == "baseline" else "challenger_rejections"
    for record in records:
        for reason in getattr(record, attr):
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def daily_paired_evidence(
    records: Sequence[PairedCaseRecord],
    *,
    start_day: date | None = None,
    end_day: date | None = None,
) -> tuple[DailyPairedEvidence, ...]:
    """Bucket paired case records by Pacific calendar day, retaining every
    zero-fill day in ``[start_day, end_day]`` (default: the data's own
    observed min/max day)."""

    by_day: dict[date, list[PairedCaseRecord]] = {}
    for record in records:
        by_day.setdefault(record.pacific_day, []).append(record)

    if start_day is None and end_day is None and not by_day:
        return ()

    lo = start_day if start_day is not None else min(by_day)
    hi = end_day if end_day is not None else max(by_day)
    if lo > hi:
        raise ValueError("start_day cannot be after end_day")

    days: list[DailyPairedEvidence] = []
    current = lo
    while current <= hi:
        day_records = by_day.get(current, [])
        days.append(
            DailyPairedEvidence(
                pacific_day=current,
                case_count=len(day_records),
                baseline_pnl=math.fsum(r.baseline_pnl for r in day_records),
                challenger_pnl=math.fsum(r.challenger_pnl for r in day_records),
                baseline_filled_count=sum(r.baseline_filled for r in day_records),
                challenger_filled_count=sum(r.challenger_filled for r in day_records),
                baseline_contracts=math.fsum(r.baseline_contracts for r in day_records),
                challenger_contracts=math.fsum(r.challenger_contracts for r in day_records),
                baseline_dollars_at_risk=math.fsum(
                    r.baseline_dollars_at_risk for r in day_records
                ),
                challenger_dollars_at_risk=math.fsum(
                    r.challenger_dollars_at_risk for r in day_records
                ),
                baseline_rejection_counts=_rejection_counts(day_records, arm="baseline"),
                challenger_rejection_counts=_rejection_counts(day_records, arm="challenger"),
            )
        )
        current += timedelta(days=1)
    return tuple(days)


@dataclass(frozen=True)
class ArmDailyKpis:
    """One arm's (baseline or challenger) full KPI bundle over a day range
    -- the exact metric list plan Task 5 Step 3 names."""

    observed_days: int
    zero_activity_days: int
    realized_pnl_total: float
    mean_daily_pnl: float | None
    median_daily_pnl: float | None
    stdev_daily_pnl: float | None
    positive_day_rate: float | None
    target_hit_rate: float | None
    after_fee_roi: float | None
    log_growth: float | None
    log_growth_per_day: float | None
    maximum_drawdown_dollars: float
    maximum_drawdown_pct: float
    turnover_ratio: float | None
    fills: int
    contracts: float
    dollars_at_risk: float
    rejection_counts: dict[str, int]


def _growth_and_drawdown(
    daily_pnls: Sequence[float], *, reference_equity: float
) -> tuple[float, float, float | None]:
    """Fixed-reference-equity cumulative log growth and max drawdown.

    Deliberately duplicated (not imported) from ``research_goals.py``'s
    private ``_growth_and_drawdown`` -- same formula, same convention
    (never compound past a non-positive running equity), but that
    function is module-private and this module must not couple to another
    module's internals. A parity test locks the two in step.
    """

    equity = reference_equity
    peak = reference_equity
    max_drawdown_dollars = 0.0
    max_drawdown_pct = 0.0
    log_growth = 0.0
    valid_growth = True
    for daily_pnl in daily_pnls:
        prior = equity
        equity += daily_pnl
        if prior <= 0 or equity <= 0:
            valid_growth = False
        elif valid_growth:
            log_growth += math.log(equity / prior)
        peak = max(peak, equity)
        drawdown_dollars = peak - equity
        drawdown_pct = drawdown_dollars / peak if peak > 0 else 0.0
        max_drawdown_dollars = max(max_drawdown_dollars, drawdown_dollars)
        max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)
    return (
        max_drawdown_dollars,
        max_drawdown_pct,
        log_growth if valid_growth else None,
    )


def arm_daily_kpis(
    days: Sequence[DailyPairedEvidence],
    *,
    arm: str,
    reference_equity: float,
    target_pnl: float,
) -> ArmDailyKpis:
    """Compute one arm's ("baseline" or "challenger") full KPI bundle from
    a day-range of ``DailyPairedEvidence`` (public: the plan's Task 5 Step
    3 metric list is exercised directly, with hand-computable inputs, not
    only through the end-to-end report builder)."""

    if reference_equity <= 0 or not math.isfinite(reference_equity):
        raise ValueError("reference_equity must be finite and positive")
    if target_pnl <= 0 or not math.isfinite(target_pnl):
        raise ValueError("target_pnl must be finite and positive")

    pnl_attr = f"{arm}_pnl"
    filled_attr = f"{arm}_filled_count"
    contracts_attr = f"{arm}_contracts"
    dollars_attr = f"{arm}_dollars_at_risk"
    rejections_attr = f"{arm}_rejection_counts"

    daily_pnls = [getattr(day, pnl_attr) for day in days]
    observed = len(daily_pnls)
    zero_days = sum(1 for pnl in daily_pnls if abs(pnl) <= 1e-9)
    total_pnl = math.fsum(daily_pnls)
    mean_pnl = statistics.fmean(daily_pnls) if daily_pnls else None
    median_pnl = statistics.median(daily_pnls) if daily_pnls else None
    stdev_pnl = statistics.pstdev(daily_pnls) if daily_pnls else None
    positive_rate = (
        sum(1 for pnl in daily_pnls if pnl > 1e-9) / observed if observed else None
    )
    hit_rate = (
        sum(1 for pnl in daily_pnls if pnl + 1e-9 >= target_pnl) / observed
        if observed
        else None
    )
    roi = total_pnl / reference_equity
    max_dd_dollars, max_dd_pct, log_growth = _growth_and_drawdown(
        daily_pnls, reference_equity=reference_equity
    )
    log_growth_per_day = (
        log_growth / observed if log_growth is not None and observed else None
    )
    total_dollars_at_risk = math.fsum(getattr(day, dollars_attr) for day in days)
    turnover = total_dollars_at_risk / reference_equity
    total_fills = sum(getattr(day, filled_attr) for day in days)
    total_contracts = math.fsum(getattr(day, contracts_attr) for day in days)
    rejection_counts: dict[str, int] = {}
    for day in days:
        for reason, count in getattr(day, rejections_attr).items():
            rejection_counts[reason] = rejection_counts.get(reason, 0) + count

    return ArmDailyKpis(
        observed_days=observed,
        zero_activity_days=zero_days,
        realized_pnl_total=total_pnl,
        mean_daily_pnl=mean_pnl,
        median_daily_pnl=median_pnl,
        stdev_daily_pnl=stdev_pnl,
        positive_day_rate=positive_rate,
        target_hit_rate=hit_rate,
        after_fee_roi=roi,
        log_growth=log_growth,
        log_growth_per_day=log_growth_per_day,
        maximum_drawdown_dollars=max_dd_dollars,
        maximum_drawdown_pct=max_dd_pct,
        turnover_ratio=turnover,
        fills=total_fills,
        contracts=total_contracts,
        dollars_at_risk=total_dollars_at_risk,
        rejection_counts=rejection_counts,
    )


@dataclass(frozen=True)
class ArmKpiDelta:
    """Challenger-minus-baseline delta for every ``ArmDailyKpis`` field
    where "higher is better" (P&L, ROI, log growth, positive-day rate, hit
    rate, turnover, fills, contracts, dollars at risk). ``maximum_drawdown``
    is the one field where LOWER is better, so it is reported as
    baseline-minus-challenger instead -- every field in this dataclass
    therefore shares the convention "positive means the challenger arm
    improved on this metric"."""

    mean_daily_pnl: float | None
    median_daily_pnl: float | None
    stdev_daily_pnl: float | None
    positive_day_rate: float | None
    target_hit_rate: float | None
    after_fee_roi: float | None
    log_growth_per_day: float | None
    maximum_drawdown_dollars: float
    turnover_ratio: float | None
    fills: int
    contracts: float
    dollars_at_risk: float


def _optional_diff(baseline: float | None, challenger: float | None) -> float | None:
    if baseline is None or challenger is None:
        return None
    return challenger - baseline


def arm_kpi_delta(baseline: ArmDailyKpis, challenger: ArmDailyKpis) -> ArmKpiDelta:
    return ArmKpiDelta(
        mean_daily_pnl=_optional_diff(baseline.mean_daily_pnl, challenger.mean_daily_pnl),
        median_daily_pnl=_optional_diff(
            baseline.median_daily_pnl, challenger.median_daily_pnl
        ),
        stdev_daily_pnl=_optional_diff(baseline.stdev_daily_pnl, challenger.stdev_daily_pnl),
        positive_day_rate=_optional_diff(
            baseline.positive_day_rate, challenger.positive_day_rate
        ),
        target_hit_rate=_optional_diff(baseline.target_hit_rate, challenger.target_hit_rate),
        after_fee_roi=_optional_diff(baseline.after_fee_roi, challenger.after_fee_roi),
        log_growth_per_day=_optional_diff(
            baseline.log_growth_per_day, challenger.log_growth_per_day
        ),
        maximum_drawdown_dollars=(
            baseline.maximum_drawdown_dollars - challenger.maximum_drawdown_dollars
        ),
        turnover_ratio=_optional_diff(baseline.turnover_ratio, challenger.turnover_ratio),
        fills=challenger.fills - baseline.fills,
        contracts=challenger.contracts - baseline.contracts,
        dollars_at_risk=challenger.dollars_at_risk - baseline.dollars_at_risk,
    )


@dataclass(frozen=True)
class SleeveCapacityEvidence:
    """How much of ONE sleeve's aggregate paper risk envelope a replayed
    arm's total dollars-at-risk would represent (plan Task 5 Step 3:
    "capacity under the target/motion account limits"). Pure evidence, not
    an allocation decision -- this module never assigns replayed dollars to
    one sleeve; it reports utilization under each declared envelope."""

    policy_version: str
    reference_equity: float
    max_aggregate_risk_pct: float
    capacity_dollars: float
    dollars_at_risk: float
    utilization_pct: float | None
    capacity_remaining_dollars: float


def sleeve_capacity_evidence(
    dollars_at_risk: float, policy: ResearchSleevePolicy
) -> SleeveCapacityEvidence:
    capacity_dollars = policy.reference_equity * policy.max_aggregate_risk_pct
    utilization = dollars_at_risk / capacity_dollars if capacity_dollars > 0 else None
    return SleeveCapacityEvidence(
        policy_version=policy.policy_version,
        reference_equity=policy.reference_equity,
        max_aggregate_risk_pct=policy.max_aggregate_risk_pct,
        capacity_dollars=capacity_dollars,
        dollars_at_risk=dollars_at_risk,
        utilization_pct=utilization,
        capacity_remaining_dollars=max(0.0, capacity_dollars - dollars_at_risk),
    )


def capacity_evidence(dollars_at_risk: float) -> dict[str, SleeveCapacityEvidence]:
    """Capacity utilization under BOTH declared sleeve envelopes (target
    and motion) for the same replayed dollars-at-risk figure."""

    return {
        "target": sleeve_capacity_evidence(dollars_at_risk, TARGET_POLICY),
        "motion": sleeve_capacity_evidence(dollars_at_risk, MOTION_POLICY),
    }


@dataclass(frozen=True)
class PairedEvidenceReport:
    """Everything Task 5 publishes for one declared challenger: paired
    daily evidence, KPI summaries and their delta, capacity evidence under
    both sleeves, coverage exclusions, and the engine/scope stamp every
    aggregate here carries (S1/S2)."""

    challenger_candidate_key: str
    reference_equity: float
    target_pnl: float
    days: tuple[DailyPairedEvidence, ...]
    baseline_kpis: ArmDailyKpis
    challenger_kpis: ArmDailyKpis
    kpi_delta: ArmKpiDelta
    baseline_capacity: dict[str, SleeveCapacityEvidence]
    challenger_capacity: dict[str, SleeveCapacityEvidence]
    coverage_exclusions: tuple[CaseCoverageExclusion, ...]
    paired_case_count: int
    execution_model_versions: tuple[str, ...]
    side_scopes: tuple[str, ...]
    fill_scopes: tuple[str, ...]


def build_paired_evidence_report(
    records: Sequence[PairedCaseRecord],
    exclusions: Sequence[CaseCoverageExclusion],
    *,
    challenger_candidate_key: str,
    reference_equity: float = TARGET_POLICY.reference_equity,
    target_pnl: float = TARGET_POLICY.target_pnl,
    start_day: date | None = None,
    end_day: date | None = None,
) -> PairedEvidenceReport:
    days = daily_paired_evidence(records, start_day=start_day, end_day=end_day)
    baseline_kpis = arm_daily_kpis(
        days, arm="baseline", reference_equity=reference_equity, target_pnl=target_pnl
    )
    challenger_kpis = arm_daily_kpis(
        days, arm="challenger", reference_equity=reference_equity, target_pnl=target_pnl
    )
    return PairedEvidenceReport(
        challenger_candidate_key=challenger_candidate_key,
        reference_equity=reference_equity,
        target_pnl=target_pnl,
        days=days,
        baseline_kpis=baseline_kpis,
        challenger_kpis=challenger_kpis,
        kpi_delta=arm_kpi_delta(baseline_kpis, challenger_kpis),
        baseline_capacity=capacity_evidence(baseline_kpis.dollars_at_risk),
        challenger_capacity=capacity_evidence(challenger_kpis.dollars_at_risk),
        coverage_exclusions=tuple(exclusions),
        paired_case_count=len(records),
        execution_model_versions=tuple(sorted({r.execution_model_version for r in records})),
        side_scopes=tuple(sorted({r.side_scope for r in records})),
        fill_scopes=tuple(sorted({r.fill_scope for r in records})),
    )
