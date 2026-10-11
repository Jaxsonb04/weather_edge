"""Decision lineage must reference the exact issued forecast, never a later one."""
from dataclasses import replace
from datetime import date
import json
import sqlite3

import pytest

from cities import get_city as weather_city
from live_forecast_evidence import LiveModelForecast, record_live_forecast
from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.forecast import SfoForecasterAdapter, matching_emos_distribution
from sfo_kalshi_quant.forecast import _ISSUED_EMOS_LINEAGE_QUERY
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.store.diagnostics import _forecast_diagnostics_payload


TARGET = date(2026, 10, 12)
STAMP = "2026-10-11T20:00:00Z"


def _record(conn, *, slug="lax", stamp=STAMP, mu=71.0, member_high=71.0,
            retrieved="2026-10-11T19:59:59Z"):
    return record_live_forecast(
        conn, city=weather_city(slug), target_date=TARGET, recorded_at=stamp,
        inputs=LiveModelForecast({"gfs": member_high}, retrieved_at=retrieved),
        used_models={"gfs"}, mu=mu, sigma=2.0, fit_lead_days=1,
        legacy_lead_days=1, method="emos_wmean",
        training=[("2026-10-10", {"gfs": 70.0}, 69.0)],
        fit_params={"biases": {"gfs": 1.0}},
        serve_policy={"bias_recalibration": True},
    )


def _seed(root, *, evidence=True):
    with sqlite3.connect(root / "weather.db") as conn:
        conn.execute("CREATE TABLE forecast_emos_daily_high ("
                     "station_id TEXT,target_date TEXT,predicted_high_f REAL,"
                     "sigma_f REAL,n_models INTEGER,model_spread_f REAL,fetched_at TEXT,"
                     "method TEXT,lead_days INTEGER,source TEXT)")
        conn.execute("INSERT INTO forecast_emos_daily_high VALUES "
                     "('KLAX',?,71,2,1,1.5,?,'emos_wmean',1,'live')",
                     (TARGET.isoformat(), STAMP))
        return _record(conn) if evidence else None


def _snapshot(root):
    return SfoForecasterAdapter(root, city=get_city("lax")).latest_emos_snapshot(TARGET)


def test_exact_vintage_links_without_changing_forecast_or_distribution(tmp_path):
    identity = _seed(tmp_path)
    forecast = _snapshot(tmp_path)
    assert forecast.predicted_high_f == 71.0
    assert forecast.source_spread_f == 1.5
    assert matching_emos_distribution(forecast, enabled=True) == (71.0, 2.0)
    receipt = forecast.raw["issued_forecast_lineage"]
    assert receipt["snapshot_id"] == identity
    assert receipt["status"] == "linked_unqualified"
    assert receipt["content_hash_verified"] is True
    assert receipt["promotion_eligible"] is False
    assert "training_truth_publication_clocks" in receipt["missing_provenance"]
    assert receipt["retrieved_at"] == "2026-10-11T19:59:59.000000+00:00"
    assert all(len(receipt[key]) == 64 for key in (
        "snapshot_id", "training_digest_sha256", "fit_params_sha256",
        "serve_policy_sha256", "members_sha256",
    ))


@pytest.mark.parametrize("other", [
    {"slug": "sfo"}, {"stamp": "2026-10-11T20:01:00Z"}, {"mu": 72.0},
])
def test_other_station_later_or_different_forecast_is_never_substituted(tmp_path, other):
    _seed(tmp_path, evidence=False)
    with sqlite3.connect(tmp_path / "weather.db") as conn:
        _record(conn, **other)
    forecast = _snapshot(tmp_path)
    assert forecast.raw["issued_forecast_lineage"] == {
        "schema_version": 1, "status": "unavailable", "reason": "exact_vintage_missing",
    }
    assert matching_emos_distribution(forecast, enabled=True) == (71.0, 2.0)


def test_same_output_clock_with_different_input_identity_is_ambiguous(tmp_path):
    _seed(tmp_path)
    with sqlite3.connect(tmp_path / "weather.db") as conn:
        _record(conn, member_high=72.0)
    assert _snapshot(tmp_path).raw["issued_forecast_lineage"]["reason"] == "ambiguous_exact_vintage"


def test_missing_tables_keep_legacy_forecast_usable_with_explicit_gap(tmp_path):
    _seed(tmp_path, evidence=False)
    forecast = _snapshot(tmp_path)
    assert forecast.raw["issued_forecast_lineage"]["reason"] == "immutable_vintage_tables_absent"
    assert forecast.predicted_high_f == 71.0


def test_partial_evidence_schema_does_not_change_forecast_admission(tmp_path):
    _seed(tmp_path, evidence=False)
    with sqlite3.connect(tmp_path / "weather.db") as conn:
        conn.execute("CREATE TABLE forecast_emos_live_vintages (snapshot_id TEXT)")
        conn.execute("CREATE TABLE nwp_live_forecast_members (snapshot_id TEXT)")
    forecast = _snapshot(tmp_path)
    assert forecast.raw["issued_forecast_lineage"]["reason"] == "immutable_vintage_schema_incompatible"
    assert matching_emos_distribution(forecast, enabled=True) == (71.0, 2.0)


def test_content_hash_rejects_corrupted_member_evidence(tmp_path):
    _seed(tmp_path)
    with sqlite3.connect(tmp_path / "weather.db") as conn:
        conn.execute("DROP TRIGGER nwp_live_forecast_members_reject_update")
        conn.execute("UPDATE nwp_live_forecast_members SET predicted_high_f=999")
    forecast = _snapshot(tmp_path)
    assert forecast.raw["issued_forecast_lineage"]["reason"] == "vintage_content_hash_mismatch"
    assert forecast.predicted_high_f == 71.0


def test_decision_context_and_forecast_snapshot_retain_receipt_after_weather_refresh(tmp_path):
    identity = _seed(tmp_path)
    forecast = _snapshot(tmp_path)
    store = PaperStore(tmp_path / "paper.db")
    snapshot_id = store.record_forecast(forecast)
    decision = TradeDecision(
        ticker="KXHIGHLAX-26OCT12-B71.5", label="71 to 72", action="BUY_NO", side="NO",
        approved=False, probability=.7, probability_lcb=.6, yes_bid=.3, yes_ask=.32,
        spread=.02, fee_per_contract=.01, cost_per_contract=.71, edge=-.01,
        edge_lcb=-.11, trade_quality_score=40, kelly_fraction=0,
        recommended_contracts=0, expected_profit=0, reasons=["edge below gate"],
    )
    # The intraday-adjusted point can differ; its source identity remains the
    # pre-intraday vintage, with the separate decision point retained as usual.
    adjusted = replace(forecast, predicted_high_f=72.0)
    ids = store.record_decisions(TARGET.isoformat(), [decision], risk_profile="live",
                                 forecast=adjusted, forecast_snapshot_id=snapshot_id)
    with sqlite3.connect(tmp_path / "weather.db") as conn:
        _record(conn, stamp="2026-10-11T20:01:00Z", mu=73.0)
        conn.execute("UPDATE forecast_emos_daily_high SET predicted_high_f=73,"
                     "fetched_at='2026-10-11T20:01:00Z'")
    assert _snapshot(tmp_path).raw["issued_forecast_lineage"]["snapshot_id"] != identity
    with store.connect() as conn:
        row = conn.execute("SELECT c.forecast_json FROM decision_snapshots d "
                           "JOIN scan_context_snapshots c ON c.id=d.scan_context_id "
                           "WHERE d.id=?", (ids[0],)).fetchone()
        saved = json.loads(row[0])
        raw = json.loads(conn.execute("SELECT raw_json FROM forecast_snapshots WHERE id=?",
                                     (snapshot_id,)).fetchone()[0])
    assert saved["predicted_high_f"] == 72.0
    assert saved["issued_forecast_lineage"]["snapshot_id"] == identity
    assert raw["issued_forecast_lineage"] == saved["issued_forecast_lineage"]


def test_lineage_lookup_is_indexed_and_receipt_is_compact(tmp_path):
    _seed(tmp_path)
    with sqlite3.connect(tmp_path / "weather.db") as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN " + _ISSUED_EMOS_LINEAGE_QUERY,
                            ("KLAX", TARGET.isoformat(), "2026-10-11T20:00:00.000000+00:00",
                             71.0, 2.0, "emos_wmean", 1)).fetchall()
    assert any("SEARCH forecast_emos_live_vintages USING INDEX idx_live_vintages_station_target_clock"
               in item[3] for item in plan)
    receipt = _forecast_diagnostics_payload(_snapshot(tmp_path))["issued_forecast_lineage"]
    assert len(json.dumps(receipt).encode()) < 2048


def test_diagnostics_strip_extra_raw_receipt_fields(tmp_path):
    _seed(tmp_path)
    forecast = _snapshot(tmp_path)
    receipt = {**forecast.raw["issued_forecast_lineage"],
               "api_key": "must-not-persist", "raw_provider_payload": "x" * 100_000}
    injected = replace(forecast, raw={**forecast.raw, "issued_forecast_lineage": receipt})
    saved = _forecast_diagnostics_payload(injected)["issued_forecast_lineage"]
    assert "api_key" not in saved and "raw_provider_payload" not in saved
    assert "must-not-persist" not in json.dumps(saved)
    assert len(json.dumps(saved).encode()) < 2048
    assert saved == forecast.raw["issued_forecast_lineage"]


@pytest.mark.parametrize("mutation", [
    {"snapshot_id": "invalid"}, {"training_digest_sha256": "A" * 64},
    {"member_count": 65}, {"training_days": True}, {"station_id": "https://secret"},
    {"target_date": "2026-99-99"}, {"recorded_at": "2026-10-11T20:00:00"},
    {"retrieved_at": "2026-10-11T20:01:00Z"}, {"source": "private-provider-secret"},
    {"promotion_eligible": True}, {"missing_provenance": []},
    {"schema_version": True}, {"status": "ready"},
])
def test_malformed_or_promotion_claiming_receipts_fail_closed(tmp_path, mutation):
    _seed(tmp_path)
    forecast = _snapshot(tmp_path)
    receipt = {**forecast.raw["issued_forecast_lineage"], **mutation}
    injected = replace(forecast, raw={"issued_forecast_lineage": receipt})
    assert _forecast_diagnostics_payload(injected)["issued_forecast_lineage"] == {
        "schema_version": 1, "status": "unavailable", "reason": "invalid_lineage_receipt",
    }


def test_unavailable_receipt_cannot_carry_private_or_unbounded_reason(tmp_path):
    _seed(tmp_path)
    forecast = _snapshot(tmp_path)
    injected = replace(forecast, raw={"issued_forecast_lineage": {
        "schema_version": 1, "status": "unavailable", "reason": "secret" * 10000,
        "private_payload": "must-not-persist",
    }})
    saved = _forecast_diagnostics_payload(injected)["issued_forecast_lineage"]
    assert saved == {"schema_version": 1, "status": "unavailable", "reason": "invalid_lineage_receipt"}


def test_unknown_original_retrieval_remains_an_explicit_decision_gap(tmp_path):
    _seed(tmp_path, evidence=False)
    with sqlite3.connect(tmp_path / "weather.db") as conn:
        _record(conn, retrieved=None)
    receipt = _forecast_diagnostics_payload(_snapshot(tmp_path))["issued_forecast_lineage"]
    assert receipt["status"] == "linked_unqualified"
    assert "retrieved_at" not in receipt
    assert "verified_retrieval_clock" in receipt["missing_provenance"]
    assert receipt["promotion_eligible"] is False
