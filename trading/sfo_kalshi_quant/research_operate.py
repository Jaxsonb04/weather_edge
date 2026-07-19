"""Task 7: publish and operate the evidence loop.

Sits on top of every prior task's frozen, reviewed surfaces (Tasks 1-6,
plus this task's own sibling ``research_google_join.py``) and adds the one
thing none of them do: talk to the database to durably run, persist, and
reload research evaluations over time, so a promotion verdict is never
computed from caller-suppliable in-memory state alone.

Review-blocking conditions (Task 6 final review, E1-E3) this module is
responsible for:

- **E1**: ``run_research_evaluation``/``load_prior_family_attempts`` load
  EVERY prior family attempt from the immutable ``research_experiments``/
  ``research_evidence`` tables -- ``research_promotion.evaluate_promotion``
  itself still accepts ``prior_family_attempts`` as a plain argument (a
  frozen Task 6 surface this module does not edit), but nothing in this
  module's own public API accepts a caller-supplied ``FamilyAttempt``
  sequence at all; ``load_prior_family_attempts`` is the ONLY source.
  ``declare_challenger`` separately rejects loudly
  (``DeclarationConflictError``) when a caller-passed declaration
  disagrees with the already-stored ``research_experiments`` row for the
  same identity.
- **E2**: ``declare_challenger`` persists all four regression tolerances
  plus ``predicted_edge_scope`` into ``research_experiments.parameter_json``
  AT DECLARATION TIME -- before ANY ``research_evidence`` row for that
  experiment can exist (the database's own declared-before-window trigger
  enforces the ordering; this module never writes evidence before
  declaring). ``load_declared_challenger``/``_declaration_from_stored_row``
  reconstruct ``ChallengerDeclaration`` ONLY by reading back that stored
  row -- never from whatever in-memory values a caller happens to be
  holding at evaluation time. ``MAX_SANE_TOLERANCE`` rejects an absurd
  declared tolerance (e.g. ``1e9``) at declaration, before it could ever
  make every downstream regression gate vacuous.
- **E3**: nothing in this module ever passes ``live_activation_allowed`` to
  ``PromotionDecision`` -- grep for the literal string in this file finds
  nothing (see ``test_research_operate.py``'s AST-based parity test,
  mirroring ``research_promotion.py``'s own
  ``test_research_promotion_module_never_imports_live_config_surfaces``
  convention). This module also never imports ``config``/``live_execution``,
  for the same reason ``research_promotion.py`` does not.

"Operate the evidence loop" is the other half of this module's job:
``persist_fold_evidence`` durably records one fold's paired
score+replay evidence (merging Task 3's ``FoldCandidateEvidence`` and
Task 4's ``FoldReplayEvidence`` into ONE ``research_evidence`` row, since
Task 1's schema is fold-grained, not evidence-kind-grained), is idempotent
across repeated runs over already-recorded folds (never re-inserts, never
raises on an exact re-run), and ``historical_rows_from_paper_store`` is
this project's first real bridge from the paper database's own
``scan_context_snapshots``/``decision_snapshots`` history into
``research_walkforward.load_research_cases``'s row contract -- see that
function's own docstring for exactly which existing, already-populated
columns it reads and why, and what it deliberately leaves out of scope.

``run_research_evaluation`` is the one end-to-end orchestration entry
point: Google-evidence join -> chronological folds -> fit/score/replay the
DECLARED challenger only -> persist newly-seen fold evidence -> reload
this hypothesis family's full prior history -> ``evaluate_promotion``.
Nothing here mutates live trading state; this module never imports
``config``, ``live_execution``, or anything under ``_cli`` (the CLI layer
imports this module, never the reverse).
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Mapping, Sequence

from ._util import _parse_timestamp
from .cities import city_for_station
from .db import PaperStore
from .research_bootstrap import DEFAULT_BOOTSTRAP_DRAWS, DEFAULT_BOOTSTRAP_SEED
from .research_candidates import FoldCandidateEvidence, score_fold_candidates
from .research_evidence import (
    PairedEvidenceReport,
    build_paired_evidence_report,
    build_paired_records_for_experiment,
)
from .research_google_join import GoogleJoinSkip, attach_google_challenger_evidence
from .research_policy import TARGET_POLICY
from .research_promotion import (
    ChallengerDeclaration,
    FamilyAttempt,
    PromotionDecision,
    evaluate_promotion,
)
from .research_replay import FoldReplayEvidence, replay_fold_candidates
from .research_significance import one_sided_bootstrap_p_value
from .research_walkforward import (
    DEFAULT_EMBARGO_DAYS,
    WalkForwardEvidence,
    build_walk_forward_evidence,
)
from .settlement_truth import SettlementKey

# E2: an upper bound on every declared regression tolerance, checked at
# declaration time (before any evidence row can exist for it).
# ``ChallengerDeclaration.__post_init__`` itself (research_promotion.py, a
# frozen Task 6 surface this module does not edit) only rejects a negative
# or non-finite value -- an absurdly large "tolerance" like 1e9 would still
# pass that check while making every downstream CRPS/Brier/
# calibration-gap/drawdown regression gate vacuous (nothing could ever
# exceed it). 1000.0 comfortably covers every legitimate value these four
# fields take today (drawdown tolerance is a fraction of equity, bounded at
# 1.0 by construction; CRPS/Brier/calibration-gap tolerances are small
# score deltas, typically well under 10) while unambiguously rejecting a
# typo or a copy-pasted sentinel like 1e9.
MAX_SANE_TOLERANCE = 1000.0

_DECLARATION_TOLERANCE_FIELDS = (
    "max_drawdown_tolerance_pct",
    "crps_regression_tolerance",
    "brier_regression_tolerance",
    "calibration_gap_regression_tolerance",
)

# The full set of parameter_json keys ``declare_challenger`` writes and
# ``_declaration_from_stored_row`` requires to reconstruct a
# ChallengerDeclaration. A stored research_experiments row declared through
# some OTHER caller (e.g. research_candidates.declared_research_candidates's
# own {"shrinkage_k": ...}-shaped rows) is missing these and fails closed
# rather than reconstructing a bogus declaration from absent data.
_REQUIRED_DECLARATION_PARAMETER_KEYS = ("predicted_edge_scope",) + _DECLARATION_TOLERANCE_FIELDS


class DeclarationConflictError(ValueError):
    """A caller-passed declaration disagrees with the immutable stored row
    for the same (hypothesis_family, candidate_key, candidate_version)
    identity (E1)."""


@dataclass(frozen=True)
class StoredExperimentRow:
    """One raw ``research_experiments`` row, as read back from the
    database -- ``parameter_json`` already parsed."""

    experiment_id: str
    hypothesis_family: str
    candidate_key: str
    candidate_version: str
    evidence_role: str
    parameter_json: dict[str, object]
    declared_at: str


def _fetch_experiment_row(
    store: PaperStore,
    *,
    hypothesis_family: str,
    candidate_key: str,
    candidate_version: str,
) -> StoredExperimentRow | None:
    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT experiment_id, declared_at, hypothesis_family, candidate_key, "
            "candidate_version, parameter_json, evidence_role FROM research_experiments "
            "WHERE hypothesis_family = ? AND candidate_key = ? AND candidate_version = ?",
            (hypothesis_family, candidate_key, candidate_version),
        ).fetchone()
    if row is None:
        return None
    return StoredExperimentRow(
        experiment_id=row["experiment_id"],
        hypothesis_family=row["hypothesis_family"],
        candidate_key=row["candidate_key"],
        candidate_version=row["candidate_version"],
        evidence_role=row["evidence_role"],
        parameter_json=json.loads(row["parameter_json"]),
        declared_at=row["declared_at"],
    )


def _declaration_from_stored_row(row: StoredExperimentRow) -> ChallengerDeclaration:
    """E2: the ONLY way this module ever builds a ``ChallengerDeclaration``
    -- entirely from an already-persisted ``research_experiments`` row,
    never from a caller's own in-memory values."""

    missing = [key for key in _REQUIRED_DECLARATION_PARAMETER_KEYS if key not in row.parameter_json]
    if missing:
        raise ValueError(
            f"stored research_experiments row for experiment_id={row.experiment_id!r} is "
            f"missing declaration field(s) required to reconstruct a ChallengerDeclaration: "
            f"{missing} -- it was not declared through research_operate.declare_challenger"
        )
    return ChallengerDeclaration(
        experiment_id=row.experiment_id,
        hypothesis_family=row.hypothesis_family,
        candidate_key=row.candidate_key,
        candidate_version=row.candidate_version,
        evidence_role=row.evidence_role,
        predicted_edge_scope=row.parameter_json["predicted_edge_scope"],
        max_drawdown_tolerance_pct=row.parameter_json["max_drawdown_tolerance_pct"],
        crps_regression_tolerance=row.parameter_json["crps_regression_tolerance"],
        brier_regression_tolerance=row.parameter_json["brier_regression_tolerance"],
        calibration_gap_regression_tolerance=row.parameter_json["calibration_gap_regression_tolerance"],
    )


def load_declared_challenger(
    store: PaperStore,
    *,
    hypothesis_family: str,
    candidate_key: str,
    candidate_version: str,
) -> ChallengerDeclaration:
    """E2: reconstruct an already-declared challenger's
    ``ChallengerDeclaration`` purely from its stored ``research_experiments``
    row. Raises ``LookupError`` if it was never declared -- a caller must
    run ``declare_challenger`` first; this function never declares one
    itself."""

    row = _fetch_experiment_row(
        store,
        hypothesis_family=hypothesis_family,
        candidate_key=candidate_key,
        candidate_version=candidate_version,
    )
    if row is None:
        raise LookupError(
            f"no declared research_experiments row for hypothesis_family={hypothesis_family!r} "
            f"candidate_key={candidate_key!r} candidate_version={candidate_version!r} -- "
            "call declare_challenger before evaluation"
        )
    return _declaration_from_stored_row(row)


def declare_challenger(
    store: PaperStore,
    *,
    experiment_id: str,
    hypothesis_family: str,
    candidate_key: str,
    candidate_version: str,
    evidence_role: str,
    predicted_edge_scope: str,
    max_drawdown_tolerance_pct: float,
    crps_regression_tolerance: float,
    brier_regression_tolerance: float,
    calibration_gap_regression_tolerance: float,
) -> ChallengerDeclaration:
    """E2: declare one challenger's immutable identity and regression
    tolerances, BEFORE any evidence for it can exist. Idempotent when the
    identity is already declared with an IDENTICAL payload (a safe re-run,
    e.g. a systemd timer invoking this every cycle); raises
    ``DeclarationConflictError`` (E1) when it is already declared with a
    DIFFERENT payload or under a different ``experiment_id`` -- never
    silently keeps the old row or silently adopts the new one.

    Every value in ``requested_parameters`` below is what actually lands
    in ``research_experiments.parameter_json`` -- the full set
    ``load_declared_challenger``/``_declaration_from_stored_row`` require
    to reconstruct this exact ``ChallengerDeclaration`` later, from the
    stored row alone.
    """

    for name in _DECLARATION_TOLERANCE_FIELDS:
        value = {
            "max_drawdown_tolerance_pct": max_drawdown_tolerance_pct,
            "crps_regression_tolerance": crps_regression_tolerance,
            "brier_regression_tolerance": brier_regression_tolerance,
            "calibration_gap_regression_tolerance": calibration_gap_regression_tolerance,
        }[name]
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative")
        if value > MAX_SANE_TOLERANCE:
            raise ValueError(
                f"{name}={value!r} exceeds the sane upper bound of "
                f"{MAX_SANE_TOLERANCE} -- a declared tolerance this large would make "
                "every promotion gate that reads it vacuous"
            )

    requested_parameters: dict[str, object] = {
        "predicted_edge_scope": predicted_edge_scope,
        "max_drawdown_tolerance_pct": max_drawdown_tolerance_pct,
        "crps_regression_tolerance": crps_regression_tolerance,
        "brier_regression_tolerance": brier_regression_tolerance,
        "calibration_gap_regression_tolerance": calibration_gap_regression_tolerance,
    }

    existing = _fetch_experiment_row(
        store,
        hypothesis_family=hypothesis_family,
        candidate_key=candidate_key,
        candidate_version=candidate_version,
    )
    if existing is None:
        store.record_research_experiment(
            experiment_id=experiment_id,
            hypothesis_family=hypothesis_family,
            candidate_key=candidate_key,
            candidate_version=candidate_version,
            parameter_json=requested_parameters,
            evidence_role=evidence_role,
        )
        existing = _fetch_experiment_row(
            store,
            hypothesis_family=hypothesis_family,
            candidate_key=candidate_key,
            candidate_version=candidate_version,
        )
        if existing is None:  # pragma: no cover - defensive, cannot happen
            raise RuntimeError("research experiment declaration did not persist")
    else:
        if existing.experiment_id != experiment_id:
            raise DeclarationConflictError(
                f"hypothesis_family={hypothesis_family!r} candidate_key={candidate_key!r} "
                f"candidate_version={candidate_version!r} is already declared under "
                f"experiment_id={existing.experiment_id!r}, not the caller-supplied "
                f"{experiment_id!r}"
            )
        if existing.evidence_role != evidence_role or existing.parameter_json != requested_parameters:
            raise DeclarationConflictError(
                "caller-supplied declaration disagrees with the immutable stored "
                f"research_experiments row for experiment_id={existing.experiment_id!r} "
                f"(stored parameter_json={existing.parameter_json!r}, "
                f"evidence_role={existing.evidence_role!r}; requested "
                f"parameter_json={requested_parameters!r}, evidence_role={evidence_role!r})"
            )

    return _declaration_from_stored_row(existing)


def persist_fold_evidence(
    store: PaperStore,
    *,
    experiment_id: str,
    candidate_row: FoldCandidateEvidence,
    replay_row: FoldReplayEvidence,
) -> None:
    """Durably record one fold's paired baseline/challenger evidence for
    ``experiment_id`` -- the "operate the evidence loop" half of Task 7.

    Merges Task 3's distributional-score evidence (``FoldCandidateEvidence``)
    and Task 4's exec-replay evidence (``FoldReplayEvidence``) for the SAME
    fold/challenger into one ``research_evidence`` row (Task 1's schema is
    one row per ``(experiment_id, fold_id, station_id, target_date)`` --
    fold-grained, not evidence-kind-grained), so a later reload
    (``load_prior_family_attempts``) can recover both from a single read.
    Raises ``ValueError`` if the two rows disagree about which
    fold/station/date/challenger they belong to -- never silently pairs
    mismatched evidence. Also raises ``ValueError`` (propagated from
    ``PaperStore.record_research_evidence``) if ``experiment_id`` was
    declared on or after this fold's own Pacific target day -- the
    declared-before-window invariant; callers that sweep a broad historical
    window (some of it predating the declaration, needed only as TRAINING
    data for later folds) should expect and handle this per-fold, not treat
    it as fatal to the whole run -- see ``run_research_evaluation``.
    """

    if candidate_row.fold_id != replay_row.fold_id:
        raise ValueError("candidate_row/replay_row fold_id mismatch")
    if candidate_row.challenger_candidate_key != replay_row.challenger_candidate_key:
        raise ValueError("candidate_row/replay_row challenger_candidate_key mismatch")
    if candidate_row.station_id != replay_row.station_id or candidate_row.target_date != replay_row.target_date:
        raise ValueError("candidate_row/replay_row station_id/target_date mismatch")

    baseline_payload = {"score": candidate_row.baseline, "replay": replay_row.baseline}
    challenger_payload = {
        "score": candidate_row.challenger,
        "replay": replay_row.challenger,
        "promotion_eligible": replay_row.promotion_eligible,
        "promotion_block_reasons": list(replay_row.promotion_block_reasons),
    }
    store.record_research_evidence(
        experiment_id=experiment_id,
        fold_id=candidate_row.fold_id,
        station_id=candidate_row.station_id,
        target_date=candidate_row.target_date.isoformat(),
        evaluated_at=candidate_row.evaluated_at.isoformat(),
        baseline=baseline_payload,
        challenger=challenger_payload,
    )


def _fold_evidence_already_recorded(store: PaperStore, *, experiment_id: str, fold_id: str) -> bool:
    with store.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM research_evidence WHERE experiment_id = ? AND fold_id = ? LIMIT 1",
            (experiment_id, fold_id),
        ).fetchone()
    return row is not None


def _fold_roi_delta(
    baseline_payload: Mapping[str, object],
    challenger_payload: Mapping[str, object],
    *,
    reference_equity: float,
) -> float | None:
    """Recompute one persisted fold's own ROI delta straight from its
    stored per-case realized P&L (``persist_fold_evidence``'s ``"replay"``
    sub-payload), pairing only cases available on BOTH arms (S3's paired-
    only rule) -- the same figure
    ``research_bootstrap.fold_paired_aggregates`` would compute for a
    freshly-replayed fold, but read back from durable storage instead of
    re-replaying. Returns ``None`` when no case in this fold is paired-
    available (nothing to contribute)."""

    baseline_cases = ((baseline_payload.get("replay") or {}).get("cases")) or {}
    challenger_cases = ((challenger_payload.get("replay") or {}).get("cases")) or {}
    baseline_total = 0.0
    challenger_total = 0.0
    counted = False
    for source_hash in sorted(set(baseline_cases) & set(challenger_cases)):
        baseline_case = baseline_cases[source_hash]
        challenger_case = challenger_cases[source_hash]
        if not baseline_case.get("available") or not challenger_case.get("available"):
            continue
        baseline_total += float(baseline_case.get("realized_pnl") or 0.0)
        challenger_total += float(challenger_case.get("realized_pnl") or 0.0)
        counted = True
    if not counted:
        return None
    return (challenger_total - baseline_total) / reference_equity


def load_prior_family_attempts(
    store: PaperStore,
    *,
    hypothesis_family: str,
    exclude_candidate_version: str,
    reference_equity: float = TARGET_POLICY.reference_equity,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    draws: int = DEFAULT_BOOTSTRAP_DRAWS,
) -> tuple[FamilyAttempt, ...]:
    """E1: the ONLY source of ``FamilyAttempt`` history this module ever
    feeds ``research_promotion.evaluate_promotion`` -- read entirely from
    the immutable ``research_experiments``/``research_evidence`` tables,
    never accepted as a caller argument anywhere in this module's public
    API. ``exclude_candidate_version`` is always the declaration currently
    being evaluated -- ``evaluate_promotion`` itself raises ``ValueError``
    (G7) if its own ``candidate_version`` reappears in
    ``prior_family_attempts``, so this function excludes it defensively
    before that call ever happens, rather than relying on the callee alone.

    Recomputes each prior candidate version's own one-sided bootstrap
    p-value FRESH from its own persisted per-fold replay evidence (never a
    cached number, and never assumed stale-safe), using the exact same
    ``one_sided_bootstrap_p_value``/seed/draws
    ``research_promotion.evaluate_promotion`` uses internally for the
    CURRENT candidate, so a family history built here is directly
    comparable. A prior candidate version with no usable persisted evidence
    at all (e.g. still mid-declaration, zero folds settled) contributes no
    ``FamilyAttempt`` -- never a fabricated p-value from zero data.
    """

    attempts: list[FamilyAttempt] = []
    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        experiment_rows = conn.execute(
            "SELECT experiment_id, candidate_version FROM research_experiments "
            "WHERE hypothesis_family = ? ORDER BY candidate_version",
            (hypothesis_family,),
        ).fetchall()
        for experiment_row in experiment_rows:
            candidate_version = str(experiment_row["candidate_version"])
            if candidate_version == exclude_candidate_version:
                continue
            evidence_rows = conn.execute(
                "SELECT baseline_json, challenger_json FROM research_evidence "
                "WHERE experiment_id = ? ORDER BY fold_id",
                (experiment_row["experiment_id"],),
            ).fetchall()
            deltas: list[float] = []
            for evidence_row in evidence_rows:
                baseline_payload = json.loads(evidence_row["baseline_json"])
                challenger_payload = json.loads(evidence_row["challenger_json"])
                delta = _fold_roi_delta(
                    baseline_payload, challenger_payload, reference_equity=reference_equity
                )
                if delta is not None:
                    deltas.append(delta)
            if not deltas:
                continue
            p_value = one_sided_bootstrap_p_value(deltas, seed=seed, draws=draws)
            if p_value is None:
                continue
            attempts.append(
                FamilyAttempt(
                    hypothesis_family=hypothesis_family,
                    candidate_version=candidate_version,
                    p_value=p_value,
                )
            )
    attempts.sort(key=lambda attempt: attempt.candidate_version)
    return tuple(attempts)


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _expected_lead_days_pacific(decision_at: datetime, target_date: date) -> int:
    """Duplicated (not imported) from
    ``research_walkforward._expected_lead_days`` -- same formula, same
    convention (whole Pacific civil days to target), parity-tested (see
    ``test_research_operate.py``) rather than reaching into that module's
    own private helper, matching this codebase's established
    duplicate-plus-parity-test convention for a formula two sibling modules
    both need."""

    from .research_policy import RESEARCH_OBJECTIVE_TZ

    return (target_date - decision_at.astimezone(RESEARCH_OBJECTIVE_TZ).date()).days


def historical_rows_from_paper_store(
    conn: sqlite3.Connection,
    *,
    settlements: Mapping[SettlementKey, float],
) -> list[dict[str, object]]:
    """This project's first real bridge from the paper database's own
    history into ``research_walkforward.load_research_cases``'s row
    contract.

    Joins each ``scan_context_snapshots`` row to ONE representative
    ``decision_snapshots`` row sharing its ``scan_context_id`` (the
    earliest by ``id``) for ``forecast_predicted_high_f``/
    ``forecast_source_spread_f`` -- the SAME Gaussian mean/spread-proxy
    pair ``backtest_rescore.run_rescore`` already feeds into
    ``TradeEvaluator`` as ``forecast_high_f``/``forecast_sigma_f`` -- as
    this row's ``baseline_mu``/``baseline_sigma``. A scan with no linked
    ``decision_snapshots`` row (e.g. one that produced no market
    candidates at all) has no forecast mean/spread to derive a research
    case from and is skipped here, before ever reaching
    ``load_research_cases`` (which would otherwise skip it anyway for a
    missing ``baseline_mu``).

    ``settled_at`` is conservatively pinned to the START of the day AFTER
    ``target_date`` in the STATION's OWN fixed-standard timezone (the
    earliest moment its climate day is fully closed) -- never the Pacific
    research-objective clock, which governs money/day bucketing
    (``research_evidence._pacific_day``), not station settlement.
    ``actual_high_f`` comes from the caller-supplied ``settlements``
    mapping (``settlement_truth.load_cli_settlement_truth``'s own return
    shape, keyed by ``(series_ticker, target_date_iso)``) -- this module
    never imports the separate ``forecaster`` package or opens weather.db
    itself, mirroring the existing ``cmd_backtest_rescore`` convention
    (``adapter.load_cli_settlement_truth()`` is the CLI layer's job). A row
    with no settlement match is skipped -- an unsettled scan is not yet a
    research case.

    This function does not itself perform the Google-evidence join
    (``research_google_join.attach_google_challenger_evidence`` is a
    separate, composable step over its output) and does not itself call
    ``load_research_cases`` -- both are the caller's job
    (``run_research_evaluation`` composes all three).
    """

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT s.created_at, s.target_date, s.station_id, s.forecast_json,
               s.intraday_json, s.market_json, s.prediction_features_json,
               (SELECT d.forecast_predicted_high_f FROM decision_snapshots AS d
                 WHERE d.scan_context_id = s.id
                 ORDER BY d.id LIMIT 1) AS baseline_mu,
               (SELECT d.forecast_source_spread_f FROM decision_snapshots AS d
                 WHERE d.scan_context_id = s.id
                 ORDER BY d.id LIMIT 1) AS baseline_sigma
        FROM scan_context_snapshots AS s
        WHERE s.station_id IS NOT NULL AND s.target_date IS NOT NULL
        ORDER BY s.id
        """
    ).fetchall()

    result: list[dict[str, object]] = []
    for row in rows:
        station_id = row["station_id"]
        target_date_raw = row["target_date"]
        baseline_mu = _finite_float(row["baseline_mu"])
        baseline_sigma = _finite_float(row["baseline_sigma"])
        if not station_id or not target_date_raw or baseline_mu is None or baseline_sigma is None:
            continue
        if baseline_sigma <= 0:
            continue
        try:
            city = city_for_station(str(station_id))
        except KeyError:
            continue
        actual_high_f = settlements.get((city.series_ticker, str(target_date_raw)))
        if actual_high_f is None:
            continue
        decision_at = _parse_timestamp(row["created_at"])
        if decision_at is None:
            continue
        try:
            target_date_value = date.fromisoformat(str(target_date_raw))
        except ValueError:
            continue
        settled_at = datetime.combine(
            target_date_value + timedelta(days=1), time.min, tzinfo=city.fixed_standard_timezone()
        )
        lead_days = _expected_lead_days_pacific(decision_at, target_date_value)
        if lead_days < 0:
            continue
        result.append(
            {
                "target_date": target_date_raw,
                "station_id": station_id,
                "forecast_json": row["forecast_json"],
                "intraday_json": row["intraday_json"],
                "market_json": row["market_json"],
                "prediction_features_json": row["prediction_features_json"],
                "decision_at": decision_at.isoformat(),
                "settled_at": settled_at.isoformat(),
                "actual_high_f": float(actual_high_f),
                "baseline_mu": baseline_mu,
                "baseline_sigma": baseline_sigma,
                "lead_days": lead_days,
            }
        )
    return result


@dataclass(frozen=True)
class EvaluationRun:
    """Everything one ``research-evaluate`` invocation produces: the
    reconstructed declaration (E2), fold-construction evidence (Task 2),
    the paired KPI/capacity report (Task 5) and promotion verdict (Task 6)
    for the DECLARED challenger, and this task's own Google-join/
    persistence diagnostics. Never carries live-activation authority (E3)
    -- ``decision.live_activation_allowed`` is always ``False`` by
    construction (``research_promotion.evaluate_promotion`` never sets it,
    and nothing in this module ever does either)."""

    declaration: ChallengerDeclaration
    walk_forward: WalkForwardEvidence
    report: PairedEvidenceReport
    decision: PromotionDecision
    persisted_fold_count: int
    skipped_fold_persist_reasons: tuple[tuple[str, str], ...]
    google_join_matched_row_count: int
    google_join_skips: tuple[GoogleJoinSkip, ...]
    prior_family_attempts: tuple[FamilyAttempt, ...]


def run_research_evaluation(
    store: PaperStore,
    *,
    declaration: ChallengerDeclaration,
    historical_rows: Sequence[Mapping[str, object]],
    embargo_days: int = DEFAULT_EMBARGO_DAYS,
    persist: bool = True,
) -> EvaluationRun:
    """Task 7's one end-to-end entry point: join Google evidence, build
    chronological folds, fit/score/replay the declaration's own challenger,
    durably persist newly-seen fold evidence (``persist=False`` for a
    read-only dry run), load this hypothesis family's ENTIRE prior history
    from the immutable tables (E1), and return the promotion verdict
    alongside every layer of evidence that produced it.

    ``declaration`` must already be the reconstructed, stored-row-backed
    object ``declare_challenger``/``load_declared_challenger`` returns --
    this function does not itself declare or persist a NEW experiment
    identity (E2's "reconstructed only from the stored row" contract
    extends to this function's own input, not just its internals).

    A fold whose evidence cannot yet be persisted (its own target day is at
    or before the Pacific day ``declaration`` was declared on -- the
    database's own declared-before-window invariant) is skipped, not
    fatal: ``historical_rows`` routinely spans dates before the
    declaration too, needed only as leakage-safe TRAINING data for folds
    that settle after it.
    """

    with store.connect() as conn:
        join_result = attach_google_challenger_evidence(historical_rows, conn)

    walk_forward = build_walk_forward_evidence(join_result.rows, embargo_days=embargo_days)

    candidate_by_fold: dict[str, FoldCandidateEvidence] = {}
    replay_by_fold: dict[str, FoldReplayEvidence] = {}
    for fold in walk_forward.folds:
        for row in score_fold_candidates(fold):
            if row.challenger_candidate_key == declaration.candidate_key:
                candidate_by_fold[row.fold_id] = row
        for row in replay_fold_candidates(fold):
            if row.challenger_candidate_key == declaration.candidate_key:
                replay_by_fold[row.fold_id] = row

    persisted = 0
    skipped_persist: list[tuple[str, str]] = []
    if persist:
        for fold_id in sorted(set(candidate_by_fold) & set(replay_by_fold)):
            if _fold_evidence_already_recorded(
                store, experiment_id=declaration.experiment_id, fold_id=fold_id
            ):
                continue
            try:
                persist_fold_evidence(
                    store,
                    experiment_id=declaration.experiment_id,
                    candidate_row=candidate_by_fold[fold_id],
                    replay_row=replay_by_fold[fold_id],
                )
            except ValueError as exc:
                skipped_persist.append((fold_id, str(exc)))
                continue
            persisted += 1

    prior_attempts = load_prior_family_attempts(
        store,
        hypothesis_family=declaration.hypothesis_family,
        exclude_candidate_version=declaration.candidate_version,
    )

    candidate_rows = tuple(candidate_by_fold.values())
    replay_rows = tuple(replay_by_fold.values())

    decision = evaluate_promotion(
        declaration,
        folds=walk_forward.folds,
        unavailable_folds=walk_forward.unavailable,
        replay_evidence=replay_rows,
        candidate_evidence=candidate_rows,
        prior_family_attempts=prior_attempts,
    )

    records, exclusions = build_paired_records_for_experiment(
        walk_forward.folds,
        replay_rows,
        candidate_rows,
        challenger_candidate_key=declaration.candidate_key,
    )
    report = build_paired_evidence_report(
        records, exclusions, challenger_candidate_key=declaration.candidate_key
    )

    return EvaluationRun(
        declaration=declaration,
        walk_forward=walk_forward,
        report=report,
        decision=decision,
        persisted_fold_count=persisted,
        skipped_fold_persist_reasons=tuple(skipped_persist),
        google_join_matched_row_count=join_result.matched_row_count,
        google_join_skips=join_result.skips,
        prior_family_attempts=prior_attempts,
    )
