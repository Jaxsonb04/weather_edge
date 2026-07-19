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
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from sfo_kalshi_quant.cities import city_for_station
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import GoogleChallengerSnapshot
from sfo_kalshi_quant.research_candidates import (
    GAUSSIAN_PIT_CANDIDATE_KEY,
    GAUSSIAN_PIT_CANDIDATE_VERSION,
    GAUSSIAN_PIT_HYPOTHESIS_FAMILY,
    FoldCandidateEvidence,
)
from sfo_kalshi_quant.research_promotion import (
    PREDICTED_EDGE_SCOPE_YES_SIDE_TAKER,
    REASON_INSUFFICIENT_DAYS,
    ChallengerDeclaration,
)
from sfo_kalshi_quant.research_replay import FoldReplayEvidence
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
        rows = historical_rows_from_paper_store(conn, settlements=settlements)

    assert len(rows) == 1
    row = rows[0]
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
        rows = historical_rows_from_paper_store(conn, settlements=settlements)

    assert rows == []


def test_historical_rows_from_paper_store_skips_an_unsettled_scan(store: PaperStore) -> None:
    with store.connect() as conn:
        _insert_scan_and_decision(
            conn, target_date=FAR_FUTURE_DATE_1, created_at=f"{FAR_FUTURE_DATE_1}T15:00:00+00:00"
        )
        rows = historical_rows_from_paper_store(conn, settlements={})

    assert rows == []


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
        rows = historical_rows_from_paper_store(conn, settlements=settlements)

    assert len(rows) == 3

    declaration = declare_challenger(store, **_declaration_kwargs(experiment_id="exp-1", candidate_version="v1"))

    first_run = run_research_evaluation(store, declaration=declaration, historical_rows=rows)

    # Structural invariants (E3) regardless of the specific verdict.
    assert first_run.decision.live_activation_allowed is False
    assert first_run.decision.experiment_id == "exp-1"
    # Fewer than 30 station-day folds -- always blocked on day count.
    assert REASON_INSUFFICIENT_DAYS in first_run.decision.block_reasons
    assert first_run.persisted_fold_count >= 1

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
