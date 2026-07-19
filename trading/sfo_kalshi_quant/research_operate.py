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

Repair notes (2026-07-19, three review-blocking findings plus cheap items
on top of E1-E3 above):

- CRITICAL-1 (pre-declaration folds counted toward the verdict):
  ``run_research_evaluation`` used to skip a pre-declaration fold only for
  PERSISTENCE (``persist_fold_evidence``'s own declared-before-window
  ``ValueError``) while still feeding that fold's evidence into
  ``evaluate_promotion`` -- letting a same-day declare-then-propose over
  purely retrospective history read as if it had real confirmatory
  evidence. ``_declared_pacific_day`` now reads the stored experiment
  row's own ``declared_at`` and reuses ``db._pacific_declaration_date``
  (the SAME Pacific-day conversion ``record_research_evidence`` itself
  enforces) to exclude every fold whose own target day is at or before
  that day from ``folds``/``unavailable_folds``/``candidate_by_fold``/
  ``replay_by_fold`` BEFORE ``evaluate_promotion`` and the paired-evidence
  report ever see them -- the verdict's own fold set is now always exactly
  the persistable set. Excluded folds are reported separately
  (``EvaluationRun.pre_declaration_fold_count``/``pre_declaration_fold_ids``),
  never silently dropped.
- CRITICAL-2 (round-trip p-value divergence): ``load_prior_family_attempts``
  used to reload evidence ``ORDER BY fold_id`` (station-major), while
  ``research_bootstrap.fold_paired_aggregates`` sorts its own aggregates
  ``(target_date, station_id, fold_id)`` (date-major) before handing them
  to the order-sensitive Monte Carlo bootstrap -- so a reloaded p-value
  for a multi-station family could silently diverge from the p-value
  computed at verdict time. The reload query now sorts
  ``target_date, station_id, fold_id`` to match, and ``_fold_roi_delta``
  now sums with ``math.fsum`` (matching ``fold_paired_aggregates``'s own
  per-fold summation bit-for-bit) instead of a plain running ``+=``.
- HIGH-1 (Holm bypass via version reuse across sibling keys):
  ``load_prior_family_attempts`` excludes prior history by
  ``candidate_version`` alone (matching ``evaluate_promotion``'s own G7
  check), but the schema's uniqueness is
  ``(hypothesis_family, candidate_key, candidate_version)`` -- so
  declaring a second, sibling ``candidate_key`` under an
  already-used ``candidate_version`` in the same family used to empty
  that version's own family history for THIS declaration, silently
  resetting Holm-Bonferroni's family size. ``declare_challenger`` now
  raises ``DeclarationConflictError`` when any OTHER experiment in the
  same ``hypothesis_family`` already uses the requested
  ``candidate_version``, regardless of ``candidate_key`` -- a genuinely
  new version is still always welcome, under the SAME or a different
  candidate_key.
- M-1 (silent historical-row drops): ``historical_rows_from_paper_store``
  now returns a ``HistoricalRowLoadResult`` (``rows`` plus ``skips``,
  each a ``HistoricalRowSkip`` with a reason) instead of a bare row list,
  so a malformed/unmatched/unsettled ``scan_context_snapshots`` row is
  diagnostic evidence, not a silent gap. A caller may thread
  ``historical_row_skips`` into ``run_research_evaluation``, surfaced as
  ``EvaluationRun.historical_row_skip_count``.
- M-2 (silent frozen-evidence staleness): ``research_evidence`` rows are
  immutable by schema -- a LATER Google-snapshot backfill (or any other
  change in what freshly scoring an already-recorded fold would now
  produce) can never retroactively change an already-persisted row.
  ``run_research_evaluation`` now recomputes each already-recorded fold's
  canonical baseline/challenger payload (the exact JSON shape
  ``persist_fold_evidence`` itself writes) and compares it, byte-for-byte,
  against the stored row -- a divergence is counted and fold-id-listed
  (``EvaluationRun.stale_evidence_fold_count``/``stale_evidence_fold_ids``)
  rather than silently ignored. The frozen row itself is never touched.
- Cheap items: the duplicate-run race on ``persist_fold_evidence``'s
  underlying INSERT (two concurrent invocations both passing the
  already-recorded pre-check before either commits) now converts the
  resulting ``sqlite3.IntegrityError`` into a clean already-recorded skip
  instead of crashing the whole evaluation. ``historical_rows_from_paper_store``
  now saves and restores the caller-supplied connection's own
  ``row_factory`` instead of leaving it mutated.
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
from .db import PaperStore, _pacific_declaration_date
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
    UnavailableFold,
    WalkForwardEvidence,
    WalkForwardFold,
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


def _fetch_sibling_experiment_row_for_version(
    store: PaperStore,
    *,
    hypothesis_family: str,
    candidate_version: str,
) -> StoredExperimentRow | None:
    """HIGH-1: find ANY already-declared experiment in ``hypothesis_family``
    using ``candidate_version``, regardless of its ``candidate_key`` --
    the schema's own uniqueness is on the FULL
    ``(hypothesis_family, candidate_key, candidate_version)`` triple, but
    ``load_prior_family_attempts`` excludes prior history by
    ``candidate_version`` alone (matching ``evaluate_promotion``'s own G7
    check), so two sibling candidate_keys sharing one version would
    silently erase each other's Holm-Bonferroni family history if this
    were ever allowed to be declared."""

    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT experiment_id, declared_at, hypothesis_family, candidate_key, "
            "candidate_version, parameter_json, evidence_role FROM research_experiments "
            "WHERE hypothesis_family = ? AND candidate_version = ?",
            (hypothesis_family, candidate_version),
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
        # HIGH-1: candidate_version must be unique within its
        # hypothesis_family, regardless of candidate_key -- see
        # _fetch_sibling_experiment_row_for_version's own docstring.
        sibling = _fetch_sibling_experiment_row_for_version(
            store, hypothesis_family=hypothesis_family, candidate_version=candidate_version
        )
        if sibling is not None and sibling.candidate_key != candidate_key:
            raise DeclarationConflictError(
                f"candidate_version={candidate_version!r} is already declared in "
                f"hypothesis_family={hypothesis_family!r} under a different "
                f"candidate_key={sibling.candidate_key!r} (experiment_id="
                f"{sibling.experiment_id!r}) -- every declared candidate_version must "
                "be unique within its hypothesis_family, regardless of candidate_key, "
                "or Holm-Bonferroni family history would be silently emptied"
            )
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


def _fold_evidence_payloads(
    candidate_row: FoldCandidateEvidence,
    replay_row: FoldReplayEvidence,
) -> tuple[dict[str, object], dict[str, object]]:
    """Build the exact ``(baseline_payload, challenger_payload)`` pair
    ``persist_fold_evidence`` writes -- factored out so ``_stored_evidence_
    diverges`` (M-2) compares a freshly-recomputed fold against an
    already-stored row using the IDENTICAL shape, never a hand-approximated
    one that could drift from what was actually persisted.

    Raises ``ValueError`` if the two rows disagree about which
    fold/station/date/challenger they belong to -- never silently pairs
    mismatched evidence.
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
    return baseline_payload, challenger_payload


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

    baseline_payload, challenger_payload = _fold_evidence_payloads(candidate_row, replay_row)
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


def _canonical_json(payload: Mapping[str, object]) -> str:
    """The exact canonicalization ``PaperStore.record_research_evidence``
    itself uses to write ``baseline_json``/``challenger_json`` -- reused
    here (M-2) so a byte-for-byte comparison against an already-stored row
    never false-positives on formatting alone."""

    return json.dumps(dict(payload), sort_keys=True, allow_nan=False)


def _stored_evidence_diverges(
    store: PaperStore,
    *,
    experiment_id: str,
    fold_id: str,
    candidate_row: FoldCandidateEvidence,
    replay_row: FoldReplayEvidence,
) -> bool:
    """M-2: ``research_evidence`` rows are immutable by schema -- a LATER
    Google-snapshot backfill (or any other change in what freshly scoring
    this fold would now produce) can never retroactively alter an
    already-persisted row. That immutability means a freshly recomputed
    payload silently drifting from what is already stored would otherwise
    go unnoticed. Compares by the SAME canonical JSON
    ``persist_fold_evidence`` itself writes (``_fold_evidence_payloads`` /
    ``_canonical_json``), so an exact match is never a false positive from
    formatting alone. Returns ``False`` (never diverges) if no stored row
    exists for this fold at all -- that is a persistence question, not a
    staleness one.
    """

    with store.connect() as conn:
        row = conn.execute(
            "SELECT baseline_json, challenger_json FROM research_evidence "
            "WHERE experiment_id = ? AND fold_id = ?",
            (experiment_id, fold_id),
        ).fetchone()
    if row is None:
        return False
    baseline_payload, challenger_payload = _fold_evidence_payloads(candidate_row, replay_row)
    return _canonical_json(baseline_payload) != row[0] or _canonical_json(challenger_payload) != row[1]


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
    baseline_pnls: list[float] = []
    challenger_pnls: list[float] = []
    for source_hash in sorted(set(baseline_cases) & set(challenger_cases)):
        baseline_case = baseline_cases[source_hash]
        challenger_case = challenger_cases[source_hash]
        if not baseline_case.get("available") or not challenger_case.get("available"):
            continue
        baseline_pnls.append(float(baseline_case.get("realized_pnl") or 0.0))
        challenger_pnls.append(float(challenger_case.get("realized_pnl") or 0.0))
    if not baseline_pnls:
        return None
    # CRITICAL-2: math.fsum, not a plain running total -- matches
    # research_bootstrap.fold_paired_aggregates's own per-fold summation
    # bit-for-bit, so a reloaded delta can never numerically drift from
    # the one computed at verdict time.
    baseline_total = math.fsum(baseline_pnls)
    challenger_total = math.fsum(challenger_pnls)
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
            # CRITICAL-2: date-major, matching fold_paired_aggregates's own
            # (target_date, station_id, fold_id) sort -- never fold_id
            # alone (station-major), which fed the order-sensitive Monte
            # Carlo bootstrap a differently-ordered sequence than the one
            # verdict time itself used for a multi-station family.
            evidence_rows = conn.execute(
                "SELECT baseline_json, challenger_json FROM research_evidence "
                "WHERE experiment_id = ? ORDER BY target_date, station_id, fold_id",
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


def _declared_pacific_day(store: PaperStore, declaration: ChallengerDeclaration) -> date:
    """CRITICAL-1: the SAME Pacific civil day
    ``PaperStore.record_research_evidence`` itself compares a fold's own
    target day against (via ``db._pacific_declaration_date`` -- reused
    here, never re-derived) -- read straight from the stored
    ``research_experiments`` row's own ``declared_at``, never from
    whatever in-memory value a caller happens to be holding. Fails closed
    (``ValueError``/``LookupError``) rather than silently treating a
    missing or malformed declaration as "declared at the dawn of time",
    which would admit every fold instead of excluding pre-declaration
    ones.
    """

    row = _fetch_experiment_row(
        store,
        hypothesis_family=declaration.hypothesis_family,
        candidate_key=declaration.candidate_key,
        candidate_version=declaration.candidate_version,
    )
    if row is None:  # pragma: no cover - defensive; declaration is already stored-row-backed
        raise LookupError(
            f"no stored research_experiments row for experiment_id={declaration.experiment_id!r} "
            "-- declaration must already be reconstructed from a stored row"
        )
    declared_pacific_day = _pacific_declaration_date(row.declared_at)
    if declared_pacific_day is None:
        raise ValueError(
            f"stored declared_at={row.declared_at!r} for experiment_id="
            f"{declaration.experiment_id!r} is malformed or timezone-naive -- cannot "
            "determine its Pacific declaration day"
        )
    return declared_pacific_day


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


@dataclass(frozen=True)
class HistoricalRowSkip:
    """One ``scan_context_snapshots`` row ``historical_rows_from_paper_store``
    could not turn into a loadable historical row, and why (M-1) -- never a
    silent gap between what the paper database holds and what actually
    reached ``run_research_evaluation``."""

    row_index: int
    station_id: str | None
    target_date: str | None
    reason: str


@dataclass(frozen=True)
class HistoricalRowLoadResult:
    """``historical_rows_from_paper_store``'s full return: the loadable
    rows plus every row it dropped, with a reason (M-1)."""

    rows: tuple[dict[str, object], ...]
    skips: tuple[HistoricalRowSkip, ...]


def _historical_row_skip(
    row_index: int, station_id: object, target_date_raw: object, reason: str
) -> HistoricalRowSkip:
    return HistoricalRowSkip(
        row_index=row_index,
        station_id=str(station_id) if station_id else None,
        target_date=str(target_date_raw) if target_date_raw else None,
        reason=reason,
    )


def historical_rows_from_paper_store(
    conn: sqlite3.Connection,
    *,
    settlements: Mapping[SettlementKey, float],
) -> HistoricalRowLoadResult:
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

    ``conn`` is caller-supplied, so its own ``row_factory`` is saved and
    restored around this function's own read -- never left mutated for
    whatever the caller does with the connection afterward.
    """

    previous_row_factory = conn.row_factory
    try:
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
    finally:
        conn.row_factory = previous_row_factory

    result: list[dict[str, object]] = []
    skips: list[HistoricalRowSkip] = []
    for index, row in enumerate(rows):
        station_id = row["station_id"]
        target_date_raw = row["target_date"]
        baseline_mu = _finite_float(row["baseline_mu"])
        baseline_sigma = _finite_float(row["baseline_sigma"])
        if not station_id or not target_date_raw or baseline_mu is None or baseline_sigma is None:
            skips.append(_historical_row_skip(index, station_id, target_date_raw, "missing_station_target_date_or_baseline"))
            continue
        if baseline_sigma <= 0:
            skips.append(_historical_row_skip(index, station_id, target_date_raw, "non_positive_baseline_sigma"))
            continue
        try:
            city = city_for_station(str(station_id))
        except KeyError:
            skips.append(_historical_row_skip(index, station_id, target_date_raw, "unknown_station"))
            continue
        actual_high_f = settlements.get((city.series_ticker, str(target_date_raw)))
        if actual_high_f is None:
            skips.append(_historical_row_skip(index, station_id, target_date_raw, "no_settlement_match"))
            continue
        decision_at = _parse_timestamp(row["created_at"])
        if decision_at is None:
            skips.append(_historical_row_skip(index, station_id, target_date_raw, "invalid_created_at"))
            continue
        try:
            target_date_value = date.fromisoformat(str(target_date_raw))
        except ValueError:
            skips.append(_historical_row_skip(index, station_id, target_date_raw, "invalid_target_date"))
            continue
        settled_at = datetime.combine(
            target_date_value + timedelta(days=1), time.min, tzinfo=city.fixed_standard_timezone()
        )
        lead_days = _expected_lead_days_pacific(decision_at, target_date_value)
        if lead_days < 0:
            skips.append(_historical_row_skip(index, station_id, target_date_raw, "negative_lead_days"))
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
    return HistoricalRowLoadResult(rows=tuple(result), skips=tuple(skips))


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
    # CRITICAL-1: folds excluded from the verdict (folds/unavailable_folds/
    # candidate_by_fold/replay_by_fold) because their own target day is at
    # or before declaration's Pacific declaration day -- reported
    # separately so the exclusion is diagnostic evidence, never silent.
    pre_declaration_fold_count: int = 0
    pre_declaration_fold_ids: tuple[str, ...] = ()
    # M-2: an already-recorded fold whose freshly recomputed canonical
    # payload no longer matches its immutable stored row (e.g. a later
    # Google-snapshot backfill) -- the stored row itself is never touched;
    # this only makes the divergence visible.
    stale_evidence_fold_count: int = 0
    stale_evidence_fold_ids: tuple[str, ...] = ()
    # M-1: a scan_context_snapshots row historical_rows_from_paper_store
    # (or another historical-row source) dropped before it ever reached
    # this function, passed through by the caller via
    # run_research_evaluation's own historical_row_skips argument.
    historical_row_skip_count: int = 0


def run_research_evaluation(
    store: PaperStore,
    *,
    declaration: ChallengerDeclaration,
    historical_rows: Sequence[Mapping[str, object]],
    embargo_days: int = DEFAULT_EMBARGO_DAYS,
    persist: bool = True,
    historical_row_skips: Sequence[HistoricalRowSkip] = (),
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

    A fold whose own target day is at or before the Pacific day
    ``declaration`` was declared on (the database's own declared-before-
    window invariant) is excluded from the verdict entirely -- from
    ``folds``/``unavailable_folds``/``candidate_by_fold``/``replay_by_fold``,
    BEFORE ``evaluate_promotion`` or the paired-evidence report ever see
    them (CRITICAL-1) -- never merely skipped at persistence while still
    silently feeding the verdict. This is not fatal: ``historical_rows``
    routinely spans dates before the declaration too, needed only as
    leakage-safe TRAINING data for folds that settle after it. Every
    excluded fold is counted and listed on the returned
    ``EvaluationRun.pre_declaration_fold_count``/``pre_declaration_fold_ids``.

    ``historical_row_skips`` is an optional, purely diagnostic pass-through
    (M-1) -- typically ``historical_rows_from_paper_store``'s own
    ``HistoricalRowLoadResult.skips`` -- surfaced unchanged as
    ``EvaluationRun.historical_row_skip_count``; this function does not
    itself inspect or validate them.
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

    # CRITICAL-1: exclude every fold whose own target day is at or before
    # declaration's Pacific declaration day from the verdict's own fold
    # set -- BEFORE persistence and BEFORE evaluate_promotion -- so the
    # verdict's fold set is always exactly the persistable set.
    declared_pacific_day = _declared_pacific_day(store, declaration)
    eligible_folds: list[WalkForwardFold] = []
    pre_declaration_fold_ids: set[str] = set()
    for fold in walk_forward.folds:
        if fold.test[0].target_date <= declared_pacific_day:
            pre_declaration_fold_ids.add(fold.fold_id)
        else:
            eligible_folds.append(fold)
    eligible_unavailable: list[UnavailableFold] = []
    for unavailable_fold in walk_forward.unavailable:
        if unavailable_fold.target_date <= declared_pacific_day:
            pre_declaration_fold_ids.add(unavailable_fold.fold_id)
        else:
            eligible_unavailable.append(unavailable_fold)
    candidate_by_fold = {
        fold_id: row for fold_id, row in candidate_by_fold.items() if fold_id not in pre_declaration_fold_ids
    }
    replay_by_fold = {
        fold_id: row for fold_id, row in replay_by_fold.items() if fold_id not in pre_declaration_fold_ids
    }

    persisted = 0
    skipped_persist: list[tuple[str, str]] = []
    stale_fold_ids: list[str] = []
    if persist:
        for fold_id in sorted(set(candidate_by_fold) & set(replay_by_fold)):
            if _fold_evidence_already_recorded(
                store, experiment_id=declaration.experiment_id, fold_id=fold_id
            ):
                # M-2: an already-recorded, immutable fold -- never
                # re-persisted, but a freshly recomputed payload that no
                # longer matches the stored row (e.g. a later Google-
                # snapshot backfill) is surfaced, not silently ignored.
                if _stored_evidence_diverges(
                    store,
                    experiment_id=declaration.experiment_id,
                    fold_id=fold_id,
                    candidate_row=candidate_by_fold[fold_id],
                    replay_row=replay_by_fold[fold_id],
                ):
                    stale_fold_ids.append(fold_id)
                continue
            try:
                persist_fold_evidence(
                    store,
                    experiment_id=declaration.experiment_id,
                    candidate_row=candidate_by_fold[fold_id],
                    replay_row=replay_by_fold[fold_id],
                )
            except sqlite3.IntegrityError:
                # Cheap item: a concurrent duplicate run already recorded
                # this exact fold between our own check above and this
                # insert (idempotent operation across concurrent
                # invocations) -- a clean already-recorded skip, not a
                # failure.
                continue
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
    eligible_folds_tuple = tuple(eligible_folds)

    decision = evaluate_promotion(
        declaration,
        folds=eligible_folds_tuple,
        unavailable_folds=tuple(eligible_unavailable),
        replay_evidence=replay_rows,
        candidate_evidence=candidate_rows,
        prior_family_attempts=prior_attempts,
    )

    records, exclusions = build_paired_records_for_experiment(
        eligible_folds_tuple,
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
        pre_declaration_fold_count=len(pre_declaration_fold_ids),
        pre_declaration_fold_ids=tuple(sorted(pre_declaration_fold_ids)),
        stale_evidence_fold_count=len(stale_fold_ids),
        stale_evidence_fold_ids=tuple(stale_fold_ids),
        historical_row_skip_count=len(historical_row_skips),
    )
