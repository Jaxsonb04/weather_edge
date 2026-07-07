"""cli_settlements: schema migration, upserts, IEM backfill parsing."""

import sqlite3
from unittest.mock import patch

import city_truth
from cities import get_city


def test_migrates_legacy_clisfo_table_once_and_drops_it():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE clisfo_settlements "
        "(local_date TEXT PRIMARY KEY, max_temperature_f INTEGER, fetched_at TEXT)"
    )
    conn.execute("INSERT INTO clisfo_settlements VALUES ('2026-06-01', 67, 't')")

    city_truth.ensure_schema(conn)

    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='clisfo_settlements'"
    ).fetchone() is None
    assert city_truth.cli_high_for(conn, "KSFO", "2026-06-01") == 67.0
    # Idempotent: a second ensure_schema is a no-op.
    city_truth.ensure_schema(conn)
    assert city_truth.cli_high_for(conn, "KSFO", "2026-06-01") == 67.0


def test_upsert_is_keyed_by_station_and_date():
    conn = sqlite3.connect(":memory:")
    city_truth.ensure_schema(conn)
    city_truth.upsert_settlement(conn, "KNYC", "2026-06-01", 85)
    city_truth.upsert_settlement(conn, "KSFO", "2026-06-01", 67)
    city_truth.upsert_settlement(conn, "KNYC", "2026-06-01", 86)  # correction wins

    assert city_truth.cli_high_for(conn, "KNYC", "2026-06-01") == 86.0
    assert city_truth.cli_high_for(conn, "KSFO", "2026-06-01") == 67.0
    assert city_truth.load_cli_truth(conn, "KNYC") == {"2026-06-01": 86.0}


def test_backfill_iem_parses_rows_and_later_products_win():
    conn = sqlite3.connect(":memory:")
    nyc = get_city("nyc")
    # Two products for the same day: the later (corrected final) row must win,
    # matching the live rule that the final CLI shadows the preliminary one.
    fake_rows = [
        {"valid": "2026-06-01", "high": 85},
        {"valid": "2026-06-01", "high": 86},
        {"valid": "2026-06-02", "high": "M"},  # missing marker must be skipped
        {"valid": "2026-06-03", "high": 90},
    ]
    with patch.object(city_truth, "_iem_rows", return_value=fake_rows):
        written = city_truth.backfill_iem(conn, nyc, start_year=2026, end_year=2026)

    assert written == 3
    assert city_truth.load_cli_truth(conn, "KNYC") == {
        "2026-06-01": 86.0,
        "2026-06-03": 90.0,
    }
    row = conn.execute(
        "SELECT source FROM cli_settlements WHERE station_id='KNYC' AND local_date='2026-06-03'"
    ).fetchone()
    assert row[0] == "iem_cli"


def test_refresh_live_is_fail_soft_per_city():
    conn = sqlite3.connect(":memory:")
    cities = (get_city("sfo"), get_city("nyc"))

    def fake_fetch(site, issuedby, **kwargs):
        if issuedby == "SFO":
            raise OSError("wfo outage")
        from datetime import date

        return {date(2026, 6, 1): 85}

    with patch.object(city_truth, "fetch_recent_cli_settlements", side_effect=fake_fetch):
        stored = city_truth.refresh_live(conn, cities)

    assert stored == {"KSFO": 0, "KNYC": 1}
    assert city_truth.cli_high_for(conn, "KNYC", "2026-06-01") == 85.0
