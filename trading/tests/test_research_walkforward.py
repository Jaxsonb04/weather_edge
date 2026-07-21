"""Task 1: source-neutral scan contexts and immutable experiment declarations.

Covers the schema/write-path regressions required before any chronological
walk-forward folding (Task 2+) can reconstruct candidates without leakage:

- one canonical, profile-neutral scan context can feed live, target, and
  motion decisions without a duplicated, insertion-order-biased row;
- the canonical content hash never depends on risk profile or bankroll;
- incomplete point-in-time forecast/market/feature payloads fail closed;
- research experiment declarations are immutable and always precede the
  evaluation windows that reference them (no-leakage foundation);
- research evidence rows are immutable once written.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from sfo_kalshi_quant.db import (
    PaperStore,
    source_context_hash,
    source_neutral_context_from_scan_context_row,
)
from sfo_kalshi_quant.research_walkforward import (
    GOOGLE_CHALLENGER_BLOCK_ACTION,
    GOOGLE_CHALLENGER_FORECAST_ACTION,
    GOOGLE_CHALLENGER_POLICY_VERSION,
    CaseSkip,
    DEFAULT_EMBARGO_DAYS,
    GoogleChallengerEvidence,
    ResearchCase,
    UnavailableFold,
    WalkForwardEvidence,
    WalkForwardFold,
    build_walk_forward_evidence,
    build_walk_forward_folds,
    load_research_cases,
)
from sfo_kalshi_quant.research_candidates import (
    CLIMATE_REGION_LEAD_COHORT,
    DEFAULT_SHRINKAGE_K,
    GAUSSIAN_PIT_CANDIDATE_KEY,
    GAUSSIAN_PIT_CANDIDATE_VERSION,
    GAUSSIAN_PIT_HYPOTHESIS_FAMILY,
    GLOBAL_LEAD_COHORT,
    GOOGLE_RUNTIME_CANDIDATE_KEY,
    GOOGLE_RUNTIME_CANDIDATE_VERSION,
    GOOGLE_RUNTIME_HYPOTHESIS_FAMILY,
    IDENTITY_CANDIDATE_KEY,
    IDENTITY_CANDIDATE_VERSION,
    IDENTITY_HYPOTHESIS_FAMILY,
    POOLING_ORDER,
    STATION_ALL_LEADS_COHORT,
    STATION_LEAD_COHORT,
    CandidateDistribution,
    FoldCandidateEvidence,
    PoolingDecision,
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
from sfo_kalshi_quant.research_scoring import (
    bracket_brier,
    gaussian_crps,
    gaussian_log_score,
    interval_covered,
    max_calibration_gap,
    pit_value,
    point_error,
    ranked_probability_score,
)

# Evidence windows chronologically safe on any real test-run date: far enough
# in the future/past that "declared_at = now()" always sorts as intended.
FAR_FUTURE_DATE = "2099-01-02"
FAR_FUTURE_AT = "2099-01-03T00:00:00+00:00"
FAR_PAST_DATE = "2020-01-02"
FAR_PAST_AT = "2020-01-01T00:00:00+00:00"


def _forecast_payload(predicted_high_f: float = 66.0) -> dict[str, object]:
    return {
        "target_date": "2026-06-20",
        "predicted_high_f": predicted_high_f,
        "fetched_at": "2026-06-20T12:00:00+00:00",
        "lead_hours": 8.0,
        "method": "weatheredge-blend",
        "source_count": 4,
    }


def _market_payload() -> dict[str, object]:
    return {
        "KXHIGHTSFO-TEST-B65.5": {
            "ticker": "KXHIGHTSFO-TEST-B65.5",
            "yes_bid": 0.24,
            "yes_ask": 0.26,
            "floor_strike": 65.0,
            "cap_strike": 66.0,
        }
    }


def _features_payload() -> dict[str, object]:
    return {
        "forecast_regime": "warm",
        "predicted_high_f": 66.0,
        "lead_hours": 8.0,
        "source_count": 4,
    }


def _declare(
    store: PaperStore,
    *,
    experiment_id: str = "exp-1",
    family: str = "gaussian-pit-station-lead",
    key: str = "gaussian-pit-station-lead-v1",
    version: str = "v1",
    role: str = "confirmatory",
    params: dict[str, object] | None = None,
) -> str:
    return store.record_research_experiment(
        experiment_id=experiment_id,
        hypothesis_family=family,
        candidate_key=key,
        candidate_version=version,
        parameter_json=params if params is not None else {"shrinkage_k": 40.0},
        evidence_role=role,
    )


_PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

# Sentinel distinguishing "not passed" from an explicit ``None`` for the four
# optional google_challenger_* fields below -- ``_case_row`` must be able to
# emit a row that omits these keys entirely (the ordinary, no-Google-evidence
# case) as well as a row that carries an explicit ``None`` for one of them
# (a deliberately malformed/partial-evidence regression).
_OMIT = object()


def _implied_lead_days(decision_at: str, target_date: str) -> int:
    """Test-only reimplementation of the production lead_days formula
    (independent of ``research_walkforward._expected_lead_days``, on
    purpose -- a test fixture should not depend on the private helper it
    is exercising), so ``_case_row``'s default rows are always
    self-consistent for whatever decision_at/target_date a test picks,
    without hardcoding one lead_days number that only matches some
    combinations (binding condition C2)."""

    decision = datetime.fromisoformat(decision_at)
    target = date.fromisoformat(target_date)
    return (target - decision.astimezone(_PACIFIC_TZ).date()).days


def _case_row(
    *,
    target_date: str = "2026-06-25",
    station_id: str = "KSFO",
    predicted_high_f: float = 66.0,
    ticker: str = "KXHIGHTSFO-TEST-B65.5",
    yes_bid: float = 0.24,
    yes_ask: float = 0.26,
    decision_at: str = "2026-06-25T15:00:00+00:00",
    settled_at: str = "2026-06-26T04:00:00+00:00",
    actual_high_f: float = 67.0,
    baseline_mu: float = 66.0,
    baseline_sigma: float = 3.0,
    lead_days: int | None = None,
    google_challenger_mu: object = _OMIT,
    google_challenger_sigma: object = _OMIT,
    google_challenger_action: object = _OMIT,
    google_challenger_policy_version: object = _OMIT,
) -> dict[str, object]:
    """One historical row: scan_context_snapshots payload columns plus the
    settled-outcome fields Task 2's loader needs to build a ResearchCase.

    ``lead_days`` defaults to the value implied by this row's own
    ``decision_at``/``target_date`` (never a fixed number), so every
    default-built row is automatically consistent under C2's
    lead_days-vs-decision_at/target_date check. Pass an explicit
    ``lead_days`` to deliberately construct an inconsistent row for a
    regression test.

    The four ``google_challenger_*`` keys are omitted entirely unless
    explicitly passed -- the loader treats "all four absent" as "no Google
    evidence for this case" (never an error).
    """

    market = {
        ticker: {
            "ticker": ticker,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "floor_strike": 65.0,
            "cap_strike": 66.0,
        }
    }
    resolved_lead_days = (
        lead_days if lead_days is not None else _implied_lead_days(decision_at, target_date)
    )
    row: dict[str, object] = {
        "target_date": target_date,
        "station_id": station_id,
        "forecast_json": json.dumps(_forecast_payload(predicted_high_f), sort_keys=True),
        "intraday_json": json.dumps({}, sort_keys=True),
        "market_json": json.dumps(market, sort_keys=True),
        "prediction_features_json": json.dumps(_features_payload(), sort_keys=True),
        "decision_at": decision_at,
        "settled_at": settled_at,
        "actual_high_f": actual_high_f,
        "baseline_mu": baseline_mu,
        "baseline_sigma": baseline_sigma,
        "lead_days": resolved_lead_days,
    }
    if google_challenger_mu is not _OMIT:
        row["google_challenger_mu"] = google_challenger_mu
    if google_challenger_sigma is not _OMIT:
        row["google_challenger_sigma"] = google_challenger_sigma
    if google_challenger_action is not _OMIT:
        row["google_challenger_action"] = google_challenger_action
    if google_challenger_policy_version is not _OMIT:
        row["google_challenger_policy_version"] = google_challenger_policy_version
    return row


# A training row far enough in the past (relative to every Task 2 test's
# target day, and beyond any embargo width exercised below) that it is
# always a leakage-safe training candidate. Its own (station, target_date)
# group has no training history of its own, though, so every evidence
# built with it also carries exactly one UnavailableFold for its own day
# unless a test explicitly builds further history before it.
_ANCHOR_TARGET_DATE = "2026-01-01"
_ANCHOR_FOLD_ID = f"KSFO:{_ANCHOR_TARGET_DATE}"


def _anchor_training_row(
    *, station_id: str = "KSFO", ticker: str = "KXHIGHTSFO-ANCHOR"
) -> dict[str, object]:
    return _case_row(
        target_date=_ANCHOR_TARGET_DATE,
        station_id=station_id,
        ticker=ticker,
        decision_at="2026-01-01T15:00:00+00:00",
        settled_at="2026-01-02T04:00:00+00:00",
        actual_high_f=55.0,
        baseline_mu=54.0,
        baseline_sigma=3.0,
    )


def _fold_by_id(evidence: WalkForwardEvidence, fold_id: str) -> WalkForwardFold | None:
    for fold in evidence.folds:
        if fold.fold_id == fold_id:
            return fold
    return None


def _unavailable_ids(evidence: WalkForwardEvidence) -> set[str]:
    return {entry.fold_id for entry in evidence.unavailable}


# ---------------------------------------------------------------------------
# source_context_hash: pure canonicalization
# ---------------------------------------------------------------------------


def test_source_context_hash_signature_excludes_profile_and_bankroll() -> None:
    """The hash cannot depend on profile/bankroll/sleeve/account identity.

    Those belong to decision rows, not the shared source hash -- enforced
    structurally by the function simply never accepting them.
    """

    parameters = set(inspect.signature(source_context_hash).parameters)
    assert parameters == {
        "target_date",
        "station_id",
        "forecast",
        "intraday",
        "market",
        "features",
    }


def test_source_context_hash_is_independent_of_input_key_order() -> None:
    forecast_a = {"predicted_high_f": 66.0, "method": "blend"}
    forecast_b = {"method": "blend", "predicted_high_f": 66.0}

    hash_a = source_context_hash(
        target_date="2026-06-20",
        station_id="KSFO",
        forecast=forecast_a,
        intraday={},
        market=_market_payload(),
        features=_features_payload(),
    )
    hash_b = source_context_hash(
        target_date="2026-06-20",
        station_id="KSFO",
        forecast=forecast_b,
        intraday={},
        market=_market_payload(),
        features=_features_payload(),
    )

    assert hash_a == hash_b


def test_source_context_hash_changes_when_forecast_content_changes() -> None:
    base = source_context_hash(
        target_date="2026-06-20",
        station_id="KSFO",
        forecast=_forecast_payload(66.0),
        intraday={},
        market=_market_payload(),
        features=_features_payload(),
    )
    changed = source_context_hash(
        target_date="2026-06-20",
        station_id="KSFO",
        forecast=_forecast_payload(67.0),
        intraday={},
        market=_market_payload(),
        features=_features_payload(),
    )

    assert base != changed


# ---------------------------------------------------------------------------
# record_source_neutral_scan_context: reuse across profiles + fail closed
# ---------------------------------------------------------------------------


def test_one_source_context_can_feed_multiple_profile_decisions(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    forecast = _forecast_payload()
    market = _market_payload()
    features = _features_payload()

    live_context_id = store.record_source_neutral_scan_context(
        target_date="2026-06-20",
        station_id="KSFO",
        scan_run_id="live-scan-run-1",
        forecast=forecast,
        intraday={},
        market=market,
        features=features,
    )
    target_context_id = store.record_source_neutral_scan_context(
        target_date="2026-06-20",
        station_id="KSFO",
        scan_run_id="target-scan-run-1",
        forecast=forecast,
        intraday={},
        market=market,
        features=features,
    )
    motion_context_id = store.record_source_neutral_scan_context(
        target_date="2026-06-20",
        station_id="KSFO",
        scan_run_id="motion-scan-run-1",
        forecast=forecast,
        intraday={},
        market=market,
        features=features,
    )

    assert live_context_id == target_context_id == motion_context_id
    with store.connect() as conn:
        row_count = conn.execute(
            "SELECT COUNT(*) FROM scan_context_snapshots"
        ).fetchone()[0]
        stored_hash, stored_run_id = conn.execute(
            "SELECT source_context_hash, source_scan_run_id FROM scan_context_snapshots"
        ).fetchone()
    assert row_count == 1
    assert stored_hash == source_context_hash(
        target_date="2026-06-20",
        station_id="KSFO",
        forecast=forecast,
        intraday={},
        market=market,
        features=features,
    )
    # The row is shared -- whichever profile observed it first labels it,
    # but that label never gates reuse by a later profile.
    assert stored_run_id == "live-scan-run-1"


def test_source_context_hash_unique_index_rejects_raw_duplicate_insert(
    tmp_path: Path,
) -> None:
    store = PaperStore(tmp_path / "paper.db")
    store.record_source_neutral_scan_context(
        target_date="2026-06-20",
        station_id="KSFO",
        scan_run_id="run-1",
        forecast=_forecast_payload(),
        intraday={},
        market=_market_payload(),
        features=_features_payload(),
    )
    with store.connect() as conn:
        context_hash = conn.execute(
            "SELECT source_context_hash FROM scan_context_snapshots"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                "INSERT INTO scan_context_snapshots (created_at, target_date, "
                "prediction_features_json, schema_version, source_context_hash) "
                "VALUES ('2026-01-01T00:00:00+00:00', '2026-06-20', '{}', 1, ?)",
                (context_hash,),
            )


def test_incomplete_forecast_payload_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    with pytest.raises(ValueError, match="forecast"):
        store.record_source_neutral_scan_context(
            target_date="2026-06-20",
            station_id="KSFO",
            scan_run_id="run-1",
            forecast={},
            intraday={},
            market=_market_payload(),
            features=_features_payload(),
        )


def test_forecast_missing_predicted_high_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    with pytest.raises(ValueError, match="predicted_high_f"):
        store.record_source_neutral_scan_context(
            target_date="2026-06-20",
            station_id="KSFO",
            scan_run_id="run-1",
            forecast={"method": "blend"},
            intraday={},
            market=_market_payload(),
            features=_features_payload(),
        )


def test_empty_market_payload_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    with pytest.raises(ValueError, match="market"):
        store.record_source_neutral_scan_context(
            target_date="2026-06-20",
            station_id="KSFO",
            scan_run_id="run-1",
            forecast=_forecast_payload(),
            intraday={},
            market={},
            features=_features_payload(),
        )


def test_market_entry_missing_a_tradeable_quote_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    with pytest.raises(ValueError, match="yes_bid|yes_ask"):
        store.record_source_neutral_scan_context(
            target_date="2026-06-20",
            station_id="KSFO",
            scan_run_id="run-1",
            forecast=_forecast_payload(),
            intraday={},
            market={"TICK": {"ticker": "TICK"}},
            features=_features_payload(),
        )


def test_empty_features_payload_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    with pytest.raises(ValueError, match="features"):
        store.record_source_neutral_scan_context(
            target_date="2026-06-20",
            station_id="KSFO",
            scan_run_id="run-1",
            forecast=_forecast_payload(),
            intraday={},
            market=_market_payload(),
            features={},
        )


def test_malformed_target_date_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    with pytest.raises(ValueError, match="target date"):
        store.record_source_neutral_scan_context(
            target_date="not-a-date",
            station_id="KSFO",
            scan_run_id="run-1",
            forecast=_forecast_payload(),
            intraday={},
            market=_market_payload(),
            features=_features_payload(),
        )


def test_missing_station_id_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    with pytest.raises(ValueError, match="station"):
        store.record_source_neutral_scan_context(
            target_date="2026-06-20",
            station_id="  ",
            scan_run_id="run-1",
            forecast=_forecast_payload(),
            intraday={},
            market=_market_payload(),
            features=_features_payload(),
        )


def test_missing_scan_run_id_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    with pytest.raises(ValueError, match="scan run id"):
        store.record_source_neutral_scan_context(
            target_date="2026-06-20",
            station_id="KSFO",
            scan_run_id="",
            forecast=_forecast_payload(),
            intraday={},
            market=_market_payload(),
            features=_features_payload(),
        )


def test_intraday_may_be_empty_for_a_day_ahead_decision(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    context_id = store.record_source_neutral_scan_context(
        target_date="2026-06-20",
        station_id="KSFO",
        scan_run_id="run-1",
        forecast=_forecast_payload(),
        intraday={},
        market=_market_payload(),
        features=_features_payload(),
    )
    assert isinstance(context_id, int)


# ---------------------------------------------------------------------------
# Schema / migration idempotency
# ---------------------------------------------------------------------------


def test_init_creates_research_experiment_and_evidence_tables(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    with store.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"research_experiments", "research_evidence"} <= tables


def test_init_is_idempotent_and_safe_under_concurrent_construction(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paper.db"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: PaperStore(db_path), range(4)))
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
    assert {"research_experiments", "research_evidence"} <= tables
    assert {
        "trg_research_experiments_immutable_update",
        "trg_research_experiments_immutable_delete",
        "trg_research_experiments_immutable_insert",
        "trg_research_evidence_immutable_update",
        "trg_research_evidence_immutable_delete",
        "trg_research_evidence_immutable_insert",
        "trg_research_evidence_declared_before_window",
    } <= triggers


def test_init_concurrently_migrates_legacy_scan_context_schema(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE scan_context_snapshots (id INTEGER PRIMARY KEY, "
            "created_at TEXT, target_date TEXT NOT NULL, "
            "prediction_features_json TEXT NOT NULL)"
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: PaperStore(db_path), range(4)))

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(scan_context_snapshots)")
        }
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(scan_context_snapshots)")
        }
    assert {"source_context_hash", "source_scan_run_id"} <= columns
    assert "idx_scan_context_snapshots_source_hash" in indexes


def test_decision_snapshots_gains_policy_fingerprint_column_on_legacy_db(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE decision_snapshots (id INTEGER PRIMARY KEY, created_at TEXT, "
            "target_date TEXT, market_ticker TEXT, label TEXT, action TEXT, side TEXT, "
            "approved INTEGER, probability REAL, probability_lcb REAL, yes_bid REAL, "
            "yes_ask REAL, spread REAL, fee_per_contract REAL, cost_per_contract REAL, "
            "edge REAL, edge_lcb REAL, kelly_fraction REAL, recommended_contracts REAL, "
            "recommended_spend REAL, expected_profit REAL, trade_quality_score REAL, "
            "intraday_is_complete INTEGER DEFAULT 0, reasons_json TEXT)"
        )

    PaperStore(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(decision_snapshots)")}
    assert "decision_policy_fingerprint" in columns


def test_source_context_hash_index_is_unique_and_partial(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    with store.connect() as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_scan_context_snapshots_source_hash'"
        ).fetchone()[0]
    assert "UNIQUE" in sql.upper()
    assert "WHERE SOURCE_CONTEXT_HASH IS NOT NULL" in sql.upper()


# ---------------------------------------------------------------------------
# research_experiments: immutable declarations
# ---------------------------------------------------------------------------


def test_declare_research_experiment_persists_row(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    with store.connect() as conn:
        row = conn.execute(
            "SELECT experiment_id, hypothesis_family, candidate_key, "
            "candidate_version, evidence_role, parameter_json "
            "FROM research_experiments"
        ).fetchone()
    assert row[0] == "exp-1"
    assert row[1] == "gaussian-pit-station-lead"
    assert row[4] == "confirmatory"
    assert json.loads(row[5]) == {"shrinkage_k": 40.0}


def test_duplicate_experiment_id_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    with pytest.raises(sqlite3.IntegrityError):
        _declare(store, family="a-different-family", key="a-different-key")


def test_duplicate_candidate_identity_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store, experiment_id="exp-1")
    with pytest.raises(sqlite3.IntegrityError):
        _declare(store, experiment_id="exp-2")


def test_invalid_evidence_role_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    with pytest.raises(ValueError, match="evidence_role"):
        _declare(store, role="speculative")


def test_invalid_evidence_role_is_rejected_at_the_sql_layer(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    with store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO research_experiments (experiment_id, declared_at, "
                "hypothesis_family, candidate_key, candidate_version, "
                "parameter_json, evidence_role) VALUES "
                "('exp-x', '2026-01-01T00:00:00+00:00', 'fam', 'key', 'v1', "
                "'{}', 'bogus')"
            )


def test_research_experiment_is_immutable_to_update(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    with store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE research_experiments SET candidate_version='v2' "
                "WHERE experiment_id='exp-1'"
            )


def test_research_experiment_is_immutable_to_delete(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    with store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM research_experiments WHERE experiment_id='exp-1'")


def test_experiment_definition_becomes_immutable_after_its_first_evidence_row(
    tmp_path: Path,
) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    store.record_research_evidence(
        experiment_id="exp-1",
        fold_id="fold-1",
        station_id="KSFO",
        target_date=FAR_FUTURE_DATE,
        evaluated_at=FAR_FUTURE_AT,
        baseline={"mu": 65.0, "sigma": 2.0},
        challenger={"mu": 66.0, "sigma": 1.8},
    )
    with store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE research_experiments SET candidate_version='v2' "
                "WHERE experiment_id='exp-1'"
            )


# ---------------------------------------------------------------------------
# research_evidence: immutable, and declared strictly before its window
# ---------------------------------------------------------------------------


def test_record_research_evidence_persists_row(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    store.record_research_evidence(
        experiment_id="exp-1",
        fold_id="fold-1",
        station_id="KSFO",
        target_date=FAR_FUTURE_DATE,
        evaluated_at=FAR_FUTURE_AT,
        baseline={"mu": 65.0, "sigma": 2.0},
        challenger={"mu": 66.0, "sigma": 1.8},
    )
    with store.connect() as conn:
        row = conn.execute(
            "SELECT experiment_id, fold_id, station_id, target_date, "
            "baseline_json, challenger_json FROM research_evidence"
        ).fetchone()
    assert row[0] == "exp-1"
    assert row[3] == FAR_FUTURE_DATE
    assert json.loads(row[4]) == {"mu": 65.0, "sigma": 2.0}
    assert json.loads(row[5]) == {"mu": 66.0, "sigma": 1.8}


def test_evidence_requires_a_declared_experiment(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    with pytest.raises(sqlite3.IntegrityError):
        store.record_research_evidence(
            experiment_id="never-declared",
            fold_id="fold-1",
            station_id="KSFO",
            target_date=FAR_FUTURE_DATE,
            evaluated_at=FAR_FUTURE_AT,
            baseline={"mu": 65.0},
            challenger={"mu": 66.0},
        )


def test_evidence_evaluated_before_declaration_is_rejected(tmp_path: Path) -> None:
    """A challenger can never be selected by the days it reports on."""

    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    with pytest.raises(sqlite3.IntegrityError, match="declared before"):
        store.record_research_evidence(
            experiment_id="exp-1",
            fold_id="fold-1",
            station_id="KSFO",
            target_date=FAR_FUTURE_DATE,
            evaluated_at=FAR_PAST_AT,
            baseline={"mu": 65.0},
            challenger={"mu": 66.0},
        )


def test_evidence_target_date_before_declaration_day_is_rejected(
    tmp_path: Path,
) -> None:
    """A target date on or before the declaration's own Pacific day is
    now caught by the Python-side Pacific check (finding F3 repair) before
    it ever reaches the UTC trigger backstop, so this raises ValueError
    rather than the trigger's sqlite3.IntegrityError."""

    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    with pytest.raises(ValueError, match="Pacific"):
        store.record_research_evidence(
            experiment_id="exp-1",
            fold_id="fold-1",
            station_id="KSFO",
            target_date=FAR_PAST_DATE,
            evaluated_at=FAR_FUTURE_AT,
            baseline={"mu": 65.0},
            challenger={"mu": 66.0},
        )


def test_evidence_accepted_when_fully_after_declaration(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    store.record_research_evidence(
        experiment_id="exp-1",
        fold_id="fold-1",
        station_id="KSFO",
        target_date=FAR_FUTURE_DATE,
        evaluated_at=FAR_FUTURE_AT,
        baseline={"mu": 65.0},
        challenger={"mu": 66.0},
    )
    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM research_evidence").fetchone()[0]
    assert count == 1


def test_research_evidence_is_immutable_to_update(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    store.record_research_evidence(
        experiment_id="exp-1",
        fold_id="fold-1",
        station_id="KSFO",
        target_date=FAR_FUTURE_DATE,
        evaluated_at=FAR_FUTURE_AT,
        baseline={"mu": 65.0},
        challenger={"mu": 66.0},
    )
    with store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE research_evidence SET baseline_json='{}' "
                "WHERE experiment_id='exp-1'"
            )


def test_research_evidence_is_immutable_to_delete(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    store.record_research_evidence(
        experiment_id="exp-1",
        fold_id="fold-1",
        station_id="KSFO",
        target_date=FAR_FUTURE_DATE,
        evaluated_at=FAR_FUTURE_AT,
        baseline={"mu": 65.0},
        challenger={"mu": 66.0},
    )
    with store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "DELETE FROM research_evidence WHERE experiment_id='exp-1'"
            )


def test_duplicate_evidence_key_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    store.record_research_evidence(
        experiment_id="exp-1",
        fold_id="fold-1",
        station_id="KSFO",
        target_date=FAR_FUTURE_DATE,
        evaluated_at=FAR_FUTURE_AT,
        baseline={"mu": 65.0},
        challenger={"mu": 66.0},
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.record_research_evidence(
            experiment_id="exp-1",
            fold_id="fold-1",
            station_id="KSFO",
            target_date=FAR_FUTURE_DATE,
            evaluated_at=FAR_FUTURE_AT,
            baseline={"mu": 99.0},
            challenger={"mu": 99.0},
        )


def test_empty_baseline_payload_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    with pytest.raises(ValueError, match="baseline"):
        store.record_research_evidence(
            experiment_id="exp-1",
            fold_id="fold-1",
            station_id="KSFO",
            target_date=FAR_FUTURE_DATE,
            evaluated_at=FAR_FUTURE_AT,
            baseline={},
            challenger={"mu": 66.0},
        )


def test_empty_challenger_payload_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    with pytest.raises(ValueError, match="challenger"):
        store.record_research_evidence(
            experiment_id="exp-1",
            fold_id="fold-1",
            station_id="KSFO",
            target_date=FAR_FUTURE_DATE,
            evaluated_at=FAR_FUTURE_AT,
            baseline={"mu": 65.0},
            challenger={},
        )


def test_evidence_requires_a_complete_fold_identity(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    with pytest.raises(ValueError, match="fold identity"):
        store.record_research_evidence(
            experiment_id="exp-1",
            fold_id="",
            station_id="KSFO",
            target_date=FAR_FUTURE_DATE,
            evaluated_at=FAR_FUTURE_AT,
            baseline={"mu": 65.0},
            challenger={"mu": 66.0},
        )


# ---------------------------------------------------------------------------
# Repair F1 (HIGH): INSERT OR REPLACE / ON CONFLICT DO UPDATE bypass the
# UPDATE/DELETE immutability triggers because REPLACE's internal delete does
# not fire a trigger while PRAGMA recursive_triggers stays off. Immutable
# BEFORE INSERT triggers close that gap without flipping recursive_triggers.
# ---------------------------------------------------------------------------


def test_insert_or_replace_on_research_experiments_is_rejected(tmp_path: Path) -> None:
    """A REPLACE-conflict rewrite must abort, and the original row must
    survive untouched -- covers the reviewer probe that rewrote
    parameter_json, backdated declared_at, and flipped evidence_role."""

    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    with store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "INSERT OR REPLACE INTO research_experiments (experiment_id, "
                "declared_at, hypothesis_family, candidate_key, candidate_version, "
                "parameter_json, evidence_role) VALUES ('exp-1', "
                "'2020-01-01T00:00:00+00:00', 'gaussian-pit-station-lead', "
                "'gaussian-pit-station-lead-v1', 'v1', "
                "'{\"shrinkage_k\": 999.0}', 'exploratory')"
            )
    with store.connect() as conn:
        row = conn.execute(
            "SELECT declared_at, evidence_role, parameter_json "
            "FROM research_experiments WHERE experiment_id='exp-1'"
        ).fetchone()
    assert row[1] == "confirmatory"
    assert json.loads(row[2]) == {"shrinkage_k": 40.0}


def test_insert_or_replace_on_research_experiments_by_candidate_identity_is_rejected(
    tmp_path: Path,
) -> None:
    """REPLACE conflicting on the UNIQUE candidate identity (a different
    experiment_id) must abort too, not just a REPLACE on the primary key."""

    store = PaperStore(tmp_path / "paper.db")
    _declare(store, experiment_id="exp-1")
    with store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "INSERT OR REPLACE INTO research_experiments (experiment_id, "
                "declared_at, hypothesis_family, candidate_key, candidate_version, "
                "parameter_json, evidence_role) VALUES ('exp-2', "
                "'2020-01-01T00:00:00+00:00', 'gaussian-pit-station-lead', "
                "'gaussian-pit-station-lead-v1', 'v1', '{}', 'exploratory')"
            )
    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM research_experiments").fetchone()[0]
    assert count == 1


def test_insert_or_replace_on_research_evidence_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    store.record_research_evidence(
        experiment_id="exp-1",
        fold_id="fold-1",
        station_id="KSFO",
        target_date=FAR_FUTURE_DATE,
        evaluated_at=FAR_FUTURE_AT,
        baseline={"mu": 65.0},
        challenger={"mu": 66.0},
    )
    with store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "INSERT OR REPLACE INTO research_evidence (experiment_id, fold_id, "
                "station_id, target_date, evaluated_at, baseline_json, "
                "challenger_json) VALUES ('exp-1', 'fold-1', 'KSFO', ?, ?, "
                "'{\"mu\": 999.0}', '{\"mu\": 999.0}')",
                (FAR_FUTURE_DATE, FAR_FUTURE_AT),
            )
    with store.connect() as conn:
        row = conn.execute(
            "SELECT baseline_json FROM research_evidence WHERE experiment_id='exp-1'"
        ).fetchone()
    assert json.loads(row[0]) == {"mu": 65.0}


def test_on_conflict_do_update_on_research_experiments_is_rejected(
    tmp_path: Path,
) -> None:
    """The identity columns make an UPSERT expressible on the primary key;
    it already routes through the ordinary UPDATE trigger (not a REPLACE
    delete), so this must already abort."""

    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    with store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "INSERT INTO research_experiments (experiment_id, declared_at, "
                "hypothesis_family, candidate_key, candidate_version, "
                "parameter_json, evidence_role) VALUES ('exp-1', "
                "'2020-01-01T00:00:00+00:00', 'gaussian-pit-station-lead', "
                "'gaussian-pit-station-lead-v1', 'v1', '{}', 'exploratory') "
                "ON CONFLICT(experiment_id) DO UPDATE SET "
                "evidence_role=excluded.evidence_role"
            )


def test_on_conflict_do_update_on_research_evidence_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    store.record_research_evidence(
        experiment_id="exp-1",
        fold_id="fold-1",
        station_id="KSFO",
        target_date=FAR_FUTURE_DATE,
        evaluated_at=FAR_FUTURE_AT,
        baseline={"mu": 65.0},
        challenger={"mu": 66.0},
    )
    with store.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "INSERT INTO research_evidence (experiment_id, fold_id, station_id, "
                "target_date, evaluated_at, baseline_json, challenger_json) VALUES "
                "('exp-1', 'fold-1', 'KSFO', ?, ?, '{}', '{}') "
                "ON CONFLICT(experiment_id, fold_id, station_id, target_date) "
                "DO UPDATE SET baseline_json=excluded.baseline_json",
                (FAR_FUTURE_DATE, FAR_FUTURE_AT),
            )


def test_plain_first_time_insert_still_works_on_both_tables(tmp_path: Path) -> None:
    """The new immutable-insert triggers must not block a genuine first
    write -- only ever a write that collides with an existing identity."""

    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    store.record_research_evidence(
        experiment_id="exp-1",
        fold_id="fold-1",
        station_id="KSFO",
        target_date=FAR_FUTURE_DATE,
        evaluated_at=FAR_FUTURE_AT,
        baseline={"mu": 65.0},
        challenger={"mu": 66.0},
    )
    with store.connect() as conn:
        experiments = conn.execute("SELECT COUNT(*) FROM research_experiments").fetchone()[0]
        evidence = conn.execute("SELECT COUNT(*) FROM research_evidence").fetchone()[0]
    assert experiments == 1
    assert evidence == 1


# ---------------------------------------------------------------------------
# Repair F2 (HIGH): NaN/inf silently accepted and persisted as invalid JSON.
# ---------------------------------------------------------------------------


def test_source_context_hash_rejects_non_finite_forecast_value() -> None:
    with pytest.raises(ValueError):
        source_context_hash(
            target_date="2026-06-20",
            station_id="KSFO",
            forecast={"predicted_high_f": float("nan")},
            intraday={},
            market=_market_payload(),
            features=_features_payload(),
        )


def test_market_yes_bid_nan_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    tampered_market = {
        "KXHIGHTSFO-TEST-B65.5": {
            "ticker": "KXHIGHTSFO-TEST-B65.5",
            "yes_bid": float("nan"),
            "yes_ask": 0.26,
        }
    }
    with pytest.raises(ValueError, match="yes_bid"):
        store.record_source_neutral_scan_context(
            target_date="2026-06-20",
            station_id="KSFO",
            scan_run_id="run-1",
            forecast=_forecast_payload(),
            intraday={},
            market=tampered_market,
            features=_features_payload(),
        )
    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM scan_context_snapshots").fetchone()[0]
    assert count == 0


def test_market_yes_ask_inf_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    tampered_market = {
        "KXHIGHTSFO-TEST-B65.5": {
            "ticker": "KXHIGHTSFO-TEST-B65.5",
            "yes_bid": 0.24,
            "yes_ask": float("inf"),
        }
    }
    with pytest.raises(ValueError, match="yes_ask"):
        store.record_source_neutral_scan_context(
            target_date="2026-06-20",
            station_id="KSFO",
            scan_run_id="run-1",
            forecast=_forecast_payload(),
            intraday={},
            market=tampered_market,
            features=_features_payload(),
        )


def test_features_inf_value_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    tampered_features = dict(_features_payload())
    tampered_features["lead_hours"] = float("inf")
    with pytest.raises(ValueError):
        store.record_source_neutral_scan_context(
            target_date="2026-06-20",
            station_id="KSFO",
            scan_run_id="run-1",
            forecast=_forecast_payload(),
            intraday={},
            market=_market_payload(),
            features=tampered_features,
        )


def test_valid_source_neutral_payload_is_unaffected_by_finiteness_checks(
    tmp_path: Path,
) -> None:
    store = PaperStore(tmp_path / "paper.db")
    context_id = store.record_source_neutral_scan_context(
        target_date="2026-06-20",
        station_id="KSFO",
        scan_run_id="run-1",
        forecast=_forecast_payload(),
        intraday={},
        market=_market_payload(),
        features=_features_payload(),
    )
    assert isinstance(context_id, int)


def test_research_experiment_parameter_json_nan_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    with pytest.raises(ValueError):
        store.record_research_experiment(
            experiment_id="exp-1",
            hypothesis_family="fam",
            candidate_key="key",
            candidate_version="v1",
            parameter_json={"shrinkage_k": float("nan")},
            evidence_role="confirmatory",
        )
    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM research_experiments").fetchone()[0]
    assert count == 0


def test_research_evidence_baseline_inf_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    with pytest.raises(ValueError):
        store.record_research_evidence(
            experiment_id="exp-1",
            fold_id="fold-1",
            station_id="KSFO",
            target_date=FAR_FUTURE_DATE,
            evaluated_at=FAR_FUTURE_AT,
            baseline={"mu": float("inf")},
            challenger={"mu": 66.0},
        )
    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM research_evidence").fetchone()[0]
    assert count == 0


def test_research_evidence_challenger_nan_is_rejected(tmp_path: Path) -> None:
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    with pytest.raises(ValueError):
        store.record_research_evidence(
            experiment_id="exp-1",
            fold_id="fold-1",
            station_id="KSFO",
            target_date=FAR_FUTURE_DATE,
            evaluated_at=FAR_FUTURE_AT,
            baseline={"mu": 65.0},
            challenger={"mu": float("nan")},
        )


# ---------------------------------------------------------------------------
# Repair F3 (MEDIUM): the UTC declared-before-window trigger alone admits a
# declaration up to 16:59:59 PDT *during* the Pacific target day. Strict "<"
# in the trigger is not the fix (it would reject the legitimate
# evening-before flow), so record_research_evidence enforces a strict
# Pacific-day check in Python, in addition to the unchanged UTC trigger.
# ---------------------------------------------------------------------------


def _declare_raw(
    store: PaperStore,
    *,
    experiment_id: str,
    declared_at: str,
) -> None:
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO research_experiments (experiment_id, declared_at, "
            "hypothesis_family, candidate_key, candidate_version, parameter_json, "
            "evidence_role) VALUES (?, ?, 'fam', 'key', 'v1', '{}', 'confirmatory')",
            (experiment_id, declared_at),
        )


def test_declaration_same_pacific_day_as_target_is_rejected(tmp_path: Path) -> None:
    """(1) Declared 2026-07-17T23:30Z = 16:30 PDT on the target's own
    Pacific day (2026-07-17) -- a same-day leak the UTC-only trigger used to
    admit."""

    store = PaperStore(tmp_path / "paper.db")
    _declare_raw(store, experiment_id="exp-1", declared_at="2026-07-17T23:30:00+00:00")
    with pytest.raises(ValueError, match="Pacific"):
        store.record_research_evidence(
            experiment_id="exp-1",
            fold_id="fold-1",
            station_id="KSFO",
            target_date="2026-07-17",
            evaluated_at="2026-07-18T00:00:00+00:00",
            baseline={"mu": 65.0},
            challenger={"mu": 66.0},
        )


def test_declaration_19_00_pdt_for_the_same_pacific_day_is_still_rejected(
    tmp_path: Path,
) -> None:
    """(2) Declared 2026-07-18T02:00Z = 19:00 PDT on 2026-07-17, evidence
    targeting that same Pacific day (2026-07-17) -- still rejected."""

    store = PaperStore(tmp_path / "paper.db")
    _declare_raw(store, experiment_id="exp-1", declared_at="2026-07-18T02:00:00+00:00")
    with pytest.raises(ValueError, match="Pacific"):
        store.record_research_evidence(
            experiment_id="exp-1",
            fold_id="fold-1",
            station_id="KSFO",
            target_date="2026-07-17",
            evaluated_at="2026-07-18T03:00:00+00:00",
            baseline={"mu": 65.0},
            challenger={"mu": 66.0},
        )


def test_declaration_evening_before_pacific_target_day_is_accepted(
    tmp_path: Path,
) -> None:
    """(3) Declared 2026-07-18T06:59Z = 23:59 PDT on 2026-07-17, evidence
    targeting the *next* Pacific day (2026-07-18) -- the legitimate
    evening-before flow strict "<" in the trigger alone would have broken."""

    store = PaperStore(tmp_path / "paper.db")
    _declare_raw(store, experiment_id="exp-1", declared_at="2026-07-18T06:59:00+00:00")
    store.record_research_evidence(
        experiment_id="exp-1",
        fold_id="fold-1",
        station_id="KSFO",
        target_date="2026-07-18",
        evaluated_at="2026-07-19T00:00:00+00:00",
        baseline={"mu": 65.0},
        challenger={"mu": 66.0},
    )
    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM research_evidence").fetchone()[0]
    assert count == 1


def test_malformed_declared_at_is_rejected(tmp_path: Path) -> None:
    """(4a) A malformed declared_at fails closed rather than comparing
    against an unparseable day."""

    store = PaperStore(tmp_path / "paper.db")
    _declare_raw(store, experiment_id="exp-1", declared_at="not-a-timestamp")
    with pytest.raises(ValueError, match="Pacific"):
        store.record_research_evidence(
            experiment_id="exp-1",
            fold_id="fold-1",
            station_id="KSFO",
            target_date=FAR_FUTURE_DATE,
            evaluated_at=FAR_FUTURE_AT,
            baseline={"mu": 65.0},
            challenger={"mu": 66.0},
        )


def test_naive_declared_at_is_rejected(tmp_path: Path) -> None:
    """(4b) A timezone-naive declared_at is ambiguous in Pacific terms and
    must fail closed rather than being assumed UTC or local."""

    store = PaperStore(tmp_path / "paper.db")
    _declare_raw(store, experiment_id="exp-1", declared_at="2026-01-01T00:00:00")
    with pytest.raises(ValueError, match="Pacific"):
        store.record_research_evidence(
            experiment_id="exp-1",
            fold_id="fold-1",
            station_id="KSFO",
            target_date=FAR_FUTURE_DATE,
            evaluated_at=FAR_FUTURE_AT,
            baseline={"mu": 65.0},
            challenger={"mu": 66.0},
        )


# ---------------------------------------------------------------------------
# Repair F4 (HIGH): record_source_neutral_scan_context has no production
# caller (option (ii) -- see the dated decision note in
# docs/superpowers/plans/2026-07-17-chronological-research-tuning-and-promotion.md).
# This section covers: (a) the load-time derivation helper Task 2's loader
# can use instead, and (b) the prune trap -- any row that DOES carry a
# source_context_hash must survive prune_decision_snapshots even with zero
# referencing decisions, since it is source-neutral by construction.
# ---------------------------------------------------------------------------


def test_derive_source_neutral_context_matches_direct_hash() -> None:
    row = {
        "target_date": "2026-06-20",
        "station_id": "KSFO",
        "forecast_json": json.dumps(_forecast_payload(), sort_keys=True),
        "intraday_json": json.dumps({}, sort_keys=True),
        "market_json": json.dumps(_market_payload(), sort_keys=True),
        "prediction_features_json": json.dumps(_features_payload(), sort_keys=True),
    }
    derived = source_neutral_context_from_scan_context_row(row)
    assert derived is not None
    assert derived["source_context_hash"] == source_context_hash(
        target_date="2026-06-20",
        station_id="KSFO",
        forecast=_forecast_payload(),
        intraday={},
        market=_market_payload(),
        features=_features_payload(),
    )


def test_derive_source_neutral_context_matches_the_hash_actually_stored_by_record(
    tmp_path: Path,
) -> None:
    """The derived hash for a row's raw JSON columns must equal the hash
    record_source_neutral_scan_context would have stored for the same
    content -- the whole point of the loader helper."""

    store = PaperStore(tmp_path / "paper.db")
    store.record_source_neutral_scan_context(
        target_date="2026-06-20",
        station_id="KSFO",
        scan_run_id="run-1",
        forecast=_forecast_payload(),
        intraday={},
        market=_market_payload(),
        features=_features_payload(),
    )
    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT target_date, station_id, forecast_json, intraday_json, "
            "market_json, prediction_features_json, source_context_hash "
            "FROM scan_context_snapshots"
        ).fetchone()
    derived = source_neutral_context_from_scan_context_row(dict(row))
    assert derived is not None
    assert derived["source_context_hash"] == row["source_context_hash"]


def test_derive_source_neutral_context_returns_none_for_missing_market() -> None:
    row = {
        "target_date": "2026-06-20",
        "station_id": "KSFO",
        "forecast_json": json.dumps(_forecast_payload(), sort_keys=True),
        "intraday_json": json.dumps({}, sort_keys=True),
        "market_json": None,
        "prediction_features_json": json.dumps(_features_payload(), sort_keys=True),
    }
    assert source_neutral_context_from_scan_context_row(row) is None


def test_derive_source_neutral_context_returns_none_for_malformed_json() -> None:
    row = {
        "target_date": "2026-06-20",
        "station_id": "KSFO",
        "forecast_json": "{not-json",
        "intraday_json": "{}",
        "market_json": json.dumps(_market_payload(), sort_keys=True),
        "prediction_features_json": json.dumps(_features_payload(), sort_keys=True),
    }
    assert source_neutral_context_from_scan_context_row(row) is None


def test_derive_source_neutral_context_returns_none_for_non_finite_price() -> None:
    tampered_market = {
        "TICK": {"ticker": "TICK", "yes_bid": float("nan"), "yes_ask": 0.5}
    }
    row = {
        "target_date": "2026-06-20",
        "station_id": "KSFO",
        "forecast_json": json.dumps(_forecast_payload(), sort_keys=True),
        "intraday_json": json.dumps({}, sort_keys=True),
        "market_json": json.dumps(tampered_market, sort_keys=True, allow_nan=True),
        "prediction_features_json": json.dumps(_features_payload(), sort_keys=True),
    }
    assert source_neutral_context_from_scan_context_row(row) is None


def test_prune_protects_source_neutral_rows_with_no_referencing_decision(
    tmp_path: Path,
) -> None:
    """record_source_neutral_scan_context rows are source-neutral by
    construction -- no decision_snapshots row ever references them by FK --
    so the generic "unreferenced context" prune sweep must not treat them as
    orphaned dead weight the way it treats an ordinary per-profile context
    row whose decisions have all aged out."""

    store = PaperStore(tmp_path / "paper.db")
    store.record_source_neutral_scan_context(
        target_date="2026-06-20",
        station_id="KSFO",
        scan_run_id="run-1",
        forecast=_forecast_payload(),
        intraday={},
        market=_market_payload(),
        features=_features_payload(),
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE scan_context_snapshots SET created_at = datetime('now', '-100 days')"
        )

    result = store.prune_decision_snapshots(full_days=7, dedup_days=45)

    with store.connect() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM scan_context_snapshots"
        ).fetchone()[0]
    assert remaining == 1
    assert result["contexts_dropped"] == 0


def test_prune_still_drops_an_ordinary_unreferenced_context_without_a_hash(
    tmp_path: Path,
) -> None:
    """Regression guard: the new source_context_hash IS NULL clause must not
    accidentally protect ordinary per-profile context rows too -- only rows
    that actually carry a canonical hash."""

    store = PaperStore(tmp_path / "paper.db")
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO scan_context_snapshots (created_at, target_date, "
            "prediction_features_json) VALUES "
            "(datetime('now', '-100 days'), '2026-06-01', '{}')"
        )

    result = store.prune_decision_snapshots(full_days=7, dedup_days=45)

    assert result["contexts_dropped"] == 1


# ---------------------------------------------------------------------------
# Task 2: leakage-resistant chronological folds
#
# Covers the five load-bearing guarantees the just-completed Task 1 review
# flagged for explicit enforcement:
#   1. chronology (settlement-before-decision + structural day ordering)
#   2. the configurable one-day embargo
#   3. load-time cross-profile dedupe via the derived source_context_hash
#   4. fail-closed handling of malformed/ambiguous/inconsistent rows
#   5. determinism (content-sorted output, independent of input row order)
# ---------------------------------------------------------------------------


def test_late_settling_training_case_is_excluded() -> None:
    """Guarantee 1 (temporal): a case that settles at/after the test
    decision can never leak into that fold's training pool."""

    test_row = _case_row(target_date="2026-06-25", decision_at="2026-06-25T15:00:00+00:00")
    late_settling_row = _case_row(
        target_date="2026-06-20",
        ticker="KXHIGHTSFO-TEST-B60.5",
        decision_at="2026-06-20T15:00:00+00:00",
        settled_at="2026-06-26T04:00:00+00:00",  # settles AFTER the test decision
    )

    evidence = build_walk_forward_evidence([_anchor_training_row(), test_row, late_settling_row])

    fold = _fold_by_id(evidence, "KSFO:2026-06-25")
    assert fold is not None
    train_days = {c.target_date.isoformat() for c in fold.train}
    assert "2026-06-20" not in train_days
    assert _ANCHOR_TARGET_DATE in train_days


def test_same_target_day_training_case_is_excluded_even_when_marked_settled_earlier() -> None:
    """Guarantee 1 (structural): a candidate sharing the test's own target
    day must be excluded even if its settled_at looks temporally eligible
    -- the day-order check does not trust settled_at alone."""

    test_row = _case_row(target_date="2026-06-25", decision_at="2026-06-25T15:00:00+00:00")
    same_day_row = _case_row(
        target_date="2026-06-25",
        ticker="KXHIGHTSFO-TEST-B70.5",
        decision_at="2026-06-25T08:00:00+00:00",
        settled_at="2026-06-25T09:00:00+00:00",  # implausibly early, but before test decision
    )

    evidence = build_walk_forward_evidence([_anchor_training_row(), test_row, same_day_row])

    fold = _fold_by_id(evidence, "KSFO:2026-06-25")
    assert fold is not None
    assert all(c.target_date.isoformat() != "2026-06-25" for c in fold.train)


def test_future_target_day_training_case_is_excluded_even_when_marked_settled_earlier() -> None:
    """Guarantee 1 (structural): a candidate whose own target day is AFTER
    the test day must be excluded, independent of a (data-anomalous)
    early settled_at."""

    test_row = _case_row(target_date="2026-06-25", decision_at="2026-06-25T15:00:00+00:00")
    future_row = _case_row(
        target_date="2026-06-26",
        ticker="KXHIGHTSFO-TEST-B75.5",
        decision_at="2026-06-20T10:00:00+00:00",
        settled_at="2026-06-20T20:00:00+00:00",  # before the test decision, but for a later day
    )

    evidence = build_walk_forward_evidence([_anchor_training_row(), test_row, future_row])

    fold = _fold_by_id(evidence, "KSFO:2026-06-25")
    assert fold is not None
    assert all(c.target_date.isoformat() != "2026-06-26" for c in fold.train)


def test_one_day_embargo_excludes_the_adjacent_day_for_the_same_station() -> None:
    """Guarantee 2: the default one-day embargo purges the day immediately
    before the test day for the SAME station, even though it settled well
    before the test decision."""

    test_row = _case_row(target_date="2026-06-25", decision_at="2026-06-25T15:00:00+00:00")
    embargoed_row = _case_row(
        target_date="2026-06-24",
        ticker="KXHIGHTSFO-TEST-GAP1",
        decision_at="2026-06-24T10:00:00+00:00",
        settled_at="2026-06-24T20:00:00+00:00",
    )
    eligible_row = _case_row(
        target_date="2026-06-23",
        ticker="KXHIGHTSFO-TEST-GAP2",
        decision_at="2026-06-23T10:00:00+00:00",
        settled_at="2026-06-23T20:00:00+00:00",
    )

    evidence = build_walk_forward_evidence(
        [_anchor_training_row(), test_row, embargoed_row, eligible_row]
    )

    fold = _fold_by_id(evidence, "KSFO:2026-06-25")
    assert fold is not None
    train_days = {c.target_date.isoformat() for c in fold.train}
    assert "2026-06-24" not in train_days
    assert "2026-06-23" in train_days


def test_embargo_does_not_restrict_a_different_station_on_the_adjacent_day() -> None:
    """Guarantee 2: the embargo is a same-station autocorrelation guard --
    a different city's data on the adjacent day is not embargoed."""

    test_row = _case_row(
        station_id="KSFO", target_date="2026-06-25", decision_at="2026-06-25T15:00:00+00:00"
    )
    other_station_adjacent_day = _case_row(
        station_id="KJFK",
        target_date="2026-06-24",
        ticker="KXHIGHTNYC-TEST-GAP1",
        decision_at="2026-06-24T10:00:00+00:00",
        settled_at="2026-06-24T20:00:00+00:00",
    )

    evidence = build_walk_forward_evidence(
        [_anchor_training_row(), test_row, other_station_adjacent_day]
    )

    fold = _fold_by_id(evidence, "KSFO:2026-06-25")
    assert fold is not None
    assert any(
        c.station_id == "KJFK" and c.target_date.isoformat() == "2026-06-24"
        for c in fold.train
    )


def test_embargo_is_configurable_and_widening_it_excludes_more_days() -> None:
    """Guarantee 2: embargo_days is a real parameter, not a hardcoded 1."""

    test_row = _case_row(target_date="2026-06-25", decision_at="2026-06-25T15:00:00+00:00")
    gap_two_row = _case_row(
        target_date="2026-06-23",
        ticker="KXHIGHTSFO-TEST-GAP2",
        decision_at="2026-06-23T10:00:00+00:00",
        settled_at="2026-06-23T20:00:00+00:00",
    )
    rows = [_anchor_training_row(), test_row, gap_two_row]

    default_evidence = build_walk_forward_evidence(rows)
    widened_evidence = build_walk_forward_evidence(rows, embargo_days=3)

    default_fold = _fold_by_id(default_evidence, "KSFO:2026-06-25")
    widened_fold = _fold_by_id(widened_evidence, "KSFO:2026-06-25")
    assert default_fold is not None and widened_fold is not None
    assert "2026-06-23" in {c.target_date.isoformat() for c in default_fold.train}
    assert "2026-06-23" not in {c.target_date.isoformat() for c in widened_fold.train}


def test_embargo_days_cannot_be_negative() -> None:
    cases = load_research_cases([_anchor_training_row()]).cases
    with pytest.raises(ValueError):
        build_walk_forward_folds(cases, embargo_days=-1)


def test_cross_profile_duplicate_scan_collapses_into_one_research_case() -> None:
    """Guarantee 3: the exact same real-world scan, written once per risk
    profile (identical content, different decision_at), must fold into
    exactly one ResearchCase -- never one per profile."""

    live_copy = _case_row(target_date="2026-06-25", decision_at="2026-06-25T15:00:00+00:00")
    target_copy = _case_row(target_date="2026-06-25", decision_at="2026-06-25T15:00:05+00:00")

    evidence = build_walk_forward_evidence([_anchor_training_row(), live_copy, target_copy])

    fold = _fold_by_id(evidence, "KSFO:2026-06-25")
    assert fold is not None
    assert len(fold.test) == 1
    assert fold.test[0].decision_at.isoformat() == "2026-06-25T15:00:00+00:00"
    assert evidence.skips == ()


def test_load_research_cases_dedup_selects_earliest_decision_at_regardless_of_row_order() -> None:
    earlier = _case_row(decision_at="2026-06-25T15:00:00+00:00")
    later = _case_row(decision_at="2026-06-25T15:00:05+00:00")

    forward = load_research_cases([earlier, later])
    backward = load_research_cases([later, earlier])

    assert len(forward.cases) == 1
    assert len(backward.cases) == 1
    assert forward.cases[0].decision_at.isoformat() == "2026-06-25T15:00:00+00:00"
    assert backward.cases[0] == forward.cases[0]


def test_inconsistent_duplicate_scan_is_skipped_with_a_recorded_reason() -> None:
    """Guarantee 3 + 4: rows sharing a hash that disagree on the settled
    outcome cannot be reconciled by guessing -- both are skipped loudly."""

    conflicting_a = _case_row(target_date="2026-06-25", actual_high_f=67.0)
    conflicting_b = _case_row(target_date="2026-06-25", actual_high_f=69.0)

    evidence = build_walk_forward_evidence([conflicting_a, conflicting_b])

    assert _fold_by_id(evidence, "KSFO:2026-06-25") is None
    assert "KSFO:2026-06-25" not in _unavailable_ids(evidence)
    reasons = {skip.reason for skip in evidence.skips}
    assert reasons == {"inconsistent_duplicate"}
    assert {skip.row_index for skip in evidence.skips} == {0, 1}


def test_malformed_market_payload_is_skipped_with_a_recorded_reason() -> None:
    """Guarantee 4: an empty/malformed market payload fails
    source_neutral_context_from_scan_context_row -- the row must be
    skipped loudly, not silently dropped."""

    row = _case_row()
    row["market_json"] = json.dumps({}, sort_keys=True)

    evidence = build_walk_forward_evidence([row])

    assert evidence.folds == ()
    assert evidence.unavailable == ()
    assert len(evidence.skips) == 1
    assert evidence.skips[0].reason == "malformed_source_context"
    assert evidence.skips[0].row_index == 0


def test_naive_decision_at_is_skipped_with_a_recorded_reason() -> None:
    """Guarantee 4: an ambiguous (timezone-naive) decision timestamp must
    fail closed rather than being assumed to be some timezone."""

    row = _case_row(decision_at="2026-06-25T15:00:00")

    evidence = build_walk_forward_evidence([row])

    assert evidence.folds == ()
    assert len(evidence.skips) == 1
    assert evidence.skips[0].reason == "invalid_decision_at"


def test_naive_settled_at_is_skipped_with_a_recorded_reason() -> None:
    row = _case_row(settled_at="2026-06-26T04:00:00")

    evidence = build_walk_forward_evidence([row])

    assert evidence.folds == ()
    assert len(evidence.skips) == 1
    assert evidence.skips[0].reason == "invalid_settled_at"


def test_settlement_before_its_own_decision_is_skipped_with_a_recorded_reason() -> None:
    """Guarantee 4: a row cannot settle before its own decision -- an
    impossible chronology for that single observation, excluded loudly."""

    row = _case_row(
        decision_at="2026-06-25T15:00:00+00:00",
        settled_at="2026-06-25T10:00:00+00:00",  # before its own decision
    )

    evidence = build_walk_forward_evidence([row])

    assert evidence.folds == ()
    assert len(evidence.skips) == 1
    assert evidence.skips[0].reason == "settlement_before_decision"


def test_non_finite_actual_high_f_is_skipped_with_a_recorded_reason() -> None:
    row = _case_row(actual_high_f=float("nan"))

    evidence = build_walk_forward_evidence([row])

    assert evidence.folds == ()
    assert len(evidence.skips) == 1
    assert evidence.skips[0].reason == "invalid_actual_high_f"


def test_non_finite_baseline_sigma_is_skipped_with_a_recorded_reason() -> None:
    row = _case_row(baseline_sigma=float("inf"))

    evidence = build_walk_forward_evidence([row])

    assert evidence.folds == ()
    assert len(evidence.skips) == 1
    assert evidence.skips[0].reason == "invalid_baseline_sigma"


def test_non_positive_baseline_sigma_is_skipped_with_a_recorded_reason() -> None:
    row = _case_row(baseline_sigma=0.0)

    evidence = build_walk_forward_evidence([row])

    assert evidence.folds == ()
    assert len(evidence.skips) == 1
    assert evidence.skips[0].reason == "invalid_baseline_sigma"


def test_negative_lead_days_is_skipped_with_a_recorded_reason() -> None:
    row = _case_row(lead_days=-1)

    evidence = build_walk_forward_evidence([row])

    assert evidence.folds == ()
    assert len(evidence.skips) == 1
    assert evidence.skips[0].reason == "invalid_lead_days"


def test_fold_with_no_eligible_training_history_is_reported_unavailable() -> None:
    """Plan Task 2 Step 4: insufficient history must be reported explicitly
    rather than silently omitted or backed by future data."""

    row = _case_row(target_date="2026-06-25")

    evidence = build_walk_forward_evidence([row])

    assert evidence.folds == ()
    assert evidence.unavailable == (
        UnavailableFold(
            fold_id="KSFO:2026-06-25",
            station_id="KSFO",
            target_date=date(2026, 6, 25),
            reason="no_training_history",
        ),
    )


def test_indivisible_station_target_day_fold_groups_multiple_brackets_together() -> None:
    """Correlated market brackets for the same city and target day stay in
    one fold's test tuple -- the fold is indivisible per (station, day)."""

    bracket_a = _case_row(target_date="2026-06-25", ticker="KXHIGHTSFO-TEST-B65.5")
    bracket_b = _case_row(target_date="2026-06-25", ticker="KXHIGHTSFO-TEST-B70.5")

    evidence = build_walk_forward_evidence([_anchor_training_row(), bracket_a, bracket_b])

    fold = _fold_by_id(evidence, "KSFO:2026-06-25")
    assert fold is not None
    assert len(fold.test) == 2
    # Both distinct hashes preserved -- neither bracket was deduped away.
    assert len({c.source_context_hash for c in fold.test}) == 2


def test_fold_id_is_deterministic_and_human_readable() -> None:
    row = _case_row(station_id="KJFK", target_date="2026-06-25")
    evidence = build_walk_forward_evidence([_anchor_training_row(station_id="KJFK"), row])

    fold = _fold_by_id(evidence, "KJFK:2026-06-25")
    assert fold is not None
    assert fold.fold_id == "KJFK:2026-06-25"


def test_fold_construction_is_invariant_to_input_row_order() -> None:
    """Guarantee 5: shuffling the input rows must not change the output --
    every collection is sorted by content, never by row order."""

    rows = [
        _anchor_training_row(),
        _case_row(target_date="2026-06-25", ticker="KXHIGHTSFO-TEST-B65.5"),
        _case_row(target_date="2026-06-25", ticker="KXHIGHTSFO-TEST-B70.5"),
        _case_row(
            target_date="2026-06-23",
            ticker="KXHIGHTSFO-TEST-GAP2",
            decision_at="2026-06-23T10:00:00+00:00",
            settled_at="2026-06-23T20:00:00+00:00",
        ),
    ]

    forward = build_walk_forward_evidence(rows)
    reversed_evidence = build_walk_forward_evidence(list(reversed(rows)))

    assert forward.folds == reversed_evidence.folds
    assert forward.unavailable == reversed_evidence.unavailable
    assert forward.skips == reversed_evidence.skips == ()


def test_same_db_state_produces_identical_folds_across_two_independent_calls() -> None:
    """Guarantee 5: no clock dependence, no randomness -- two calls over
    the same rows must be byte-identical."""

    rows = [
        _anchor_training_row(),
        _case_row(target_date="2026-06-25"),
        _case_row(
            target_date="2026-06-23",
            ticker="KXHIGHTSFO-TEST-GAP2",
            decision_at="2026-06-23T10:00:00+00:00",
            settled_at="2026-06-23T20:00:00+00:00",
        ),
    ]

    first = build_walk_forward_evidence(rows)
    second = build_walk_forward_evidence(rows)

    assert first == second


def test_dst_spring_forward_seam_preserves_settlement_before_decision_ordering() -> None:
    """A row whose own decision-to-settlement window crosses the US spring
    -forward transition (2026-03-08, America/Los_Angeles) must still be
    ordered and included correctly -- explicit UTC offsets make the
    'missing hour' irrelevant to absolute-instant comparisons."""

    test_row = _case_row(target_date="2026-03-09", decision_at="2026-03-09T20:00:00+00:00")
    spanning_row = _case_row(
        target_date="2026-03-07",
        ticker="KXHIGHTSFO-TEST-DST-SPRING",
        # Pacific civil date 2026-03-07 (lead_days=0, C2-consistent with its
        # own target_date) -- decision made the evening before the
        # transition; settled_at still crosses it.
        decision_at="2026-03-07T20:00:00-08:00",  # pre-transition (PST)
        settled_at="2026-03-08T03:30:00-07:00",  # post-transition (PDT)
    )

    evidence = build_walk_forward_evidence([_anchor_training_row(), test_row, spanning_row])

    fold = _fold_by_id(evidence, "KSFO:2026-03-09")
    assert fold is not None
    assert "2026-03-07" in {c.target_date.isoformat() for c in fold.train}


def test_fall_back_ambiguous_local_hour_is_disambiguated_by_explicit_utc_offset() -> None:
    """The US fall-back transition (2026-11-01, America/Los_Angeles) repeats
    the 01:00-02:00 local hour once as PDT (-07:00) then once as PST
    (-08:00). Two test-fold brackets using that identical local wall-clock
    time, but different explicit offsets, must still be ordered by their
    true, distinct absolute instants -- not treated as simultaneous or
    misordered."""

    earlier_bracket = _case_row(
        target_date="2026-11-02",
        ticker="KXHIGHTSFO-TEST-FALLBACK-A",
        decision_at="2026-11-01T01:30:00-07:00",  # pre-transition instant
        settled_at="2026-11-02T20:00:00+00:00",
    )
    later_bracket = _case_row(
        target_date="2026-11-02",
        ticker="KXHIGHTSFO-TEST-FALLBACK-B",
        decision_at="2026-11-01T01:30:00-08:00",  # post-transition instant, 1h later
        settled_at="2026-11-02T20:00:00+00:00",
    )

    evidence = build_walk_forward_evidence(
        [_anchor_training_row(), earlier_bracket, later_bracket]
    )

    fold = _fold_by_id(evidence, "KSFO:2026-11-02")
    assert fold is not None
    assert len(fold.test) == 2
    assert fold.decision_at.isoformat() == "2026-11-01T01:30:00-07:00"
    assert fold.decision_at < datetime.fromisoformat("2026-11-01T01:30:00-08:00")


def test_late_pacific_evening_decision_still_uses_its_stored_pacific_target_date() -> None:
    """The UTC/Pacific evening seam: a decision made at 23:30 Pacific on
    the target day is already the next UTC calendar day. Fold grouping and
    the embargo must key off the row's trusted, stored Pacific
    ``target_date`` field -- never a UTC-derived reinterpretation of
    ``decision_at``'s raw calendar date."""

    test_row = _case_row(
        target_date="2026-06-20",
        decision_at="2026-06-20T23:30:00-07:00",  # 2026-06-21T06:30:00Z in UTC
    )
    embargoed_adjacent_day = _case_row(
        target_date="2026-06-19",
        ticker="KXHIGHTSFO-TEST-EVENING-GAP1",
        decision_at="2026-06-19T10:00:00+00:00",
        settled_at="2026-06-19T20:00:00+00:00",
    )
    eligible_earlier_day = _case_row(
        target_date="2026-06-17",
        ticker="KXHIGHTSFO-TEST-EVENING-GAP3",
        decision_at="2026-06-17T10:00:00+00:00",
        settled_at="2026-06-17T20:00:00+00:00",
    )

    evidence = build_walk_forward_evidence(
        [_anchor_training_row(), test_row, embargoed_adjacent_day, eligible_earlier_day]
    )

    fold = _fold_by_id(evidence, "KSFO:2026-06-20")
    assert fold is not None
    train_days = {c.target_date.isoformat() for c in fold.train}
    assert "2026-06-19" not in train_days
    assert "2026-06-17" in train_days


def test_research_case_rejects_naive_decision_at_on_direct_construction() -> None:
    """Guarantee 4, defense in depth: ResearchCase itself refuses an
    ambiguous timestamp even when constructed directly, not just via the
    loader."""

    with pytest.raises(ValueError):
        ResearchCase(
            station_id="KSFO",
            target_date=date(2026, 6, 20),
            decision_at=datetime(2026, 6, 20, 15, 0, 0),  # naive
            settled_at=datetime(2026, 6, 21, 4, 0, 0, tzinfo=timezone.utc),
            lead_days=1,
            source_context_hash="abc",
            baseline_mu=66.0,
            baseline_sigma=3.0,
            actual_high_f=67.0,
        )


def test_research_case_rejects_settlement_before_its_own_decision_on_direct_construction() -> None:
    with pytest.raises(ValueError):
        ResearchCase(
            station_id="KSFO",
            target_date=date(2026, 6, 20),
            decision_at=datetime(2026, 6, 20, 15, 0, 0, tzinfo=timezone.utc),
            settled_at=datetime(2026, 6, 20, 10, 0, 0, tzinfo=timezone.utc),
            lead_days=1,
            source_context_hash="abc",
            baseline_mu=66.0,
            baseline_sigma=3.0,
            actual_high_f=67.0,
        )


def test_research_case_rejects_non_positive_sigma_on_direct_construction() -> None:
    with pytest.raises(ValueError):
        ResearchCase(
            station_id="KSFO",
            target_date=date(2026, 6, 20),
            decision_at=datetime(2026, 6, 20, 15, 0, 0, tzinfo=timezone.utc),
            settled_at=datetime(2026, 6, 21, 4, 0, 0, tzinfo=timezone.utc),
            lead_days=1,
            source_context_hash="abc",
            baseline_mu=66.0,
            baseline_sigma=0.0,
            actual_high_f=67.0,
        )


def test_research_case_rejects_negative_lead_days_on_direct_construction() -> None:
    with pytest.raises(ValueError):
        ResearchCase(
            station_id="KSFO",
            target_date=date(2026, 6, 20),
            decision_at=datetime(2026, 6, 20, 15, 0, 0, tzinfo=timezone.utc),
            settled_at=datetime(2026, 6, 21, 4, 0, 0, tzinfo=timezone.utc),
            lead_days=-1,
            source_context_hash="abc",
            baseline_mu=66.0,
            baseline_sigma=3.0,
            actual_high_f=67.0,
        )


def test_default_embargo_days_constant_is_one() -> None:
    """Plan text repeatedly names this 'the one-day embargo' -- the
    configurable default must actually be one day."""

    assert DEFAULT_EMBARGO_DAYS == 1


# ---------------------------------------------------------------------------
# Binding condition C3 (Task 2 review finding L4): the settled_at ==
# min(test decision_at) boundary must be EXCLUDED (strict <), not just the
# "settles strictly after" case already covered above.
# ---------------------------------------------------------------------------


def test_settlement_exactly_at_min_test_decision_at_is_excluded() -> None:
    """A training case that settles at the EXACT SAME instant as the
    fold's earliest test decision must still be excluded -- the
    eligibility check (``_case_eligible_for_training``) is strict ``<``,
    not ``<=``. Only "settles strictly after" had a pinned regression
    before this; this pins the exact-equality boundary too."""

    boundary_instant = "2026-06-25T15:00:00+00:00"
    test_row = _case_row(target_date="2026-06-25", decision_at=boundary_instant)
    exact_boundary_row = _case_row(
        target_date="2026-06-20",
        ticker="KXHIGHTSFO-TEST-BOUNDARY",
        decision_at="2026-06-20T10:00:00+00:00",
        settled_at=boundary_instant,  # settles at the EXACT test decision instant
    )

    evidence = build_walk_forward_evidence(
        [_anchor_training_row(), test_row, exact_boundary_row]
    )

    fold = _fold_by_id(evidence, "KSFO:2026-06-25")
    assert fold is not None
    assert all(c.target_date.isoformat() != "2026-06-20" for c in fold.train)


# ---------------------------------------------------------------------------
# Binding condition C2 (Task 2 review finding L5): lead_days must be
# consistent with the case's own decision_at/target_date before it can be
# used for pooling cohorts -- a corrupt lead_days is a recorded skip, never
# a silent mispool.
# ---------------------------------------------------------------------------


def test_lead_days_consistent_with_decision_and_target_is_accepted() -> None:
    """Positive case: a row whose declared lead_days matches what its own
    decision_at/target_date (Pacific civil days) imply loads normally."""

    row = _case_row(
        target_date="2026-06-25",
        decision_at="2026-06-24T15:00:00+00:00",  # Pacific civil date 2026-06-24
        lead_days=1,
    )
    result = load_research_cases([row])
    assert len(result.cases) == 1
    assert result.cases[0].lead_days == 1
    assert result.skips == ()


def test_lead_days_inconsistent_with_decision_and_target_is_skipped_with_a_recorded_reason() -> (
    None
):
    """C2: a declared lead_days that does not match what decision_at/
    target_date imply must be a recorded skip, not a silent mispool into
    the wrong station/lead pooling cohort."""

    row = _case_row(
        target_date="2026-06-25",
        decision_at="2026-06-24T15:00:00+00:00",  # implies lead_days=1
        lead_days=3,  # declared inconsistently
    )
    result = load_research_cases([row])
    assert result.cases == ()
    assert len(result.skips) == 1
    assert result.skips[0].reason == "lead_days_inconsistent_with_decision_and_target"


def test_research_case_rejects_lead_days_inconsistent_with_decision_and_target_on_direct_construction() -> (
    None
):
    """Defense in depth: ``ResearchCase`` itself refuses an internally
    inconsistent lead_days even on direct construction, not just via the
    loader (C2)."""

    with pytest.raises(ValueError):
        ResearchCase(
            station_id="KSFO",
            target_date=date(2026, 6, 25),
            # Pacific civil date 2026-06-24 -- implies lead_days=1, not 3.
            decision_at=datetime(2026, 6, 24, 15, 0, 0, tzinfo=timezone.utc),
            settled_at=datetime(2026, 6, 26, 4, 0, 0, tzinfo=timezone.utc),
            lead_days=3,
            source_context_hash="abc",
            baseline_mu=66.0,
            baseline_sigma=3.0,
            actual_high_f=67.0,
        )


def test_negative_computed_lead_days_is_still_rejected_as_invalid_lead_days() -> None:
    """A row whose decision_at civil date is AFTER its own target_date (an
    inherently anomalous case: deciding on a market after its own target
    day) implies a negative lead_days via ``_case_row``'s own
    auto-derivation -- still caught by the pre-existing non-negative
    guard, before C2's consistency check is ever reached."""

    row = _case_row(
        target_date="2026-06-20",
        decision_at="2026-06-22T15:00:00+00:00",  # Pacific civil date 2026-06-22 (after target)
    )  # lead_days left at its auto-derived (here, negative) default
    result = load_research_cases([row])
    assert result.cases == ()
    assert len(result.skips) == 1
    assert result.skips[0].reason == "invalid_lead_days"


# ---------------------------------------------------------------------------
# Optional Google-conditioned challenger evidence loading (research-only
# consumption of already-derived evidence; never backfilled from a raw
# Google value -- see GoogleChallengerEvidence's docstring).
# ---------------------------------------------------------------------------


def test_row_with_no_google_fields_loads_with_no_google_evidence() -> None:
    """The ordinary, expected state today: nothing yet writes durable
    Google challenger evidence (Google Task 7's google_challenger_snapshots
    has not landed), so a case has none -- not an error."""

    result = load_research_cases([_case_row()])
    assert len(result.cases) == 1
    assert result.cases[0].google_evidence is None
    assert result.skips == ()


def test_row_with_valid_forecast_google_evidence_loads_it() -> None:
    row = _case_row(
        google_challenger_mu=67.2,
        google_challenger_sigma=3.0,
        google_challenger_action=GOOGLE_CHALLENGER_FORECAST_ACTION,
        google_challenger_policy_version=GOOGLE_CHALLENGER_POLICY_VERSION,
    )
    result = load_research_cases([row])
    assert len(result.cases) == 1
    assert result.cases[0].google_evidence == GoogleChallengerEvidence(
        mu=67.2, sigma=3.0, action=GOOGLE_CHALLENGER_FORECAST_ACTION
    )


def test_row_with_valid_block_google_evidence_loads_it_with_no_mu() -> None:
    row = _case_row(
        google_challenger_mu=None,
        google_challenger_sigma=3.0,
        google_challenger_action=GOOGLE_CHALLENGER_BLOCK_ACTION,
        google_challenger_policy_version=GOOGLE_CHALLENGER_POLICY_VERSION,
    )
    result = load_research_cases([row])
    assert len(result.cases) == 1
    assert result.cases[0].google_evidence == GoogleChallengerEvidence(
        mu=None, sigma=3.0, action=GOOGLE_CHALLENGER_BLOCK_ACTION
    )


def test_block_action_with_a_mu_present_is_rejected_as_invalid_google_evidence() -> None:
    """A blocked action must never carry a tradeable mean -- present
    together, that is a data-integrity problem, not usable evidence."""

    row = _case_row(
        google_challenger_mu=67.2,
        google_challenger_sigma=3.0,
        google_challenger_action=GOOGLE_CHALLENGER_BLOCK_ACTION,
        google_challenger_policy_version=GOOGLE_CHALLENGER_POLICY_VERSION,
    )
    result = load_research_cases([row])
    assert result.cases == ()
    assert result.skips[0].reason == "invalid_google_evidence"


def test_forecast_action_missing_mu_is_rejected_as_invalid_google_evidence() -> None:
    row = _case_row(
        google_challenger_mu=None,
        google_challenger_sigma=3.0,
        google_challenger_action=GOOGLE_CHALLENGER_FORECAST_ACTION,
        google_challenger_policy_version=GOOGLE_CHALLENGER_POLICY_VERSION,
    )
    result = load_research_cases([row])
    assert result.cases == ()
    assert result.skips[0].reason == "invalid_google_evidence"


def test_unrecognized_google_action_is_rejected() -> None:
    row = _case_row(
        google_challenger_mu=67.2,
        google_challenger_sigma=3.0,
        google_challenger_action="made_up_action",
        google_challenger_policy_version=GOOGLE_CHALLENGER_POLICY_VERSION,
    )
    result = load_research_cases([row])
    assert result.cases == ()
    assert result.skips[0].reason == "invalid_google_evidence"


def test_unsupported_google_policy_version_is_rejected() -> None:
    row = _case_row(
        google_challenger_mu=67.2,
        google_challenger_sigma=3.0,
        google_challenger_action=GOOGLE_CHALLENGER_FORECAST_ACTION,
        google_challenger_policy_version="google-runtime-fixed-v2-not-yet-declared",
    )
    result = load_research_cases([row])
    assert result.cases == ()
    assert result.skips[0].reason == "invalid_google_evidence"


def test_non_positive_google_sigma_is_rejected() -> None:
    row = _case_row(
        google_challenger_mu=67.2,
        google_challenger_sigma=0.0,
        google_challenger_action=GOOGLE_CHALLENGER_FORECAST_ACTION,
        google_challenger_policy_version=GOOGLE_CHALLENGER_POLICY_VERSION,
    )
    result = load_research_cases([row])
    assert result.cases == ()
    assert result.skips[0].reason == "invalid_google_evidence"


def test_duplicate_rows_with_matching_google_evidence_collapse_normally() -> None:
    """Two profile-duplicated rows sharing the same source content AND the
    same Google evidence still collapse into one case, as Task 2 already
    guarantees for every other field."""

    shared = dict(
        google_challenger_mu=67.2,
        google_challenger_sigma=3.0,
        google_challenger_action=GOOGLE_CHALLENGER_FORECAST_ACTION,
        google_challenger_policy_version=GOOGLE_CHALLENGER_POLICY_VERSION,
    )
    row_a = _case_row(decision_at="2026-06-25T15:00:00+00:00", **shared)
    row_b = _case_row(decision_at="2026-06-25T15:00:05+00:00", **shared)
    result = load_research_cases([row_a, row_b])
    assert len(result.cases) == 1
    assert result.cases[0].google_evidence == GoogleChallengerEvidence(
        mu=67.2, sigma=3.0, action=GOOGLE_CHALLENGER_FORECAST_ACTION
    )


def test_duplicate_rows_with_conflicting_google_evidence_is_skipped_with_a_recorded_reason() -> (
    None
):
    """Two rows sharing one source_context_hash but disagreeing on Google
    evidence is a data-integrity problem (same real-world scan, two
    different derived challengers) -- skipped, never guessed at, matching
    ``_reconcile_duplicates``'s existing treatment of every other field."""

    row_a = _case_row(
        decision_at="2026-06-25T15:00:00+00:00",
        google_challenger_mu=67.2,
        google_challenger_sigma=3.0,
        google_challenger_action=GOOGLE_CHALLENGER_FORECAST_ACTION,
        google_challenger_policy_version=GOOGLE_CHALLENGER_POLICY_VERSION,
    )
    row_b = _case_row(
        decision_at="2026-06-25T15:00:05+00:00",
        google_challenger_mu=64.0,  # disagrees
        google_challenger_sigma=3.0,
        google_challenger_action=GOOGLE_CHALLENGER_FORECAST_ACTION,
        google_challenger_policy_version=GOOGLE_CHALLENGER_POLICY_VERSION,
    )
    result = load_research_cases([row_a, row_b])
    assert result.cases == ()
    assert all(s.reason == "inconsistent_duplicate" for s in result.skips)
    assert len(result.skips) == 2


def test_google_evidence_row_matches_real_challenger_formula_output() -> None:
    """Builds Google evidence from forecaster's actual frozen formula
    (``google_runtime_blend.google_challenger``) instead of made-up
    numbers, keeping the fixture honest. Calling it requires coercing
    inputs to Python float -- its own ``_finite_float`` rejects anything
    whose ``type(value) not in (int, float)``, including e.g. a
    ``numpy.float64`` or a bare ``Decimal``."""

    import sys

    forecaster_root = str(Path(__file__).resolve().parents[2] / "forecaster")
    sys.path.insert(0, forecaster_root)
    try:
        from google_runtime_blend import google_challenger
    finally:
        sys.path.remove(forecaster_root)

    baseline_mu, baseline_sigma, google_high = 66.0, 3.0, 69.0  # gap=3F, under the 7F block
    challenger = google_challenger(float(baseline_mu), float(baseline_sigma), float(google_high))
    assert challenger.action == GOOGLE_CHALLENGER_FORECAST_ACTION

    row = _case_row(
        baseline_mu=baseline_mu,
        baseline_sigma=baseline_sigma,
        google_challenger_mu=challenger.mu,
        google_challenger_sigma=challenger.sigma,
        google_challenger_action=challenger.action,
        google_challenger_policy_version=challenger.policy_version,
    )
    result = load_research_cases([row])
    assert len(result.cases) == 1
    assert result.cases[0].google_evidence == GoogleChallengerEvidence(
        mu=challenger.mu, sigma=challenger.sigma, action=challenger.action
    )


def test_google_runtime_challenger_constants_match_forecaster() -> None:
    """Parity lock for the constants ``research_walkforward.py`` duplicates
    from ``forecaster/google_runtime_blend.py`` (see that module's own
    docstring note on why it is a duplicate, not an import) -- same
    convention ``test_cities_parity.py`` already established for
    ``cities.py``."""

    import sys

    forecaster_root = str(Path(__file__).resolve().parents[2] / "forecaster")
    sys.path.insert(0, forecaster_root)
    try:
        import google_runtime_blend
    finally:
        sys.path.remove(forecaster_root)

    assert GOOGLE_CHALLENGER_POLICY_VERSION == google_runtime_blend.GOOGLE_CHALLENGER_POLICY_VERSION
    assert GOOGLE_CHALLENGER_FORECAST_ACTION == google_runtime_blend.GOOGLE_CHALLENGER_FORECAST_ACTION
    assert GOOGLE_CHALLENGER_BLOCK_ACTION == google_runtime_blend.GOOGLE_CHALLENGER_BLOCK_ACTION
    assert GOOGLE_RUNTIME_CANDIDATE_KEY == google_runtime_blend.GOOGLE_CHALLENGER_POLICY_VERSION


# ---------------------------------------------------------------------------
# Task 4: the loader attaches each case's own scan-time market snapshot, so
# an execution replay can price/size/gate a candidate's decision against the
# exact quote observed at decision time (research_replay.py) instead of a
# synthetic mid-price.
# ---------------------------------------------------------------------------


def test_load_research_cases_attaches_the_market_snapshot_from_its_own_context() -> None:
    row = _case_row()
    result = load_research_cases([row])

    assert len(result.cases) == 1
    assert result.cases[0].market_snapshot == _market_payload()


def test_research_case_market_snapshot_defaults_to_none_for_direct_construction() -> None:
    """Every Task 2/3 direct-construction call site that predates Task 4
    (this file's own earlier tests, test_research_candidates.py,
    test_research_scoring.py) omits ``market_snapshot`` entirely -- it must
    keep constructing a valid case with no market evidence attached, never
    a required argument or a fabricated default quote."""

    case = ResearchCase(
        station_id="KSFO",
        target_date=date(2026, 6, 20),
        decision_at=datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc),
        settled_at=datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc),
        lead_days=1,
        source_context_hash="direct-construction-hash",
        baseline_mu=66.0,
        baseline_sigma=3.0,
        actual_high_f=67.0,
    )
    assert case.market_snapshot is None


def test_load_research_cases_reports_no_market_snapshot_leakage_between_cases() -> None:
    """Two distinct scans (different tickers/quotes) must each carry only
    their own market snapshot -- never another row's."""

    row_a = _case_row(
        target_date="2026-06-25", ticker="KXHIGHTSFO-A", yes_bid=0.10, yes_ask=0.12,
        decision_at="2026-06-25T15:00:00+00:00", settled_at="2026-06-26T04:00:00+00:00",
    )
    row_b = _case_row(
        target_date="2026-06-26", ticker="KXHIGHTSFO-B", yes_bid=0.60, yes_ask=0.62,
        decision_at="2026-06-26T15:00:00+00:00", settled_at="2026-06-27T04:00:00+00:00",
    )
    result = load_research_cases([row_a, row_b])

    assert len(result.cases) == 2
    by_target_date = {case.target_date.isoformat(): case for case in result.cases}
    assert set(by_target_date["2026-06-25"].market_snapshot) == {"KXHIGHTSFO-A"}
    assert set(by_target_date["2026-06-26"].market_snapshot) == {"KXHIGHTSFO-B"}
