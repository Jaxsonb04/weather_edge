from datetime import date, timedelta
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from cities import CITIES


PILOT = {"KMIA", "KLAX", "KMDW", "KATL", "KNYC", "KDFW", "KSEA", "KPHL",
         "KPHX", "KAUS", "KSFO", "KHOU", "KOKC", "KBOS", "KDEN"}


def test_bias_pilot_keeps_fixed_cohort_in_twenty_city_export(tmp_path, monkeypatch, capsys):
    root = Path(__file__).resolve().parents[2]
    path = root / "scripts" / "evaluate_v7_bias_pilot.py"
    spec = importlib.util.spec_from_file_location("bias_pilot", path)
    pilot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pilot)
    forecasts, truth = [], []
    for city in CITIES:
        for offset in range(49):
            target = (date(2026, 1, 1) + timedelta(days=offset)).isoformat()
            truth.append({"station_id": city.nws_station_id, "local_date": target,
                          "is_final": 1, "max_temperature_f": 70,
                          "fetched_at": "2026-03-01T00:00:00Z"})
            for lead in (1, 2):
                forecasts.append({"station_id": city.nws_station_id, "target_date": target,
                                  "lead_days": lead, "source": "rolling_origin_v2",
                                  "method": "emos_wmean", "predicted_high_f": 71, "sigma_f": 2,
                                  "fetched_at": "2026-03-01T00:00:00Z"})
    export = {"captured_at": "2026-03-02T00:00:00Z",
              "forecast_emos_daily_high": {"truncated": False, "rows": forecasts},
              "cli_settlements": {"truncated": False, "rows": truth},
              "nwp_model_forecasts": {"truncated": False, "rows": []}}
    input_path = tmp_path / "export.json"
    output_path = tmp_path / "pilot.json"
    input_path.write_text(json.dumps(export))
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr(sys, "argv", [str(path), "--repo", str(root), "--export", str(input_path),
                                    "--output", str(output_path)])
    pilot.main()
    result = json.loads(output_path.read_text())
    assert {group["station"] for group in result["by_station_lead"]} == PILOT
    assert len(result["by_station_lead"]) == 30
    assert result["paired_case_count"] == 49 * 30
    assert result["primary_pilot"]["training_cases_per_prediction"] == 45
    assert result["active"] is result["promotion_eligible"] is False
    capsys.readouterr()

    # A new city must never substitute for a missing member of the fixed pilot.
    export["forecast_emos_daily_high"]["rows"] = [
        row for row in forecasts if row["station_id"] != "KSFO"
    ]
    input_path.write_text(json.dumps(export))
    with pytest.raises(ValueError, match="15 stations and both leads required"):
        pilot.main()
