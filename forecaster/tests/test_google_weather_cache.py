"""Network-free regression tests for google_weather_cache source-MOS path.

The 2026-07 outage: ``latest_scored_blend_rows`` omitted
``station_adjustment_f`` from its SELECT while ``_weighted_sources_for_row``
read it, so once enough clean scored days accumulated the whole refresh
crashed after the (quota-consuming) Google fetch. These tests pin the fixed
query shape and the fail-open behaviour of ``source_mos_corrections``.
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import google_weather_cache as gwc


def _assert_facade_callable_contract(expected):
    actual_names = {
        name
        for name, value in vars(gwc).items()
        if not name.startswith("__") and callable(value)
    }
    assert actual_names == set(expected)

    for name, contract in expected.items():
        value = getattr(gwc, name)
        if contract["kind"] == "imported":
            module_name, attribute = contract["identity"].split(":", 1)
            assert value is getattr(importlib.import_module(module_name), attribute), name
            continue

        try:
            signature = str(inspect.signature(value))
            signature_error = None
        except (TypeError, ValueError) as exc:
            signature = None
            signature_error = type(exc).__name__
        annotations = {
            key: repr(annotation)
            for key, annotation in getattr(value, "__annotations__", {}).items()
        }
        assert signature == contract["signature"], name
        assert signature_error == contract["signature_error"], name
        assert annotations == contract["annotations"], name


def test_cache_responsibilities_live_in_focused_modules():
    expected = {
        "google_api": ("fetch_google_forecast", True),
        "blend_sources": ("build_blend_snapshot", True),
        "blend_learners": ("compute_adaptive_blend_weights", False),
        "blend_archive": ("archive_forecast", True),
    }
    for module_name, (function_name, facade_exported) in expected.items():
        module = importlib.import_module(module_name)
        function = getattr(module, function_name)
        assert function.__module__ == module_name
        assert hasattr(gwc, function_name) is facade_exported
        if facade_exported:
            assert getattr(gwc, function_name) is function

    assert len(Path(gwc.__file__).read_text(encoding="utf-8").splitlines()) < 250


def test_facade_callable_signatures_match_frozen_monolith_inventory():
    inventory_path = Path(__file__).with_name("google_weather_cache_signatures.json")
    expected = json.loads(inventory_path.read_text(encoding="utf-8"))
    _assert_facade_callable_contract(expected)


def test_imported_callable_contracts_do_not_freeze_runtime_signatures():
    inventory_path = Path(__file__).with_name("google_weather_cache_signatures.json")
    contract = json.loads(inventory_path.read_text(encoding="utf-8"))["urlopen"]

    assert contract["kind"] == "imported"
    assert "signature" not in contract
    assert contract["identity"] == "urllib.request:urlopen"


def test_imported_contract_validation_ignores_version_specific_signature(monkeypatch):
    inventory_path = Path(__file__).with_name("google_weather_cache_signatures.json")
    expected = json.loads(inventory_path.read_text(encoding="utf-8"))
    real_signature = inspect.signature

    def simulated_version_signature(value):
        if value is gwc.urlopen:
            raise AssertionError("simulated Python-version signature drift")
        return real_signature(value)

    monkeypatch.setattr(inspect, "signature", simulated_version_signature)
    _assert_facade_callable_contract(expected)


def test_blend_learners_fresh_import_is_dependency_light():
    forecaster = Path(gwc.__file__).resolve().parent
    code = f"""
import json, sys
sys.path.insert(0, {str(forecaster)!r})
import blend_learners
print(json.dumps(sorted(sys.modules)))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    modules = set(json.loads(completed.stdout))
    assert "blend_archive" not in modules
    assert "google_api" not in modules
    assert "city_truth" not in modules
    assert "clisfo" not in modules


def test_pure_adaptive_learner_accepts_rows_without_archive():
    import blend_learners

    rows = []
    start = date(2026, 5, 1)
    for offset in range(18):
        recent = offset >= 12
        rows.append(
            _learner_row(
                (start + timedelta(days=offset)).isoformat(),
                70.0,
                84.0 if recent else 70.0,
                70.0 if recent else 80.0,
            )
        )

    weights, metadata = blend_learners.compute_adaptive_blend_weights(rows)

    assert weights == gwc.BLEND_WEIGHTS
    assert metadata["mode"] == "base"
    assert "did not improve walk-forward holdout" in metadata["reason"]


def test_pure_rolling_bias_learner_accepts_rows_without_archive():
    import blend_learners

    rows = []
    start = date(2026, 4, 1)
    for offset in range(36):
        recent = offset >= 24
        raw = 72.0 if offset % 3 == 0 else 65.0
        actual = raw if recent and raw >= 70.0 else raw + 2.0
        row = _learner_row(
            (start + timedelta(days=offset)).isoformat(),
            actual,
            raw,
            raw,
            raw,
            raw,
        )
        row["predicted_high_f"] = raw
        row["details_json"] = '{"raw_weighted_prediction_f":' + str(raw) + "}"
        rows.append(row)

    table, metadata = blend_learners.compute_rolling_blend_residual_bias(rows)

    assert table == gwc.DISABLED_BIAS_TABLE
    assert metadata["mode"] == "base"
    assert "warm" in metadata["holdout"]["cohort_regressions"]


def test_pure_source_mos_learner_accepts_rows_without_archive():
    import blend_learners

    rows = [
        _learner_row("2026-07-01", 70.0, 69.0, 71.0),
        _learner_row("2026-07-02", 71.0, 70.0, 72.0),
    ]

    corrections, metadata = blend_learners.compute_source_mos_corrections(rows)

    assert corrections == {}
    assert metadata["mode"] == "disabled"
    assert metadata["scored_days"] == 2
    assert "need 30" in metadata["reason"]


def test_disabled_source_mos_does_not_acquire_archive_rows(monkeypatch):
    calls = []
    monkeypatch.setattr(gwc, "ENABLE_SOURCE_MOS_CORRECTION", False)
    monkeypatch.setattr(gwc, "latest_scored_blend_rows", lambda: calls.append(True))
    monkeypatch.delattr(gwc.source_mos_corrections, "_cached", raising=False)

    corrections, metadata = gwc.source_mos_corrections()

    assert calls == []
    assert corrections == {}
    assert metadata["mode"] == "disabled"


def test_disabled_rolling_bias_does_not_acquire_archive_rows(monkeypatch):
    calls = []
    monkeypatch.setattr(gwc, "ENABLE_ROLLING_BLEND_BIAS", False)
    monkeypatch.setattr(gwc, "latest_scored_blend_rows", lambda: calls.append(True))
    monkeypatch.delattr(gwc.rolling_blend_residual_bias, "_cached", raising=False)

    table, metadata = gwc.rolling_blend_residual_bias()

    assert calls == []
    assert table == gwc.DISABLED_BIAS_TABLE
    assert metadata["mode"] == "disabled"


def test_cache_cli_help_is_byte_compatible():
    completed = subprocess.run(
        [sys.executable, gwc.__file__, "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stderr == ""
    assert completed.stdout == (
        "usage: google_weather_cache.py [-h] [--refresh] [--force]\n"
        "\n"
        "options:\n"
        "  -h, --help  show this help message and exit\n"
        "  --refresh   fetch a fresh Google forecast\n"
        "  --force     ignore a valid cache\n"
    )


def test_missing_key_cache_artifact_and_output_are_deterministic(
    tmp_path, monkeypatch, capsys
):
    cache_path = tmp_path / "google_weather_cache.json"
    usage = {
        "daily_event_budget": 260,
        "daily_events": 17,
        "monthly_event_budget": 8000,
        "monthly_events": 611,
    }
    monkeypatch.setattr(gwc, "CACHE_PATH", cache_path)
    monkeypatch.setattr(gwc, "DB_PATH", tmp_path / "weather.db")
    monkeypatch.setattr(gwc, "target_date", lambda: "2026-07-11")
    monkeypatch.setattr(gwc, "load_usage", lambda: dict(usage))
    monkeypatch.setattr(gwc, "api_key", lambda: None)
    monkeypatch.setattr(
        gwc,
        "score_archive",
        lambda: {"daily_rows": 0, "blend_rows": 0, "hourly_rows": 0, "scored": 0},
    )
    monkeypatch.setattr(sys, "argv", ["google_weather_cache.py"])

    gwc.main()

    expected = {
        "available": False,
        "reason": "Google Weather cache unavailable.",
        "target_date": "2026-07-11",
        "max_calls_per_day": 260,
        "calls_used_today": 17,
        "max_google_events_per_month": 8000,
        "google_events_used_month": 611,
        "fetched_at": None,
    }
    assert cache_path.read_text(encoding="utf-8") == json.dumps(expected, indent=2) + "\n"
    assert capsys.readouterr().out == (
        "missing GOOGLE_WEATHER_API_KEY; Google cache not refreshed; scored 0\n"
    )


def test_google_event_reservation_adjustment_is_exact(tmp_path, monkeypatch):
    monkeypatch.setattr(gwc, "USAGE_PATH", tmp_path / "usage.json")
    usage = {
        "date": "2026-07-11",
        "month": "2026-07",
        "daily_events": 17,
        "monthly_events": 611,
        "daily_event_budget": 260,
        "monthly_event_budget": 8000,
        "limit": 260,
    }

    reserved = gwc.reserve_google_weather_events(dict(usage), 5)
    adjusted = gwc.adjust_reserved_google_weather_events(reserved, 5, 3)

    assert adjusted["daily_events"] == 20
    assert adjusted["monthly_events"] == 614
    assert adjusted["calls"] == 20
    assert adjusted["limit"] == 260


def test_negative_actual_adjustment_cannot_refund_pre_refresh_usage():
    usage = {
        "daily_events": 17,
        "monthly_events": 611,
    }

    reserved = gwc.reserve_google_weather_events(dict(usage), 5)
    adjusted = gwc.adjust_reserved_google_weather_events(reserved, 5, -3)

    assert adjusted["daily_events"] == 17
    assert adjusted["monthly_events"] == 611
    assert adjusted["last_refresh_events"] == 0


class _DispatchedFetchFailure(RuntimeError):
    def __init__(self, dispatched_events):
        super().__init__("safe synthetic Google fetch failure")
        self.dispatched_events = dispatched_events


def _run_failed_refresh(tmp_path, monkeypatch, dispatched_events):
    cache_path = tmp_path / "google_weather_cache.json"
    usage_path = tmp_path / "usage.json"
    starting_usage = {
        "date": "2026-07-11",
        "month": "2026-07",
        "daily_events": 17,
        "monthly_events": 611,
        "daily_event_budget": 260,
        "monthly_event_budget": 8000,
        "refreshes": 4,
        "calls": 17,
    }
    monkeypatch.setattr(gwc, "CACHE_PATH", cache_path)
    monkeypatch.setattr(gwc, "USAGE_PATH", usage_path)
    monkeypatch.setattr(gwc, "DB_PATH", tmp_path / "weather.db")
    monkeypatch.setattr(gwc, "target_date", lambda: "2026-07-12")
    monkeypatch.setattr(gwc, "api_key", lambda: "test-secret-key")
    monkeypatch.setattr(gwc, "load_usage", lambda: dict(starting_usage))
    monkeypatch.setattr(gwc, "estimated_google_weather_events_per_refresh", lambda: 5)
    monkeypatch.setattr(gwc, "usage_has_budget", lambda _usage, _events: True)
    monkeypatch.setattr(
        gwc,
        "fetch_google_forecast",
        lambda _key: (_ for _ in ()).throw(
            _DispatchedFetchFailure(dispatched_events)
        ),
    )
    monkeypatch.setattr(
        gwc,
        "score_archive",
        lambda: {"daily_rows": 0, "blend_rows": 0, "hourly_rows": 0, "scored": 0},
    )
    monkeypatch.setattr(sys, "argv", ["google_weather_cache.py", "--refresh"])

    gwc.main()

    return json.loads(usage_path.read_text(encoding="utf-8"))


def test_predispatch_failure_releases_estimated_event_reservation(
    tmp_path, monkeypatch
):
    usage = _run_failed_refresh(tmp_path, monkeypatch, dispatched_events=0)

    assert usage["daily_events"] == 17
    assert usage["monthly_events"] == 611
    assert usage["last_refresh_events"] == 0


def test_postdispatch_failure_keeps_ambiguous_events_consumed(tmp_path, monkeypatch):
    usage = _run_failed_refresh(tmp_path, monkeypatch, dispatched_events=2)

    assert usage["daily_events"] == 19
    assert usage["monthly_events"] == 613
    assert usage["last_refresh_events"] == 2


def _run_successful_refresh(
    tmp_path, monkeypatch, reported_events=None, fetch_result=None
):
    cache_path = tmp_path / "google_weather_cache.json"
    usage_path = tmp_path / "usage.json"
    starting_usage = {
        "date": "2026-07-11",
        "month": "2026-07",
        "daily_events": 17,
        "monthly_events": 611,
        "daily_event_budget": 260,
        "monthly_event_budget": 8000,
        "refreshes": 4,
        "calls": 17,
    }
    summarized_usage = {}
    stats = {"daily_rows": 0, "blend_rows": 0, "hourly_rows": 0, "scored": 0}

    monkeypatch.setattr(gwc, "CACHE_PATH", cache_path)
    monkeypatch.setattr(gwc, "USAGE_PATH", usage_path)
    monkeypatch.setattr(gwc, "DB_PATH", tmp_path / "weather.db")
    monkeypatch.setattr(gwc, "target_date", lambda: "2026-07-12")
    monkeypatch.setattr(gwc, "api_key", lambda: "test-key")
    monkeypatch.setattr(gwc, "load_usage", lambda: dict(starting_usage))
    monkeypatch.setattr(gwc, "estimated_google_weather_events_per_refresh", lambda: 5)
    monkeypatch.setattr(gwc, "HOURLY_LOOKAHEAD_HOURS", 72)
    monkeypatch.setattr(gwc, "ENABLE_GOOGLE_DAILY_FORECAST", True)
    monkeypatch.setattr(gwc, "ENABLE_GOOGLE_CURRENT_CONDITIONS", True)
    monkeypatch.setattr(gwc, "usage_has_budget", lambda _usage, _events: True)
    monkeypatch.setattr(
        gwc,
        "fetch_google_forecast",
        lambda _key: (
            fetch_result
            if fetch_result is not None
            else {"google_weather_events_used": reported_events}
        ),
    )

    def summarize(_raw, target_iso, usage):
        summarized_usage.update(usage)
        return {"available": True, "target_date": target_iso}

    monkeypatch.setattr(gwc, "summarize_forecast", summarize)
    monkeypatch.setattr(gwc, "score_archive", lambda: dict(stats))
    monkeypatch.setattr(gwc, "archive_forecast", lambda *_args: dict(stats))
    monkeypatch.setattr(gwc, "blend_targets", lambda *_args: [])
    monkeypatch.setattr(sys, "argv", ["google_weather_cache.py", "--refresh"])

    gwc.main()

    usage = json.loads(usage_path.read_text(encoding="utf-8"))
    return usage, summarized_usage


def test_success_reconciles_actual_events_before_summarizing(tmp_path, monkeypatch):
    usage, summarized_usage = _run_successful_refresh(
        tmp_path,
        monkeypatch,
        reported_events=3,
    )

    assert usage["daily_events"] == 20
    assert usage["monthly_events"] == 614
    assert usage["last_refresh_events"] == 3
    assert summarized_usage["daily_events"] == 20
    assert summarized_usage["monthly_events"] == 614


@pytest.mark.parametrize("reported_events", [-3, "not-an-integer", 3.5, True])
def test_invalid_success_count_keeps_conservative_reservation(
    reported_events, tmp_path, monkeypatch
):
    usage, summarized_usage = _run_successful_refresh(
        tmp_path,
        monkeypatch,
        reported_events=reported_events,
    )

    assert usage["daily_events"] == 22
    assert usage["monthly_events"] == 616
    assert usage["daily_events"] >= 17
    assert usage["monthly_events"] >= 611
    assert summarized_usage["daily_events"] == 22
    assert summarized_usage["monthly_events"] == 616


def test_excessive_success_count_is_capped_at_known_fetch_maximum(
    tmp_path, monkeypatch
):
    usage, summarized_usage = _run_successful_refresh(
        tmp_path,
        monkeypatch,
        reported_events=999,
    )

    assert usage["daily_events"] == 24
    assert usage["monthly_events"] == 618
    assert usage["daily_events"] >= 17
    assert usage["monthly_events"] >= 611
    assert summarized_usage["daily_events"] == 24
    assert summarized_usage["monthly_events"] == 618


def test_malformed_success_result_keeps_conservative_reservation(
    tmp_path, monkeypatch
):
    usage, summarized_usage = _run_successful_refresh(
        tmp_path,
        monkeypatch,
        fetch_result=[],
    )

    assert usage["daily_events"] == 22
    assert usage["monthly_events"] == 616
    assert usage["daily_events"] >= 17
    assert usage["monthly_events"] >= 611
    assert summarized_usage == {}


def _learner_row(target_date, actual, google, nws, open_meteo=80.0, history=80.0):
    return {
        "target_date": target_date,
        "actual_high_f": actual,
        "predicted_high_f": 72.0,
        "google_high_f": google,
        "nws_high_f": nws,
        "open_meteo_high_f": open_meteo,
        "history_high_f": history,
        "station_adjustment_f": 0.0,
        "details_json": "{}",
        "effective_truth_source": "clisfo",
    }


def test_adaptive_weight_walk_forward_gate_rejects_recent_regression(monkeypatch):
    rows = []
    start = date(2026, 5, 1)
    for offset in range(18):
        recent = offset >= 12
        rows.append(
            _learner_row(
                (start + timedelta(days=offset)).isoformat(),
                70.0,
                84.0 if recent else 70.0,
                70.0 if recent else 80.0,
            )
        )
    monkeypatch.setattr(gwc, "latest_scored_blend_rows", lambda: rows)
    monkeypatch.delattr(gwc.adaptive_blend_weights, "_cached", raising=False)

    weights, metadata = gwc.adaptive_blend_weights()

    assert weights == gwc.BLEND_WEIGHTS
    assert metadata["mode"] == "base"
    assert "did not improve walk-forward holdout" in metadata["reason"]


def test_rolling_bias_walk_forward_gate_rejects_tail_regression(monkeypatch):
    rows = []
    start = date(2026, 4, 1)
    for offset in range(36):
        recent = offset >= 24
        raw = 72.0 if offset % 3 == 0 else 65.0
        # Training suggests +1.5 F everywhere. On the holdout that still helps
        # normal days overall, but worsens already-perfect warm tail days.
        actual = raw if recent and raw >= 70.0 else raw + 2.0
        row = _learner_row(
            (start + timedelta(days=offset)).isoformat(),
            actual,
            raw,
            raw,
            raw,
            raw,
        )
        row["predicted_high_f"] = raw
        row["details_json"] = '{"raw_weighted_prediction_f":' + str(raw) + "}"
        rows.append(row)
    monkeypatch.setattr(gwc, "latest_scored_blend_rows", lambda: rows)
    monkeypatch.delattr(gwc.rolling_blend_residual_bias, "_cached", raising=False)

    table, metadata = gwc.rolling_blend_residual_bias()

    assert table == gwc.DISABLED_BIAS_TABLE
    assert metadata["mode"] == "base"
    assert "regressed cohort(s)" in metadata["reason"]
    assert "warm" in metadata["holdout"]["cohort_regressions"]


@contextmanager
def _tmp_weather_db(n_days=40):
    """chdir into a temp dir holding a weather.db with clean scored blend rows."""
    prev_cwd = os.getcwd()
    prev_cache = getattr(gwc.source_mos_corrections, "_cached", None)
    gwc.source_mos_corrections._cached = None
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            with sqlite3.connect("weather.db") as conn:
                gwc.create_blend_archive_table(conn)
                start = date(2026, 5, 1)
                for i in range(n_days):
                    target = start + timedelta(days=i)
                    fetched = datetime(
                        target.year, target.month, target.day, 18, 0, tzinfo=timezone.utc
                    ) - timedelta(days=1)
                    conn.execute(
                        """
                        INSERT INTO forecast_blend_daily_high (
                            fetched_at, target_date, method, predicted_high_f,
                            google_high_f, nws_high_f, open_meteo_high_f,
                            history_high_f, station_adjustment_f, details_json,
                            actual_high_f, abs_error_f
                        ) VALUES (?, ?, 'blend', ?, ?, ?, ?, ?, ?, '{}', ?, ?)
                        """,
                        (
                            fetched.isoformat(),
                            target.isoformat(),
                            65.0 + (i % 5),
                            64.0 + (i % 5),
                            66.0 + (i % 5),
                            65.5 + (i % 5),
                            63.0 + (i % 5),
                            0.5,
                            67.0 + (i % 5),
                            2.0,
                        ),
                    )
                conn.commit()
            yield
        finally:
            os.chdir(prev_cwd)
            gwc.source_mos_corrections._cached = prev_cache


def test_scored_blend_rows_include_station_adjustment():
    with _tmp_weather_db():
        rows = gwc.latest_scored_blend_rows()
        assert rows, "expected clean scored blend rows"
        for row in rows:
            assert "station_adjustment_f" in row.keys()
        # The crash site: must be readable for every eligible row.
        pred = gwc._weighted_sources_for_row(rows[0], corrections=None)
        assert pred is not None


def test_source_mos_corrections_do_not_crash_on_real_query_rows():
    with _tmp_weather_db():
        corrections, metadata = gwc.source_mos_corrections()
        assert isinstance(corrections, dict)
        # Whatever the holdout verdict, an internal failure must not be the reason.
        assert "failed" not in metadata.get("reason", "")


def test_source_mos_corrections_fail_open_on_internal_error():
    prev_cache = getattr(gwc.source_mos_corrections, "_cached", None)
    original = gwc.latest_scored_blend_rows

    def _boom():
        raise RuntimeError("synthetic failure")

    gwc.source_mos_corrections._cached = None
    gwc.latest_scored_blend_rows = _boom
    try:
        corrections, metadata = gwc.source_mos_corrections()
        assert corrections == {}
        assert metadata["mode"] == "disabled"
        assert "failed" in metadata["reason"]
    finally:
        gwc.latest_scored_blend_rows = original
        gwc.source_mos_corrections._cached = prev_cache


def test_missing_station_adjustment_column_degrades_to_null():
    """Older DBs without the column must still be queryable (NULL adjustment)."""
    prev_cwd = os.getcwd()
    prev_cache = getattr(gwc.source_mos_corrections, "_cached", None)
    gwc.source_mos_corrections._cached = None
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            with sqlite3.connect("weather.db") as conn:
                conn.execute(
                    """
                    CREATE TABLE forecast_blend_daily_high (
                        fetched_at TEXT NOT NULL,
                        target_date TEXT NOT NULL,
                        method TEXT NOT NULL,
                        predicted_high_f REAL NOT NULL,
                        google_high_f REAL,
                        nws_high_f REAL,
                        open_meteo_high_f REAL,
                        history_high_f REAL,
                        details_json TEXT,
                        actual_high_f REAL,
                        abs_error_f REAL,
                        PRIMARY KEY (fetched_at, target_date)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO forecast_blend_daily_high VALUES (
                        '2026-05-01T18:00:00+00:00', '2026-05-02', 'blend',
                        65.0, 64.0, 66.0, 65.5, 63.0, '{}', 67.0, 2.0
                    )
                    """
                )
                conn.commit()
            rows = gwc.latest_scored_blend_rows()
            assert rows
            assert rows[0]["station_adjustment_f"] is None
            pred = gwc._weighted_sources_for_row(rows[0], corrections=None)
            assert pred is not None
        finally:
            os.chdir(prev_cwd)
            gwc.source_mos_corrections._cached = prev_cache


def test_sfo_cli_refresh_marks_current_settlement_day_preliminary(monkeypatch):
    conn = sqlite3.connect(":memory:")
    current = date(2026, 7, 10)
    observed = datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)
    import clisfo

    monkeypatch.setattr(
        gwc,
        "fetch_recent_cli_reports",
        lambda site, issuedby: {
            current: clisfo.CliReport(current, 68, "VALID AS OF 5 PM LOCAL TIME", True)
        },
    )
    monkeypatch.setattr(gwc.city_truth, "_utcnow", lambda: observed)

    assert gwc.refresh_clisfo_settlements(conn) == 1

    assert conn.execute("SELECT is_final FROM cli_settlements").fetchone()[0] == 0
    assert gwc.clisfo_high_for(conn, current.isoformat()) is None


def test_scoring_does_not_use_fallback_when_preliminary_cli_row_exists():
    conn = sqlite3.connect(":memory:")
    gwc.create_blend_archive_table(conn)
    gwc.city_truth.ensure_schema(conn)
    conn.execute(
        "INSERT INTO forecast_blend_daily_high "
        "(fetched_at, target_date, method, predicted_high_f) "
        "VALUES ('2026-07-09T18:00:00+00:00', '2026-07-10', 'blend', 70)"
    )
    conn.execute(
        "CREATE TABLE nws_daily_high_ground_truth "
        "(station_id TEXT, local_date TEXT, high_f REAL, is_complete INTEGER)"
    )
    conn.execute(
        "INSERT INTO nws_daily_high_ground_truth VALUES ('KSFO', '2026-07-10', 68, 1)"
    )
    gwc.city_truth.upsert_settlement(
        conn, "KSFO", "2026-07-10", 68, is_final=False
    )

    assert gwc.update_scores_for_table(conn, "forecast_blend_daily_high") == 0
    assert conn.execute(
        "SELECT actual_high_f FROM forecast_blend_daily_high"
    ).fetchone()[0] is None

    gwc.city_truth.upsert_settlement(
        conn, "KSFO", "2026-07-10", 71, is_final=True
    )
    assert gwc.update_scores_for_table(conn, "forecast_blend_daily_high") == 1
    assert conn.execute(
        "SELECT actual_high_f, truth_source FROM forecast_blend_daily_high"
    ).fetchone() == (71.0, "clisfo")


def test_adaptive_training_does_not_fallback_to_embedded_actual_for_preliminary_cli():
    prev_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            with sqlite3.connect("weather.db") as conn:
                gwc.create_blend_archive_table(conn)
                gwc.city_truth.ensure_schema(conn)
                conn.execute(
                    "INSERT INTO forecast_blend_daily_high "
                    "(fetched_at, target_date, method, predicted_high_f, details_json, "
                    "actual_high_f, abs_error_f, truth_source) VALUES "
                    "('2026-07-09T18:00:00+00:00', '2026-07-10', 'blend', 70, '{}', "
                    "68, 2, 'nws_daily')"
                )
                gwc.city_truth.upsert_settlement(
                    conn, "KSFO", "2026-07-10", 71, is_final=False
                )

            assert gwc.latest_scored_blend_rows() == []

            with sqlite3.connect("weather.db") as conn:
                gwc.city_truth.upsert_settlement(
                    conn, "KSFO", "2026-07-10", 71, is_final=True
                )
            rows = gwc.latest_scored_blend_rows()
            assert len(rows) == 1
            assert rows[0]["actual_high_f"] == 71.0
        finally:
            os.chdir(prev_cwd)
