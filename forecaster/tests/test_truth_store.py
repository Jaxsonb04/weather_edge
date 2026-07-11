"""Truth/NWP store contracts kept independent from research backtests."""

from __future__ import annotations

import sqlite3

from truth_store import load_clisfo_truth, load_nwp_forecasts


def test_truth_store_migrates_legacy_sfo_truth():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE clisfo_settlements "
        "(local_date TEXT PRIMARY KEY, max_temperature_f INTEGER, fetched_at TEXT, source TEXT)"
    )
    conn.execute("INSERT INTO clisfo_settlements VALUES ('2020-07-01', 71, 'x', 'nws_cli')")

    assert load_clisfo_truth(conn) == {"2020-07-01": 71.0}
    assert load_clisfo_truth(conn, "KNYC") == {}


def test_truth_store_filters_preliminary_truth_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE cli_settlements "
        "(station_id TEXT, local_date TEXT, max_temperature_f INTEGER, fetched_at TEXT, "
        "source TEXT, is_final INTEGER, PRIMARY KEY (station_id, local_date))"
    )
    conn.executemany(
        "INSERT INTO cli_settlements VALUES ('KSFO', ?, ?, 'x', 'nws_cli', ?)",
        [("2020-07-01", 71, 1), ("2020-07-02", 72, 0)],
    )

    assert load_clisfo_truth(conn) == {"2020-07-01": 71.0}


def test_truth_store_loads_station_keyed_nwp_forecasts_and_legacy_sfo_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE nwp_model_forecasts "
        "(station_id TEXT, target_date TEXT, model TEXT, lead_days INTEGER, "
        "predicted_high_f REAL)"
    )
    conn.executemany(
        "INSERT INTO nwp_model_forecasts VALUES (?, ?, ?, ?, ?)",
        [
            ("KSFO", "2026-07-10", "gfs", 1, 70.0),
            ("KSFO", "2026-07-10", "ecmwf", 2, 71.0),
            ("KNYC", "2026-07-10", "gfs", 1, 90.0),
        ],
    )

    assert load_nwp_forecasts(conn, 1, "KSFO") == {"2026-07-10": {"gfs": 70.0}}
    assert load_nwp_forecasts(conn, 1, "KNYC") == {"2026-07-10": {"gfs": 90.0}}

    legacy = sqlite3.connect(":memory:")
    legacy.execute(
        "CREATE TABLE nwp_model_forecasts "
        "(target_date TEXT, model TEXT, lead_days INTEGER, predicted_high_f REAL)"
    )
    legacy.execute("INSERT INTO nwp_model_forecasts VALUES ('2026-07-10', 'gfs', 1, 70.0)")
    assert load_nwp_forecasts(legacy, 1) == {"2026-07-10": {"gfs": 70.0}}
    assert load_nwp_forecasts(legacy, 1, "KNYC") == {}
