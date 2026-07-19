"""Task 7: CLI wiring for research-evaluate / research-propose-target.

End-to-end smoke tests through ``cli.main`` against a constructed paper DB
and a minimal weather.db (settlement truth) -- the same two databases a
real invocation reads. Focuses on the CLI's own authority split (plan
Task 7 Step 1): research-evaluate is read-only with respect to promotion
authority (never writes a target-paper proposal, but does durably record
evidence); research-propose-target writes a proposal artifact ONLY when
eligible.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sfo_kalshi_quant import cli
from sfo_kalshi_quant.cities import city_for_station
from sfo_kalshi_quant.db import PaperStore

STATION = "KSFO"
# Two distinct days, far enough apart to clear the default one-day
# station embargo at the fold boundary, so the second has leakage-safe
# training history from the first -- a single day (or two adjacent days)
# always folds to an UnavailableFold (no training pool at all, or an
# embargoed one), which would never persist any evidence.
FAR_FUTURE_DATE_ANCHOR = "2099-01-28"
FAR_FUTURE_DATE = "2099-02-01"


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


def _seed_paper_db(db_path: Path, *, target_dates: tuple[str, ...]) -> None:
    store = PaperStore(db_path)
    with store.connect() as conn:
        for target_date in target_dates:
            cursor = conn.execute(
                "INSERT INTO scan_context_snapshots (created_at, target_date, station_id, "
                "forecast_json, intraday_json, market_json, prediction_features_json, schema_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    f"{target_date}T15:00:00+00:00",
                    target_date,
                    STATION,
                    json.dumps({"target_date": target_date, "predicted_high_f": 66.0}),
                    json.dumps({}),
                    _market_payload(),
                    json.dumps({"predicted_high_f": 66.0}),
                ),
            )
            scan_context_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO decision_snapshots (scan_context_id, created_at, target_date, "
                "market_ticker, label, action, side, approved, probability, probability_lcb, "
                "yes_bid, yes_ask, spread, fee_per_contract, cost_per_contract, edge, edge_lcb, "
                "kelly_fraction, recommended_contracts, recommended_spend, expected_profit, "
                "trade_quality_score, reasons_json, forecast_predicted_high_f, "
                "forecast_source_spread_f) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scan_context_id, f"{target_date}T15:00:00+00:00", target_date,
                    "KXHIGHTSFO-TEST-B65.5", "65.5", "buy_yes", "YES",
                    0.5, 0.5, 0.24, 0.26, 0.02, 0.01, 0.25, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "[]",
                    66.0, 3.0,
                ),
            )
        conn.commit()


def _seed_weather_db(weather_db_path: Path, *, target_dates: tuple[str, ...]) -> None:
    with sqlite3.connect(weather_db_path) as conn:
        conn.execute(
            "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
            "max_temperature_f REAL, is_final INTEGER NOT NULL DEFAULT 1)"
        )
        for target_date in target_dates:
            conn.execute(
                "INSERT INTO cli_settlements VALUES (?, ?, ?, 1)", (STATION, target_date, 67.0)
            )
        conn.commit()


@pytest.fixture()
def db_paths(tmp_path: Path) -> tuple[Path, Path]:
    db_path = tmp_path / "paper.db"
    forecaster_root = tmp_path / "forecaster"
    forecaster_root.mkdir()
    dates = (FAR_FUTURE_DATE_ANCHOR, FAR_FUTURE_DATE)
    _seed_paper_db(db_path, target_dates=dates)
    _seed_weather_db(forecaster_root / "weather.db", target_dates=dates)
    return db_path, forecaster_root


def _base_argv(db_path: Path, forecaster_root: Path) -> list[str]:
    return [
        "--db-path", str(db_path),
        "--forecaster-root", str(forecaster_root),
    ]


def test_research_evaluate_requires_declare_on_first_run(db_paths, capsys) -> None:
    db_path, forecaster_root = db_paths
    argv = _base_argv(db_path, forecaster_root) + [
        "research-evaluate",
        "--hypothesis-family", "gaussian-pit-station-lead",
        "--candidate-key", "gaussian-pit-station-lead-v1",
        "--candidate-version", "v1",
    ]
    exit_code = cli.main(argv)
    assert exit_code == 2
    assert "no declared research" in capsys.readouterr().err.lower()


def test_research_evaluate_declares_and_persists_evidence(db_paths, tmp_path, capsys) -> None:
    db_path, forecaster_root = db_paths
    output_path = tmp_path / "report.json"
    argv = _base_argv(db_path, forecaster_root) + [
        "research-evaluate",
        "--hypothesis-family", "gaussian-pit-station-lead",
        "--candidate-key", "gaussian-pit-station-lead-v1",
        "--candidate-version", "v1",
        "--declare",
        "--output", str(output_path),
    ]
    exit_code = cli.main(argv)
    assert exit_code == 0

    report = json.loads(output_path.read_text())
    assert report["experiment_identity"]["hypothesis_family"] == "gaussian-pit-station-lead"
    assert report["promotion_gate"]["live_activation_allowed"] is False
    assert "insufficient_independent_confirmatory_days" in report["promotion_gate"]["block_reasons"]
    assert "never guaranteed" in report["daily_target_kpi"]["label"].lower()

    with PaperStore(db_path).connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM research_evidence").fetchone()[0]
    assert count >= 1


def test_research_evaluate_creates_parent_directories_for_output(db_paths, tmp_path) -> None:
    # Cheap item: research-propose-target already does this
    # (output_path.parent.mkdir(parents=True, exist_ok=True)) --
    # research-evaluate must too, rather than crashing on a nonexistent
    # output directory.
    db_path, forecaster_root = db_paths
    output_path = tmp_path / "nested" / "report-dir" / "report.json"
    assert not output_path.parent.exists()
    argv = _base_argv(db_path, forecaster_root) + [
        "research-evaluate",
        "--hypothesis-family", "gaussian-pit-station-lead",
        "--candidate-key", "gaussian-pit-station-lead-v1",
        "--candidate-version", "v1",
        "--declare",
        "--output", str(output_path),
    ]
    exit_code = cli.main(argv)
    assert exit_code == 0
    assert output_path.exists()


def test_research_evaluate_second_declare_with_different_tolerance_is_rejected(
    db_paths, capsys
) -> None:
    db_path, forecaster_root = db_paths
    first_argv = _base_argv(db_path, forecaster_root) + [
        "research-evaluate",
        "--hypothesis-family", "gaussian-pit-station-lead",
        "--candidate-key", "gaussian-pit-station-lead-v1",
        "--candidate-version", "v1",
        "--declare",
    ]
    assert cli.main(first_argv) == 0

    second_argv = _base_argv(db_path, forecaster_root) + [
        "research-evaluate",
        "--hypothesis-family", "gaussian-pit-station-lead",
        "--candidate-key", "gaussian-pit-station-lead-v1",
        "--candidate-version", "v1",
        "--declare",
        "--max-drawdown-tolerance-pct", "0.99",
    ]
    exit_code = cli.main(second_argv)
    assert exit_code == 2
    assert "declaration conflict" in capsys.readouterr().err.lower()


def test_research_propose_target_writes_nothing_when_not_eligible(
    db_paths, tmp_path, capsys
) -> None:
    db_path, forecaster_root = db_paths
    evaluate_argv = _base_argv(db_path, forecaster_root) + [
        "research-evaluate",
        "--hypothesis-family", "gaussian-pit-station-lead",
        "--candidate-key", "gaussian-pit-station-lead-v1",
        "--candidate-version", "v1",
        "--declare",
    ]
    assert cli.main(evaluate_argv) == 0

    proposal_path = tmp_path / "proposal.json"
    propose_argv = _base_argv(db_path, forecaster_root) + [
        "research-propose-target",
        "--hypothesis-family", "gaussian-pit-station-lead",
        "--candidate-key", "gaussian-pit-station-lead-v1",
        "--candidate-version", "v1",
        "--output", str(proposal_path),
    ]
    exit_code = cli.main(propose_argv)
    assert exit_code == 1
    assert "not eligible" in capsys.readouterr().err.lower()
    assert not proposal_path.exists()


def test_research_propose_target_requires_prior_declaration(db_paths, capsys) -> None:
    db_path, forecaster_root = db_paths
    argv = _base_argv(db_path, forecaster_root) + [
        "research-propose-target",
        "--hypothesis-family", "gaussian-pit-station-lead",
        "--candidate-key", "gaussian-pit-station-lead-v1",
        "--candidate-version", "v-never-declared",
        "--output", str(db_path.parent / "proposal.json"),
    ]
    exit_code = cli.main(argv)
    assert exit_code == 2
