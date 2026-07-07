"""Network-free tests for the NWP forecast archive (Phase 0)."""

from __future__ import annotations

import sqlite3

import nwp_archive
from nwp_archive import (
    NwpArchiveError,
    _date_chunks,
    ensure_schema,
    fetch_model_range,
    reconstruct_daily_max,
    upsert_forecasts,
)


def test_reconstruct_groups_by_local_date_and_takes_max():
    payload = {
        "hourly": {
            "time": [
                "2025-06-10T08:00", "2025-06-10T14:00", "2025-06-10T20:00",
                "2025-06-11T09:00", "2025-06-11T15:00",
            ],
            "temperature_2m_previous_day1": [55.0, 63.2, 58.0, 60.0, 61.5],
        }
    }
    out = reconstruct_daily_max(payload, lead_days=1)
    assert out == {"2025-06-10": 63.2, "2025-06-11": 61.5}


def test_reconstruct_reads_previous_day_variable_not_base():
    # Leakage guard: the freshest 'temperature_2m' (analysis-like) must be ignored
    # in favour of the day-ahead 'temperature_2m_previous_day1'.
    payload = {
        "hourly": {
            "time": ["2025-06-10T14:00", "2025-06-10T15:00"],
            "temperature_2m": [99.0, 98.0],                 # leaky -- must NOT be used
            "temperature_2m_previous_day1": [63.2, 62.0],   # the honest day-ahead value
        }
    }
    out = reconstruct_daily_max(payload, lead_days=1)
    assert out == {"2025-06-10": 63.2}


def test_reconstruct_skips_none_values():
    payload = {
        "hourly": {
            "time": ["2025-06-10T08:00", "2025-06-10T14:00"],
            "temperature_2m_previous_day1": [None, 61.0],
        }
    }
    assert reconstruct_daily_max(payload, lead_days=1) == {"2025-06-10": 61.0}


def test_reconstruct_handles_higher_leads():
    payload = {
        "hourly": {
            "time": ["2025-06-10T14:00"],
            "temperature_2m_previous_day3": [70.5],
        }
    }
    assert reconstruct_daily_max(payload, lead_days=3) == {"2025-06-10": 70.5}


def test_schema_and_upsert_is_idempotent():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    key = ("2025-06-10", "gfs_seamless", 1)
    upsert_forecasts(conn, [("KSFO", *key, 63.2, "t1", "openmeteo_previous_runs")])
    upsert_forecasts(conn, [("KSFO", *key, 64.0, "t2", "openmeteo_previous_runs")])  # same PK
    rows = conn.execute(
        "SELECT predicted_high_f, fetched_at FROM nwp_model_forecasts"
    ).fetchall()
    assert len(rows) == 1, "re-fetch must overwrite in place, not duplicate"
    assert rows[0][0] == 64.0 and rows[0][1] == "t2"


def test_fetch_requests_only_previous_day_variable():
    # Leakage regression guard: fetch_model_range must request ONLY the
    # previous-day variable, never the leaky freshest temperature_2m. A drift to
    # the base variable would reintroduce look-ahead and pass every other test.
    captured: dict[str, str] = {}

    def fake_get(url, timeout=45.0):
        captured["url"] = url
        return {"hourly": {"time": ["2025-06-01T14:00"], "temperature_2m_previous_day2": [70.0]}}

    original = nwp_archive._http_get_json
    nwp_archive._http_get_json = fake_get
    try:
        out = fetch_model_range("gfs_seamless", "2025-06-01", "2025-06-02", 2)
    finally:
        nwp_archive._http_get_json = original

    url = captured["url"]
    assert "temperature_2m_previous_day2" in url
    # the bare base token must not be requested as its own hourly variable
    assert "hourly=temperature_2m&" not in url
    assert not url.split("&hourly=")[-1].split("&")[0] == "temperature_2m"
    assert out == {"2025-06-01": 70.0}


def test_reconstruct_returns_empty_when_only_base_variable_present():
    # No silent fallback to the leaky series when the previous-day key is absent.
    payload = {"hourly": {"time": ["2025-06-01T14:00"], "temperature_2m": [99.0]}}
    assert reconstruct_daily_max(payload, lead_days=1) == {}


def test_upsert_keys_on_source():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    base = ("2025-06-10", "gfs_seamless", 1)
    upsert_forecasts(conn, [("KSFO", *base, 70.0, "t", "openmeteo_previous_runs")])
    upsert_forecasts(conn, [("KSFO", *base, 71.0, "t", "ghcn_backfill")])  # different source
    assert conn.execute("SELECT COUNT(*) FROM nwp_model_forecasts").fetchone()[0] == 2

    upsert_forecasts(conn, [("KSFO", *base, 72.0, "t2", "openmeteo_previous_runs")])  # same source -> in place
    assert conn.execute("SELECT COUNT(*) FROM nwp_model_forecasts").fetchone()[0] == 2
    updated = conn.execute(
        "SELECT predicted_high_f FROM nwp_model_forecasts WHERE source = 'openmeteo_previous_runs'"
    ).fetchone()
    assert updated[0] == 72.0


def test_date_chunks_single_day():
    assert list(_date_chunks("2021-01-01", "2021-01-01")) == [("2021-01-01", "2021-01-01")]


def test_date_chunks_rejects_reversed_range():
    raised = False
    try:
        list(_date_chunks("2021-02-01", "2021-01-01"))
    except NwpArchiveError:
        raised = True
    assert raised


def test_date_chunks_cover_range_without_gaps_or_overlap():
    chunks = list(_date_chunks("2021-01-01", "2022-06-15", span_days=300))
    assert chunks[0][0] == "2021-01-01"
    assert chunks[-1][1] == "2022-06-15"
    # contiguous: each chunk starts the day after the previous chunk ends
    from datetime import date, timedelta
    for (_, prev_end), (next_start, _) in zip(chunks, chunks[1:]):
        assert date.fromisoformat(next_start) == date.fromisoformat(prev_end) + timedelta(days=1)
