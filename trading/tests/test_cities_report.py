from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sfo_kalshi_quant import cities_report
from sfo_kalshi_quant.db import PaperStore


NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
CUTOFF = "2026-07-08T12:00:00+00:00"


def _create_activity_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE decision_snapshots (
            created_at TEXT NOT NULL,
            market_ticker TEXT NOT NULL,
            approved INTEGER NOT NULL
        );
        CREATE TABLE paper_orders (
            market_ticker TEXT NOT NULL,
            risk_profile TEXT,
            contracts REAL NOT NULL,
            cost_per_contract REAL NOT NULL,
            status TEXT NOT NULL,
            settled_at TEXT,
            closed_at TEXT,
            realized_pnl REAL
        );
        """
    )


def _seed_activity(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO decision_snapshots VALUES (?, ?, ?)",
        [
            (CUTOFF, "KXHIGHTSFO-26JUL09-B65.5", 1),
            ("2026-07-09T11:00:00+00:00", "KXHIGHTSFO-26JUL09-B65.5", 0),
            ("2026-07-09T11:30:00+00:00", "KXHIGHTSFO-26JUL09-B67.5", 1),
            ("2026-07-08T11:59:59+00:00", "KXHIGHTSFO-26JUL09-B69.5", 1),
            ("2026-07-09T10:00:00+00:00", "KXHIGHNY-26JUL09-B81.5", 1),
            ("2026-07-09T10:00:00+00:00", "UNMAPPED-26JUL09", 1),
        ],
    )
    conn.executemany(
        "INSERT INTO paper_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "KXHIGHTSFO-26JUL09-B65.5",
                "live",
                2.0,
                0.25,
                "PAPER_FILLED",
                None,
                None,
                None,
            ),
            (
                "KXHIGHTSFO-26JUL09-B67.5",
                "research",
                3.0,
                0.10,
                "PAPER_LIMIT_RESTING",
                None,
                None,
                None,
            ),
            (
                "KXHIGHTSFO-26JUL08-B63.5",
                "live",
                1.0,
                0.20,
                "PAPER_SETTLED",
                "2026-07-09T01:00:00+00:00",
                None,
                0.8,
            ),
            (
                "KXHIGHNY-26JUL09-B81.5",
                None,
                4.0,
                0.15,
                "PAPER_FILLED",
                None,
                None,
                None,
            ),
        ],
    )


def test_all_city_books_uses_one_grouped_pass_and_does_not_duplicate_decisions_by_profile():
    with sqlite3.connect(":memory:") as conn:
        _create_activity_tables(conn)
        _seed_activity(conn)
        statements: list[str] = []
        conn.set_trace_callback(statements.append)

        books = cities_report._all_city_books(conn, CUTOFF)

    sfo = books["sfo"]
    assert sfo["decisions_24h"] == 3
    assert sfo["approved_24h"] == 2
    assert "decisions_24h" not in sfo["live"]
    assert "decisions_24h" not in sfo["research"]
    assert sfo["live"] == {
        "open_positions": 1,
        "open_exposure": 0.5,
        "settled_orders": 1,
        "settled_pnl": 0.8,
    }
    assert sfo["research"] == {
        "open_positions": 1,
        "open_exposure": 0.3,
        "settled_orders": 0,
        "settled_pnl": 0.0,
    }
    assert books["nyc"]["decisions_24h"] == 1
    assert books["nyc"]["live"]["open_positions"] == 1
    assert len([sql for sql in statements if "FROM decision_snapshots" in sql]) == 1
    assert len([sql for sql in statements if "FROM paper_orders" in sql]) == 1


def test_all_city_books_tolerates_missing_tables():
    with sqlite3.connect(":memory:") as conn:
        books = cities_report._all_city_books(conn, CUTOFF)

    assert books["sfo"]["decisions_24h"] == 0
    assert books["sfo"]["live"]["open_positions"] == 0
    assert books["sfo"]["research"]["settled_orders"] == 0


def test_cities_payload_includes_city_settlement_day_and_forecast_target_status():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        root.mkdir()
        with sqlite3.connect(root / "weather.db") as conn:
            conn.execute(
                """
                CREATE TABLE forecast_emos_daily_high (
                    station_id TEXT NOT NULL,
                    target_date TEXT NOT NULL,
                    lead_days INTEGER,
                    predicted_high_f REAL NOT NULL,
                    sigma_f REAL NOT NULL,
                    n_models INTEGER,
                    model_spread_f REAL,
                    fetched_at TEXT NOT NULL,
                    method TEXT,
                    source TEXT NOT NULL
                )
                """
            )
            conn.executemany(
                "INSERT INTO forecast_emos_daily_high VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("KSFO", "2026-07-09", 0, 68.0, 2.0, 4, 1.0, "2026-07-09T11:00:00+00:00", "emos", "live"),
                    ("KSFO", "2026-07-10", 1, 69.0, 2.1, 4, 1.1, "2026-07-09T11:00:00+00:00", "emos", "live"),
                ],
            )

        payload = cities_report.build_cities_data(
            root,
            Path(tmp) / "missing-paper.db",
            now=NOW,
        )

    sfo = next(city for city in payload["cities"] if city["slug"] == "sfo")
    assert payload["generated_at"] == "2026-07-09T12:00:00+00:00"
    assert sfo["settlement_today"] == "2026-07-09"
    assert [forecast["target_status"] for forecast in sfo["forecasts"]] == [
        "settlement_day",
        "upcoming",
    ]


def test_public_coverage_prefers_live_then_v2_then_v1_before_timestamp():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        root.mkdir()
        with sqlite3.connect(root / "weather.db") as conn:
            conn.execute(
                """
                CREATE TABLE forecast_emos_daily_high (
                    station_id TEXT NOT NULL, target_date TEXT NOT NULL,
                    lead_days INTEGER, predicted_high_f REAL NOT NULL,
                    sigma_f REAL NOT NULL, n_models INTEGER,
                    model_spread_f REAL, fetched_at TEXT NOT NULL,
                    method TEXT, source TEXT NOT NULL
                )
                """
            )
            conn.executemany(
                "INSERT INTO forecast_emos_daily_high VALUES (?, ?, ?, ?, 2, 8, 1, ?, 'emos', ?)",
                [
                    # Live wins even though both rolling rebuilds are newer.
                    ("KSFO", "2026-07-09", 0, 68, "2026-07-09T09:00:00+00:00", "live"),
                    ("KSFO", "2026-07-09", 0, 88, "2026-07-09T11:00:00+00:00", "rolling_origin_v2"),
                    ("KSFO", "2026-07-09", 0, 98, "2026-07-09T12:00:00+00:00", "rolling_origin"),
                    # Without live, v2 wins over a newer v1 rebuild.
                    ("KSFO", "2026-07-10", 1, 69, "2026-07-09T09:00:00+00:00", "rolling_origin_v2"),
                    ("KSFO", "2026-07-10", 1, 99, "2026-07-09T12:00:00+00:00", "rolling_origin"),
                    # A v1-only target remains available as the compatibility fallback.
                    ("KSFO", "2026-07-11", 2, 70, "2026-07-09T12:00:00+00:00", "rolling_origin"),
                ],
            )

        payload = cities_report.build_cities_data(
            root,
            Path(tmp) / "missing-paper.db",
            now=NOW,
        )

    sfo = next(city for city in payload["cities"] if city["slug"] == "sfo")
    assert [row["predicted_high_f"] for row in sfo["forecasts"]] == [68.0, 69.0, 70.0]


def test_decision_aggregation_query_uses_created_market_covering_index():
    with tempfile.TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        with store.connect() as conn:
            plan = conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT market_ticker, COUNT(*), COALESCE(SUM(approved), 0)
                FROM decision_snapshots
                WHERE created_at >= ?
                GROUP BY market_ticker
                """,
                (CUTOFF,),
            ).fetchall()

    details = " ".join(str(row[3]) for row in plan)
    assert "SEARCH decision_snapshots USING COVERING INDEX idx_decision_snapshots_created_market" in details
    assert "SCAN decision_snapshots" not in details


def test_existing_database_warns_but_does_not_build_large_decision_index_on_service_init(caplog):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        with store.connect() as conn:
            conn.execute("DROP INDEX idx_decision_snapshots_created_market")
            conn.execute(
                """
                INSERT INTO decision_snapshots (
                    created_at, target_date, market_ticker, label, action, side,
                    approved, probability, probability_lcb, yes_bid, yes_ask,
                    spread, fee_per_contract, cost_per_contract, edge, edge_lcb,
                    kelly_fraction, recommended_contracts, recommended_spend,
                    expected_profit, trade_quality_score, reasons_json
                ) VALUES (
                    '2026-07-09T12:00:00+00:00', '2026-07-09', 'TEST', 'test',
                    'BUY_YES', 'YES', 0, 0.5, 0.4, 0.4, 0.5, 0.1, 0.01,
                    0.51, -0.01, -0.11, 0, 0, 0, 0, 0, '[]'
                )
                """
            )

        with caplog.at_level("WARNING", logger="sfo_kalshi_quant.db"):
            PaperStore(db_path)

        with sqlite3.connect(db_path) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                ("idx_decision_snapshots_created_market",),
            ).fetchone()

    assert exists is None
    assert "idx_decision_snapshots_created_market" in caplog.text
    assert "create_decision_snapshot_index.sh" in caplog.text


def test_atomic_cities_write_keeps_previous_artifact_when_replace_fails():
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "cities_data.json"
        output.write_text('{"snapshot": "previous"}\n', encoding="utf-8")

        with patch("sfo_kalshi_quant.cities_report.os.replace", side_effect=OSError("disk")):
            try:
                cities_report._atomic_write_json(output, {"snapshot": "next"})
            except OSError as exc:
                assert str(exc) == "disk"
            else:
                raise AssertionError("replace failure must be surfaced")

        assert json.loads(output.read_text(encoding="utf-8")) == {"snapshot": "previous"}
        assert list(output.parent.glob(f".{output.name}.*.tmp")) == []


def test_index_maintenance_script_builds_the_same_named_index():
    trading_root = Path(__file__).resolve().parents[1]
    script = (trading_root / "deploy" / "aws" / "create_decision_snapshot_index.sh").read_text(
        encoding="utf-8"
    )
    deploy_readme = (trading_root / "deploy" / "aws" / "README.md").read_text(
        encoding="utf-8"
    )
    aws_notes = (trading_root.parent / "docs" / "aws_deployment.md").read_text(
        encoding="utf-8"
    )

    assert "idx_decision_snapshots_created_market" in script
    assert "created_at, market_ticker, approved" in script
    assert "paper-scan" in script
    assert "paper-monitor" in script
    assert 'conn.execute("ANALYZE")' in script
    assert 'ANALYZE decision_snapshots' not in script
    for documentation in (deploy_readme, aws_notes):
        assert "create_decision_snapshot_index.sh" in documentation
        assert "paused" in documentation
