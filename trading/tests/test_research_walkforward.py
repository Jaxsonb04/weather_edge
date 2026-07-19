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
from pathlib import Path

import pytest

from sfo_kalshi_quant.db import PaperStore, source_context_hash

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
        "trg_research_evidence_immutable_update",
        "trg_research_evidence_immutable_delete",
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
    store = PaperStore(tmp_path / "paper.db")
    _declare(store)
    with pytest.raises(sqlite3.IntegrityError, match="declared before"):
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
