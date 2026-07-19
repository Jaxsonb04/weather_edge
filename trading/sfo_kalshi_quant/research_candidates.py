"""Task 3: predeclared candidate pooling, fitting, and evidence bundling.

Sits on top of ``research_walkforward.py``'s ``ResearchCase``/``WalkForwardFold``
(Task 2, frozen/reviewed -- this module only ever *reads* ``fold.train``/
``fold.test``, never reconstructs or mutates a fold) and
``research_scoring.py``'s pure distributional math. Implements the three
predeclared arms named in the plan (Task 3 Step 3):

1. ``active-identity-v1`` -- unchanged archived baseline distribution.
2. ``gaussian-pit-station-lead-v1`` -- training-only shrunk PIT
   recalibration (``recalibration.fit_recalibration``), fit from a pooled
   training cohort selected by the fixed station/lead -> station/all-leads
   -> climate-region/lead -> global/lead fallback order (binding condition
   C1), with the exact fallback path and fitted shrinkage recorded on
   every ``PoolingDecision``.
3. ``google-runtime-fixed-v1`` -- the fixed 15%/+-1.5F Google-conditioned
   challenger (``forecaster/google_runtime_blend.py``'s frozen formula),
   evaluated only where a case already carries derived, already-computed
   challenger evidence; fails closed (``available=False``, never a
   fabricated or backfilled value) whenever a case has none, or its
   evidence recorded the fixed formula's own corroboration block.

Predeclaration linkage (guarantee 6 in ``research_walkforward.py``'s module
docstring): ``declared_research_candidates()`` is the one frozen source of
truth for the two challengers' ``(hypothesis_family, candidate_key,
candidate_version, evidence_role)`` identities. Every ``CandidateDistribution``
this module produces for a challenger carries exactly that same
``candidate_key``/``candidate_version``, so a caller who declares both via
``PaperStore.record_research_experiment`` before persisting this module's
scores with ``PaperStore.record_research_evidence`` can never drift from
what was actually fit -- and a caller who skips declaration gets
``sqlite3.IntegrityError`` from the database itself, not a silently
accepted write.

This module never touches the database, the clock, or any random state:
``fit_fold_candidates``/``score_fold_candidates`` are pure functions of a
``WalkForwardFold``'s own ``train``/``test`` tuples.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Sequence

from .account import REGION_BY_SERIES
from .cities import CITY_BY_STATION
from .recalibration import GaussianRecalibration, fit_recalibration
from .research_scoring import (
    bracket_brier,
    gaussian_crps,
    gaussian_log_score,
    interval_covered,
    max_calibration_gap,
    pit_value,
    point_error,
    ranked_probability_score,
)
from .research_walkforward import (
    GOOGLE_CHALLENGER_BLOCK_ACTION,
    GOOGLE_CHALLENGER_POLICY_VERSION,
    ResearchCase,
    WalkForwardFold,
)

# ---------------------------------------------------------------------------
# Task 3: pooling machinery (binding condition C1)
# ---------------------------------------------------------------------------

STATION_LEAD_COHORT = "station_lead"
STATION_ALL_LEADS_COHORT = "station_all_leads"
CLIMATE_REGION_LEAD_COHORT = "climate_region_lead"
GLOBAL_LEAD_COHORT = "global_lead"

# Fixed fallback order (plan Task 2 Step 4, deferred to Task 3 per binding
# condition C1): exact station/lead first, then progressively coarser
# cohorts, ending at global/lead. Deliberately does not fall back past
# global/lead to "any station, any lead" -- a station/lead pair with no
# case anywhere sharing its exact lead_days is reported unavailable
# (``PoolingUnavailable``) rather than silently pooling across forecast
# horizons that are not comparable.
POOLING_ORDER: tuple[str, ...] = (
    STATION_LEAD_COHORT,
    STATION_ALL_LEADS_COHORT,
    CLIMATE_REGION_LEAD_COHORT,
    GLOBAL_LEAD_COHORT,
)

DEFAULT_SHRINKAGE_K = 40.0


@dataclass(frozen=True)
class PoolingDecision:
    """Which training cohort a station/lead pair was fit from, and why.

    ``attempted_levels`` always lists every pooling level tried, in the
    fixed fallback order, up to and including ``cohort_level`` -- so a
    reviewer can see not just the cohort a candidate landed on but every
    narrower cohort that was tried and rejected first for lacking any
    training case at all ("every fallback is recorded in fold evidence").
    ``recalibration`` is the exact ``fit_recalibration`` output (bias_f,
    sigma_scale, and the shrinkage sample size ``n``) fit from that cohort
    -- the "recorded shrinkage" half of C1. ``shrinkage_k`` is the exact
    parameter this cohort was fit with -- whatever a caller passed to
    ``pool_training_cohort`` (default or override) -- so a fit made with a
    non-default override is distinguishable in evidence from one made with
    the fixed module default, and the declared ``parameter_json`` for
    ``gaussian-pit-station-lead-v1`` can be checked against what was
    actually used.
    """

    cohort_level: str
    cohort_key: str
    training_count: int
    attempted_levels: tuple[str, ...]
    recalibration: GaussianRecalibration
    shrinkage_k: float = DEFAULT_SHRINKAGE_K


@dataclass(frozen=True)
class PoolingUnavailable:
    """No training cohort exists at any pooling level for this station/lead."""

    station_id: str
    lead_days: int
    attempted_levels: tuple[str, ...]
    reason: str = "no_pooled_training_history"


def _climate_region_for_station(station_id: str) -> str:
    """Coarse multi-city geographic cohort for the climate-region pooling level.

    Reuses ``REGION_BY_SERIES`` (``trading/sfo_kalshi_quant/account.py``) --
    the project's one existing geographic grouping of the 15 settlement
    cities (west-coast, southeast, texas, northeast, southwest, midwest,
    mountain, southern-plains), already reviewed and load-bearing for live
    correlated-exposure risk caps. The plan names this pooling level
    "climate-region" but defines no separate climate taxonomy of its own;
    rather than invent a new, unreviewed classification, this reuses the
    existing one read-only (no coupling to live risk state -- it is a
    plain, static dict lookup). An unrecognized station falls back to the
    same "unknown" bucket ``policy_capacity`` already uses.
    """

    city = CITY_BY_STATION.get(station_id)
    if city is None:
        return "unknown"
    return REGION_BY_SERIES.get(city.series_ticker, "unknown")


def _pooling_cohort(
    train: Sequence[ResearchCase],
    *,
    level: str,
    station_id: str,
    lead_days: int,
    region: str,
) -> tuple[ResearchCase, ...]:
    if level == STATION_LEAD_COHORT:
        return tuple(
            c for c in train if c.station_id == station_id and c.lead_days == lead_days
        )
    if level == STATION_ALL_LEADS_COHORT:
        return tuple(c for c in train if c.station_id == station_id)
    if level == CLIMATE_REGION_LEAD_COHORT:
        return tuple(
            c
            for c in train
            if _climate_region_for_station(c.station_id) == region
            and c.lead_days == lead_days
        )
    if level == GLOBAL_LEAD_COHORT:
        return tuple(c for c in train if c.lead_days == lead_days)
    raise ValueError(f"unknown pooling cohort level: {level!r}")


def _cohort_key(level: str, *, station_id: str, lead_days: int, region: str) -> str:
    if level == STATION_LEAD_COHORT:
        return f"station={station_id}:lead={lead_days}"
    if level == STATION_ALL_LEADS_COHORT:
        return f"station={station_id}:all-leads"
    if level == CLIMATE_REGION_LEAD_COHORT:
        return f"region={region}:lead={lead_days}"
    return f"global:lead={lead_days}"


def pool_training_cohort(
    train: Sequence[ResearchCase],
    *,
    station_id: str,
    lead_days: int,
    shrinkage_k: float = DEFAULT_SHRINKAGE_K,
) -> PoolingDecision | PoolingUnavailable:
    """Select and fit the first non-empty cohort in the pooling fallback order.

    Fitting itself (``fit_recalibration``) already shrinks a small cohort
    toward the identity map by ``n / (n + shrinkage_k)`` -- pooling only
    ever decides *which* leakage-safe training cases are eligible to fit
    from, never how much to trust them; that is shrinkage's job, and its
    result is recorded on the returned ``PoolingDecision`` alongside the
    cohort that produced it.
    """

    region = _climate_region_for_station(station_id)
    attempted: list[str] = []
    for level in POOLING_ORDER:
        attempted.append(level)
        cohort = _pooling_cohort(
            train, level=level, station_id=station_id, lead_days=lead_days, region=region
        )
        if not cohort:
            continue
        triples = [(c.baseline_mu, c.baseline_sigma, c.actual_high_f) for c in cohort]
        recalibration = fit_recalibration(triples, shrinkage_k=shrinkage_k)
        return PoolingDecision(
            cohort_level=level,
            cohort_key=_cohort_key(
                level, station_id=station_id, lead_days=lead_days, region=region
            ),
            training_count=len(cohort),
            attempted_levels=tuple(attempted),
            recalibration=recalibration,
            shrinkage_k=shrinkage_k,
        )
    return PoolingUnavailable(
        station_id=station_id, lead_days=lead_days, attempted_levels=tuple(attempted)
    )


# ---------------------------------------------------------------------------
# Task 3: predeclared candidate identities and fitting
# ---------------------------------------------------------------------------

IDENTITY_CANDIDATE_KEY = "active-identity-v1"
IDENTITY_CANDIDATE_VERSION = "v1"
IDENTITY_HYPOTHESIS_FAMILY = "active-identity"

GAUSSIAN_PIT_CANDIDATE_KEY = "gaussian-pit-station-lead-v1"
GAUSSIAN_PIT_CANDIDATE_VERSION = "v1"
GAUSSIAN_PIT_HYPOTHESIS_FAMILY = "gaussian-pit-station-lead"

# Deliberately equal to the duplicated GOOGLE_CHALLENGER_POLICY_VERSION
# above (parity-tested), not just similar -- the candidate_key a caller
# declares for this challenger IS the fixed challenger's own policy
# version identity.
GOOGLE_RUNTIME_CANDIDATE_KEY = GOOGLE_CHALLENGER_POLICY_VERSION
GOOGLE_RUNTIME_CANDIDATE_VERSION = "v1"
GOOGLE_RUNTIME_HYPOTHESIS_FAMILY = "google-runtime-fixed"


def declared_research_candidates() -> tuple[tuple[str, str, str, str], ...]:
    """The two challenger identities a caller must declare before evaluation.

    Each tuple is ``(hypothesis_family, candidate_key, candidate_version,
    evidence_role)`` -- exactly the identity fields
    ``PaperStore.record_research_experiment`` requires. Declaring both,
    before ever calling ``PaperStore.record_research_evidence`` with the
    matching ``candidate_key`` this module fit, is what makes "a challenger
    evaluated without a prior declaration" impossible: the FK plus BEFORE
    INSERT trigger Task 1 built rejects any ``research_evidence`` row whose
    ``experiment_id`` was not already declared
    (``test_evidence_requires_a_declared_experiment``). This module never
    talks to the database itself; it only guarantees that what it fits and
    scores carries exactly the identity a caller must declare first.
    ``active-identity-v1`` is the archived baseline the two challengers are
    paired against, not a hypothesis under test -- it needs no declaration
    of its own.
    """

    return (
        (
            GAUSSIAN_PIT_HYPOTHESIS_FAMILY,
            GAUSSIAN_PIT_CANDIDATE_KEY,
            GAUSSIAN_PIT_CANDIDATE_VERSION,
            "confirmatory",
        ),
        (
            GOOGLE_RUNTIME_HYPOTHESIS_FAMILY,
            GOOGLE_RUNTIME_CANDIDATE_KEY,
            GOOGLE_RUNTIME_CANDIDATE_VERSION,
            "confirmatory",
        ),
    )


@dataclass(frozen=True)
class CandidateDistribution:
    """One candidate's fitted ``(mu, sigma)`` for one specific test case.

    ``available=False`` means this candidate has no usable distribution for
    this case (no pooled training history, no Google evidence, or a
    corroboration block) -- ``mu``/``sigma`` stay ``None`` rather than
    falling back to some other value, so an unavailable candidate can never
    be silently scored as though it agreed with the baseline.
    """

    candidate_key: str
    candidate_version: str
    hypothesis_family: str
    available: bool
    mu: float | None = None
    sigma: float | None = None
    unavailable_reason: str = ""
    pooling: PoolingDecision | None = None
    google_action: str | None = None


def identity_candidate(case: ResearchCase) -> CandidateDistribution:
    """``active-identity-v1``: the unchanged archived baseline distribution."""

    return CandidateDistribution(
        candidate_key=IDENTITY_CANDIDATE_KEY,
        candidate_version=IDENTITY_CANDIDATE_VERSION,
        hypothesis_family=IDENTITY_HYPOTHESIS_FAMILY,
        available=True,
        mu=case.baseline_mu,
        sigma=case.baseline_sigma,
    )


def gaussian_pit_candidate(
    case: ResearchCase,
    train: Sequence[ResearchCase],
    *,
    shrinkage_k: float = DEFAULT_SHRINKAGE_K,
) -> CandidateDistribution:
    """``gaussian-pit-station-lead-v1``: training-only shrunk PIT recalibration.

    Fits only from ``train`` (never from ``case`` itself or any other test
    case) via the pooling fallback order in ``pool_training_cohort``.
    """

    pooled = pool_training_cohort(
        train, station_id=case.station_id, lead_days=case.lead_days, shrinkage_k=shrinkage_k
    )
    if isinstance(pooled, PoolingUnavailable):
        return CandidateDistribution(
            candidate_key=GAUSSIAN_PIT_CANDIDATE_KEY,
            candidate_version=GAUSSIAN_PIT_CANDIDATE_VERSION,
            hypothesis_family=GAUSSIAN_PIT_HYPOTHESIS_FAMILY,
            available=False,
            unavailable_reason=pooled.reason,
        )
    mu, sigma = pooled.recalibration.apply(case.baseline_mu, case.baseline_sigma)
    return CandidateDistribution(
        candidate_key=GAUSSIAN_PIT_CANDIDATE_KEY,
        candidate_version=GAUSSIAN_PIT_CANDIDATE_VERSION,
        hypothesis_family=GAUSSIAN_PIT_HYPOTHESIS_FAMILY,
        available=True,
        mu=mu,
        sigma=sigma,
        pooling=pooled,
    )


def google_runtime_candidate(case: ResearchCase) -> CandidateDistribution:
    """``google-runtime-fixed-v1``: only where derived paired evidence exists.

    Fails closed -- ``available=False``, never a fabricated or backfilled
    value -- whenever this case carries no Google evidence at all (the
    ordinary state today, since nothing yet writes the durable
    ``google_challenger_snapshots`` evidence Google Task 7 will add), or its
    evidence recorded the fixed formula's own 7F corroboration block (no
    tradeable probability, per ``forecaster/google_runtime_blend.py``).
    """

    evidence = case.google_evidence
    if evidence is None:
        return CandidateDistribution(
            candidate_key=GOOGLE_RUNTIME_CANDIDATE_KEY,
            candidate_version=GOOGLE_RUNTIME_CANDIDATE_VERSION,
            hypothesis_family=GOOGLE_RUNTIME_HYPOTHESIS_FAMILY,
            available=False,
            unavailable_reason="no_google_evidence_for_case",
        )
    if evidence.action == GOOGLE_CHALLENGER_BLOCK_ACTION:
        return CandidateDistribution(
            candidate_key=GOOGLE_RUNTIME_CANDIDATE_KEY,
            candidate_version=GOOGLE_RUNTIME_CANDIDATE_VERSION,
            hypothesis_family=GOOGLE_RUNTIME_HYPOTHESIS_FAMILY,
            available=False,
            unavailable_reason="google_corroboration_blocked",
            google_action=evidence.action,
        )
    if evidence.mu is None:
        # Only reachable from a directly-constructed GoogleChallengerEvidence
        # (a forecast action always has a non-None mu when parsed through
        # research_walkforward.py's own _build_google_evidence -- see that
        # function's "google_challenger_mu missing or non-finite for a
        # forecast action" skip path). Reported under its own reason rather
        # than folded into "google_corroboration_blocked" -- a genuinely
        # blocked corroboration and a malformed forecast-action evidence
        # object are different failure modes and must not look identical
        # to a caller reading unavailable_reason.
        return CandidateDistribution(
            candidate_key=GOOGLE_RUNTIME_CANDIDATE_KEY,
            candidate_version=GOOGLE_RUNTIME_CANDIDATE_VERSION,
            hypothesis_family=GOOGLE_RUNTIME_HYPOTHESIS_FAMILY,
            available=False,
            unavailable_reason="google_forecast_evidence_missing_mu",
            google_action=evidence.action,
        )
    return CandidateDistribution(
        candidate_key=GOOGLE_RUNTIME_CANDIDATE_KEY,
        candidate_version=GOOGLE_RUNTIME_CANDIDATE_VERSION,
        hypothesis_family=GOOGLE_RUNTIME_HYPOTHESIS_FAMILY,
        available=True,
        mu=evidence.mu,
        sigma=evidence.sigma,
        google_action=evidence.action,
    )


def fit_case_candidates(
    case: ResearchCase,
    train: Sequence[ResearchCase],
    *,
    shrinkage_k: float = DEFAULT_SHRINKAGE_K,
) -> tuple[CandidateDistribution, CandidateDistribution, CandidateDistribution]:
    """Fit all three predeclared candidates for one test case.

    Returns ``(identity, gaussian_pit, google_runtime)``. ``train`` is read
    only for the gaussian-pit pooling fit -- ``case`` itself contributes
    only its own station/lead identity and Google evidence, never its
    ``actual_high_f`` -- so fitting is a pure function of the fold's
    training pool alone.
    """

    return (
        identity_candidate(case),
        gaussian_pit_candidate(case, train, shrinkage_k=shrinkage_k),
        google_runtime_candidate(case),
    )


def fit_fold_candidates(
    fold: WalkForwardFold, *, shrinkage_k: float = DEFAULT_SHRINKAGE_K
) -> dict[str, tuple[CandidateDistribution, CandidateDistribution, CandidateDistribution]]:
    """Fit every test case in a fold, keyed by its ``source_context_hash``.

    Only ever reads ``fold.train`` -- never ``fold.test`` -- for fitting, so
    two folds sharing the same ``train`` tuple but different ``test``
    tuples always fit byte-identical gaussian-pit parameters for the same
    station/lead pair, no matter what a test case's own ``actual_high_f``
    is (guarantee 5: mutating a test outcome cannot change the parameters
    used to score that case).
    """

    return {
        case.source_context_hash: fit_case_candidates(case, fold.train, shrinkage_k=shrinkage_k)
        for case in fold.test
    }


# ---------------------------------------------------------------------------
# Task 3: per-case distributional scoring and evidence bundling (math lives
# in .research_scoring; see that module's docstring for why it is split
# out into its own file)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseCandidateScore:
    """One candidate's full score for one test case, ready to persist."""

    candidate_key: str
    candidate_version: str
    hypothesis_family: str
    available: bool
    mu: float | None
    sigma: float | None
    unavailable_reason: str
    crps: float | None = None
    ranked_probability_score: float | None = None
    log_score: float | None = None
    pit: float | None = None
    point_error: float | None = None
    interval_80_covered: bool | None = None
    bracket_brier: float | None = None
    pooling: PoolingDecision | None = None
    google_action: str | None = None


def score_candidate_for_case(
    case: ResearchCase, candidate: CandidateDistribution
) -> CaseCandidateScore:
    """Score one fitted candidate against one test case's settled outcome.

    Deliberately never reads any *other* test case, and never re-fits
    anything -- ``candidate`` is already ``fit_case_candidates``'s output --
    so scoring can never leak between cases either.
    """

    if not candidate.available or candidate.mu is None or candidate.sigma is None:
        return CaseCandidateScore(
            candidate_key=candidate.candidate_key,
            candidate_version=candidate.candidate_version,
            hypothesis_family=candidate.hypothesis_family,
            available=False,
            mu=None,
            sigma=None,
            unavailable_reason=candidate.unavailable_reason,
            pooling=candidate.pooling,
            google_action=candidate.google_action,
        )
    actual = case.actual_high_f
    mu, sigma = candidate.mu, candidate.sigma
    return CaseCandidateScore(
        candidate_key=candidate.candidate_key,
        candidate_version=candidate.candidate_version,
        hypothesis_family=candidate.hypothesis_family,
        available=True,
        mu=mu,
        sigma=sigma,
        unavailable_reason="",
        crps=gaussian_crps(mu, sigma, actual),
        ranked_probability_score=ranked_probability_score(mu, sigma, actual),
        log_score=gaussian_log_score(mu, sigma, actual),
        pit=pit_value(mu, sigma, actual),
        point_error=point_error(mu, actual),
        interval_80_covered=interval_covered(mu, sigma, actual),
        bracket_brier=bracket_brier(mu, sigma, actual),
        pooling=candidate.pooling,
        google_action=candidate.google_action,
    )


def case_score_payload(score: CaseCandidateScore) -> dict[str, object]:
    """Serialize one candidate's case score for ``record_research_evidence``.

    Every value is a JSON-safe primitive (``None``, never ``NaN`` -- an
    unavailable candidate reports ``None`` for every metric rather than a
    non-finite placeholder, so ``json.dumps(..., allow_nan=False)`` -- Task
    1's own persistence path -- never rejects it).
    """

    payload: dict[str, object] = {
        "candidate_key": score.candidate_key,
        "candidate_version": score.candidate_version,
        "hypothesis_family": score.hypothesis_family,
        "available": score.available,
        "mu": score.mu,
        "sigma": score.sigma,
        "unavailable_reason": score.unavailable_reason,
        "crps": score.crps,
        "ranked_probability_score": score.ranked_probability_score,
        "log_score": score.log_score,
        "pit": score.pit,
        "point_error": score.point_error,
        "interval_80_covered": score.interval_80_covered,
        "bracket_brier": score.bracket_brier,
    }
    if score.google_action is not None:
        payload["google_action"] = score.google_action
    if score.pooling is not None:
        payload["pooling"] = {
            "cohort_level": score.pooling.cohort_level,
            "cohort_key": score.pooling.cohort_key,
            "training_count": score.pooling.training_count,
            "attempted_levels": list(score.pooling.attempted_levels),
            # The exact shrinkage_k this cohort was fit with -- whatever
            # was passed at fit time (default or override) -- so the
            # declared parameter_json can be checked against what a fit
            # actually used, not just what the module default happens to
            # be today.
            "shrinkage_k": score.pooling.shrinkage_k,
            "recalibration": {
                "bias_f": score.pooling.recalibration.bias_f,
                "sigma_scale": score.pooling.recalibration.sigma_scale,
                "n": score.pooling.recalibration.n,
            },
        }
    return payload


@dataclass(frozen=True)
class FoldCandidateEvidence:
    """One challenger's fold-grained baseline/challenger score bundle, ready
    to persist.

    One row per ``(fold, challenger)`` -- never per test case -- matching
    ``research_evidence``'s fold-grained primary key (``experiment_id,
    fold_id, station_id, target_date``; Task 1, frozen). A real fold
    routinely carries more than one test case sharing that single
    station/target-day (one per scan cycle -- ``WalkForwardFold``'s own
    "indivisible station/target-day test group" invariant), so a naive
    row-per-case write would collide every case after the fold's first
    against that same primary key and raise ``sqlite3.IntegrityError``
    from the immutable-insert trigger. ``baseline``/``challenger`` are
    therefore per-case collections keyed by each case's own
    ``source_context_hash`` -- ``{"cases": {hash: case_score_payload(...),
    ...}}`` -- wrapping every test case's already-computed
    ``case_score_payload`` output for this fold/challenger pairing without
    losing any of them to that collision.

    ``challenger_candidate_key`` identifies which of the two declared
    challengers (``GAUSSIAN_PIT_CANDIDATE_KEY`` or
    ``GOOGLE_RUNTIME_CANDIDATE_KEY``) this row pairs against the baseline --
    matching the ``experiment_id`` a caller must have already declared via
    ``PaperStore.record_research_experiment`` before writing this row with
    ``PaperStore.record_research_evidence``.
    """

    fold_id: str
    station_id: str
    target_date: date
    evaluated_at: datetime
    challenger_candidate_key: str
    baseline: dict[str, object]
    challenger: dict[str, object]


def score_fold_candidates(
    fold: WalkForwardFold, *, shrinkage_k: float = DEFAULT_SHRINKAGE_K
) -> tuple[FoldCandidateEvidence, ...]:
    """Fit and score both challengers, paired against the baseline, for a fold.

    Produces ONE ``FoldCandidateEvidence`` row per challenger -- never per
    test case -- each ready to become one
    ``PaperStore.record_research_evidence`` call keyed by that challenger's
    declared ``experiment_id``. A fold's ``test`` tuple is one indivisible
    station/target-day group that routinely holds more than one case (one
    per scan cycle), so every test case's score is folded into that one
    row's ``baseline``/``challenger`` per-case collections (see
    ``FoldCandidateEvidence``) rather than emitted as its own row --
    emitting a row per case would collide every case after the first
    against ``research_evidence``'s fold-grained primary key.
    ``evaluated_at`` is the latest of every case's own ``settled_at`` in
    the fold -- the fold as a whole is only fully evaluated once every one
    of its cases has settled.
    """

    fitted = fit_fold_candidates(fold, shrinkage_k=shrinkage_k)
    station_id = fold.test[0].station_id
    target_date = fold.test[0].target_date
    evaluated_at = max(case.settled_at for case in fold.test)

    challenger_keys = (GAUSSIAN_PIT_CANDIDATE_KEY, GOOGLE_RUNTIME_CANDIDATE_KEY)
    baseline_cases: dict[str, dict[str, dict[str, object]]] = {key: {} for key in challenger_keys}
    challenger_cases: dict[str, dict[str, dict[str, object]]] = {
        key: {} for key in challenger_keys
    }
    for case in fold.test:
        identity, gaussian, google = fitted[case.source_context_hash]
        baseline_payload = case_score_payload(score_candidate_for_case(case, identity))
        for challenger_candidate in (gaussian, google):
            challenger_payload = case_score_payload(
                score_candidate_for_case(case, challenger_candidate)
            )
            key = challenger_candidate.candidate_key
            baseline_cases[key][case.source_context_hash] = baseline_payload
            challenger_cases[key][case.source_context_hash] = challenger_payload

    return tuple(
        FoldCandidateEvidence(
            fold_id=fold.fold_id,
            station_id=station_id,
            target_date=target_date,
            evaluated_at=evaluated_at,
            challenger_candidate_key=key,
            baseline={"cases": baseline_cases[key]},
            challenger={"cases": challenger_cases[key]},
        )
        for key in challenger_keys
    )


def candidate_calibration_gap(
    evidence: Sequence[FoldCandidateEvidence], *, candidate_key: str
) -> float | None:
    """Maximum calibration-bucket gap for one candidate across evidence rows.

    Pools every *available* PIT value recorded for ``candidate_key`` --
    whether it appears in ``baseline`` (``active-identity-v1``) or
    ``challenger`` (either declared challenger) in each row's per-case
    ``{"cases": {hash: case_score_payload(...), ...}}`` collection -- since
    a calibration gap is inherently a multi-case aggregate, not a per-case
    value (plan Task 3 Step 4's "maximum calibration-bucket gap"). Callers
    decide the scope (one fold's evidence, or every fold's) by what they
    pass in. Returns ``None`` when no evidence row has an available score
    for this candidate, rather than fabricating a gap from zero data.
    """

    pits = [
        case_payload["pit"]
        for row in evidence
        for bundle in (row.baseline, row.challenger)
        for case_payload in bundle["cases"].values()
        if case_payload["candidate_key"] == candidate_key
        and case_payload["available"]
        and case_payload["pit"] is not None
    ]
    if not pits:
        return None
    return max_calibration_gap(pits)
