"""Task 2: leakage-resistant chronological folds for research walk-forward tuning.

Builds on Task 1's immutable ``research_experiments``/``research_evidence``
tables and the load-time ``source_neutral_context_from_scan_context_row``
helper (``trading/sfo_kalshi_quant/db.py``) to turn historical, per-profile
``scan_context_snapshots`` rows into deduplicated, chronologically ordered
``WalkForwardFold`` objects.

Task 3 (predeclared candidate fitting -- pooling, the three challenger
arms, and per-case scoring) builds on top of this module's
``ResearchCase``/``WalkForwardFold`` types from two siblings kept separate
for file-size cohesion (coding-style guidance: many small files over one
large one):

- ``research_candidates.py`` -- pooling machinery, the three predeclared
  candidate identities, and fold/case fitting + evidence bundling.
- ``research_scoring.py`` -- pure Gaussian distributional scoring math
  (CRPS, log score, PIT, bracket Brier, calibration gap, etc.), with zero
  dependency on any type in this module.

Five load-bearing leakage-resistance guarantees, enforced structurally
(not by caller convention) in this module:

1. **Chronology.** A fold's training pool never contains a case whose own
   ``target_date`` is at-or-after the test fold's ``target_date``
   (``_case_eligible_for_training``'s day-order check), *and* never
   contains a case that settled at or after the earliest test decision in
   the fold (settlement-before-decision, strict ``<`` -- a case that
   settles at the exact same instant as the earliest test decision is
   excluded, not just one that settles later). Both checks are
   independent of each other by design -- the day-order check holds even
   if a row's ``settled_at`` were wrong or adversarially early.
2. **Embargo.** A configurable number of days (default
   ``DEFAULT_EMBARGO_DAYS``) immediately preceding the test day is purged
   from that *same station's* training pool, to bound weather
   autocorrelation leakage at the fold boundary. Cross-station data on an
   adjacent day is not embargoed.
3. **Load-time dedupe.** Rows are grouped by the derived
   ``source_context_hash`` before folding, so the same real-world scan
   observed by multiple risk profiles collapses into exactly one
   ``ResearchCase``. Rows that cannot be canonicalized, or that disagree
   with a sibling row sharing their hash, are skipped with a recorded
   reason rather than guessed at.
4. **Fail closed.** Malformed contexts, timezone-naive or unparseable
   timestamps, non-finite outcome values, cases whose own ``settled_at``
   precedes their own ``decision_at``, and a ``lead_days`` value that is
   inconsistent with the case's own ``decision_at``/``target_date`` (a
   corrupt lead value can never silently mispool a candidate into the
   wrong cohort -- see ``_expected_lead_days``) are all excluded loudly (a
   ``CaseSkip`` reason), never silently dropped or coerced.
5. **Determinism.** ``load_research_cases``/``build_walk_forward_folds``
   take no clock or randomness inputs; every output collection is sorted
   by content (never by input/row order), so the same input rows -- in any
   order -- always produce byte-identical folds. ``research_candidates.py``'s
   ``fit_fold_candidates`` extends this: it is a pure function of a fold's
   ``train`` tuple alone -- it never reads ``test`` -- so mutating a test
   case's outcome can never change the parameters used to score that same
   case.

``research_candidates.py`` adds one more guarantee on top of these:

6. **Predeclaration linkage.** Every challenger candidate it fits
   (``GAUSSIAN_PIT_CANDIDATE_KEY``, ``GOOGLE_RUNTIME_CANDIDATE_KEY``) has a
   frozen ``candidate_key``/``candidate_version``/``hypothesis_family``
   identity (``declared_research_candidates()``) that must be registered
   via ``PaperStore.record_research_experiment`` *before*
   ``PaperStore.record_research_evidence`` can persist any score for it --
   the database enforces this with a foreign key plus a BEFORE INSERT
   trigger (Task 1), so writing evidence for an undeclared candidate is a
   ``sqlite3.IntegrityError``, not a silent acceptance. Neither this module
   nor ``research_candidates.py`` talks to the database itself; they only
   guarantee that what gets fit and scored carries exactly the identity a
   caller must declare first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Mapping, Sequence

from .db import source_neutral_context_from_scan_context_row
from .research_policy import RESEARCH_OBJECTIVE_TZ

# Number of days immediately preceding a test day, for the same station,
# whose training candidates are purged even though they technically settled
# before the test decision -- bounds weather autocorrelation leakage at the
# fold boundary (plan Task 2 Step 3: "the configurable one-day embargo").
DEFAULT_EMBARGO_DAYS = 1

# Mirrors forecaster/google_runtime_blend.py's frozen policy identity and
# action vocabulary -- duplicated rather than imported, on purpose. Unlike
# ``.cities``/``.account`` (same ``sfo_kalshi_quant`` package, always on a
# production sys.path), ``forecaster`` is a separate top-level package that
# is NOT part of this project's installed package
# (``[tool.setuptools.packages.find]`` only includes ``sfo_kalshi_quant*``);
# it is importable only under pytest's dev-only ``pythonpath``. This is the
# same duplicate-plus-parity-test convention ``cities.py`` documents and
# ``test_cities_parity.py`` enforces --
# ``test_google_runtime_challenger_constants_match_forecaster`` in
# ``test_research_walkforward.py`` is this module's parity lock.
GOOGLE_CHALLENGER_POLICY_VERSION = "google-runtime-fixed-v1"
GOOGLE_CHALLENGER_FORECAST_ACTION = "forecast"
GOOGLE_CHALLENGER_BLOCK_ACTION = "external_runtime_corroboration_block"


@dataclass(frozen=True)
class GoogleChallengerEvidence:
    """One case's already-derived Google-conditioned challenger evidence.

    Shape mirrors the durable, TTL-free evidence spec section 7.2 allows to
    persist past the raw Google runtime TTL -- mean, sigma, action, and
    policy version only. It never carries a raw Google high, response, or
    disagreement gap; this module only ever *consumes* an already-computed
    challenger (never recomputes one from a raw Google value), so a
    historical row with no such evidence attached is simply unavailable,
    never backfilled or guessed at.
    """

    mu: float | None
    sigma: float
    action: str
    policy_version: str = GOOGLE_CHALLENGER_POLICY_VERSION


@dataclass(frozen=True)
class ResearchCase:
    """One settled, source-neutral observation available for folding."""

    station_id: str
    target_date: date
    decision_at: datetime
    settled_at: datetime
    lead_days: int
    source_context_hash: str
    baseline_mu: float
    baseline_sigma: float
    actual_high_f: float
    google_evidence: GoogleChallengerEvidence | None = None
    # The same scan-time market payload (ticker -> {yes_bid, yes_ask,
    # yes_bid_size, yes_ask_size, strike_type, floor_strike, cap_strike, ...},
    # the `_market_diagnostics_payload` shape) that already contributes to
    # this case's own `source_context_hash` -- attached here (Task 4) so an
    # execution replay can price/size/gate a candidate's decision against the
    # exact quote observed at decision time, instead of a synthetic mid-price.
    # Optional/defaulted so every existing direct-construction call site
    # (Task 2/3 tests) that predates Task 4 keeps constructing valid cases
    # unchanged. ``None`` means "no market snapshot available for this case"
    # (fails closed downstream -- never a fabricated quote), which is the
    # normal state for a case built without going through
    # ``load_research_cases``.
    #
    # NOTE: this field is typically a plain ``dict`` alias of whatever the
    # loader produced (never copied), so it is mutable even though
    # ``ResearchCase`` itself is a frozen dataclass, and it makes the
    # dataclass's auto-generated ``__hash__`` raise ``TypeError`` the moment
    # anything actually calls it (dicts aren't hashable). Treat every
    # ``ResearchCase`` carrying a non-``None`` ``market_snapshot`` as
    # read-only and non-hashable; never mutate this mapping in place.
    market_snapshot: Mapping[str, Mapping[str, object]] | None = None

    def __post_init__(self) -> None:
        if self.decision_at.tzinfo is None:
            raise ValueError("ResearchCase.decision_at must be timezone-aware")
        if self.settled_at.tzinfo is None:
            raise ValueError("ResearchCase.settled_at must be timezone-aware")
        if self.settled_at < self.decision_at:
            raise ValueError(
                "ResearchCase cannot settle before its own decision"
            )
        if self.lead_days < 0:
            raise ValueError("ResearchCase.lead_days cannot be negative")
        for label, value in (
            ("baseline_mu", self.baseline_mu),
            ("baseline_sigma", self.baseline_sigma),
            ("actual_high_f", self.actual_high_f),
        ):
            if not math.isfinite(value):
                raise ValueError(f"ResearchCase.{label} must be finite")
        if self.baseline_sigma <= 0:
            raise ValueError("ResearchCase.baseline_sigma must be positive")
        # Binding condition C2 (Task 2 review finding L5): lead_days must be
        # consistent with the case's own decision_at/target_date, checked
        # last so every other direct-construction regression above keeps
        # raising for its own originally-intended reason first. A corrupt
        # lead_days must be a loud rejection here (and, via the loader's
        # own explicit pre-check in ``_build_case``, a recorded skip) --
        # never a silent mispool into the wrong station/lead cohort.
        if self.lead_days != _expected_lead_days(self.decision_at, self.target_date):
            raise ValueError(
                "ResearchCase.lead_days is inconsistent with its own "
                "decision_at/target_date"
            )


def _expected_lead_days(decision_at: datetime, target_date: date) -> int:
    """Canonical lead-days formula: whole Pacific civil days to target.

    Mirrors ``PaperStore``'s own ``lead_days = (target_day - civil_day).days``
    (``trading/sfo_kalshi_quant/db.py``, research admission), anchored to
    ``RESEARCH_OBJECTIVE_TZ`` (America/Los_Angeles) -- the one civil clock
    every research lead/embargo/promotion computation in this project uses,
    never a per-station local time.
    """

    return (target_date - decision_at.astimezone(RESEARCH_OBJECTIVE_TZ).date()).days


@dataclass(frozen=True)
class CaseSkip:
    """One historical row excluded from research cases, with why."""

    row_index: int
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class CaseLoadResult:
    cases: tuple[ResearchCase, ...]
    skips: tuple[CaseSkip, ...]


@dataclass(frozen=True)
class WalkForwardFold:
    """One indivisible station/target-day test group and its training pool."""

    fold_id: str
    decision_at: datetime
    train: tuple[ResearchCase, ...]
    test: tuple[ResearchCase, ...]


@dataclass(frozen=True)
class UnavailableFold:
    """A candidate test group with no leakage-safe training pool at all."""

    fold_id: str
    station_id: str
    target_date: date
    reason: str


@dataclass(frozen=True)
class FoldBuildResult:
    folds: tuple[WalkForwardFold, ...]
    unavailable: tuple[UnavailableFold, ...]


@dataclass(frozen=True)
class WalkForwardEvidence:
    """Bundled fold-construction evidence: what folded, what didn't, and why
    every skipped historical row never made it into a ``ResearchCase``."""

    folds: tuple[WalkForwardFold, ...]
    unavailable: tuple[UnavailableFold, ...]
    skips: tuple[CaseSkip, ...]


def _parse_strict_timestamp(value: object) -> datetime | None:
    """Parse a timezone-aware timestamp; return ``None`` on any ambiguity.

    Deliberately does not fall back to assuming UTC for a naive value (the
    way ``trading/sfo_kalshi_quant/_util.py``'s ``_parse_timestamp`` does
    for legacy tolerant call sites) -- a chronological fold has no safe
    default for an ambiguous decision or settlement instant, so it must
    fail closed instead of guessing.
    """

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _build_case(
    index: int,
    row: Mapping[str, object],
    context: Mapping[str, object],
    skips: list[CaseSkip],
) -> ResearchCase | None:
    decision_at = _parse_strict_timestamp(row.get("decision_at"))
    if decision_at is None:
        skips.append(
            CaseSkip(index, "invalid_decision_at", "missing or timezone-naive")
        )
        return None
    settled_at = _parse_strict_timestamp(row.get("settled_at"))
    if settled_at is None:
        skips.append(
            CaseSkip(index, "invalid_settled_at", "missing or timezone-naive")
        )
        return None
    if settled_at < decision_at:
        skips.append(
            CaseSkip(
                index,
                "settlement_before_decision",
                "row's own settled_at precedes its own decision_at",
            )
        )
        return None

    actual_high_f = _finite_float(row.get("actual_high_f"))
    if actual_high_f is None:
        skips.append(
            CaseSkip(index, "invalid_actual_high_f", "missing or non-finite")
        )
        return None
    baseline_mu = _finite_float(row.get("baseline_mu"))
    if baseline_mu is None:
        skips.append(CaseSkip(index, "invalid_baseline_mu", "missing or non-finite"))
        return None
    baseline_sigma = _finite_float(row.get("baseline_sigma"))
    if baseline_sigma is None or baseline_sigma <= 0:
        skips.append(
            CaseSkip(
                index,
                "invalid_baseline_sigma",
                "missing, non-finite, or non-positive",
            )
        )
        return None

    lead_days = row.get("lead_days")
    if isinstance(lead_days, bool) or not isinstance(lead_days, int) or lead_days < 0:
        skips.append(
            CaseSkip(index, "invalid_lead_days", "missing, non-integer, or negative")
        )
        return None

    target_date_value = date.fromisoformat(str(context["target_date"]))
    expected_lead_days = _expected_lead_days(decision_at, target_date_value)
    if lead_days != expected_lead_days:
        # C2 (Task 2 review finding L5): never pool a case under a
        # lead_days value its own decision_at/target_date does not imply --
        # a corrupt lead_days is a recorded skip, not a silent mispool.
        skips.append(
            CaseSkip(
                index,
                "lead_days_inconsistent_with_decision_and_target",
                f"declared lead_days={lead_days} but decision_at/target_date "
                f"implies {expected_lead_days}",
            )
        )
        return None

    google_evidence, google_skip = _build_google_evidence(row)
    if google_skip is not None:
        skips.append(CaseSkip(index, "invalid_google_evidence", google_skip))
        return None

    try:
        return ResearchCase(
            station_id=str(context["station_id"]),
            target_date=target_date_value,
            decision_at=decision_at,
            settled_at=settled_at,
            lead_days=lead_days,
            source_context_hash=str(context["source_context_hash"]),
            baseline_mu=baseline_mu,
            baseline_sigma=baseline_sigma,
            actual_high_f=actual_high_f,
            google_evidence=google_evidence,
            # Same payload the hash was already derived from (Task 1); never
            # re-fetched or re-derived independently, so it can never drift
            # from the identity this case is keyed by.
            market_snapshot=context.get("market"),
        )
    except ValueError as exc:
        skips.append(CaseSkip(index, "invalid_case", str(exc)))
        return None


def _build_google_evidence(
    row: Mapping[str, object],
) -> tuple[GoogleChallengerEvidence | None, str | None]:
    """Parse a row's optional, already-derived Google challenger evidence.

    Returns ``(evidence, skip_reason)``: at most one is non-``None``. A row
    that carries none of the four optional ``google_challenger_*`` fields
    has no Google evidence at all -- either because the historical row
    predates that evidence, or because
    ``research_google_join.attach_google_challenger_evidence`` (chrono
    Task 7) found no vintage-coherent, point-in-time-eligible match in the
    now-landed ``google_challenger_snapshots`` table for this case's own
    source-context group -- and returns ``(None, None)``: not an error,
    just "no evidence for this case" (fails neutral downstream, per spec
    section 7.3). A row that carries a partial or self-inconsistent set of
    these fields is a data-integrity problem, not silently-missing
    evidence, so it is reported as a skip reason instead: never backfilled
    or guessed at. (``attach_google_challenger_evidence`` itself never
    produces a partial set -- see that module's J4 -- so reaching this
    branch means a row was hand-built or came from some other caller.)
    """

    action = row.get("google_challenger_action")
    mu = row.get("google_challenger_mu")
    sigma = row.get("google_challenger_sigma")
    policy_version = row.get("google_challenger_policy_version")

    if action is None and mu is None and sigma is None and policy_version is None:
        return None, None

    if policy_version != GOOGLE_CHALLENGER_POLICY_VERSION:
        return None, f"unsupported google_challenger_policy_version={policy_version!r}"
    if action not in (GOOGLE_CHALLENGER_FORECAST_ACTION, GOOGLE_CHALLENGER_BLOCK_ACTION):
        return None, f"unrecognized google_challenger_action={action!r}"

    sigma_value = _finite_float(sigma)
    if sigma_value is None or sigma_value <= 0:
        return None, "google_challenger_sigma missing, non-finite, or non-positive"

    if action == GOOGLE_CHALLENGER_BLOCK_ACTION:
        if mu is not None:
            return None, "google_challenger_mu must be absent for a blocked action"
        return (
            GoogleChallengerEvidence(
                mu=None, sigma=sigma_value, action=action, policy_version=policy_version
            ),
            None,
        )

    mu_value = _finite_float(mu)
    if mu_value is None:
        return None, "google_challenger_mu missing or non-finite for a forecast action"
    return (
        GoogleChallengerEvidence(
            mu=mu_value, sigma=sigma_value, action=action, policy_version=policy_version
        ),
        None,
    )


def _reconcile_duplicates(
    context_hash: str,
    built: list[tuple[int, ResearchCase]],
    skips: list[CaseSkip],
) -> ResearchCase | None:
    """Collapse rows sharing one source_context_hash into one canonical case.

    ``decision_at`` legitimately varies by a few seconds across profiles
    scanning the same content in the same cycle, so the earliest is used.
    Every other research-relevant field must agree exactly -- a mismatch
    means two profiles disagree about the same real-world observation's
    outcome, which is a data-integrity problem this loader must not paper
    over by picking a winner.
    """

    if not built:
        return None
    if len(built) == 1:
        return built[0][1]

    reference = built[0][1]
    for _, case in built[1:]:
        if (
            case.settled_at != reference.settled_at
            or case.actual_high_f != reference.actual_high_f
            or case.baseline_mu != reference.baseline_mu
            or case.baseline_sigma != reference.baseline_sigma
            or case.lead_days != reference.lead_days
            or case.google_evidence != reference.google_evidence
        ):
            for skip_index, _ in built:
                skips.append(
                    CaseSkip(
                        skip_index,
                        "inconsistent_duplicate",
                        f"rows sharing source_context_hash={context_hash} "
                        "disagree on settlement/outcome fields",
                    )
                )
            return None

    _, canonical = min(built, key=lambda item: (item[1].decision_at, item[0]))
    return canonical


def load_research_cases(rows: Sequence[Mapping[str, object]]) -> CaseLoadResult:
    """Derive deduplicated, source-neutral ``ResearchCase``s from historical rows.

    Each row is expected to carry the ``scan_context_snapshots`` payload
    columns (``target_date``, ``station_id``, ``forecast_json``,
    ``intraday_json``, ``market_json``, ``prediction_features_json``) plus
    the settled-outcome fields a ``ResearchCase`` needs (``decision_at``,
    ``settled_at``, ``actual_high_f``, ``baseline_mu``, ``baseline_sigma``,
    ``lead_days``).

    Rows are grouped by the derived ``source_context_hash`` (Task 1's
    loader contract, via ``source_neutral_context_from_scan_context_row``)
    -- the same real-world scan observed by more than one risk profile
    collapses into one ``ResearchCase``. A row that cannot be
    canonicalized, has an ambiguous/invalid outcome field, or disagrees
    with a sibling row sharing its hash, is skipped with a recorded reason
    instead of being silently dropped or guessed at.

    Output is sorted purely by content (``target_date``, ``station_id``,
    ``source_context_hash``), never by input row order, so the same input
    rows in any order produce an identical ``cases`` tuple.
    """

    by_hash: dict[str, list[tuple[int, Mapping[str, object], Mapping[str, object]]]] = {}
    skips: list[CaseSkip] = []
    for index, row in enumerate(rows):
        context = source_neutral_context_from_scan_context_row(dict(row))
        if context is None:
            skips.append(
                CaseSkip(
                    index,
                    "malformed_source_context",
                    "source_neutral_context_from_scan_context_row returned None",
                )
            )
            continue
        by_hash.setdefault(str(context["source_context_hash"]), []).append(
            (index, row, context)
        )

    cases: list[ResearchCase] = []
    for context_hash, entries in by_hash.items():
        built: list[tuple[int, ResearchCase]] = []
        for index, row, context in entries:
            case = _build_case(index, row, context, skips)
            if case is not None:
                built.append((index, case))
        canonical = _reconcile_duplicates(context_hash, built, skips)
        if canonical is not None:
            cases.append(canonical)

    cases.sort(key=lambda c: (c.target_date, c.station_id, c.source_context_hash))
    skips.sort(key=lambda s: s.row_index)
    return CaseLoadResult(cases=tuple(cases), skips=tuple(skips))


def _case_eligible_for_training(
    candidate: ResearchCase,
    *,
    station_id: str,
    target_date: date,
    min_test_decision_at: datetime,
    embargo_days: int,
) -> bool:
    # Guarantee 1 (structural): never use a case whose own target day is at
    # or after the day being predicted -- independent of what its
    # settled_at timestamp claims, so a corrupted or adversarial settled_at
    # cannot smuggle same-day or future-day truth into training.
    if candidate.target_date >= target_date:
        return False
    # Guarantee 1 (temporal): settlement-before-decision. A training case's
    # truth value is usable only once it settled strictly before the
    # earliest test decision in this fold.
    if not candidate.settled_at < min_test_decision_at:
        return False
    # Guarantee 2: embargo. Purge the window immediately preceding the test
    # day for the SAME station, where weather autocorrelation risk is
    # highest. A different station on an adjacent day is not embargoed.
    if candidate.station_id == station_id:
        gap_days = (target_date - candidate.target_date).days
        if gap_days <= embargo_days:
            return False
    return True


def build_walk_forward_folds(
    cases: Sequence[ResearchCase],
    *,
    embargo_days: int = DEFAULT_EMBARGO_DAYS,
) -> FoldBuildResult:
    """Group cases into indivisible station/target-day folds.

    Test rows are grouped by ``(station_id, target_date)`` -- an
    indivisible unit, so correlated market brackets for the same city and
    target day always stay in one fold's ``test`` tuple. A group with no
    leakage-safe training candidate at all is reported as an
    ``UnavailableFold`` rather than silently omitted or backed by future
    data.

    Deterministic: every fold's ``train``/``test`` tuples and the overall
    ``folds``/``unavailable`` sequences are sorted by content, never by
    ``cases`` input order.
    """

    if embargo_days < 0:
        raise ValueError("embargo_days cannot be negative")

    groups: dict[tuple[str, date], list[ResearchCase]] = {}
    for case in cases:
        groups.setdefault((case.station_id, case.target_date), []).append(case)

    folds: list[WalkForwardFold] = []
    unavailable: list[UnavailableFold] = []

    for (station_id, target_date), test_cases in groups.items():
        test_sorted = tuple(
            sorted(test_cases, key=lambda c: (c.source_context_hash, c.decision_at))
        )
        min_test_decision_at = min(c.decision_at for c in test_sorted)
        fold_id = f"{station_id}:{target_date.isoformat()}"

        train_cases = [
            candidate
            for candidate in cases
            if _case_eligible_for_training(
                candidate,
                station_id=station_id,
                target_date=target_date,
                min_test_decision_at=min_test_decision_at,
                embargo_days=embargo_days,
            )
        ]

        if not train_cases:
            unavailable.append(
                UnavailableFold(
                    fold_id=fold_id,
                    station_id=station_id,
                    target_date=target_date,
                    reason="no_training_history",
                )
            )
            continue

        train_sorted = tuple(
            sorted(
                train_cases,
                key=lambda c: (c.target_date, c.station_id, c.source_context_hash),
            )
        )
        folds.append(
            WalkForwardFold(
                fold_id=fold_id,
                decision_at=min_test_decision_at,
                train=train_sorted,
                test=test_sorted,
            )
        )

    folds.sort(key=lambda f: f.fold_id)
    unavailable.sort(key=lambda f: f.fold_id)
    return FoldBuildResult(folds=tuple(folds), unavailable=tuple(unavailable))


def build_walk_forward_evidence(
    rows: Sequence[Mapping[str, object]],
    *,
    embargo_days: int = DEFAULT_EMBARGO_DAYS,
) -> WalkForwardEvidence:
    """End-to-end: historical rows -> deduplicated cases -> chronological folds.

    Bundles the fold-construction evidence a research report needs: the
    folds that were built, the test groups that had no safe training pool,
    and every historical row that was skipped on the way, with why.
    """

    load_result = load_research_cases(rows)
    fold_result = build_walk_forward_folds(load_result.cases, embargo_days=embargo_days)
    return WalkForwardEvidence(
        folds=fold_result.folds,
        unavailable=fold_result.unavailable,
        skips=load_result.skips,
    )
