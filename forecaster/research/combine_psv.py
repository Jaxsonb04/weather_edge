"""Combine NOAA pipe-delimited weather files into one hourly CSV."""
import argparse
import glob
import sys
import pandas as pd
from pathlib import Path

SFO_STATION_ID = "USW00023234"

KEEP_COLS = {
    "STATION":                  "station_id",
    "Station_name":             "station_name",
    "DATE":                     "timestamp_raw",
    "Year":                     "year",
    "Month":                    "month",
    "Day":                      "day",
    "Hour":                     "hour",
    "Minute":                   "minute",
    "LATITUDE":                 "latitude",
    "LONGITUDE":                "longitude",
    "ELEVATION":                "elevation",
    "temperature":              "temp_c",
    "dew_point_temperature":    "dew_point_c",
    "sea_level_pressure":       "pressure_hpa",
    "wind_direction":           "wind_dir",
    "wind_speed":               "wind_speed_ms",
    "wind_gust":                "wind_gust_ms",
    "precipitation":            "precip_mm",
    "relative_humidity":        "humidity",
    "visibility":               "visibility_km",
    "sky_condition":            "sky_condition",
    "ceiling_height":           "ceiling_height_m",
    "wet_bulb_temperature":     "wet_bulb_c",
    "pressure_3hr_change":      "pressure_change_3h",
    "snow_depth":               "snow_depth_mm",
}


def parse_args():
    p = argparse.ArgumentParser(description="Combine GHCNh PSV files to CSV")
    p.add_argument("--dir", default=".", help="folder containing .psv files")
    p.add_argument("--out", default="combined_weather.csv", help="output csv path")
    p.add_argument(
        "--station",
        default=SFO_STATION_ID,
        help="NOAA station id to keep; default is SFO Airport",
    )
    return p.parse_args()


def load_psv(path: str, station_id: str) -> pd.DataFrame:
    """Load one PSV file and keep the columns used downstream."""
    df = pd.read_csv(path, sep="|", low_memory=False, dtype=str)

    present = {raw: nice for raw, nice in KEEP_COLS.items() if raw in df.columns}
    missing_key = set(KEEP_COLS) - set(present)
    if missing_key:
        print(f"    (columns absent in this file: {sorted(missing_key)})")

    df = df[list(present.keys())].rename(columns=present)
    if "station_id" in df.columns:
        df = df[df["station_id"] == station_id].copy()
    df["source_file"] = Path(path).name
    return df


def to_numeric_col(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def clean(df: pd.DataFrame) -> pd.DataFrame:
    df["timestamp"] = pd.to_datetime(df["timestamp_raw"], errors="coerce", utc=True)

    num_cols = [
        "temp_c", "dew_point_c", "pressure_hpa", "wind_dir",
        "wind_speed_ms", "wind_gust_ms", "precip_mm", "humidity",
        "visibility_km", "ceiling_height_m", "wet_bulb_c",
        "pressure_change_3h", "snow_depth_mm",
        "latitude", "longitude", "elevation",
    ]
    for col in num_cols:
        if col in df.columns:
            df[col] = to_numeric_col(df[col])

    df["temp_f"]         = df["temp_c"]        * 9/5 + 32
    df["dew_point_f"]    = df["dew_point_c"]   * 9/5 + 32
    df["wind_speed_mph"] = df["wind_speed_ms"] * 2.23694
    if "wind_gust_ms" in df.columns:
        df["wind_gust_mph"] = df["wind_gust_ms"] * 2.23694

    # sky_condition is a coded string like "02;;" -- pull numeric oktas (0-9)
    if "sky_condition" in df.columns:
        df["sky_oktas"] = to_numeric_col(
            df["sky_condition"].str.extract(r"^(\d+)")[0]
        )

    return df


def resample_hourly(df: pd.DataFrame) -> pd.DataFrame:
    """Floor to the hour; average weather readings and sum precipitation."""
    df = df.copy()
    df["timestamp"] = df["timestamp"].dt.floor("h")

    agg = {}
    mean_cols = [
        "temp_c", "temp_f", "dew_point_c", "dew_point_f",
        "pressure_hpa", "wind_dir", "wind_speed_ms", "wind_speed_mph",
        "wind_gust_ms", "wind_gust_mph", "humidity",
        "visibility_km", "ceiling_height_m", "wet_bulb_c",
        "pressure_change_3h", "snow_depth_mm", "sky_oktas",
    ]
    sum_cols   = ["precip_mm"]
    first_cols = ["station_id", "station_name", "latitude", "longitude",
                  "elevation", "sky_condition", "source_file"]

    for col in mean_cols:
        if col in df.columns:
            agg[col] = "mean"
    for col in sum_cols:
        if col in df.columns:
            agg[col] = "sum"
    for col in first_cols:
        if col in df.columns:
            agg[col] = "first"

    df = df.set_index("timestamp").groupby("timestamp").agg(agg).reset_index()
    return df


def main():
    args   = parse_args()
    folder = Path(args.dir).expanduser().resolve()

    psv_files = sorted(glob.glob(str(folder / "*.psv")) +
                       glob.glob(str(folder / "*.PSV")))
    if not psv_files:
        sys.exit(f"no .psv files found in {folder}")

    print(f"found {len(psv_files)} psv file(s) in {folder}\n")

    frames = []
    for path in psv_files:
        print(f"  loading {Path(path).name} ...", end=" ", flush=True)
        try:
            df = load_psv(path, args.station)
            if df.empty:
                print(f"0 rows for station {args.station}")
                continue
            df = clean(df)
            df = resample_hourly(df)
            print(f"{len(df):,} hourly rows")
            frames.append(df)
        except Exception as e:
            print(f"failed: {e}")

    if not frames:
        sys.exit("no data loaded.")

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values("timestamp").drop_duplicates(
        subset=["station_id", "timestamp"]
    )
    print(f"\ncombined + deduped for {args.station}: {len(combined):,} hourly rows")

    ordered = [
        "timestamp", "station_id", "station_name",
        "latitude", "longitude", "elevation",
        "temp_c", "temp_f", "dew_point_c", "dew_point_f",
        "humidity", "pressure_hpa", "pressure_change_3h",
        "wind_dir", "wind_speed_ms", "wind_speed_mph",
        "wind_gust_ms", "wind_gust_mph",
        "precip_mm", "snow_depth_mm",
        "visibility_km", "ceiling_height_m",
        "sky_oktas", "sky_condition",
        "wet_bulb_c",
        "source_file",
    ]
    final_cols = [c for c in ordered if c in combined.columns]
    combined   = combined[final_cols]

    out_path = Path(args.out)
    combined.to_csv(out_path, index=False)

    print(f"\nsummary")
    print(f"  output:     {out_path.resolve()}")
    print(f"  rows:       {len(combined):,}")
    print(f"  columns:    {len(combined.columns)}")
    ts = combined["timestamp"]
    print(f"  date range: {ts.min()} to {ts.max()}")
    span_h = int((ts.max() - ts.min()).total_seconds() / 3600) + 1
    print(f"  expected hours: {span_h:,}   actual: {len(combined):,}   "
          f"gap: {span_h - len(combined):,} missing hours")
    print(f"\n  temp (f):  min={combined['temp_f'].min():.1f}  "
          f"max={combined['temp_f'].max():.1f}  "
          f"mean={combined['temp_f'].mean():.1f}")
    print(f"\n  missing values (key columns):")
    key = ["temp_f", "dew_point_f", "humidity", "pressure_hpa",
           "wind_speed_mph", "precip_mm", "visibility_km"]
    for col in key:
        if col in combined.columns:
            n   = combined[col].isna().sum()
            pct = n / len(combined) * 100
            print(f"    {col:<20s} {n:>5,}  ({pct:.1f}%)")

    print(f"\nsaved to {out_path}")


if __name__ == "__main__":
    main()
