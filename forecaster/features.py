"""Build the hourly feature matrix used by the forecasting models."""

import sqlite3
import pandas as pd
import numpy as np

from settlement_calendar import integer_settlement_high_f, local_standard_date

DB_PATH      = "weather.db"
FEATURES_OUT = "weather_features.csv"
SFO_TZ       = "America/Los_Angeles"
SFO_STATION_ID = "USW00023234"

# Marine-layer / sea-breeze regime is the dominant driver of the SFO daily high
# and exactly where the blend is weakest (the warm/hot tail). SFO heat is an
# offshore, easterly, light-wind regime; cool days are the onshore WSW sea breeze
# through the Golden Gate. ~70 deg (ENE) is the warm offshore bearing; the cool
# onshore flow sits ~180 deg opposite. Approximate -- the model learns the
# response; this just supplies a continuous regime signal.
SFO_OFFSHORE_BEARING_DEG = 70.0
# Dew-point depression (F) at/below which low stratus/fog becomes increasingly
# likely -- a near-saturated, cloudy morning caps the afternoon high.
MARINE_LAYER_DEPRESSION_F = 10.0


def load_data(db_path=DB_PATH, station_id=SFO_STATION_ID):
    """Load raw hourly weather from SQLite on a regular UTC hourly index."""
    conn = sqlite3.connect(db_path)
    df = pd.read_sql(
        """
        SELECT *
        FROM weather
        WHERE station_id = ?
        ORDER BY timestamp
        """,
        conn,
        params=(station_id,),
    )
    conn.close()

    if df.empty:
        raise ValueError(f"no weather rows found for station {station_id}")

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp")

    df = df[~df.index.duplicated(keep="last")]

    # Missing hours become NaN rows so lag features stay aligned.
    df = df.asfreq("h")
    return df


def engineer_features(df):
    """Add time, lag, rolling, and derived weather features.

    naming convention:
      _lag_Nh    -> value from N hours ago
      _roll_X_Nh -> rolling stat X over the past N hours
      target_*   -> prediction target (shifted forward in time)
    """
    df = df.copy()
    local_index = df.index.tz_convert(SFO_TZ)
    local_dates = pd.Series([local_standard_date(ts) for ts in df.index], index=df.index)

    # Calendar features use local time because the temperature cycle is local.
    df["hour"]        = local_index.hour
    df["day_of_week"] = local_index.dayofweek
    df["day_of_year"] = local_index.dayofyear
    df["month"]       = local_index.month
    df["is_weekend"]  = (df["day_of_week"] >= 5).astype(int)

    df["hour_sin"]  = np.sin(2 * np.pi * df["hour"]  / 24)
    df["hour_cos"]  = np.cos(2 * np.pi * df["hour"]  / 24)
    df["month_sin"] = np.sin(2 * np.pi * df["month"] / 12)
    df["month_cos"] = np.cos(2 * np.pi * df["month"] / 12)
    df["doy_sin"]   = np.sin(2 * np.pi * df["day_of_year"] / 365.25)
    df["doy_cos"]   = np.cos(2 * np.pi * df["day_of_year"] / 365.25)

    next_day = pd.to_datetime(local_dates) + pd.Timedelta(days=1)
    df["next_day_of_year"] = next_day.dt.dayofyear
    df["next_doy_sin"] = np.sin(2 * np.pi * df["next_day_of_year"] / 365.25)
    df["next_doy_cos"] = np.cos(2 * np.pi * df["next_day_of_year"] / 365.25)

    # wind direction cyclical encoding (359 degrees is adjacent to 0)
    if "wind_dir" in df.columns:
        df["wind_dir_sin"] = np.sin(np.radians(df["wind_dir"]))
        df["wind_dir_cos"] = np.cos(np.radians(df["wind_dir"]))

    # Temperature memory is the strongest signal in the series.
    for lag in [1, 2, 3, 6, 12, 24, 36, 48, 72, 96, 120, 144, 168]:
        df[f"temp_lag_{lag}h"] = df["temp_f"].shift(lag)

    for lag in [3, 6, 24, 48]:
        df[f"humidity_lag_{lag}h"] = df["humidity"].shift(lag)
        df[f"pressure_lag_{lag}h"] = df["pressure_hpa"].shift(lag)
    df["wind_lag_24h"]     = df["wind_speed_mph"].shift(24)
    df["dewpoint_lag_24h"] = df["dew_point_f"].shift(24)

    # Rolling stats smooth short-term noise.
    for window in [3, 6, 24, 48]:
        df[f"temp_roll_mean_{window}h"] = df["temp_f"].rolling(window).mean()
    df["temp_roll_std_6h"]  = df["temp_f"].rolling(6).std()
    df["temp_roll_std_24h"] = df["temp_f"].rolling(24).std()

    df["humidity_roll_mean_6h"]  = df["humidity"].rolling(6).mean()
    df["humidity_roll_mean_24h"] = df["humidity"].rolling(24).mean()
    df["wind_roll_mean_6h"]      = df["wind_speed_mph"].rolling(6).mean()
    df["pressure_roll_mean_24h"] = df["pressure_hpa"].rolling(24).mean()

    # pressure tendency -- falling pressure precedes storms, rising precedes clearing
    # recomputed from raw data as the NOAA pressure_change_3h column was 49% null
    df["pressure_change_3h"]  = df["pressure_hpa"] - df["pressure_hpa"].shift(3)
    df["pressure_change_6h"]  = df["pressure_hpa"] - df["pressure_hpa"].shift(6)
    df["pressure_change_24h"] = df["pressure_hpa"] - df["pressure_hpa"].shift(24)

    # dew point depression: distance from saturation. 0 -> fog, large -> dry
    if "dew_point_f" in df.columns:
        df["dewpoint_depression"] = df["temp_f"] - df["dew_point_f"]

    # cloud cover converted from sky_oktas (0-8) to percent (0-100)
    if "sky_oktas" in df.columns:
        df["cloud_cover_pct"] = df["sky_oktas"] * 12.5

    # interaction: clear + dry + calm air -> largest diurnal swing
    if "cloud_cover_pct" in df.columns:
        df["clear_dry_calm"] = (
            (100 - df["cloud_cover_pct"]) / 100
            * (100 - df["humidity"]) / 100
            * np.maximum(0, 15 - df["wind_speed_mph"]) / 15
        )

    # --- Marine-layer / sea-breeze regime (dominant driver of SFO highs) ---
    # All point-in-time (current hour or past lags only) -> no target leakage.
    if "wind_dir" in df.columns:
        # +1 = offshore/easterly (warm), -1 = onshore/WSW sea breeze (cool).
        offshore = np.cos(np.radians(df["wind_dir"] - SFO_OFFSHORE_BEARING_DEG))
        df["offshore_flow"] = offshore
        if "wind_speed_mph" in df.columns:
            # Signed by regime, scaled by wind speed: strong offshore -> hottest,
            # strong onshore -> coolest, calm -> near zero either way.
            df["offshore_flow_strength"] = offshore * df["wind_speed_mph"]
            df["offshore_flow_strength_lag_24h"] = df["offshore_flow_strength"].shift(24)

    # Marine layer (stratus/fog): near-saturation + cloudy -> capped, cool high.
    if "dewpoint_depression" in df.columns and "cloud_cover_pct" in df.columns:
        near_saturation = np.maximum(
            0.0, 1.0 - df["dewpoint_depression"] / MARINE_LAYER_DEPRESSION_F
        )
        df["marine_layer_index"] = near_saturation * (df["cloud_cover_pct"] / 100.0)
        df["marine_layer_index_lag_24h"] = df["marine_layer_index"].shift(24)

    # heat momentum: how much warmer than the same hour N days ago
    df["temp_vs_24h_ago"]  = df["temp_f"] - df["temp_lag_24h"]
    df["temp_vs_48h_ago"]  = df["temp_f"] - df["temp_lag_48h"]
    df["temp_vs_168h_ago"] = df["temp_f"] - df["temp_lag_168h"]

    # Same-day summaries only use observations available up to the current hour.
    df["temp_today_high_so_far"] = df["temp_f"].groupby(local_dates).cummax()
    df["temp_today_low_so_far"] = df["temp_f"].groupby(local_dates).cummin()
    df["temp_today_range_so_far"] = (
        df["temp_today_high_so_far"] - df["temp_today_low_so_far"]
    )

    # Rolling daily summaries.
    df["temp_daily_high"]  = df["temp_f"].rolling(24).max()
    df["temp_daily_low"]   = df["temp_f"].rolling(24).min()
    df["temp_daily_range"] = df["temp_daily_high"] - df["temp_daily_low"]
    df["precip_sum_24h"]   = df["precip_mm"].rolling(24).sum()
    df["precip_sum_72h"]   = df["precip_mm"].rolling(72).sum()
    df["temp_max_72h"]     = df["temp_f"].rolling(72).max()
    df["temp_max_168h"]    = df["temp_f"].rolling(168).max()

    # targets
    df["target_temp_next_24h"] = df["temp_f"].shift(-24)
    df["target_temp_next_48h"] = df["temp_f"].shift(-48)

    daily_high = df["temp_f"].groupby(local_dates).max().map(integer_settlement_high_f)
    target_dates = (pd.to_datetime(local_dates) + pd.Timedelta(days=1)).dt.date
    df["target_daily_high_next_day"] = target_dates.map(daily_high)

    # Drop rows missing a target or the longest lag; XGBoost handles other NaNs.
    required = [
        "target_temp_next_24h",
        "target_temp_next_48h",
        "target_daily_high_next_day",
        "temp_lag_168h",
    ]
    before = len(df)
    df = df.dropna(subset=required)
    after  = len(df)
    print(f"  dropped {before - after:,} rows missing required features "
          f"({(before - after) / before:.1%})")

    return df


if __name__ == "__main__":
    print("loading raw data from weather.db...")
    df_raw = load_data()
    print(f"  {len(df_raw):,} rows on hourly grid "
          f"({df_raw.index.min()} to {df_raw.index.max()})")

    print("\nengineering features...")
    df = engineer_features(df_raw)
    print(f"  final shape: {df.shape}")

    df.to_csv(FEATURES_OUT)
    print(f"\nsaved: {FEATURES_OUT}")
