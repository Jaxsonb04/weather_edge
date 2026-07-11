from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
FORECASTER = ROOT / "forecaster"
if str(FORECASTER) not in sys.path:
    sys.path.insert(0, str(FORECASTER))

from research import features


def _synthetic_raw(
    hours: int = 24 * 14,
    *,
    wind_dir: float = 70.0,
    wind_speed: float = 5.0,
    depression: float = 0.0,
    oktas: float = 8.0,
) -> pd.DataFrame:
    """A clean hourly weather frame shaped like the `weather` table load."""
    idx = pd.date_range("2026-01-01", periods=hours, freq="h", tz="UTC")
    hod = idx.hour.to_numpy()
    temp = 60.0 + 8.0 * np.sin(2 * np.pi * (hod - 9) / 24)  # diurnal swing
    return pd.DataFrame(
        {
            "station_id": features.SFO_STATION_ID,
            "temp_f": temp,
            "humidity": 70.0,
            "pressure_hpa": 1015.0,
            "wind_speed_mph": wind_speed,
            "wind_dir": wind_dir,
            "dew_point_f": temp - depression,
            "sky_oktas": oktas,
            "precip_mm": 0.0,
        },
        index=idx,
    )


def test_offshore_flow_plus_one_for_easterly_minus_one_for_onshore():
    east = features.engineer_features(
        _synthetic_raw(wind_dir=features.SFO_OFFSHORE_BEARING_DEG)
    )
    assert "offshore_flow" in east.columns
    assert np.allclose(east["offshore_flow"], 1.0, atol=1e-9)
    assert np.allclose(east["offshore_flow_strength"], 5.0, atol=1e-9)

    onshore = features.engineer_features(
        _synthetic_raw(wind_dir=features.SFO_OFFSHORE_BEARING_DEG + 180.0)
    )
    assert np.allclose(onshore["offshore_flow"], -1.0, atol=1e-9)


def test_marine_layer_index_high_when_saturated_cloudy_zero_when_dry():
    wet = features.engineer_features(_synthetic_raw(depression=0.0, oktas=8.0))
    assert "marine_layer_index" in wet.columns
    assert np.allclose(wet["marine_layer_index"], 1.0, atol=1e-9)

    dry = features.engineer_features(_synthetic_raw(depression=20.0, oktas=8.0))
    assert np.allclose(dry["marine_layer_index"], 0.0, atol=1e-9)


def test_marine_features_have_no_nan_on_surviving_rows():
    df = features.engineer_features(_synthetic_raw())
    assert len(df) > 0
    for col in ("offshore_flow", "offshore_flow_strength", "marine_layer_index"):
        assert col in df.columns
        assert not df[col].isna().any()
