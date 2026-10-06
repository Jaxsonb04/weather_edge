from copy import deepcopy
import importlib.util
import math
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "forecast_evidence_audit", Path(__file__).resolve().parents[2] / "scripts/audit_forecast_evidence.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def export():
    return {
        "captured_at": "2026-01-05T12:00:00+00:00",
        "nwp_model_forecasts": {"rows": [], "truncated": False},
        "cli_settlements": {"rows": [{"station_id": "KSFO", "local_date": "2026-01-01", "is_final": 1, "max_temperature_f": 60, "fetched_at": "2026-01-02T18:00:00+00:00"}], "truncated": False},
        "forecast_emos_daily_high": {"rows": [{"station_id": "KSFO", "target_date": "2026-01-01", "lead_days": 1, "source": "rolling_origin_v2", "predicted_high_f": 60, "sigma_f": 2, "fetched_at": "2026-01-04T12:00:00+00:00"}], "truncated": False},
    }


def test_source_separation_scoring_and_vintage_limits():
    data = export()
    other = deepcopy(data["forecast_emos_daily_high"]["rows"][0])
    other.update(source="live", predicted_high_f=150)
    data["forecast_emos_daily_high"]["rows"].append(other)
    result = module.audit(data, source_hash="fixture")
    assert result["overall"]["cases"] == 1
    assert result["overall"]["mae_f"] == 0
    assert result["overall"]["crps_f"] == pytest.approx(2 * (math.sqrt(2) - 1) / math.sqrt(math.pi))
    assert result["overall"]["central_90pct_coverage"] == 1
    assert result["excluded"] == {"other_source": 1}
    assert result["records_stored_by_nominal_serve_clock"] == 0
    assert result["original_vintage_qualification"] == "unestablished"


@pytest.mark.parametrize("change", ["preliminary", "missing", "unfinished", "nan", "zero_sigma"])
def test_ineligible_truth_or_distribution_cannot_enter_metrics(change):
    data = export()
    forecast = data["forecast_emos_daily_high"]["rows"][0]
    if change == "preliminary": data["cli_settlements"]["rows"][0]["is_final"] = 0
    elif change == "missing": data["cli_settlements"]["rows"] = []
    elif change == "unfinished": forecast["target_date"] = "2026-01-05"
    elif change == "nan": forecast["predicted_high_f"] = float("nan")
    else: forecast["sigma_f"] = 0
    assert module.audit(data, source_hash="fixture")["overall"]["cases"] == 0


@pytest.mark.parametrize("change", ["duplicate", "conflicting_truth", "future_forecast", "future_truth", "truncated"])
def test_ambiguous_or_incomplete_export_fails_closed(change):
    data = export()
    if change == "duplicate":
        data["forecast_emos_daily_high"]["rows"].append(deepcopy(data["forecast_emos_daily_high"]["rows"][0]))
    elif change == "conflicting_truth":
        duplicate = deepcopy(data["cli_settlements"]["rows"][0]); duplicate["max_temperature_f"] = 61
        data["cli_settlements"]["rows"].append(duplicate)
    elif change == "future_forecast": data["forecast_emos_daily_high"]["rows"][0]["fetched_at"] = "2026-01-06T00:00:00+00:00"
    elif change == "future_truth": data["cli_settlements"]["rows"][0]["fetched_at"] = "2026-01-06T00:00:00+00:00"
    else: data["nwp_model_forecasts"]["truncated"] = True
    with pytest.raises(ValueError): module.audit(data, source_hash="fixture")
