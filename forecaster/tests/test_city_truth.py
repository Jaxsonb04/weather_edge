"""cli_settlements: schema migration, upserts, IEM backfill parsing."""

import sqlite3
from datetime import date, datetime, timezone
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


def test_migrates_existing_cli_settlements_with_final_default():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE cli_settlements ("
        "station_id TEXT NOT NULL, local_date TEXT NOT NULL, "
        "max_temperature_f INTEGER, fetched_at TEXT NOT NULL, source TEXT NOT NULL, "
        "PRIMARY KEY (station_id, local_date))"
    )
    conn.execute("INSERT INTO cli_settlements VALUES ('KSFO', '2026-06-01', 67, 't', 'legacy')")

    city_truth.ensure_schema(conn)

    columns = {row[1]: row for row in conn.execute("PRAGMA table_info(cli_settlements)")}
    assert columns["is_final"][4] == "1"
    assert conn.execute("SELECT is_final FROM cli_settlements").fetchone()[0] == 1
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


def test_preliminary_settlement_is_stored_but_not_loaded_as_truth():
    conn = sqlite3.connect(":memory:")
    city_truth.ensure_schema(conn)

    city_truth.upsert_settlement(conn, "KSFO", "2026-06-01", 68, is_final=False)

    assert conn.execute(
        "SELECT max_temperature_f, is_final FROM cli_settlements"
    ).fetchone() == (68, 0)
    assert city_truth.load_cli_truth(conn, "KSFO") == {}
    assert city_truth.cli_high_for(conn, "KSFO", "2026-06-01") is None


def test_final_settlement_replaces_preliminary_and_metadata():
    conn = sqlite3.connect(":memory:")
    city_truth.ensure_schema(conn)
    city_truth.upsert_settlement(
        conn, "KSFO", "2026-06-01", 68, is_final=False, source="prelim", fetched_at="early"
    )

    city_truth.upsert_settlement(
        conn, "KSFO", "2026-06-01", 71, is_final=True, source="final", fetched_at="late"
    )

    assert conn.execute(
        "SELECT max_temperature_f, fetched_at, source, is_final FROM cli_settlements"
    ).fetchone() == (71, "late", "final", 1)


def test_preliminary_settlement_cannot_replace_final():
    conn = sqlite3.connect(":memory:")
    city_truth.ensure_schema(conn)
    city_truth.upsert_settlement(
        conn, "KSFO", "2026-06-01", 71, is_final=True, source="final", fetched_at="late"
    )

    city_truth.upsert_settlement(
        conn, "KSFO", "2026-06-01", 68, is_final=False, source="prelim", fetched_at="later-fetch"
    )

    assert conn.execute(
        "SELECT max_temperature_f, fetched_at, source, is_final FROM cli_settlements"
    ).fetchone() == (71, "late", "final", 1)


def test_settlement_finality_uses_city_fixed_standard_day_end():
    sfo = get_city("sfo")
    report_date = date(2026, 7, 10)

    assert not city_truth.settlement_is_final(
        sfo, report_date, datetime(2026, 7, 11, 7, 59, tzinfo=timezone.utc)
    )
    assert city_truth.settlement_is_final(
        sfo, report_date, datetime(2026, 7, 11, 8, 0, tzinfo=timezone.utc)
    )


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


def test_backfill_iem_marks_in_progress_settlement_day_preliminary(monkeypatch):
    conn = sqlite3.connect(":memory:")
    sfo = get_city("sfo")
    monkeypatch.setattr(
        city_truth,
        "_utcnow",
        lambda: datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc),
    )
    with patch.object(
        city_truth,
        "_iem_rows",
        return_value=[{"valid": "2026-07-10", "high": 68}],
    ):
        city_truth.backfill_iem(conn, sfo, start_year=2026, end_year=2026)

    assert conn.execute("SELECT is_final FROM cli_settlements").fetchone()[0] == 0
    assert city_truth.load_cli_truth(conn, "KSFO") == {}


def test_backfill_iem_skips_malformed_local_date_fail_soft():
    conn = sqlite3.connect(":memory:")
    sfo = get_city("sfo")
    with patch.object(
        city_truth,
        "_iem_rows",
        return_value=[{"valid": "not-a-date", "high": 68}],
    ):
        written = city_truth.backfill_iem(conn, sfo, start_year=2026, end_year=2026)

    assert written == 0
    assert conn.execute("SELECT COUNT(*) FROM cli_settlements").fetchone()[0] == 0


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


def test_refresh_live_marks_in_progress_settlement_day_preliminary(monkeypatch):
    conn = sqlite3.connect(":memory:")
    sfo = get_city("sfo")
    monkeypatch.setattr(
        city_truth,
        "_utcnow",
        lambda: datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc),
    )
    with patch.object(
        city_truth,
        "fetch_recent_cli_settlements",
        return_value={date(2026, 7, 10): 68},
    ):
        city_truth.refresh_live(conn, (sfo,))

    assert conn.execute("SELECT is_final FROM cli_settlements").fetchone()[0] == 0
    assert city_truth.cli_high_for(conn, "KSFO", "2026-07-10") is None
