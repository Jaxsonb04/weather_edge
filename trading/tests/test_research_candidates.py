"""Task 3: predeclared candidate pooling, fitting, and evidence bundling.

Covers binding conditions C1 (pooling order + recorded shrinkage/fallback
evidence) and predeclaration linkage (a challenger evaluated without a
prior ``PaperStore.record_research_experiment`` declaration must be
impossible), plus fold/case fitting determinism and the fixed candidate
arms named in plan Task 3 Step 3.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.recalibration import fit_recalibration
from sfo_kalshi_quant.research_walkforward import (
    GOOGLE_CHALLENGER_BLOCK_ACTION,
    GOOGLE_CHALLENGER_FORECAST_ACTION,
    GoogleChallengerEvidence,
    ResearchCase,
    WalkForwardFold,
)
from sfo_kalshi_quant.research_candidates import (
    CLIMATE_REGION_LEAD_COHORT,
    GAUSSIAN_PIT_CANDIDATE_KEY,
    GAUSSIAN_PIT_CANDIDATE_VERSION,
    GAUSSIAN_PIT_HYPOTHESIS_FAMILY,
    GLOBAL_LEAD_COHORT,
    GOOGLE_RUNTIME_CANDIDATE_KEY,
    GOOGLE_RUNTIME_CANDIDATE_VERSION,
    GOOGLE_RUNTIME_HYPOTHESIS_FAMILY,
    IDENTITY_CANDIDATE_KEY,
    POOLING_ORDER,
    STATION_ALL_LEADS_COHORT,
    STATION_LEAD_COHORT,
    PoolingUnavailable,
    candidate_calibration_gap,
    case_score_payload,
    declared_research_candidates,
    fit_case_candidates,
    fit_fold_candidates,
    gaussian_pit_candidate,
    google_runtime_candidate,
    identity_candidate,
    pool_training_cohort,
    score_candidate_for_case,
    score_fold_candidates,
)


_case_hash_counter = itertools.count()


def _case(
    *,
    station_id: str = "KSFO",
    target_date: date = date(2026, 6, 20),
    lead_days: int = 1,
    baseline_mu: float = 66.0,
    baseline_sigma: float = 3.0,
    actual_high_f: float = 66.0,
    source_context_hash: str | None = None,
    google_evidence: GoogleChallengerEvidence | None = None,
) -> ResearchCase:
    # Pacific civil date must be exactly ``lead_days`` before target_date, at
    # noon UTC (comfortably clear of any DST-boundary hour ambiguity for
    # every date used in this file), so every case is C2-consistent by
    # construction without hardcoding decision_at separately per call site.
    decision_at = datetime(
        target_date.year, target_date.month, target_date.day, 20, 0, 0, tzinfo=timezone.utc
    ) - timedelta(days=lead_days)
    settled_at = datetime(
        target_date.year, target_date.month, target_date.day, 20, 0, 0, tzinfo=timezone.utc
    ) + timedelta(days=1, hours=8)  # always well after decision_at, any lead_days >= 0
    return ResearchCase(
        station_id=station_id,
        target_date=target_date,
        decision_at=decision_at,
        settled_at=settled_at,
        lead_days=lead_days,
        source_context_hash=source_context_hash
        or f"{station_id}:{target_date.isoformat()}:{lead_days}:{next(_case_hash_counter)}",
        baseline_mu=baseline_mu,
        baseline_sigma=baseline_sigma,
        actual_high_f=actual_high_f,
        google_evidence=google_evidence,
    )


def _fold(train: tuple[ResearchCase, ...], test: tuple[ResearchCase, ...]) -> WalkForwardFold:
    return WalkForwardFold(
        fold_id="KSFO:2026-06-25",
        decision_at=min(c.decision_at for c in test),
        train=train,
        test=test,
    )


# ---------------------------------------------------------------------------
# Binding condition C1: pooling order, and "every fallback is recorded".
# ---------------------------------------------------------------------------


def test_pooling_uses_exact_station_lead_cohort_when_available() -> None:
    train = (
        _case(station_id="KSFO", lead_days=1, actual_high_f=68.0),
        _case(station_id="KSFO", lead_days=2, actual_high_f=90.0),  # wrong lead, must be ignored
        _case(station_id="KNYC", lead_days=1, actual_high_f=90.0),  # wrong station, must be ignored
    )
    decision = pool_training_cohort(train, station_id="KSFO", lead_days=1)
    assert decision.cohort_level == STATION_LEAD_COHORT
    assert decision.attempted_levels == (STATION_LEAD_COHORT,)
    assert decision.training_count == 1


def test_pooling_falls_back_to_station_all_leads_when_exact_lead_is_empty() -> None:
    train = (
        _case(station_id="KSFO", lead_days=2, actual_high_f=68.0),
        _case(station_id="KSFO", lead_days=3, actual_high_f=70.0),
        _case(station_id="KNYC", lead_days=1, actual_high_f=90.0),  # wrong station
    )
    decision = pool_training_cohort(train, station_id="KSFO", lead_days=1)
    assert decision.cohort_level == STATION_ALL_LEADS_COHORT
    assert decision.attempted_levels == (STATION_LEAD_COHORT, STATION_ALL_LEADS_COHORT)
    assert decision.training_count == 2


def test_pooling_falls_back_to_climate_region_lead_when_station_has_nothing() -> None:
    # KSFO and KLAX are both "west-coast" in REGION_BY_SERIES.
    train = (
        _case(station_id="KLAX", lead_days=1, actual_high_f=80.0),
        _case(station_id="KNYC", lead_days=1, actual_high_f=90.0),  # different region
    )
    decision = pool_training_cohort(train, station_id="KSFO", lead_days=1)
    assert decision.cohort_level == CLIMATE_REGION_LEAD_COHORT
    assert decision.attempted_levels == (
        STATION_LEAD_COHORT,
        STATION_ALL_LEADS_COHORT,
        CLIMATE_REGION_LEAD_COHORT,
    )
    assert decision.training_count == 1


def test_pooling_falls_back_to_global_lead_when_no_station_or_region_match() -> None:
    # KNYC is "northeast", not "west-coast" -- no climate-region match for KSFO,
    # but it shares lead_days=1 so the global/lead level still applies.
    train = (_case(station_id="KNYC", lead_days=1, actual_high_f=90.0),)
    decision = pool_training_cohort(train, station_id="KSFO", lead_days=1)
    assert decision.cohort_level == GLOBAL_LEAD_COHORT
    assert decision.attempted_levels == POOLING_ORDER
    assert decision.training_count == 1


def test_pooling_is_unavailable_when_no_case_anywhere_shares_the_exact_lead_days() -> None:
    """Never falls back past global/lead to "any lead" -- a station/lead
    pair whose lead_days matches nothing anywhere is reported unavailable,
    not silently pooled across incomparable forecast horizons."""

    train = (_case(station_id="KNYC", lead_days=5, actual_high_f=90.0),)
    decision = pool_training_cohort(train, station_id="KSFO", lead_days=1)
    assert isinstance(decision, PoolingUnavailable)
    assert decision.reason == "no_pooled_training_history"
    assert decision.attempted_levels == POOLING_ORDER


def test_pooling_records_the_exact_fitted_shrinkage_for_its_cohort() -> None:
    """The 'recorded shrinkage' half of C1: the returned recalibration
    must be byte-identical to calling fit_recalibration directly on the
    same cohort's triples."""

    train = (
        _case(station_id="KSFO", lead_days=1, baseline_mu=64.0, actual_high_f=68.0),
        _case(station_id="KSFO", lead_days=1, baseline_mu=65.0, actual_high_f=69.0),
        _case(station_id="KSFO", lead_days=2, baseline_mu=70.0, actual_high_f=71.0),  # excluded
    )
    decision = pool_training_cohort(train, station_id="KSFO", lead_days=1)
    expected = fit_recalibration(
        [(c.baseline_mu, c.baseline_sigma, c.actual_high_f) for c in train if c.lead_days == 1]
    )
    assert decision.recalibration == expected
    assert decision.recalibration.n == 2


def test_pooling_shrinkage_k_is_threaded_through_to_fit_recalibration() -> None:
    train = (_case(station_id="KSFO", lead_days=1, baseline_mu=60.0, actual_high_f=70.0),)
    loose = pool_training_cohort(train, station_id="KSFO", lead_days=1, shrinkage_k=1.0)
    tight = pool_training_cohort(train, station_id="KSFO", lead_days=1, shrinkage_k=1000.0)
    # A tiny shrinkage_k trusts the single training case far more (bigger
    # |bias_f|) than a huge shrinkage_k, which shrinks almost fully to
    # identity regardless of how sharp the training signal is.
    assert abs(loose.recalibration.bias_f) > abs(tight.recalibration.bias_f)


def test_unknown_pooling_cohort_level_is_rejected() -> None:
    from sfo_kalshi_quant.research_candidates import _pooling_cohort

    with pytest.raises(ValueError):
        _pooling_cohort((), level="not-a-real-level", station_id="KSFO", lead_days=1, region="x")


# ---------------------------------------------------------------------------
# Predeclared candidate identities
# ---------------------------------------------------------------------------


def test_declared_research_candidates_matches_the_fixed_module_constants() -> None:
    assert declared_research_candidates() == (
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


def test_identity_candidate_is_always_available_and_unchanged() -> None:
    case = _case(baseline_mu=61.5, baseline_sigma=2.25)
    candidate = identity_candidate(case)
    assert candidate.candidate_key == IDENTITY_CANDIDATE_KEY
    assert candidate.available is True
    assert candidate.mu == 61.5
    assert candidate.sigma == 2.25


def test_gaussian_pit_candidate_applies_the_pooled_recalibration_to_this_case() -> None:
    train = tuple(
        _case(station_id="KSFO", lead_days=1, baseline_mu=64.0, actual_high_f=68.0)
        for _ in range(3)
    )
    case = _case(station_id="KSFO", lead_days=1, baseline_mu=64.0, baseline_sigma=3.0)
    candidate = gaussian_pit_candidate(case, train)
    assert candidate.available is True
    assert candidate.pooling is not None
    expected_mu, expected_sigma = candidate.pooling.recalibration.apply(
        case.baseline_mu, case.baseline_sigma
    )
    assert candidate.mu == pytest.approx(expected_mu)
    assert candidate.sigma == pytest.approx(expected_sigma)


def test_gaussian_pit_candidate_is_unavailable_when_pooling_has_nothing() -> None:
    case = _case(station_id="KSFO", lead_days=1)
    candidate = gaussian_pit_candidate(case, ())
    assert candidate.available is False
    assert candidate.mu is None and candidate.sigma is None
    assert candidate.unavailable_reason == "no_pooled_training_history"


def test_google_runtime_candidate_is_available_only_with_forecast_evidence() -> None:
    case = _case(
        google_evidence=GoogleChallengerEvidence(
            mu=68.0, sigma=3.0, action=GOOGLE_CHALLENGER_FORECAST_ACTION
        )
    )
    candidate = google_runtime_candidate(case)
    assert candidate.available is True
    assert candidate.mu == 68.0
    assert candidate.candidate_key == GOOGLE_RUNTIME_CANDIDATE_KEY


def test_google_runtime_candidate_fails_closed_with_no_evidence() -> None:
    candidate = google_runtime_candidate(_case())
    assert candidate.available is False
    assert candidate.unavailable_reason == "no_google_evidence_for_case"
    assert candidate.mu is None


def test_google_runtime_candidate_fails_closed_on_corroboration_block() -> None:
    case = _case(
        google_evidence=GoogleChallengerEvidence(
            mu=None, sigma=3.0, action=GOOGLE_CHALLENGER_BLOCK_ACTION
        )
    )
    candidate = google_runtime_candidate(case)
    assert candidate.available is False
    assert candidate.unavailable_reason == "google_corroboration_blocked"
    assert candidate.mu is None


def test_google_runtime_candidate_reports_a_distinct_reason_for_missing_mu_on_a_forecast_action() -> (  # noqa: E501
    None
):
    """L1: a directly-constructed forecast-action evidence with ``mu=None``
    (unreachable through ``research_walkforward.py``'s own
    ``_build_google_evidence`` parsing, which never lets a forecast action
    through without a finite mu -- only reachable via a hand-built
    ``GoogleChallengerEvidence``) must not be misreported as
    ``google_corroboration_blocked``. A genuinely blocked corroboration and
    malformed forecast-action evidence are different failure modes."""

    case = _case(
        google_evidence=GoogleChallengerEvidence(
            mu=None, sigma=3.0, action=GOOGLE_CHALLENGER_FORECAST_ACTION
        )
    )
    candidate = google_runtime_candidate(case)
    assert candidate.available is False
    assert candidate.unavailable_reason == "google_forecast_evidence_missing_mu"
    assert candidate.unavailable_reason != "google_corroboration_blocked"


# ---------------------------------------------------------------------------
# Fold/case fitting: determinism and "train only, never test" (guarantee 5)
# ---------------------------------------------------------------------------


def test_fit_fold_candidates_gaussian_pit_params_are_independent_of_test_outcomes() -> None:
    """Two folds sharing the same train tuple but different test tuples
    (different actual_high_f) must fit byte-identical gaussian-pit
    parameters for the same station/lead pair -- mutating a test case's
    outcome can never change the parameters used to score any case."""

    train = (
        _case(station_id="KSFO", lead_days=1, baseline_mu=64.0, actual_high_f=68.0),
        _case(station_id="KSFO", lead_days=1, baseline_mu=65.0, actual_high_f=70.0),
    )
    test_a = (_case(station_id="KSFO", lead_days=1, actual_high_f=66.0),)
    test_b = (_case(station_id="KSFO", lead_days=1, actual_high_f=95.0),)  # wildly different

    fold_a = _fold(train, test_a)
    fold_b = _fold(train, test_b)

    fitted_a = fit_fold_candidates(fold_a)
    fitted_b = fit_fold_candidates(fold_b)

    gaussian_a = fitted_a[test_a[0].source_context_hash][1]
    gaussian_b = fitted_b[test_b[0].source_context_hash][1]
    assert gaussian_a.pooling.recalibration == gaussian_b.pooling.recalibration
    assert gaussian_a.mu == gaussian_b.mu
    assert gaussian_a.sigma == gaussian_b.sigma


def test_fit_case_candidates_never_reads_the_case_actual_high_f_for_gaussian_pit() -> None:
    train = (_case(station_id="KSFO", lead_days=1, baseline_mu=64.0, actual_high_f=68.0),)
    low = _case(station_id="KSFO", lead_days=1, baseline_mu=60.0, actual_high_f=-40.0)
    high = _case(station_id="KSFO", lead_days=1, baseline_mu=60.0, actual_high_f=999.0)
    _, gaussian_low, _ = fit_case_candidates(low, train)
    _, gaussian_high, _ = fit_case_candidates(high, train)
    assert gaussian_low.mu == gaussian_high.mu
    assert gaussian_low.sigma == gaussian_high.sigma


def test_fit_fold_candidates_returns_all_three_arms_per_test_case() -> None:
    train = (_case(station_id="KSFO", lead_days=1, actual_high_f=68.0),)
    test = (_case(station_id="KSFO", lead_days=1, actual_high_f=66.0),)
    fold = _fold(train, test)
    fitted = fit_fold_candidates(fold)
    identity, gaussian, google = fitted[test[0].source_context_hash]
    assert identity.candidate_key == IDENTITY_CANDIDATE_KEY
    assert gaussian.candidate_key == GAUSSIAN_PIT_CANDIDATE_KEY
    assert google.candidate_key == GOOGLE_RUNTIME_CANDIDATE_KEY


# ---------------------------------------------------------------------------
# Scoring: available vs unavailable candidates, and JSON-safe serialization
# ---------------------------------------------------------------------------


def test_score_candidate_for_case_computes_every_metric_when_available() -> None:
    case = _case(baseline_mu=66.0, baseline_sigma=3.0, actual_high_f=70.0)
    candidate = identity_candidate(case)
    score = score_candidate_for_case(case, candidate)
    assert score.available is True
    for field in (
        score.crps,
        score.ranked_probability_score,
        score.log_score,
        score.pit,
        score.point_error,
        score.bracket_brier,
    ):
        assert field is not None
    assert score.interval_80_covered is not None


def test_score_candidate_for_case_reports_all_none_metrics_when_unavailable() -> None:
    case = _case()
    candidate = google_runtime_candidate(case)  # no evidence -> unavailable
    score = score_candidate_for_case(case, candidate)
    assert score.available is False
    for field in (
        score.crps,
        score.ranked_probability_score,
        score.log_score,
        score.pit,
        score.point_error,
        score.interval_80_covered,
        score.bracket_brier,
    ):
        assert field is None


def test_case_score_payload_is_json_serializable_with_allow_nan_false() -> None:
    case = _case(actual_high_f=70.0)
    available = score_candidate_for_case(case, identity_candidate(case))
    unavailable = score_candidate_for_case(case, google_runtime_candidate(case))
    for score in (available, unavailable):
        payload = case_score_payload(score)
        # allow_nan=False mirrors Task 1's own persistence path
        # (record_research_experiment/record_research_evidence) -- a
        # payload this function produces must never fail it.
        json.dumps(payload, sort_keys=True, allow_nan=False)


def test_case_score_payload_includes_pooling_when_present() -> None:
    train = (_case(station_id="KSFO", lead_days=1, actual_high_f=68.0),)
    case = _case(station_id="KSFO", lead_days=1, actual_high_f=70.0)
    candidate = gaussian_pit_candidate(case, train)
    score = score_candidate_for_case(case, candidate)
    payload = case_score_payload(score)
    assert payload["pooling"]["cohort_level"] == STATION_LEAD_COHORT
    assert payload["pooling"]["training_count"] == 1
    assert "recalibration" in payload["pooling"]


def test_case_score_payload_pooling_captures_a_shrinkage_k_override() -> None:
    """L2: a ``shrinkage_k`` override passed at fit time must be captured
    in the evidence payload, not just the module default -- so the
    declared ``parameter_json`` for ``gaussian-pit-station-lead-v1`` can be
    checked against what a fit actually used."""

    train = (_case(station_id="KSFO", lead_days=1, actual_high_f=68.0),)
    case = _case(station_id="KSFO", lead_days=1, actual_high_f=70.0)
    candidate = gaussian_pit_candidate(case, train, shrinkage_k=7.0)
    score = score_candidate_for_case(case, candidate)
    payload = case_score_payload(score)
    assert payload["pooling"]["shrinkage_k"] == 7.0


def test_score_fold_candidates_produces_two_challenger_rows_per_fold() -> None:
    """H1: one row per (fold, challenger) -- never per test case.

    A fold's ``test`` tuple is one indivisible station/target-day group
    that routinely holds more than one case (one per scan cycle). Emitting
    a row per case instead of per fold would collide every case after the
    first against ``research_evidence``'s fold-grained primary key -- this
    pins that two test cases still produce exactly two rows (one per
    challenger), with every case's score folded into that row's per-case
    ``cases`` collection rather than spilling into extra rows.
    """

    train = (_case(station_id="KSFO", lead_days=1, actual_high_f=68.0),)
    test = (
        _case(station_id="KSFO", lead_days=1, actual_high_f=66.0),
        _case(station_id="KSFO", lead_days=1, actual_high_f=67.0),
    )
    fold = _fold(train, test)
    evidence = score_fold_candidates(fold)
    assert len(evidence) == 2  # one row per challenger, not per test case
    challenger_keys = {row.challenger_candidate_key for row in evidence}
    assert challenger_keys == {GAUSSIAN_PIT_CANDIDATE_KEY, GOOGLE_RUNTIME_CANDIDATE_KEY}

    expected_hashes = {case.source_context_hash for case in test}
    for row in evidence:
        assert set(row.baseline["cases"]) == expected_hashes
        assert set(row.challenger["cases"]) == expected_hashes
        for case_payload in row.baseline["cases"].values():
            assert case_payload["candidate_key"] == IDENTITY_CANDIDATE_KEY
        for case_payload in row.challenger["cases"].values():
            assert case_payload["candidate_key"] == row.challenger_candidate_key


def test_fold_candidate_evidence_json_fingerprint_is_independent_of_test_case_order() -> None:
    """The persisted evidence 'fingerprint' -- the exact JSON
    ``PaperStore.record_research_evidence`` writes via its own
    ``json.dumps(..., sort_keys=True)`` -- must be identical no matter what
    order a fold's ``test`` cases happen to be considered in.

    The reshaped per-case ``cases`` collection is a plain dict keyed by
    ``source_context_hash``, built by iterating ``fold.test`` in whatever
    order it is given -- so this pins that ``sort_keys=True`` at
    persistence time fully normalizes that insertion order away, and that
    reordering a fold's cases never changes which cases end up in the
    written row (only the presence of every case's own hash matters, not
    the order it was folded in).
    """

    train = (_case(station_id="KSFO", lead_days=1, actual_high_f=68.0),)
    a = _case(station_id="KSFO", lead_days=1, actual_high_f=66.0, source_context_hash="case-a")
    b = _case(station_id="KSFO", lead_days=1, actual_high_f=67.0, source_context_hash="case-b")
    c = _case(station_id="KSFO", lead_days=1, actual_high_f=65.0, source_context_hash="case-c")

    forward = {
        row.challenger_candidate_key: row
        for row in score_fold_candidates(_fold(train, (a, b, c)))
    }
    reordered = {
        row.challenger_candidate_key: row
        for row in score_fold_candidates(_fold(train, (c, a, b)))
    }
    assert forward.keys() == reordered.keys()
    for key in forward:
        assert json.dumps(forward[key].baseline, sort_keys=True) == json.dumps(
            reordered[key].baseline, sort_keys=True
        )
        assert json.dumps(forward[key].challenger, sort_keys=True) == json.dumps(
            reordered[key].challenger, sort_keys=True
        )


def test_candidate_calibration_gap_pools_pit_values_for_one_candidate() -> None:
    train = tuple(
        _case(station_id="KSFO", lead_days=1, baseline_mu=64.0, actual_high_f=68.0)
        for _ in range(5)
    )
    test = tuple(
        _case(station_id="KSFO", lead_days=1, actual_high_f=float(65 + i)) for i in range(4)
    )
    fold = _fold(train, test)
    evidence = score_fold_candidates(fold)
    gap = candidate_calibration_gap(evidence, candidate_key=IDENTITY_CANDIDATE_KEY)
    assert gap is not None
    assert 0.0 <= gap <= 1.0


def test_candidate_calibration_gap_returns_none_when_nothing_is_available() -> None:
    train = (_case(station_id="KSFO", lead_days=1, actual_high_f=68.0),)
    test = (_case(station_id="KSFO", lead_days=1, actual_high_f=66.0),)
    fold = _fold(train, test)
    evidence = score_fold_candidates(fold)
    gap = candidate_calibration_gap(evidence, candidate_key=GOOGLE_RUNTIME_CANDIDATE_KEY)
    assert gap is None  # every case in this fold has no Google evidence


# ---------------------------------------------------------------------------
# Predeclaration linkage: a challenger evaluated without a prior
# declaration must be impossible (DB-enforced by Task 1; this pins that
# Task 3's own candidate identities are exactly what gets declared/written).
# ---------------------------------------------------------------------------


def _declare_both_challengers(store: PaperStore) -> None:
    for family, key, version, role in declared_research_candidates():
        store.record_research_experiment(
            experiment_id=key,
            hypothesis_family=family,
            candidate_key=key,
            candidate_version=version,
            parameter_json={"shrinkage_k": 40.0} if key == GAUSSIAN_PIT_CANDIDATE_KEY else {},
            evidence_role=role,
        )


def _multi_case_fold(target_date: date = date(2099, 1, 2)) -> WalkForwardFold:
    """A fold whose ``test`` tuple has >=2 cases sharing one station/day --
    the ordinary shape of a real fold (multiple scan cycles per
    station/day), and the exact shape that reproduced H1: a naive
    row-per-test-case write collides every case after the first against
    ``research_evidence``'s fold-grained primary key
    (``experiment_id, fold_id, station_id, target_date``)."""

    train = (_case(station_id="KSFO", lead_days=1, actual_high_f=68.0),)
    test = (
        _case(station_id="KSFO", lead_days=1, actual_high_f=66.0, target_date=target_date),
        _case(station_id="KSFO", lead_days=1, actual_high_f=67.0, target_date=target_date),
        _case(station_id="KSFO", lead_days=1, actual_high_f=65.5, target_date=target_date),
    )
    return _fold(train, test)


def test_declared_candidate_identities_round_trip_through_record_research_evidence(
    tmp_path: Path,
) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare_both_challengers(store)

    # A degenerate single-test-case fold would hide H1 (each challenger's
    # one row is a PK the fold's own first case can't collide with) --
    # this fold carries three, the ordinary multi-scan-cycle shape.
    fold = _multi_case_fold()
    evidence = score_fold_candidates(fold)
    assert len(evidence) == 2  # one row per challenger, not per test case

    for row in evidence:
        store.record_research_evidence(
            experiment_id=row.challenger_candidate_key,
            fold_id=row.fold_id,
            station_id=row.station_id,
            target_date=row.target_date.isoformat(),
            evaluated_at=row.evaluated_at.isoformat(),
            baseline=row.baseline,
            challenger=row.challenger,
        )

    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM research_evidence").fetchone()[0]
    assert count == 2


def test_multi_case_fold_round_trips_without_integrity_error(tmp_path: Path) -> None:
    """H1 pinned regression: a fold with >=2 test cases must round-trip
    through ``PaperStore.record_research_evidence`` without
    ``sqlite3.IntegrityError``.

    Before the fix, ``score_fold_candidates`` emitted one row per test
    case per challenger; a fold's second test case (same station_id,
    target_date, fold_id as its first -- an ordinary multi-scan-cycle
    fold) collided with the first on ``research_evidence``'s fold-grained
    primary key and the immutable-insert trigger converted that collision
    into ``sqlite3.IntegrityError: research evidence is immutable``. This
    pins that a fold with three test cases -- not the degenerate
    single-case fold the old round-trip test used -- writes cleanly, and
    that every case's score actually made it into the persisted payload
    rather than being silently dropped.
    """

    store = PaperStore(tmp_path / "paper.db")
    _declare_both_challengers(store)

    fold = _multi_case_fold()
    evidence = score_fold_candidates(fold)
    assert len(evidence) == 2

    for row in evidence:
        store.record_research_evidence(
            experiment_id=row.challenger_candidate_key,
            fold_id=row.fold_id,
            station_id=row.station_id,
            target_date=row.target_date.isoformat(),
            evaluated_at=row.evaluated_at.isoformat(),
            baseline=row.baseline,
            challenger=row.challenger,
        )

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT baseline_json, challenger_json FROM research_evidence "
            "ORDER BY fold_id"
        ).fetchall()
    assert len(rows) == 2
    expected_hashes = {case.source_context_hash for case in fold.test}
    for baseline_json, challenger_json in rows:
        baseline = json.loads(baseline_json)
        challenger = json.loads(challenger_json)
        assert set(baseline["cases"]) == expected_hashes
        assert set(challenger["cases"]) == expected_hashes


def test_writing_evidence_for_an_undeclared_candidate_key_is_impossible(
    tmp_path: Path,
) -> None:
    """The other half of predeclaration linkage: skip declaring
    GAUSSIAN_PIT_CANDIDATE_KEY, then try to persist a real, correctly
    computed score for it anyway -- the database must refuse it."""

    store = PaperStore(tmp_path / "paper.db")
    # Deliberately do NOT declare GAUSSIAN_PIT_CANDIDATE_KEY.

    train = (_case(station_id="KSFO", lead_days=1, actual_high_f=68.0),)
    test = (
        _case(
            station_id="KSFO",
            lead_days=1,
            actual_high_f=66.0,
            target_date=date(2099, 1, 2),
        ),
    )
    fold = _fold(train, test)
    evidence = score_fold_candidates(fold)
    gaussian_row = next(
        row for row in evidence if row.challenger_candidate_key == GAUSSIAN_PIT_CANDIDATE_KEY
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.record_research_evidence(
            experiment_id=gaussian_row.challenger_candidate_key,
            fold_id=gaussian_row.fold_id,
            station_id=gaussian_row.station_id,
            target_date=gaussian_row.target_date.isoformat(),
            evaluated_at=gaussian_row.evaluated_at.isoformat(),
            baseline=gaussian_row.baseline,
            challenger=gaussian_row.challenger,
        )
