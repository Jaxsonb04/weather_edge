from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
FORECASTER = ROOT / "forecaster"
if str(FORECASTER) not in sys.path:
    sys.path.insert(0, str(FORECASTER))

from research import features


def _raw_with_inland(hours: int = 24 * 16) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=hours, freq="h", tz="UTC")
    hod = idx.hour.to_numpy()
    sfo = 60.0 + 8.0 * np.sin(2 * np.pi * (hod - 9) / 24)
    # Inland warmer, peaks later, plus day-varying drift so maxima differ by day.
    day = np.arange(hours) / 24.0
    inland = 65.0 + 12.0 * np.sin(2 * np.pi * (hod - 12) / 24) + 0.4 * day
    return pd.DataFrame(
        {
            "station_id": features.SFO_STATION_ID,
            "temp_f": sfo,
            "humidity": 70.0,
            "pressure_hpa": 1015.0,
            "wind_speed_mph": 5.0,
            "wind_dir": 70.0,
            "dew_point_f": sfo - 5.0,
            "sky_oktas": 4.0,
            "precip_mm": 0.0,
            "inland_temp": inland,
        },
        index=idx,
    )


def test_inland_features_never_use_future_values():
    raw = _raw_with_inland()
    eng = features.engineer_features(raw)
    raw_inland = raw["inland_temp"]
    tol = 1e-6
    for t in eng.index:
        past_max = raw_inland.loc[:t].max()  # max over timestamps <= t only
        # Trailing/expanding maxima must never exceed the past-only running max.
        assert eng.loc[t, "inland_high_so_far_today"] <= past_max + tol
        assert eng.loc[t, "inland_temp_max_24h"] <= past_max + tol
        # Exact 24h past lag.
        lag_t = t - pd.Timedelta(hours=24)
        if lag_t in raw_inland.index:
            assert abs(eng.loc[t, "inland_temp_lag_24h"] - raw_inland.loc[lag_t]) < tol


def test_inland_high_so_far_resets_each_settlement_day():
    raw = _raw_with_inland()
    eng = features.engineer_features(raw)
    # Within any settlement day the so-far high is non-decreasing; it must drop at
    # a day boundary (otherwise it is leaking across days into a flat cummax).
    local_dates = pd.Series(
        [features.local_standard_date(ts) for ts in eng.index], index=eng.index
    )
    s = eng["inland_high_so_far_today"]
    drops = 0
    prev = None
    for i in range(1, len(s)):
        same_day = local_dates.iloc[i] == local_dates.iloc[i - 1]
        if same_day:
            assert s.iloc[i] >= s.iloc[i - 1] - 1e-6  # non-decreasing within a day
        elif s.iloc[i] < s.iloc[i - 1] - 1e-6:
            drops += 1
    assert drops > 0  # the daily reset actually happens


def test_no_negative_shift_on_inland_columns_in_source():
    src = (FORECASTER / "research/features.py").read_text()
    # The only legitimate negative shifts are the target_* definitions.
    for line in src.splitlines():
        if "inland" in line and ".shift(" in line:
            for m in re.findall(r"\.shift\(\s*(-?\d+)", line):
                assert int(m) >= 1, f"inland feature uses non-positive shift: {line.strip()}"
