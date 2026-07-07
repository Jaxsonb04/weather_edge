#!/usr/bin/env python3
"""Archive NWS station observations and daily highs into weather.db.

Runs for any configured city (``--cities``); each station's climate day is
computed in its own fixed standard time so the daily high covers the same
window its CLI settlement report does.
"""

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from cities import CITY_BY_STATION, get_city, parse_city_slugs
from settlement_calendar import (
    local_standard_date,
    standard_timezone,
    today_local_standard,
    utc_window_for_local_standard_date,
)

SFO_TZ = ZoneInfo("America/Los_Angeles")
DB_PATH = Path("weather.db")
NWS_STATION_ID = "KSFO"
# GHCN-Daily id is only wired for the legacy SFO history bootstrap; other
# stations do not need one (their history comes from the IEM CLI archive).
NOAA_STATION_ID = "USW00023234"
NOAA_STATION_BY_NWS = {"KSFO": NOAA_STATION_ID}
NWS_API = "https://api.weather.gov"
USER_AGENT = "WeatherEdge forecaster student project"


def _station_tz(station_id):
    city = CITY_BY_STATION.get(station_id)
    if city is None:
        return standard_timezone(-8)
    return city.fixed_standard_timezone()


def read_json_url(url):
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def c_to_f(value):
    return value * 9 / 5 + 32


def temp_to_f(node):
    value = node.get("value") if isinstance(node, dict) else None
    if value is None:
        return None
    unit = str(node.get("unitCode", ""))
    return c_to_f(float(value)) if "degC" in unit else float(value)


def wind_to_mph(node):
    value = node.get("value") if isinstance(node, dict) else None
    if value is None:
        return None
    unit = str(node.get("unitCode", ""))
    value = float(value)
    if "m_s-1" in unit:
        return value * 2.23694
    if "km_h-1" in unit:
        return value * 0.621371
    return value


def parse_timestamp(raw):
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def station_local_date(timestamp, station_id=NWS_STATION_ID):
    return local_standard_date(timestamp, _station_tz(station_id)).isoformat()


def iso_utc(timestamp):
    return timestamp.astimezone(timezone.utc).isoformat()


def utc_window_for_local_date(local_date, station_id=NWS_STATION_ID):
    return utc_window_for_local_standard_date(local_date, _station_tz(station_id))


def init_db(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nws_station_observations (
            station_id TEXT NOT NULL,
            noaa_station_id TEXT,
            observed_at TEXT NOT NULL,
            local_date TEXT NOT NULL,
            temp_f REAL,
            dewpoint_f REAL,
            wind_mph REAL,
            raw_json TEXT NOT NULL,
            inserted_at TEXT NOT NULL,
            PRIMARY KEY (station_id, observed_at)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nws_daily_high_ground_truth (
            station_id TEXT NOT NULL,
            noaa_station_id TEXT,
            local_date TEXT NOT NULL,
            high_f REAL,
            high_observed_at TEXT,
            observation_count INTEGER NOT NULL,
            first_observed_at TEXT,
            last_observed_at TEXT,
            is_complete INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (station_id, local_date)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_nws_obs_local_date
        ON nws_station_observations(station_id, local_date)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_nws_daily_date
        ON nws_daily_high_ground_truth(station_id, local_date)
        """
    )


def fetch_observations_for_date(local_date, station_id=NWS_STATION_ID):
    start_utc, end_utc = utc_window_for_local_date(local_date, station_id)
    params = urlencode(
        {
            "start": start_utc.isoformat().replace("+00:00", "Z"),
            "end": end_utc.isoformat().replace("+00:00", "Z"),
        }
    )
    url = f"{NWS_API}/stations/{station_id}/observations?{params}"
    return read_json_url(url).get("features") or []


def observation_row(feature, station_id=NWS_STATION_ID):
    props = feature.get("properties") or {}
    observed = parse_timestamp(props.get("timestamp"))
    if not observed:
        return None

    return (
        station_id,
        NOAA_STATION_BY_NWS.get(station_id),
        iso_utc(observed),
        station_local_date(observed, station_id),
        temp_to_f(props.get("temperature") or {}),
        temp_to_f(props.get("dewpoint") or {}),
        wind_to_mph(props.get("windSpeed") or {}),
        json.dumps(feature, separators=(",", ":")),
        datetime.now(timezone.utc).isoformat(),
    )


def archive_observations(conn, features, station_id=NWS_STATION_ID):
    rows = [observation_row(feature, station_id) for feature in features]
    rows = [row for row in rows if row]
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO nws_station_observations (
            station_id,
            noaa_station_id,
            observed_at,
            local_date,
            temp_f,
            dewpoint_f,
            wind_mph,
            raw_json,
            inserted_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows), conn.total_changes - before


def update_daily_high(conn, local_date, station_id=NWS_STATION_ID):
    rows = conn.execute(
        """
        SELECT observed_at, temp_f
        FROM nws_station_observations
        WHERE station_id = ?
          AND local_date = ?
          AND temp_f IS NOT NULL
        ORDER BY observed_at
        """,
        (station_id, local_date),
    ).fetchall()
    now_local_date = today_local_standard(tz=_station_tz(station_id)).isoformat()
    is_complete = 1 if local_date < now_local_date else 0

    if not rows:
        conn.execute(
            """
            INSERT INTO nws_daily_high_ground_truth (
                station_id, noaa_station_id, local_date, observation_count,
                is_complete, updated_at, source
            )
            VALUES (?, ?, ?, 0, ?, ?, ?)
            ON CONFLICT(station_id, local_date) DO UPDATE SET
                observation_count = 0,
                is_complete = excluded.is_complete,
                updated_at = excluded.updated_at
            """,
            (
                station_id,
                NOAA_STATION_BY_NWS.get(station_id),
                local_date,
                is_complete,
                datetime.now(timezone.utc).isoformat(),
                "NWS station observations",
            ),
        )
        return 0

    high_observed_at, high_f = max(rows, key=lambda row: row[1])
    conn.execute(
        """
        INSERT INTO nws_daily_high_ground_truth (
            station_id,
            noaa_station_id,
            local_date,
            high_f,
            high_observed_at,
            observation_count,
            first_observed_at,
            last_observed_at,
            is_complete,
            updated_at,
            source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(station_id, local_date) DO UPDATE SET
            high_f = excluded.high_f,
            high_observed_at = excluded.high_observed_at,
            observation_count = excluded.observation_count,
            first_observed_at = excluded.first_observed_at,
            last_observed_at = excluded.last_observed_at,
            is_complete = excluded.is_complete,
            updated_at = excluded.updated_at,
            source = excluded.source
        """,
        (
            station_id,
            NOAA_STATION_BY_NWS.get(station_id),
            local_date,
            round(float(high_f), 2),
            high_observed_at,
            len(rows),
            rows[0][0],
            rows[-1][0],
            is_complete,
            datetime.now(timezone.utc).isoformat(),
            "NWS station observations",
        ),
    )
    return len(rows)


def refresh_ground_truth(days, station_id=NWS_STATION_ID):
    today = today_local_standard(tz=_station_tz(station_id))
    local_dates = [
        (today - timedelta(days=offset)).isoformat()
        for offset in range(days - 1, -1, -1)
    ]

    with sqlite3.connect(DB_PATH) as conn:
        init_db(conn)
        seen = 0
        inserted = 0
        daily_updates = 0
        for local_date in local_dates:
            features = fetch_observations_for_date(local_date, station_id)
            seen_count, inserted_count = archive_observations(conn, features, station_id)
            seen += seen_count
            inserted += inserted_count
            update_daily_high(conn, local_date, station_id)
            daily_updates += 1
        conn.commit()

    return {
        "station_id": station_id,
        "dates": local_dates,
        "observations_seen": seen,
        "observations_inserted": inserted,
        "daily_rows_updated": daily_updates,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=3, help="local days to refresh, ending today")
    parser.add_argument("--station", default=None, help="single NWS station identifier (legacy)")
    parser.add_argument(
        "--cities", default=None, help="'all' or comma slugs; overrides --station"
    )
    args = parser.parse_args()

    if args.cities:
        stations = [city.nws_station_id for city in parse_city_slugs(args.cities)]
    else:
        stations = [args.station or NWS_STATION_ID]

    for station_id in stations:
        # Fail-soft per station: one station's API hiccup must not stall the
        # other fourteen (the freshness watchdog still catches a stale station).
        try:
            result = refresh_ground_truth(max(1, args.days), station_id)
        except OSError as exc:
            print(f"{station_id}: refresh failed ({exc})")
            continue
        print(
            f"fetched {result['observations_seen']} NWS observations "
            f"({result['observations_inserted']} new) for "
            f"{result['station_id']} across {len(result['dates'])} local day(s)"
        )


if __name__ == "__main__":
    main()
