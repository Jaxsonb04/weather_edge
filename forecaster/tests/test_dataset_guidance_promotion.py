"""Serving cannot turn retrospective accuracy into live guidance approval."""
import json
from datetime import date

import pytest

import blend_sources as sources
from sfo_kalshi_quant.dataset_research import _load_forecast_feature_candidates, build_dataset_research
from sfo_kalshi_quant.datasets import DatasetStore

TARGET = "2026-01-03"
KEY = "test/guidance/temperature_2m_max/24h"


def _feature(*, station="KSFO", issued="2026-01-02T08:00:00+00:00", value=70.0, lead=24.0):
    return {"source": "test", "model": "guidance", "station_id": station,
            "issued_at": issued, "target_date": TARGET, "valid_time": TARGET,
            "lead_hours": lead, "variable": "temperature_2m_max", "value": value,
            "units": "degF", "raw": {}}


def _research(path, promotion=None):
    payload = {"accuracy_gate": {"candidates": [{"dataset_key": KEY, "decision": "accuracy_candidate"}]}}
    if promotion is not None:
        payload["live_promotion"] = promotion
    path.write_text(json.dumps(payload))
    return path


@pytest.mark.parametrize("promotion", [None, {"decision": "approved", "after_cost_approved": False, "approved_dataset_keys": [KEY]},
                                         {"decision": "approved", "after_cost_approved": True, "approved_dataset_keys": []}])
def test_accuracy_candidate_never_supplies_implicit_after_cost_approval(tmp_path, promotion):
    path = _research(tmp_path / "research.json", promotion)
    assert sources._promoted_dataset_keys(path) == set()


def test_explicit_approval_is_scoped_to_eligible_dataset_keys(tmp_path):
    path = _research(tmp_path / "research.json", {"decision": "approved", "after_cost_approved": True, "approved_dataset_keys": [KEY, "unknown"]})
    assert sources._promoted_dataset_keys(path) == {KEY}


def test_guidance_uses_only_observed_before_target_ksfo_vintage(tmp_path):
    db = tmp_path / "features.db"
    store = DatasetStore(db)
    store.upsert_forecast_features([
        _feature(),
        _feature(station="KATL", issued="2026-01-02T10:00:00+00:00", value=95.0),
        _feature(issued="2026-01-03T09:00:00+00:00", value=99.0),
    ])
    with store.connect() as conn:
        conn.execute("UPDATE dataset_forecast_features SET fetched_at='2026-01-02T11:00:00+00:00'")
    research = _research(tmp_path / "research.json", {"decision": "approved", "after_cost_approved": True, "approved_dataset_keys": [KEY]})
    result = sources.load_promoted_dataset_guidance(TARGET, db, research)
    assert result["highF"] == 70.0
    assert result["components"][0]["issued_at"] == "2026-01-02T08:00:00+00:00"
    assert result["metadata"]["ineligible_point_in_time_count"] == 1


def test_retrospectively_fetched_vintage_cannot_be_live_guidance(tmp_path):
    db = tmp_path / "features.db"
    store = DatasetStore(db)
    store.upsert_forecast_features([_feature()])
    with store.connect() as conn:
        conn.execute("UPDATE dataset_forecast_features SET fetched_at='2026-01-04T09:00:00+00:00'")
    research = _research(tmp_path / "research.json", {"decision": "approved", "after_cost_approved": True, "approved_dataset_keys": [KEY]})
    assert sources.load_promoted_dataset_guidance(TARGET, db, research)["highF"] is None


def test_historical_accuracy_uses_pre_target_issue_not_latest_retrospective_revision(tmp_path):
    db = tmp_path / "features.db"
    store = DatasetStore(db)
    store.upsert_forecast_features([_feature(), _feature(issued="2026-01-03T09:00:00+00:00", value=99.0), _feature(lead=0.0, issued="2026-01-02T07:00:00+00:00", value=80.0)])
    candidates = _load_forecast_feature_candidates(db)
    assert [(item.key, item.rows) for item in candidates] == [(KEY, ((date.fromisoformat(TARGET), 70.0),))]


def test_generated_research_never_fabricates_live_approval(tmp_path):
    result = build_dataset_research(db_path=tmp_path / "missing.db", forecaster_root=tmp_path)
    assert result["live_promotion"]["decision"] == "blocked"
    assert result["live_promotion"]["after_cost_approved"] is False
    assert result["live_promotion"]["approved_dataset_keys"] == []


def test_repeated_historical_vintages_do_not_create_ten_independent_mos_days(tmp_path):
    db = tmp_path / "features.db"
    store = DatasetStore(db)
    rows = []
    for hour in range(12):
        row = _feature(issued=f"2025-12-31T{hour:02d}:00:00+00:00")
        row.update(target_date="2026-01-02", valid_time="2026-01-02")
        rows.append(row)
    store.upsert_forecast_features(rows)
    with store.connect() as conn:
        conn.execute("UPDATE dataset_forecast_features SET fetched_at='2026-01-01T08:00:00+00:00'")
        conn.execute("CREATE TABLE clisfo_settlements (local_date TEXT, max_temperature_f REAL)")
        conn.execute("INSERT INTO clisfo_settlements VALUES ('2026-01-02', 72)")
    corrections = sources._dataset_guidance_corrections(db, TARGET, {KEY})
    assert corrections["metadata"]["source_counts"] == {KEY: 1}
    assert corrections["corrections"] == {}
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM dataset_forecast_features").fetchone()[0] == 12
