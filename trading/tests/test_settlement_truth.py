import sqlite3
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.forecast import SfoForecasterAdapter
from sfo_kalshi_quant.settlement_truth import (
    load_cli_settlement_truth,
    normalize_settlement_truth,
    settlement_for_market,
)


def test_date_only_legacy_truth_can_only_settle_sfo() -> None:
    truth = normalize_settlement_truth({"2026-07-08": 67.0})

    assert settlement_for_market(truth, "KXHIGHTSFO-26JUL08-B67", "2026-07-08") == 67.0
    assert settlement_for_market(truth, "KXHIGHNY-26JUL08-B87", "2026-07-08") is None


def test_legacy_cli_schema_fails_closed_for_settlement_sensitive_loaders() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "weather.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
                "max_temperature_f REAL)"
            )
            conn.executemany(
                "INSERT INTO cli_settlements VALUES (?, ?, ?)",
                [
                    ("KSFO", "2026-07-08", 67.0),
                    ("KNYC", "2026-07-08", 89.0),
                ],
            )
            conn.execute(
                "CREATE TABLE nws_daily_high_ground_truth "
                "(station_id TEXT, local_date TEXT, high_f REAL, is_complete INTEGER)"
            )
            conn.executemany(
                "INSERT INTO nws_daily_high_ground_truth VALUES (?, ?, ?, 1)",
                [
                    ("KSFO", "2026-07-08", 62.0),
                    ("KNYC", "2026-07-08", 95.0),
                ],
            )

        adapter = SfoForecasterAdapter(Path(tmp))
        assert adapter.load_cli_settlement_highs() == {}
        assert adapter.load_cli_settlement_truth() == {}
        assert adapter.load_ksfo_daily_highs() == {}
        with sqlite3.connect(db_path) as conn:
            assert load_cli_settlement_truth(conn) == {}


def test_adapter_excludes_preliminary_cli_truth_when_finality_column_exists() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "weather.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE cli_settlements (station_id TEXT, local_date TEXT, "
                "max_temperature_f REAL, is_final INTEGER NOT NULL DEFAULT 1)"
            )
            conn.executemany(
                "INSERT INTO cli_settlements VALUES (?, ?, ?, ?)",
                [
                    ("KSFO", "2026-07-08", 68.0, 0),
                    ("KSFO", "2026-07-07", 71.0, 1),
                ],
            )

        adapter = SfoForecasterAdapter(Path(tmp))

        assert adapter.load_cli_settlement_highs() == {date(2026, 7, 7): 71.0}
        assert adapter.load_cli_settlement_truth() == {
            ("KXHIGHTSFO", "2026-07-07"): 71.0
        }
