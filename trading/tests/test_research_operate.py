"""Task 7: publish and operate the evidence loop (E1-E3 + end-to-end).

Covers the review-blocking conditions from the Task 6 final review:

- E1: prior family attempts are loaded ONLY from the immutable
  research_experiments/research_evidence tables, never accepted as a
  caller argument; a caller-passed declaration that disagrees with its
  stored row is rejected loudly.
- E2: all four tolerances plus predicted_edge_scope are persisted in
  parameter_json AT DECLARATION TIME; ChallengerDeclaration is
  reconstructed ONLY from the stored row at evaluation time; an absurd
  declared tolerance (e.g. 1e9) is rejected at declaration.
- E3: PromotionDecision is never constructed with live_activation_allowed
  set anywhere in research_operate.py (grep/AST-testable).

Plus an end-to-end loop on a constructed DB (real scan_context_snapshots/
decision_snapshots/google_challenger_snapshots rows via raw INSERT --
production has no caller of this pipeline yet to reuse, so this module's
own historical_rows_from_paper_store is the thing under test here too).
"""

from __future__ import annotations

import ast
import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sfo_kalshi_quant.cities import city_for_station
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import GoogleChallengerSnapshot
from sfo_kalshi_quant.research_bootstrap import DEFAULT_BOOTSTRAP_DRAWS, DEFAULT_BOOTSTRAP_SEED
from sfo_kalshi_quant.research_candidates import (
    GAUSSIAN_PIT_CANDIDATE_KEY,
    GAUSSIAN_PIT_CANDIDATE_VERSION,
    GAUSSIAN_PIT_HYPOTHESIS_FAMILY,
    FoldCandidateEvidence,
)
from sfo_kalshi_quant.research_policy import TARGET_POLICY
from sfo_kalshi_quant.research_promotion import (
    PREDICTED_EDGE_SCOPE_YES_SIDE_TAKER,
    REASON_INSUFFICIENT_DAYS,
    ChallengerDeclaration,
)
from sfo_kalshi_quant.research_replay import FoldReplayEvidence
from sfo_kalshi_quant.research_significance import one_sided_bootstrap_p_value
from sfo_kalshi_quant.research_walkforward import _expected_lead_days
import sfo_kalshi_quant.research_operate as research_operate
from sfo_kalshi_quant.research_operate import (
    MAX_SANE_TOLERANCE,
    DeclarationConflictError,
    _expected_lead_days_pacific,
    declare_challenger,
    historical_rows_from_paper_store,
    load_declared_challenger,
    load_prior_family_attempts,
    persist_fold_evidence,
    run_research_evaluation,
)

STATION = "KSFO"
HYPOTHESIS_FAMILY = "gaussian-pit-station-lead"
CANDIDATE_KEY = GAUSSIAN_PIT_CANDIDATE_KEY

# Every declared_at in these tests is the REAL wall clock (record_research_
# experiment's created_at is not injectable), so every target_date used
# anywhere below must be safely in the future relative to "now" -- far
# enough that no plausible test-run date ever collides. Mirrors the
# FAR_FUTURE_DATE convention test_research_walkforward.py already
# established for the exact same reason.
FAR_FUTURE_DATE_1 = "2099-01-02"
FAR_FUTURE_DATE_2 = "2099-01-03"
FAR_FUTURE_DATE_3 = "2099-01-04"


@pytest.fixture()
def store(tmp_path: Path) -> PaperStore:
    return PaperStore(tmp_path / "paper.db")


def _declaration_kwargs(**overrides) -> dict[str, object]:
    defaults = dict(
        experiment_id="exp-1",
        hypothesis_family=HYPOTHESIS_FAMILY,
        candidate_key=CANDIDATE_KEY,
        candidate_version="v1",
        evidence_role="confirmatory",
        predicted_edge_scope=PREDICTED_EDGE_SCOPE_YES_SIDE_TAKER,
        max_drawdown_tolerance_pct=0.10,
        crps_regression_tolerance=0.5,
        brier_regression_tolerance=0.5,
        calibration_gap_regression_tolerance=0.3,
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# E2: declaration-time persistence + reconstruction + tolerance bound.
# ---------------------------------------------------------------------------


def test_declare_challenger_returns_a_matching_declaration(store: PaperStore) -> None:
    declaration = declare_challenger(store, **_declaration_kwargs())
    assert isinstance(declaration, ChallengerDeclaration)
    assert declaration.experiment_id == "exp-1"
    assert declaration.hypothesis_family == HYPOTHESIS_FAMILY
    assert declaration.candidate_key == CANDIDATE_KEY
    assert declaration.predicted_edge_scope == PREDICTED_EDGE_SCOPE_YES_SIDE_TAKER
    assert declaration.max_drawdown_tolerance_pct == 0.10


def test_declared_parameter_json_persists_all_five_declaration_fields(store: PaperStore) -> None:
    declare_challenger(store, **_declaration_kwargs())
    with store.connect() as conn:
        row = conn.execute(
            "SELECT parameter_json FROM research_experiments WHERE experiment_id = 'exp-1'"
        ).fetchone()
    stored = json.loads(row[0])
    assert stored["predicted_edge_scope"] == PREDICTED_EDGE_SCOPE_YES_SIDE_TAKER
    assert stored["max_drawdown_tolerance_pct"] == 0.10
    assert stored["crps_regression_tolerance"] == 0.5
    assert stored["brier_regression_tolerance"] == 0.5
    assert stored["calibration_gap_regression_tolerance"] == 0.3


def test_load_declared_challenger_reconstructs_only_from_stored_row(store: PaperStore) -> None:
    declared = declare_challenger(store, **_declaration_kwargs())
    reloaded = load_declared_challenger(
        store, hypothesis_family=HYPOTHESIS_FAMILY, candidate_key=CANDIDATE_KEY, candidate_version="v1"
    )
    assert reloaded == declared


def test_load_declared_challenger_raises_lookup_error_when_never_declared(store: PaperStore) -> None:
    with pytest.raises(LookupError):
        load_declared_challenger(
            store, hypothesis_family=HYPOTHESIS_FAMILY, candidate_key=CANDIDATE_KEY, candidate_version="v-never"
        )


def test_declare_challenger_is_idempotent_for_an_identical_repeat(store: PaperStore) -> None:
    first = declare_challenger(store, **_declaration_kwargs())
    second = declare_challenger(store, **_declaration_kwargs())
    assert first == second
    with store.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM research_experiments WHERE experiment_id = 'exp-1'"
        ).fetchone()[0]
    assert count == 1


def test_declare_challenger_rejects_a_disagreeing_repeat_declaration(store: PaperStore) -> None:
    declare_challenger(store, **_declaration_kwargs())
    with pytest.raises(DeclarationConflictError):
        declare_challenger(store, **_declaration_kwargs(max_drawdown_tolerance_pct=0.20))


def test_declare_challenger_rejects_a_repeat_under_a_different_experiment_id(store: PaperStore) -> None:
    declare_challenger(store, **_declaration_kwargs())
    with pytest.raises(DeclarationConflictError):
        declare_challenger(store, **_declaration_kwargs(experiment_id="exp-2"))


@pytest.mark.parametrize(
    "field",
    [
        "max_drawdown_tolerance_pct",
        "crps_regression_tolerance",
        "brier_regression_tolerance",
        "calibration_gap_regression_tolerance",
    ],
)
def test_declare_challenger_rejects_an_absurd_tolerance(store: PaperStore, field: str) -> None:
    with pytest.raises(ValueError):
        declare_challenger(store, **_declaration_kwargs(**{field: 1e9}))


def test_max_sane_tolerance_boundary_is_inclusive(store: PaperStore) -> None:
    # Exactly at the ceiling is still accepted; only strictly above it is not.
    declare_challenger(store, **_declaration_kwargs(max_drawdown_tolerance_pct=MAX_SANE_TOLERANCE))


def test_declare_challenger_still_rejects_negative_tolerance(store: PaperStore) -> None:
    with pytest.raises(ValueError):
        declare_challenger(store, **_declaration_kwargs(crps_regression_tolerance=-0.01))


def test_declaration_from_stored_row_fails_closed_on_a_foreign_declaration_shape(
    store: PaperStore,
) -> None:
    # A row declared through some OTHER caller (e.g. research_candidates.py's
    # own declared_research_candidates() convention) carries a totally
    # different parameter_json shape and must never be silently
    # misinterpreted as a ChallengerDeclaration.
    store.record_research_experiment(
        experiment_id="foreign-exp",
        hypothesis_family=GAUSSIAN_PIT_HYPOTHESIS_FAMILY,
        candidate_key=GAUSSIAN_PIT_CANDIDATE_KEY,
        candidate_version=GAUSSIAN_PIT_CANDIDATE_VERSION,
        parameter_json={"shrinkage_k": 40.0},
        evidence_role="confirmatory",
    )
    with pytest.raises(ValueError):
        load_declared_challenger(
            store,
            hypothesis_family=GAUSSIAN_PIT_HYPOTHESIS_FAMILY,
            candidate_key=GAUSSIAN_PIT_CANDIDATE_KEY,
            candidate_version=GAUSSIAN_PIT_CANDIDATE_VERSION,
        )


# ---------------------------------------------------------------------------
# E1: prior family attempts sourced ONLY from stored tables.
# ---------------------------------------------------------------------------


def _candidate_row(*, fold_id: str, station_id: str, target_date_value: date, pnl_delta: float) -> tuple:
    stamp = {
        "execution_model_version": "exec-v4-test",
        "reference_equity": 1000.0,
        "max_position_risk_pct": 0.03,
        "policy_fingerprint": "fp-test",
        "order_ttl_minutes": 15,
        "side_scope": "yes_only",
        "fill_scope": "taker_only_no_tape",
    }
    source_hash = f"hash-{fold_id}"
    baseline_case = {
        "candidate_key": "active-identity-v1", "available": True, "skip_reason": "",
        "realized_pnl": 5.0, "filled_count": 1, "stamp": stamp, "tickers": [],
    }
    challenger_case = {
        "candidate_key": CANDIDATE_KEY, "available": True, "skip_reason": "",
        "realized_pnl": 5.0 + pnl_delta, "filled_count": 1, "stamp": stamp, "tickers": [],
    }
    candidate_row = FoldCandidateEvidence(
        fold_id=fold_id, station_id=station_id, target_date=target_date_value,
        evaluated_at=datetime(target_date_value.year, target_date_value.month, target_date_value.day, 23, tzinfo=timezone.utc),
        challenger_candidate_key=CANDIDATE_KEY,
        baseline={"cases": {source_hash: {**baseline_case, "crps": 1.0, "bracket_brier": 0.1, "pit": 0.5}}},
        challenger={"cases": {source_hash: {**challenger_case, "crps": 0.9, "bracket_brier": 0.09, "pit": 0.5}}},
    )
    replay_row = FoldReplayEvidence(
        fold_id=fold_id, station_id=station_id, target_date=target_date_value,
        challenger_candidate_key=CANDIDATE_KEY,
        baseline={"cases": {source_hash: baseline_case}, "stamp": stamp},
        challenger={"cases": {source_hash: challenger_case}, "stamp": stamp},
        promotion_eligible=True,
    )
    return candidate_row, replay_row


def test_load_prior_family_attempts_is_empty_with_no_other_declared_versions(store: PaperStore) -> None:
    declare_challenger(store, **_declaration_kwargs(candidate_version="v1"))
    attempts = load_prior_family_attempts(
        store, hypothesis_family=HYPOTHESIS_FAMILY, exclude_candidate_version="v1"
    )
    assert attempts == ()


def test_load_prior_family_attempts_excludes_the_current_candidate_version(store: PaperStore) -> None:
    declare_challenger(store, **_declaration_kwargs(experiment_id="exp-1", candidate_version="v1"))
    candidate_row, replay_row = _candidate_row(
        fold_id="KSFO:2099-01-02", station_id=STATION, target_date_value=date(2099, 1, 2), pnl_delta=3.0
    )
    persist_fold_evidence(store, experiment_id="exp-1", candidate_row=candidate_row, replay_row=replay_row)

    attempts = load_prior_family_attempts(
        store, hypothesis_family=HYPOTHESIS_FAMILY, exclude_candidate_version="v1"
    )
    assert attempts == ()


def test_load_prior_family_attempts_recomputes_p_value_from_persisted_evidence(store: PaperStore) -> None:
    declare_challenger(store, **_declaration_kwargs(experiment_id="exp-1", candidate_version="v1"))
    for day in range(2, 5):
        candidate_row, replay_row = _candidate_row(
            fold_id=f"KSFO:2099-01-0{day}", station_id=STATION,
            target_date_value=date(2099, 1, day), pnl_delta=3.0,
        )
        persist_fold_evidence(store, experiment_id="exp-1", candidate_row=candidate_row, replay_row=replay_row)

    attempts = load_prior_family_attempts(
        store, hypothesis_family=HYPOTHESIS_FAMILY, exclude_candidate_version="v2"
    )
    assert len(attempts) == 1
    assert attempts[0].hypothesis_family == HYPOTHESIS_FAMILY
    assert attempts[0].candidate_version == "v1"
    assert 0.0 <= attempts[0].p_value <= 1.0


def test_load_prior_family_attempts_skips_a_version_with_no_persisted_evidence(store: PaperStore) -> None:
    declare_challenger(store, **_declaration_kwargs(experiment_id="exp-1", candidate_version="v1"))
    # No persist_fold_evidence call for exp-1 at all.
    attempts = load_prior_family_attempts(
        store, hypothesis_family=HYPOTHESIS_FAMILY, exclude_candidate_version="v2"
    )
    assert attempts == ()


# ---------------------------------------------------------------------------
# persist_fold_evidence: mismatch guards + immutability/idempotency.
# ---------------------------------------------------------------------------


def test_persist_fold_evidence_rejects_mismatched_fold_ids(store: PaperStore) -> None:
    candidate_row, replay_row = _candidate_row(
        fold_id="KSFO:2099-01-02", station_id=STATION, target_date_value=date(2099, 1, 2), pnl_delta=1.0
    )
    other_candidate_row, _ = _candidate_row(
        fold_id="KSFO:2099-01-03", station_id=STATION, target_date_value=date(2099, 1, 3), pnl_delta=1.0
    )
    with pytest.raises(ValueError):
        persist_fold_evidence(
            store, experiment_id="exp-1", candidate_row=other_candidate_row, replay_row=replay_row
        )


def test_persist_fold_evidence_second_call_for_the_same_fold_raises(store: PaperStore) -> None:
    declare_challenger(store, **_declaration_kwargs(experiment_id="exp-1", candidate_version="v1"))
    candidate_row, replay_row = _candidate_row(
        fold_id="KSFO:2099-01-02", station_id=STATION, target_date_value=date(2099, 1, 2), pnl_delta=1.0
    )
    persist_fold_evidence(store, experiment_id="exp-1", candidate_row=candidate_row, replay_row=replay_row)
    with pytest.raises(sqlite3.IntegrityError):
        persist_fold_evidence(store, experiment_id="exp-1", candidate_row=candidate_row, replay_row=replay_row)


# ---------------------------------------------------------------------------
# _expected_lead_days_pacific parity with research_walkforward's own
# private formula (duplicate-plus-parity-test convention).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "decision_at,target",
    [
        (datetime(2099, 1, 1, 15, 0, tzinfo=timezone.utc), date(2099, 1, 2)),
        (datetime(2099, 1, 1, 23, 59, tzinfo=timezone.utc), date(2099, 1, 3)),
        (datetime(2099, 6, 15, 4, 0, tzinfo=timezone.utc), date(2099, 6, 15)),
    ],
)
def test_expected_lead_days_pacific_matches_research_walkforward(decision_at, target) -> None:
    assert _expected_lead_days_pacific(decision_at, target) == _expected_lead_days(decision_at, target)


# ---------------------------------------------------------------------------
# E3: no PromotionDecision(...) call in research_operate.py ever passes
# live_activation_allowed, and this module never imports live-trading
# surfaces -- AST-based, mirroring research_promotion.py's own parity test.
# ---------------------------------------------------------------------------


def test_research_operate_never_sets_live_activation_allowed() -> None:
    # AST-based (not substring) on purpose: this module's own docstring
    # names live_activation_allowed in PROSE to document this very
    # invariant -- a raw substring scan would false-positive on that
    # explanation, exactly as research_promotion.py's own parity test
    # documents for the same reason. What actually matters is that no
    # *call* anywhere in this file passes it as a keyword argument.
    source = Path(research_operate.__file__).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                assert keyword.arg != "live_activation_allowed"


_FORBIDDEN_LIVE_MODULES = {"config", "live_execution"}


def test_research_operate_never_imports_live_config_surfaces() -> None:
    source = Path(research_operate.__file__).read_text()
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
    assert not collision, f"research_operate.py must never import {collision}"


# ---------------------------------------------------------------------------
# historical_rows_from_paper_store: the DB-native row loader.
# ---------------------------------------------------------------------------


def _forecast_payload(predicted_high_f: float) -> str:
    return json.dumps({"target_date": FAR_FUTURE_DATE_1, "predicted_high_f": predicted_high_f})


def _market_payload() -> str:
    return json.dumps(
        {
            "KXHIGHTSFO-TEST-B65.5": {
                "ticker": "KXHIGHTSFO-TEST-B65.5",
                "yes_bid": 0.24,
                "yes_ask": 0.26,
                "floor_strike": 65.0,
                "cap_strike": 66.0,
            }
        }
    )


def _insert_scan_and_decision(
    conn: sqlite3.Connection,
    *,
    target_date: str,
    station_id: str = STATION,
    created_at: str,
    baseline_mu: float | None = 66.0,
    baseline_sigma: float | None = 3.0,
    with_decision: bool = True,
) -> int:
    cursor = conn.execute(
        "INSERT INTO scan_context_snapshots (created_at, target_date, station_id, "
        "forecast_json, intraday_json, market_json, prediction_features_json, schema_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (
            created_at,
            target_date,
            station_id,
            _forecast_payload(baseline_mu or 66.0),
            json.dumps({}),
            _market_payload(),
            json.dumps({"predicted_high_f": baseline_mu or 66.0}),
        ),
    )
    scan_context_id = int(cursor.lastrowid)
    if with_decision:
        conn.execute(
            "INSERT INTO decision_snapshots (scan_context_id, created_at, target_date, "
            "market_ticker, label, action, side, approved, probability, probability_lcb, "
            "yes_bid, yes_ask, spread, fee_per_contract, cost_per_contract, edge, edge_lcb, "
            "kelly_fraction, recommended_contracts, recommended_spend, expected_profit, "
            "trade_quality_score, reasons_json, forecast_predicted_high_f, "
            "forecast_source_spread_f) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                scan_context_id, created_at, target_date, "KXHIGHTSFO-TEST-B65.5", "65.5", "buy_yes", "YES",
                0.5, 0.5, 0.24, 0.26, 0.02, 0.01, 0.25, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "[]",
                baseline_mu, baseline_sigma,
            ),
        )
    conn.commit()
    return scan_context_id


def test_historical_rows_from_paper_store_builds_a_loadable_row(store: PaperStore) -> None:
    with store.connect() as conn:
        _insert_scan_and_decision(
            conn, target_date=FAR_FUTURE_DATE_1, created_at=f"{FAR_FUTURE_DATE_1}T15:00:00+00:00"
        )
        city = city_for_station(STATION)
        settlements = {(city.series_ticker, FAR_FUTURE_DATE_1): 67.0}
        load_result = historical_rows_from_paper_store(conn, settlements=settlements)

    assert len(load_result.rows) == 1
    assert load_result.skips == ()
    row = load_result.rows[0]
    assert row["station_id"] == STATION
    assert row["target_date"] == FAR_FUTURE_DATE_1
    assert row["baseline_mu"] == 66.0
    assert row["baseline_sigma"] == 3.0
    assert row["actual_high_f"] == 67.0
    assert row["decision_at"].startswith(FAR_FUTURE_DATE_1)
    assert row["lead_days"] >= 0


def test_historical_rows_from_paper_store_skips_a_scan_with_no_decision_link(store: PaperStore) -> None:
    with store.connect() as conn:
        _insert_scan_and_decision(
            conn, target_date=FAR_FUTURE_DATE_1, created_at=f"{FAR_FUTURE_DATE_1}T15:00:00+00:00",
            with_decision=False,
        )
        city = city_for_station(STATION)
        settlements = {(city.series_ticker, FAR_FUTURE_DATE_1): 67.0}
        load_result = historical_rows_from_paper_store(conn, settlements=settlements)

    # M-1: silently dropped no longer -- a distinct, reasoned skip.
    assert load_result.rows == ()
    assert len(load_result.skips) == 1
    assert load_result.skips[0].reason == "missing_station_target_date_or_baseline"
    assert load_result.skips[0].station_id == STATION


def test_historical_rows_from_paper_store_skips_an_unsettled_scan(store: PaperStore) -> None:
    with store.connect() as conn:
        _insert_scan_and_decision(
            conn, target_date=FAR_FUTURE_DATE_1, created_at=f"{FAR_FUTURE_DATE_1}T15:00:00+00:00"
        )
        load_result = historical_rows_from_paper_store(conn, settlements={})

    assert load_result.rows == ()
    assert len(load_result.skips) == 1
    assert load_result.skips[0].reason == "no_settlement_match"
    assert load_result.skips[0].target_date == FAR_FUTURE_DATE_1


def test_historical_rows_from_paper_store_restores_the_callers_row_factory(store: PaperStore) -> None:
    with store.connect() as conn:
        assert conn.row_factory is None
        _insert_scan_and_decision(
            conn, target_date=FAR_FUTURE_DATE_1, created_at=f"{FAR_FUTURE_DATE_1}T15:00:00+00:00"
        )
        historical_rows_from_paper_store(conn, settlements={})
        # Cheap item: this function must never leave a caller-supplied
        # connection's own row_factory mutated for whatever the caller
        # does with it afterward.
        assert conn.row_factory is None


# ---------------------------------------------------------------------------
# End-to-end: run_research_evaluation over a constructed DB.
# ---------------------------------------------------------------------------


def test_end_to_end_evaluation_loop_on_a_constructed_db(store: PaperStore) -> None:
    city = city_for_station(STATION)
    dates = [FAR_FUTURE_DATE_1, FAR_FUTURE_DATE_2, FAR_FUTURE_DATE_3]
    settlements: dict[tuple[str, str], float] = {}
    with store.connect() as conn:
        for offset, target_date in enumerate(dates):
            _insert_scan_and_decision(
                conn,
                target_date=target_date,
                created_at=f"{target_date}T15:00:00+00:00",
                baseline_mu=66.0 + offset,
                baseline_sigma=3.0,
            )
            settlements[(city.series_ticker, target_date)] = 67.0 + offset
        load_result = historical_rows_from_paper_store(conn, settlements=settlements)

    assert len(load_result.rows) == 3
    assert load_result.skips == ()
    rows = load_result.rows

    declaration = declare_challenger(store, **_declaration_kwargs(experiment_id="exp-1", candidate_version="v1"))

    first_run = run_research_evaluation(
        store, declaration=declaration, historical_rows=rows, historical_row_skips=load_result.skips
    )

    # Structural invariants (E3) regardless of the specific verdict.
    assert first_run.decision.live_activation_allowed is False
    assert first_run.decision.experiment_id == "exp-1"
    # Fewer than 30 station-day folds -- always blocked on day count.
    assert REASON_INSUFFICIENT_DAYS in first_run.decision.block_reasons
    assert first_run.persisted_fold_count >= 1
    # M-1: no historical-row-loading drops in this scenario.
    assert first_run.historical_row_skip_count == 0
    # CRITICAL-1: every target_date here is far in the future, so nothing
    # is pre-declaration.
    assert first_run.pre_declaration_fold_count == 0
    assert first_run.pre_declaration_fold_ids == ()
    assert first_run.stale_evidence_fold_count == 0

    with store.connect() as conn:
        persisted_count = conn.execute(
            "SELECT COUNT(*) FROM research_evidence WHERE experiment_id = 'exp-1'"
        ).fetchone()[0]
    assert persisted_count == first_run.persisted_fold_count
    assert persisted_count >= 1

    # A second run over the SAME rows must not crash and must not
    # re-persist already-recorded fold evidence (idempotent operation).
    second_run = run_research_evaluation(store, declaration=declaration, historical_rows=rows)
    assert second_run.persisted_fold_count == 0
    assert second_run.decision.live_activation_allowed is False

    with store.connect() as conn:
        persisted_count_after = conn.execute(
            "SELECT COUNT(*) FROM research_evidence WHERE experiment_id = 'exp-1'"
        ).fetchone()[0]
    assert persisted_count_after == persisted_count

    # A second candidate_version in the SAME family sees the first one as
    # prior family history, loaded only from the stored tables (E1).
    declaration_v2 = declare_challenger(
        store, **_declaration_kwargs(experiment_id="exp-2", candidate_version="v2")
    )
    second_candidate_run = run_research_evaluation(
        store, declaration=declaration_v2, historical_rows=rows
    )
    assert any(
        attempt.candidate_version == "v1" for attempt in second_candidate_run.prior_family_attempts
    )


# ---------------------------------------------------------------------------
# CRITICAL-1: pre-declaration folds are excluded from the verdict, not
# merely skipped at persistence -- so the verdict's own fold set is always
# exactly the persistable set.
# ---------------------------------------------------------------------------


def test_pre_declaration_folds_are_excluded_from_the_verdict_not_just_persistence(
    store: PaperStore,
) -> None:
    # The reviewer's own construction: every historical row's target date
    # is strictly in the PAST (well before "now", the real wall-clock
    # declared_at), and the challenger is declared TODAY -- so every one
    # of these folds' own target day is at or before the Pacific
    # declaration day. Under the bug, all >=30 folds still fed
    # evaluate_promotion's verdict even though every one of them failed to
    # persist (zero enforced evidence).
    training_station = "KMIA"
    training_date = "2024-06-01"
    test_station = "KSFO"
    test_dates = [
        (date(2025, 1, 1) + timedelta(days=offset)).isoformat() for offset in range(30)
    ]

    training_city = city_for_station(training_station)
    test_city = city_for_station(test_station)
    settlements: dict[tuple[str, str], float] = {
        (training_city.series_ticker, training_date): 70.0,
    }
    with store.connect() as conn:
        _insert_scan_and_decision(
            conn, target_date=training_date, station_id=training_station,
            created_at=f"{training_date}T15:00:00+00:00",
        )
        for target_date in test_dates:
            _insert_scan_and_decision(
                conn, target_date=target_date, station_id=test_station,
                created_at=f"{target_date}T15:00:00+00:00",
            )
            settlements[(test_city.series_ticker, target_date)] = 67.0
        load_result = historical_rows_from_paper_store(conn, settlements=settlements)

    assert load_result.skips == ()
    assert len(load_result.rows) == 31

    declaration = declare_challenger(store, **_declaration_kwargs(experiment_id="exp-1", candidate_version="v1"))
    run = run_research_evaluation(store, declaration=declaration, historical_rows=load_result.rows)

    # Zero enforced (persisted) evidence -- every fold predates the
    # declaration, and pre-declaration exclusion (not a persistence-time
    # ValueError catch) is now the reason, so there is nothing to log
    # there either.
    assert run.persisted_fold_count == 0
    assert run.skipped_fold_persist_reasons == ()

    # CRITICAL-1: the verdict must show ZERO confirmatory days and block
    # on insufficient days -- never silently pass 30 pre-declaration folds
    # through as if they were real confirmatory evidence.
    assert run.decision.independent_confirmatory_days == 0
    assert REASON_INSUFFICIENT_DAYS in run.decision.block_reasons

    # The exclusion itself is populated, distinct diagnostic evidence.
    expected_ksfo_fold_ids = {f"{test_station}:{target_date}" for target_date in test_dates}
    assert expected_ksfo_fold_ids <= set(run.pre_declaration_fold_ids)
    assert run.pre_declaration_fold_count == len(run.pre_declaration_fold_ids)
    assert run.pre_declaration_fold_count >= 30


# ---------------------------------------------------------------------------
# CRITICAL-2: a reloaded family p-value must exactly match the p-value
# computed at verdict time for a multi-station family -- both the reload
# ORDER (date-major, not station-major) and the per-fold summation
# algorithm (math.fsum, not a plain running total) must match.
# ---------------------------------------------------------------------------


def _multi_case_rows(
    *, fold_id: str, station_id: str, target_date_value: date, case_pnls: list[tuple[float, float]]
) -> tuple[FoldCandidateEvidence, FoldReplayEvidence]:
    """Like ``_candidate_row``, but with a caller-controlled number of
    cases and exact per-case (baseline_pnl, challenger_pnl) values -- lets
    a test pin the exact figure ``_fold_roi_delta``/
    ``research_bootstrap.fold_paired_aggregates`` must both compute."""

    stamp = {
        "execution_model_version": "exec-v4-test",
        "reference_equity": 1000.0,
        "max_position_risk_pct": 0.03,
        "policy_fingerprint": "fp-test",
        "order_ttl_minutes": 15,
        "side_scope": "yes_only",
        "fill_scope": "taker_only_no_tape",
    }
    baseline_cases: dict[str, dict] = {}
    challenger_cases: dict[str, dict] = {}
    baseline_score_cases: dict[str, dict] = {}
    challenger_score_cases: dict[str, dict] = {}
    for index, (baseline_pnl, challenger_pnl) in enumerate(case_pnls):
        source_hash = f"hash-{fold_id}-{index}"
        baseline_case = {
            "candidate_key": "active-identity-v1", "available": True, "skip_reason": "",
            "realized_pnl": baseline_pnl, "filled_count": 1, "stamp": stamp, "tickers": [],
        }
        challenger_case = {
            "candidate_key": CANDIDATE_KEY, "available": True, "skip_reason": "",
            "realized_pnl": challenger_pnl, "filled_count": 1, "stamp": stamp, "tickers": [],
        }
        baseline_cases[source_hash] = baseline_case
        challenger_cases[source_hash] = challenger_case
        baseline_score_cases[source_hash] = {**baseline_case, "crps": 1.0, "bracket_brier": 0.1, "pit": 0.5}
        challenger_score_cases[source_hash] = {**challenger_case, "crps": 0.9, "bracket_brier": 0.09, "pit": 0.5}

    candidate_row = FoldCandidateEvidence(
        fold_id=fold_id, station_id=station_id, target_date=target_date_value,
        evaluated_at=datetime(
            target_date_value.year, target_date_value.month, target_date_value.day, 23, tzinfo=timezone.utc
        ),
        challenger_candidate_key=CANDIDATE_KEY,
        baseline={"cases": baseline_score_cases},
        challenger={"cases": challenger_score_cases},
    )
    replay_row = FoldReplayEvidence(
        fold_id=fold_id, station_id=station_id, target_date=target_date_value,
        challenger_candidate_key=CANDIDATE_KEY,
        baseline={"cases": baseline_cases, "stamp": stamp},
        challenger={"cases": challenger_cases, "stamp": stamp},
        promotion_eligible=True,
    )
    return candidate_row, replay_row


def test_load_prior_family_attempts_round_trips_the_verdict_time_p_value_for_a_multi_station_family(
    store: PaperStore,
) -> None:
    declare_challenger(store, **_declaration_kwargs(experiment_id="exp-1", candidate_version="v1"))

    dates = [date(2099, 1, 5), date(2099, 1, 6)]
    # >= 4 stations x 2 days, each fold with its own per-case pnl values.
    # One fold gets ten 0.1 challenger cases against ten 0.0 baseline
    # cases -- plain left-to-right += leaves this fold's own delta short
    # of math.fsum's exact 1.0 by one ULP, and the fold-level VALUES are
    # varied and mixed-sign enough that reordering the fold sequence
    # (station-major vs date-major) changes the seeded Monte Carlo
    # bootstrap's outcome.
    case_pnls_by_combo: dict[tuple[str, date], list[tuple[float, float]]] = {
        ("KATL", dates[0]): [(0.0, 0.1)] * 10,
        ("KATL", dates[1]): [(0.0, -4.0)],
        ("KBOS", dates[0]): [(0.0, 6.5)],
        ("KBOS", dates[1]): [(0.0, -2.25)],
        ("KLAX", dates[0]): [(0.0, 1.75)],
        ("KLAX", dates[1]): [(0.0, -5.5)],
        ("KSEA", dates[0]): [(0.0, 3.25)],
        ("KSEA", dates[1]): [(0.0, -1.0)],
    }

    reference_equity = TARGET_POLICY.reference_equity
    expected_fold_deltas: list[tuple[date, str, str, float]] = []
    for (station_id, target_date_value), case_pnls in case_pnls_by_combo.items():
        fold_id = f"{station_id}:{target_date_value.isoformat()}"
        candidate_row, replay_row = _multi_case_rows(
            fold_id=fold_id, station_id=station_id, target_date_value=target_date_value, case_pnls=case_pnls
        )
        persist_fold_evidence(store, experiment_id="exp-1", candidate_row=candidate_row, replay_row=replay_row)

        baseline_total = math.fsum(pair[0] for pair in case_pnls)
        challenger_total = math.fsum(pair[1] for pair in case_pnls)
        roi_delta = (challenger_total - baseline_total) / reference_equity
        expected_fold_deltas.append((target_date_value, station_id, fold_id, roi_delta))

    # research_bootstrap.fold_paired_aggregates's own final sort --
    # (target_date, station_id, fold_id), i.e. date-major -- is the ONLY
    # ordering "verdict time" ever feeds the bootstrap.
    expected_fold_deltas.sort(key=lambda item: (item[0], item[1], item[2]))
    expected_deltas = [item[3] for item in expected_fold_deltas]
    expected_p = one_sided_bootstrap_p_value(
        expected_deltas, seed=DEFAULT_BOOTSTRAP_SEED, draws=DEFAULT_BOOTSTRAP_DRAWS
    )
    assert expected_p is not None

    attempts = load_prior_family_attempts(
        store, hypothesis_family=HYPOTHESIS_FAMILY, exclude_candidate_version="v-not-declared"
    )
    assert len(attempts) == 1
    assert attempts[0].candidate_version == "v1"
    # EXACT equality -- not approximate -- proving both the reload ORDER
    # and the per-fold summation algorithm match verdict time bit-for-bit.
    assert attempts[0].p_value == expected_p


def test_fold_roi_delta_sums_with_fsum_matching_research_bootstraps_own_precision() -> None:
    # Classic float non-associativity: sum([0.1] * 10) via plain += lands
    # on 0.9999999999999999, but math.fsum resolves it to exactly 1.0 --
    # research_bootstrap.fold_paired_aggregates already sums this way, so
    # _fold_roi_delta must match it bit-for-bit, not just approximately.
    baseline_payload = {
        "replay": {"cases": {f"h{i}": {"available": True, "realized_pnl": 0.0} for i in range(10)}}
    }
    challenger_payload = {
        "replay": {"cases": {f"h{i}": {"available": True, "realized_pnl": 0.1} for i in range(10)}}
    }
    delta = research_operate._fold_roi_delta(baseline_payload, challenger_payload, reference_equity=1.0)
    assert delta == 1.0
    assert delta == math.fsum([0.1] * 10) - math.fsum([0.0] * 10)


# ---------------------------------------------------------------------------
# HIGH-1: candidate_version must be unique within its hypothesis_family,
# regardless of candidate_key -- load_prior_family_attempts excludes by
# version alone, so a sibling candidate_key reusing a version would
# silently empty that version's own Holm-Bonferroni family history.
# ---------------------------------------------------------------------------


def test_declare_challenger_rejects_a_sibling_candidate_key_reusing_the_same_version(
    store: PaperStore,
) -> None:
    declare_challenger(
        store, **_declaration_kwargs(experiment_id="exp-a", candidate_key="candidate-a", candidate_version="v1")
    )
    with pytest.raises(DeclarationConflictError):
        declare_challenger(
            store,
            **_declaration_kwargs(experiment_id="exp-b", candidate_key="candidate-b", candidate_version="v1"),
        )


def test_declare_challenger_still_allows_a_genuinely_new_version_under_a_different_key(
    store: PaperStore,
) -> None:
    declare_challenger(
        store, **_declaration_kwargs(experiment_id="exp-a", candidate_key="candidate-a", candidate_version="v1")
    )
    declared = declare_challenger(
        store,
        **_declaration_kwargs(experiment_id="exp-b", candidate_key="candidate-b", candidate_version="v2"),
    )
    assert declared.candidate_version == "v2"
    assert declared.candidate_key == "candidate-b"


def test_declare_challenger_still_allows_a_new_version_under_the_same_key(store: PaperStore) -> None:
    # The NORMAL family-history use case: the SAME candidate_key evolving
    # to a new version must never be treated as a sibling conflict.
    declare_challenger(store, **_declaration_kwargs(experiment_id="exp-a", candidate_version="v1"))
    declared = declare_challenger(store, **_declaration_kwargs(experiment_id="exp-b", candidate_version="v2"))
    assert declared.candidate_version == "v2"


# ---------------------------------------------------------------------------
# M-2: an already-recorded, immutable fold whose freshly recomputed
# canonical payload no longer matches its stored row is surfaced, never
# silently ignored -- the stored row itself is never touched.
# ---------------------------------------------------------------------------


def test_run_research_evaluation_surfaces_stale_evidence_when_recompute_diverges(
    store: PaperStore,
) -> None:
    # A cross-station, far-earlier training row -- embargo only applies to
    # the SAME station, so this is leakage-safe training history for the
    # fold under test without itself being affected by the later "drift".
    training_station = "KMIA"
    training_date = "2098-01-01"
    training_city = city_for_station(training_station)
    city = city_for_station(STATION)
    with store.connect() as conn:
        _insert_scan_and_decision(
            conn, target_date=training_date, station_id=training_station,
            created_at=f"{training_date}T15:00:00+00:00",
        )
        _insert_scan_and_decision(
            conn, target_date=FAR_FUTURE_DATE_1, created_at=f"{FAR_FUTURE_DATE_1}T15:00:00+00:00",
            baseline_mu=66.0, baseline_sigma=3.0,
        )
        settlements = {
            (training_city.series_ticker, training_date): 70.0,
            (city.series_ticker, FAR_FUTURE_DATE_1): 67.0,
        }
        load_result = historical_rows_from_paper_store(conn, settlements=settlements)
    assert len(load_result.rows) == 2

    declaration = declare_challenger(store, **_declaration_kwargs(experiment_id="exp-1", candidate_version="v1"))
    first_run = run_research_evaluation(store, declaration=declaration, historical_rows=load_result.rows)
    assert first_run.persisted_fold_count >= 1
    assert first_run.stale_evidence_fold_count == 0

    # Simulate a later Google-snapshot backfill (or any other change)
    # altering what freshly scoring this SAME real-world observation would
    # now produce -- the frozen research_evidence row must never change,
    # but the divergence must be surfaced, not silently ignored.
    ksfo_row = next(row for row in load_result.rows if row["station_id"] == STATION)
    other_rows = [row for row in load_result.rows if row["station_id"] != STATION]
    drifted_rows = [*other_rows, dict(ksfo_row, baseline_mu=90.0)]
    second_run = run_research_evaluation(store, declaration=declaration, historical_rows=drifted_rows)
    assert second_run.persisted_fold_count == 0  # never re-persisted
    assert second_run.stale_evidence_fold_count >= 1
    assert second_run.stale_evidence_fold_ids != ()

    # The stored row itself is untouched (still exactly as many rows as
    # were originally persisted).
    with store.connect() as conn:
        stored_count = conn.execute("SELECT COUNT(*) FROM research_evidence").fetchone()[0]
    assert stored_count == first_run.persisted_fold_count


# ---------------------------------------------------------------------------
# Cheap item: a concurrent duplicate-run race on the underlying INSERT is
# a clean already-recorded skip, never an uncaught crash.
# ---------------------------------------------------------------------------


def test_run_research_evaluation_treats_a_concurrent_duplicate_insert_as_a_clean_skip(
    store: PaperStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    training_station = "KMIA"
    training_date = "2098-01-01"
    training_city = city_for_station(training_station)
    city = city_for_station(STATION)
    with store.connect() as conn:
        _insert_scan_and_decision(
            conn, target_date=training_date, station_id=training_station,
            created_at=f"{training_date}T15:00:00+00:00",
        )
        _insert_scan_and_decision(
            conn, target_date=FAR_FUTURE_DATE_1, created_at=f"{FAR_FUTURE_DATE_1}T15:00:00+00:00",
        )
        settlements = {
            (training_city.series_ticker, training_date): 70.0,
            (city.series_ticker, FAR_FUTURE_DATE_1): 67.0,
        }
        load_result = historical_rows_from_paper_store(conn, settlements=settlements)

    declaration = declare_challenger(store, **_declaration_kwargs(experiment_id="exp-1", candidate_version="v1"))
    first_run = run_research_evaluation(store, declaration=declaration, historical_rows=load_result.rows)
    assert first_run.persisted_fold_count >= 1

    # Force the already-recorded pre-check to always report False --
    # exactly simulating the race window between two concurrent
    # invocations both passing that check before either commits. The
    # underlying INSERT must then raise sqlite3.IntegrityError against the
    # immutable primary key, and that must never crash the whole
    # evaluation.
    monkeypatch.setattr(research_operate, "_fold_evidence_already_recorded", lambda *a, **k: False)
    second_run = run_research_evaluation(store, declaration=declaration, historical_rows=load_result.rows)
    assert second_run.persisted_fold_count == 0
    assert second_run.skipped_fold_persist_reasons == ()
