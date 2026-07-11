"""Fetch inland (Concord) hourly temperature history and load it into weather.db
as a second station, to give the SFO daily-high model upstream hot-day signal.

SFO heat is led by inland Central Valley / East Bay warmth: a hot Concord day
precedes a hot SFO day (validated +7F next-day lead, r~0.70). Source is the
Open-Meteo ERA5 historical archive (free, no key, hourly back to 2016, clean UTC
grid, no 2020 gap). Written as a second station_id in the existing `weather`
table via a scoped delete-then-append (the table has no unique constraint, so
re-running must delete its own station first; SFO rows are never touched).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

DB_PATH = "weather.db"
ARCHIVE_API = "https://archive-api.open-meteo.com/v1/archive"
USER_AGENT = "SFO Weather Forecaster student project"

INLAND_STATION_ID = "KCCR"          # Concord / Buchanan Field
INLAND_STATION_NAME = "CONCORD"
INLAND_LAT, INLAND_LON = 37.97, -122.06


def fetch_hourly(lat: float, lon: float, start: str, end: str) -> pd.DataFrame:
    """Hourly temp (F, UTC) for a point, fetched year-by-year to respect fair use."""
    frames = []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        chunk_start = max(f"{year}-01-01", start)
        chunk_end = min(f"{year}-12-31", end)
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": chunk_start,
            "end_date": chunk_end,
            "hourly": "temperature_2m",
            "temperature_unit": "fahrenheit",
            "timezone": "UTC",
        }
        url = ARCHIVE_API + "?" + urlencode(params)
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
        hourly = data.get("hourly") or {}
        frames.append(
            pd.DataFrame(
                {
                    "timestamp": pd.to_datetime(hourly["time"], utc=True),
                    "temp_f": hourly["temperature_2m"],
                }
            )
        )
        print(f"  {chunk_start}..{chunk_end}: {len(frames[-1]):,} hourly rows")
    return pd.concat(frames, ignore_index=True)


def load_inland(df: pd.DataFrame, station_id: str, db_path: str) -> int:
    df = df.dropna(subset=["temp_f"]).copy()
    # Match the SFO text-timestamp format so SQLite date funcs and load_data's
    # to_datetime(utc=True) parse identically.
    df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    df["station_id"] = station_id
    df["station_name"] = INLAND_STATION_NAME
    df["latitude"] = INLAND_LAT
    df["longitude"] = INLAND_LON
    df = df[["timestamp", "station_id", "station_name", "latitude", "longitude", "temp_f"]]

    conn = sqlite3.connect(db_path)
    try:
        # Scoped, idempotent: delete only this inland station, then append. The
        # table has no UNIQUE(station_id, timestamp), so this prevents dup rows
        # on re-run and never touches SFO. Never if_exists="replace" (drops SFO).
        conn.execute("DELETE FROM weather WHERE station_id = ?", (station_id,))
        df.to_sql("weather", conn, if_exists="append", index=False)
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM weather WHERE station_id = ?", (station_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    print(f"wrote {count:,} inland rows for {station_id}")
    return count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--station-id", default=INLAND_STATION_ID)
    parser.add_argument("--lat", type=float, default=INLAND_LAT)
    parser.add_argument("--lon", type=float, default=INLAND_LON)
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default="2026-05-22")
    args = parser.parse_args(argv)

    print(f"fetching inland hourly temps for {args.station_id} "
          f"({args.lat},{args.lon}) {args.start}..{args.end}")
    df = fetch_hourly(args.lat, args.lon, args.start, args.end)
    print(f"fetched {len(df):,} hourly rows; loading into {args.db} ...")
    load_inland(df, args.station_id, args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
