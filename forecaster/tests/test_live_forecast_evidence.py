from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from cities import get_city
from live_forecast_evidence import LiveModelForecast, export_live_forecasts, record_live_forecast


def _record(conn, *, recorded_at="2026-10-06T00:00:00Z", inputs=None, mu=71.0):
    return record_live_forecast(
        conn, city=get_city("sfo"), target_date=date(2026, 10, 6), recorded_at=recorded_at,
        inputs=inputs if inputs is not None else LiveModelForecast(
            {"gfs": 71.0, "ecmwf": 70.0, "unseen": 80.0}, retrieved_at="2026-10-05T23:59:59Z"
        ), used_models={"gfs", "ecmwf"}, mu=mu, sigma=2.0,
        fit_lead_days=1, legacy_lead_days=1, method="emos_wmean",
        training=[("2026-10-04", {"gfs": 70.0}, 69.0)],
        fit_params={"biases": {"gfs": 1.0}}, serve_policy={"bias_recalibration": True},
    )


def test_live_vintages_are_immutable_named_and_not_reconstructed_training_rows():
    conn = sqlite3.connect(":memory:")
    first = _record(conn)
    assert _record(conn) == first
    second = _record(conn, recorded_at="2026-10-06T00:30:00Z", mu=72.0)
    assert second != first
    rows = export_live_forecasts(conn, before="2026-10-06T00:10:00Z")
    assert len(rows) == 1
    row = rows[0]
    assert row["snapshot_id"] == first
    assert row["predicted_high_f"] == 71.0
    assert row["lead_days"] == row["fit_lead_days"] == 1
    assert row["window_start_utc"] == "2026-10-06T08:00:00+00:00"
    assert row["window_end_utc"] == "2026-10-07T08:00:00+00:00"
    assert row["provider_initialized_at"] is None
    assert row["retrieved_at"] == "2026-10-05T23:59:59.000000+00:00"
    assert row["source"] == "openmeteo_current_forecast"
    assert row["training_truth_end"] == "2026-10-04"
    assert len(row["training_digest_sha256"]) == 64
    assert json.loads(row["fit_params_json"]) == {"biases": {"gfs": 1.0}}
    assert row["members"] == [
        {"model": "ecmwf", "predicted_high_f": 70.0, "used_in_fit": 1,
         "complete_hour_count": None, "aggregation_basis": "provider_daily_max"},
        {"model": "gfs", "predicted_high_f": 71.0, "used_in_fit": 1,
         "complete_hour_count": None, "aggregation_basis": "provider_daily_max"},
        {"model": "unseen", "predicted_high_f": 80.0, "used_in_fit": 0,
         "complete_hour_count": None, "aggregation_basis": "provider_daily_max"},
    ]
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='nwp_model_forecasts'").fetchone() is None


def test_live_evidence_never_fabricates_retrieval_or_hourly_coverage():
    conn = sqlite3.connect(":memory:")
    _record(conn, inputs={"gfs": 71.0})
    row = export_live_forecasts(conn, before="2026-10-07T00:00:00Z")[0]
    assert row["retrieved_at"] is None
    assert row["source"] == "unverified_input"
    assert row["members"][0]["complete_hour_count"] is None
    assert row["members"][0]["aggregation_basis"] == "unverified_input"


def test_same_clock_with_different_inputs_remains_two_distinct_vintages():
    conn = sqlite3.connect(":memory:")
    a = _record(conn, inputs={"gfs": 71.0})
    b = _record(conn, inputs={"gfs": 72.0})
    assert a != b
    assert len(export_live_forecasts(conn, before="2026-10-07T00:00:00Z")) == 2


def test_evidence_export_is_bounded_and_requires_timezone():
    conn = sqlite3.connect(":memory:")
    assert export_live_forecasts(conn, before="2026-10-07T00:00:00Z") == []
    _record(conn)
    _record(conn, recorded_at="2026-10-06T00:30:00Z")
    rows = export_live_forecasts(conn, before="2026-10-07T00:00:00Z", limit=1)
    assert len(rows) == 1 and rows[0]["recorded_at"] == "2026-10-06T00:30:00.000000+00:00"
    with pytest.raises(ValueError, match="1..10000"):
        export_live_forecasts(conn, before="2026-10-07T00:00:00Z", limit=10001)
    with pytest.raises(ValueError, match="timezone"):
        _record(conn, recorded_at="2026-10-06T00:00:00")


def test_record_rejects_future_retrieval_clock():
    conn = sqlite3.connect(":memory:")
    with pytest.raises(ValueError, match="retrieval"):
        _record(conn, inputs=LiveModelForecast({"gfs": 71.0}, retrieved_at="2026-10-06T00:01:00Z"))


def test_live_fetch_retains_finite_named_members_with_actual_retrieval_clock(monkeypatch):
    import emos_forecast as ef
    monkeypatch.setattr(ef, "_http_get_json", lambda url: {"daily": {
        "time": ["2026-10-06"], "temperature_2m_max_gfs": [71.0],
        "temperature_2m_max_bad": [float("inf")], "temperature_2m_max_malformed": ["invalid"],
    }})
    value = ef.fetch_live_model_forecasts_multi(models=("gfs", "bad", "malformed"))[date(2026, 10, 6)]
    assert value == {"gfs": 71.0}
    assert isinstance(value, LiveModelForecast)
    assert "+00:00" in value.retrieved_at


def test_live_serve_keeps_prior_vintage_after_latest_table_is_overwritten(monkeypatch):
    import emos_forecast as ef
    from test_emos_forecast import _seed

    conn = sqlite3.connect(":memory:")
    _seed(conn)
    target = date(2024, 6, 1)
    monkeypatch.setattr(ef, "_settlement_today", lambda city=ef.DEFAULT_CITY: target)
    live = {"gfs_seamless": 71.0, "ecmwf_ifs025": 70.0, "ncep_nbm_conus": 72.0}
    ef.serve_live_emos(conn, target, live_models=live, fetched_at="2024-06-01T08:10:00Z", recalibrate=False)
    ef.serve_live_emos(conn, target, live_models={k: v + 1 for k, v in live.items()},
                       fetched_at="2024-06-01T08:40:00Z", recalibrate=False)
    assert conn.execute("SELECT COUNT(*) FROM forecast_emos_daily_high WHERE source='live'").fetchone()[0] == 1
    rows = export_live_forecasts(conn, before="2024-06-01T09:00:00Z")
    assert len(rows) == 2
    assert rows[0]["predicted_high_f"] > rows[1]["predicted_high_f"]
    assert len(rows[0]["members"]) == len(rows[1]["members"]) == 3


@pytest.mark.parametrize("table", ["forecast_emos_live_vintages", "nwp_live_forecast_members"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "REPLACE"])
def test_database_enforces_append_only_evidence(table, operation):
    conn = sqlite3.connect(":memory:")
    _record(conn)
    if operation == "UPDATE":
        statement = f"UPDATE {table} SET predicted_high_f=999"
    elif operation == "DELETE":
        statement = f"DELETE FROM {table}"
    else:
        statement = f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(statement)
    assert _record(conn)  # idempotent API writes stay usable after rejected mutation
    row = export_live_forecasts(conn, before="2026-10-07T00:00:00Z")[0]
    assert row["predicted_high_f"] == 71.0
    assert row["members"][1]["predicted_high_f"] == 71.0
