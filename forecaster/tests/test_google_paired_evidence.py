"""Durable paired baseline/challenger evidence (Task 7).

Covers: deriving and persisting ONLY derived mu/sigma/action evidence (never
a raw Google high, gap, response, or conditions field -- spec section
7.2/7.3); failing closed when Google runtime evidence is missing or
incomplete; idempotent re-derivation; and coercing EMOS-derived scalars
(including numpy) to plain Python floats before they reach the frozen
``google_challenger`` formula (T7-2 -- ``google_runtime_blend._finite_float``
uses an exact ``type(value) not in (int, float)`` check that rejects
numpy scalars).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from cities import get_city
from google_paired_evidence import (
    PAIRED_EVIDENCE_TABLE,
    derive_and_record_paired_evidence,
    ensure_paired_evidence_table,
    latest_paired_evidence,
)
from google_weather_store import GoogleRuntimeStore

TEST_NOW = datetime(2026, 7, 18, 19, 0, tzinfo=timezone.utc)


def _runtime_store(tmp_path):
    return GoogleRuntimeStore(tmp_path / "google_runtime.db", production=False)


def _write_full_station_day(runtime, *, city, issued_at, target_date, base_temp=60.0):
    """Write 24 consecutive fixed-standard hourly rows covering target_date."""

    tz = city.fixed_standard_timezone()
    start_local = datetime.combine(
        date.fromisoformat(target_date), datetime.min.time(), tzinfo=tz
    )
    temperatures_by_valid_at = {
        (start_local + timedelta(hours=hour)).astimezone(timezone.utc): base_temp + hour
        for hour in range(24)
    }
    runtime.replace_hourly_generation(
        city_slug=city.slug,
        station_id=city.nws_station_id,
        issued_at=issued_at,
        temperatures_by_valid_at=temperatures_by_valid_at,
        stored_at=issued_at,
    )
    return temperatures_by_valid_at


def test_returns_none_and_writes_nothing_without_google_runtime_evidence(tmp_path):
    city = get_city("nyc")
    runtime = _runtime_store(tmp_path)
    conn = sqlite3.connect(tmp_path / "weather.db")

    result = derive_and_record_paired_evidence(
        conn,
        city=city,
        target_date="2026-07-19",
        baseline_mu=80.0,
        baseline_sigma=3.0,
        runtime_store=runtime,
        now=TEST_NOW,
    )

    assert result is None
    assert (
        latest_paired_evidence(conn, station_id=city.nws_station_id, target_date="2026-07-19")
        is None
    )


def test_derives_and_persists_only_mu_sigma_action_never_a_raw_google_field(tmp_path):
    city = get_city("nyc")
    runtime = _runtime_store(tmp_path)
    target_date = "2026-07-19"
    # base_temp=60 => hourly highs 60..83, so the station-day high is 83F.
    _write_full_station_day(
        runtime, city=city, issued_at=TEST_NOW, target_date=target_date
    )
    conn = sqlite3.connect(tmp_path / "weather.db")

    result = derive_and_record_paired_evidence(
        conn,
        city=city,
        target_date=target_date,
        baseline_mu=80.0,
        baseline_sigma=3.0,
        runtime_store=runtime,
        now=TEST_NOW,
    )

    assert result is not None
    assert result["station_id"] == city.nws_station_id
    assert result["target_date"] == target_date
    assert result["policy_version"] == "google-runtime-fixed-v1"
    assert result["baseline_mu"] == 80.0
    assert result["baseline_sigma"] == 3.0
    # gap = 83 - 80 = 3 < 7F => forecast action, mu = 80 + 0.15*3 = 80.45
    assert result["challenger_mu"] == pytest.approx(80.45)
    assert result["challenger_sigma"] == 3.0
    assert result["action"] == "forecast"

    # The persisted row -- and the table's own column set -- carry only
    # derived evidence, never a raw Google high, gap, response, or URL/key.
    columns = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({PAIRED_EVIDENCE_TABLE})")
    }
    forbidden = {
        "google_high_f", "google_high", "high_f", "gap", "raw", "response",
        "conditions", "url", "key", "token", "body",
    }
    assert not (columns & forbidden)
    assert columns == {
        "station_id", "target_date", "issued_at", "policy_version",
        "baseline_mu", "baseline_sigma", "challenger_mu", "challenger_sigma",
        "action",
    }

    stored = latest_paired_evidence(
        conn, station_id=city.nws_station_id, target_date=target_date
    )
    assert stored == result


def test_seven_degree_gap_blocks_rather_than_persisting_a_probability(tmp_path):
    city = get_city("nyc")
    runtime = _runtime_store(tmp_path)
    target_date = "2026-07-19"
    # base_temp=73 => high is 96F, gap = 96 - 80 = 16 >= 7F => blocked.
    _write_full_station_day(
        runtime, city=city, issued_at=TEST_NOW, target_date=target_date, base_temp=73.0
    )
    conn = sqlite3.connect(tmp_path / "weather.db")

    result = derive_and_record_paired_evidence(
        conn,
        city=city,
        target_date=target_date,
        baseline_mu=80.0,
        baseline_sigma=3.0,
        runtime_store=runtime,
        now=TEST_NOW,
    )

    assert result is not None
    assert result["action"] == "external_runtime_corroboration_block"
    assert result["challenger_mu"] is None
    assert result["challenger_sigma"] == 3.0


def test_incomplete_station_day_fails_closed(tmp_path):
    city = get_city("nyc")
    runtime = _runtime_store(tmp_path)
    target_date = "2026-07-19"
    temperatures = _write_full_station_day(
        runtime, city=city, issued_at=TEST_NOW, target_date=target_date
    )
    # Drop one hour so the derived high is marked incomplete.
    dropped_valid_at = next(iter(temperatures))
    del temperatures[dropped_valid_at]
    runtime.replace_hourly_generation(
        city_slug=city.slug,
        station_id=city.nws_station_id,
        issued_at=TEST_NOW,
        temperatures_by_valid_at=temperatures,
        stored_at=TEST_NOW,
    )
    conn = sqlite3.connect(tmp_path / "weather.db")

    result = derive_and_record_paired_evidence(
        conn,
        city=city,
        target_date=target_date,
        baseline_mu=80.0,
        baseline_sigma=3.0,
        runtime_store=runtime,
        now=TEST_NOW,
    )

    assert result is None
    assert (
        latest_paired_evidence(
            conn, station_id=city.nws_station_id, target_date=target_date
        )
        is None
    )


def test_derivation_is_idempotent_under_a_repeated_call(tmp_path):
    city = get_city("nyc")
    runtime = _runtime_store(tmp_path)
    target_date = "2026-07-19"
    _write_full_station_day(
        runtime, city=city, issued_at=TEST_NOW, target_date=target_date
    )
    conn = sqlite3.connect(tmp_path / "weather.db")

    first = derive_and_record_paired_evidence(
        conn, city=city, target_date=target_date, baseline_mu=80.0,
        baseline_sigma=3.0, runtime_store=runtime, now=TEST_NOW,
    )
    second = derive_and_record_paired_evidence(
        conn, city=city, target_date=target_date, baseline_mu=80.0,
        baseline_sigma=3.0, runtime_store=runtime, now=TEST_NOW,
    )

    assert first == second
    rows = conn.execute(
        f"SELECT COUNT(*) FROM {PAIRED_EVIDENCE_TABLE}"
    ).fetchone()
    assert rows[0] == 1


def test_returns_the_stored_row_not_a_stale_recompute_when_baseline_refits_between_calls(
    tmp_path,
):
    """W2 (Task 7 review, MEDIUM): INSERT OR IGNORE means a second call that
    resolves to the same (station_id, target_date, issued_at, policy_version)
    identity -- e.g. the Google runtime evidence is unchanged but the
    permanent EMOS baseline refit between calls -- silently skips the write.
    The return value must reflect what is actually durably stored, never the
    freshly computed (and in this case discarded) second-call values.
    """

    city = get_city("nyc")
    runtime = _runtime_store(tmp_path)
    target_date = "2026-07-19"
    _write_full_station_day(
        runtime, city=city, issued_at=TEST_NOW, target_date=target_date
    )
    conn = sqlite3.connect(tmp_path / "weather.db")

    first = derive_and_record_paired_evidence(
        conn, city=city, target_date=target_date, baseline_mu=80.0,
        baseline_sigma=3.0, runtime_store=runtime, now=TEST_NOW,
    )
    # The Google runtime evidence (and therefore its issued_at identity) is
    # unchanged, but the permanent baseline moved -- e.g. an EMOS refit
    # between calls within the same Google issue window.
    second = derive_and_record_paired_evidence(
        conn, city=city, target_date=target_date, baseline_mu=85.0,
        baseline_sigma=3.0, runtime_store=runtime, now=TEST_NOW,
    )

    assert second == first
    assert second["baseline_mu"] == 80.0
    stored = conn.execute(
        f"SELECT baseline_mu FROM {PAIRED_EVIDENCE_TABLE}"
    ).fetchone()
    assert stored[0] == 80.0


def test_ensure_paired_evidence_table_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "weather.db")

    ensure_paired_evidence_table(conn)
    ensure_paired_evidence_table(conn)

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert PAIRED_EVIDENCE_TABLE in tables


def test_numpy_baseline_scalars_are_coerced_to_plain_float_before_the_formula(tmp_path):
    """T7-2: the challenger's exact-type guard rejects numpy scalars.

    ``google_runtime_blend._finite_float`` uses ``type(value) not in (int,
    float)``, so a numpy.float64 baseline (as EMOS fitting can hand back)
    would otherwise raise ValueError. This proves the paired-evidence
    boundary coerces to plain float first.
    """

    numpy = pytest.importorskip("numpy")
    city = get_city("nyc")
    runtime = _runtime_store(tmp_path)
    target_date = "2026-07-19"
    _write_full_station_day(
        runtime, city=city, issued_at=TEST_NOW, target_date=target_date
    )
    conn = sqlite3.connect(tmp_path / "weather.db")

    result = derive_and_record_paired_evidence(
        conn,
        city=city,
        target_date=target_date,
        baseline_mu=numpy.float64(80.0),
        baseline_sigma=numpy.float64(3.0),
        runtime_store=runtime,
        now=TEST_NOW,
    )

    assert result is not None
    assert result["baseline_mu"] == 80.0
    assert type(result["baseline_mu"]) is float
    assert result["challenger_mu"] == pytest.approx(80.45)


def test_non_finite_baseline_is_rejected_not_silently_poisoned(tmp_path):
    city = get_city("nyc")
    runtime = _runtime_store(tmp_path)
    target_date = "2026-07-19"
    _write_full_station_day(
        runtime, city=city, issued_at=TEST_NOW, target_date=target_date
    )
    conn = sqlite3.connect(tmp_path / "weather.db")

    with pytest.raises(ValueError):
        derive_and_record_paired_evidence(
            conn,
            city=city,
            target_date=target_date,
            baseline_mu=float("nan"),
            baseline_sigma=3.0,
            runtime_store=runtime,
            now=TEST_NOW,
        )
    assert (
        latest_paired_evidence(
            conn, station_id=city.nws_station_id, target_date=target_date
        )
        is None
    )
