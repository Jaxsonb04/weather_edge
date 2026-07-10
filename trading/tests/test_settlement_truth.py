import sqlite3
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.forecast import SfoForecasterAdapter
from sfo_kalshi_quant.settlement_truth import (
    normalize_settlement_truth,
    settlement_for_market,
)


def test_date_only_legacy_truth_can_only_settle_sfo() -> None:
    truth = normalize_settlement_truth({"2026-07-08": 67.0})

    assert settlement_for_market(truth, "KXHIGHTSFO-26JUL08-B67", "2026-07-08") == 67.0
    assert settlement_for_market(truth, "KXHIGHNY-26JUL08-B87", "2026-07-08") is None


def test_adapter_uses_city_scoped_cli_truth_not_observation_maxima() -> None:
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
        truth = adapter.load_cli_settlement_truth()

        assert truth[("KXHIGHTSFO", "2026-07-08")] == 67.0
        assert truth[("KXHIGHNY", "2026-07-08")] == 89.0
        assert adapter.load_ksfo_daily_highs() == {date(2026, 7, 8): 67.0}
