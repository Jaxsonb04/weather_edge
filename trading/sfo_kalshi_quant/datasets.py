from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from io import StringIO, TextIOWrapper
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import SERIES_TICKER
from .settlement_day import IANA_FIXED_PST, PACIFIC_STANDARD_TZ
from .kalshi import KalshiPublicClient


KSFO_ISD_STATION = "72494023234"
KSFO_ASOS_STATION = "SFO"
KSFO_LATITUDE = 37.62
KSFO_LONGITUDE = -122.38

OPEN_METEO_PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
OPEN_METEO_HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
NOAA_GLOBAL_HOURLY_BASE_URL = "https://www.ncei.noaa.gov/data/global-hourly/access"
IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
NOAA_NOMADS_BASE_URL = "https://nomads.ncep.noaa.gov/pub/data/nccf/com"
OPEN_METEO_PREVIOUS_RUN_RESEARCH_MODELS = (
    "ncep_nbm_conus",
    "gfs_hrrr",
    "gfs_global",
    "ecmwf_ifs",
)
OPEN_METEO_PREVIOUS_RUN_MODEL_PRESETS = {
    "research_core": OPEN_METEO_PREVIOUS_RUN_RESEARCH_MODELS,
}
NOAA_GUIDANCE_CYCLES_6H = (0, 6, 12, 18)
NOAA_LAMP_CYCLES_3H = (0, 3, 6, 9, 12, 15, 18, 21)
NOAA_GUIDANCE_UNAVAILABLE_HTTP_CODES = {403, 404}


DATASET_SCHEMA = """
CREATE TABLE IF NOT EXISTS dataset_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    rows_written INTEGER NOT NULL DEFAULT 0,
    params_json TEXT NOT NULL,
    message TEXT
);

CREATE TABLE IF NOT EXISTS dataset_station_observations (
    source TEXT NOT NULL,
    station_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    local_date TEXT,
    temp_f REAL,
    dewpoint_f REAL,
    relative_humidity REAL,
    wind_speed_kt REAL,
    wind_direction_deg REAL,
    cloud_cover_fraction REAL,
    altimeter_in REAL,
    sea_level_pressure_hpa REAL,
    source_url TEXT,
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (source, station_id, observed_at)
);

CREATE TABLE IF NOT EXISTS dataset_forecast_features (
    source TEXT NOT NULL,
    model TEXT NOT NULL,
    station_id TEXT NOT NULL DEFAULT 'KSFO',
    issued_at TEXT NOT NULL,
    target_date TEXT NOT NULL,
    valid_time TEXT NOT NULL,
    lead_hours REAL,
    latitude REAL,
    longitude REAL,
    variable TEXT NOT NULL,
    value REAL NOT NULL,
    units TEXT,
    source_url TEXT,
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (source, model, station_id, issued_at, target_date, valid_time, variable)
);

CREATE TABLE IF NOT EXISTS dataset_kalshi_markets (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT NOT NULL,
    target_date TEXT,
    market_status TEXT,
    result TEXT,
    expiration_value REAL,
    open_time TEXT,
    close_time TEXT,
    settlement_ts TEXT,
    settlement_value_dollars REAL,
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    liquidity_dollars REAL,
    volume REAL,
    volume_24h REAL,
    open_interest REAL,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_kalshi_candles (
    ticker TEXT NOT NULL,
    end_period_ts INTEGER NOT NULL,
    period_interval INTEGER NOT NULL,
    yes_bid_open REAL,
    yes_bid_low REAL,
    yes_bid_high REAL,
    yes_bid_close REAL,
    yes_ask_open REAL,
    yes_ask_low REAL,
    yes_ask_high REAL,
    yes_ask_close REAL,
    price_open REAL,
    price_low REAL,
    price_high REAL,
    price_close REAL,
    price_mean REAL,
    price_previous REAL,
    volume REAL,
    open_interest REAL,
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (ticker, end_period_ts, period_interval)
);

CREATE TABLE IF NOT EXISTS dataset_kalshi_trades (
    trade_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    created_time TEXT NOT NULL,
    count REAL,
    yes_price REAL,
    no_price REAL,
    is_block_trade INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_kalshi_orderbook_events (
    event_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    sequence INTEGER,
    yes_bids_json TEXT NOT NULL,
    no_bids_json TEXT NOT NULL,
    source TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dataset_orderbook_ticker_time
ON dataset_kalshi_orderbook_events(ticker, observed_at);
"""


@dataclass(frozen=True)
class DatasetResult:
    source: str
    rows_written: int
    detail: str


class DatasetStore:
    def __init__(self, db_path: Path, *, init: bool = True) -> None:
        self.db_path = Path(db_path)
        if init:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.init()

    def connect(self) -> sqlite3.Connection:
        # The nightly dataset backfill writes the SAME paper_trading.db that the
        # 24/7 paper scan (q5min), monitor (q2min), settle, and strategy-lab
        # refresh hit. On the default 5s rollback-journal connection a lock
        # collision fails fast with "database is locked" -- aborting a dataset
        # source or, worse, dropping a monitor/scan tick. Mirror PaperStore.connect
        # so every writer shares the same WAL + busy_timeout regime and waits
        # instead of failing.
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.DatabaseError:
            pass
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(DATASET_SCHEMA)
            self._migrate_forecast_features_station_key(conn)
            self._ensure_column(conn, "dataset_station_observations", "cloud_cover_fraction", "REAL")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        if column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @staticmethod
    def _migrate_forecast_features_station_key(conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(dataset_forecast_features)")}
        if "station_id" in columns:
            return
        conn.execute("ALTER TABLE dataset_forecast_features RENAME TO dataset_forecast_features_legacy")
        conn.execute(
            """
            CREATE TABLE dataset_forecast_features (
                source TEXT NOT NULL, model TEXT NOT NULL,
                station_id TEXT NOT NULL DEFAULT 'KSFO', issued_at TEXT NOT NULL,
                target_date TEXT NOT NULL, valid_time TEXT NOT NULL, lead_hours REAL,
                latitude REAL, longitude REAL, variable TEXT NOT NULL, value REAL NOT NULL,
                units TEXT, source_url TEXT, raw_json TEXT NOT NULL, fetched_at TEXT NOT NULL,
                PRIMARY KEY (source, model, station_id, issued_at, target_date, valid_time, variable)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO dataset_forecast_features (
                source, model, station_id, issued_at, target_date, valid_time,
                lead_hours, latitude, longitude, variable, value, units,
                source_url, raw_json, fetched_at
            )
            SELECT source, model, 'KSFO', issued_at, target_date, valid_time,
                   lead_hours, latitude, longitude, variable, value, units,
                   source_url, raw_json, fetched_at
            FROM dataset_forecast_features_legacy
            """
        )
        conn.execute("DROP TABLE dataset_forecast_features_legacy")

    def start_run(self, source: str, params: dict[str, Any]) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO dataset_runs (source, started_at, status, params_json)
                VALUES (?, ?, 'running', ?)
                """,
                (source, _now(), json.dumps(params, sort_keys=True)),
            )
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, *, status: str, rows_written: int, message: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE dataset_runs
                SET completed_at = ?, status = ?, rows_written = ?, message = ?
                WHERE id = ?
                """,
                (_now(), status, rows_written, message, run_id),
            )

    def upsert_station_observations(self, rows: Iterable[dict[str, Any]]) -> int:
        payload = list(rows)
        if not payload:
            return 0
        fetched_at = _now()
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO dataset_station_observations (
                    source, station_id, observed_at, local_date, temp_f, dewpoint_f,
                    relative_humidity, wind_speed_kt, wind_direction_deg,
                    cloud_cover_fraction, altimeter_in, sea_level_pressure_hpa,
                    source_url, raw_json, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, station_id, observed_at) DO UPDATE SET
                    local_date = excluded.local_date,
                    temp_f = excluded.temp_f,
                    dewpoint_f = excluded.dewpoint_f,
                    relative_humidity = excluded.relative_humidity,
                    wind_speed_kt = excluded.wind_speed_kt,
                    wind_direction_deg = excluded.wind_direction_deg,
                    cloud_cover_fraction = excluded.cloud_cover_fraction,
                    altimeter_in = excluded.altimeter_in,
                    sea_level_pressure_hpa = excluded.sea_level_pressure_hpa,
                    source_url = excluded.source_url,
                    raw_json = excluded.raw_json,
                    fetched_at = excluded.fetched_at
                """,
                [
                    (
                        row["source"],
                        row["station_id"],
                        row["observed_at"],
                        row.get("local_date"),
                        row.get("temp_f"),
                        row.get("dewpoint_f"),
                        row.get("relative_humidity"),
                        row.get("wind_speed_kt"),
                        row.get("wind_direction_deg"),
                        row.get("cloud_cover_fraction"),
                        row.get("altimeter_in"),
                        row.get("sea_level_pressure_hpa"),
                        row.get("source_url"),
                        json.dumps(row.get("raw", {}), sort_keys=True),
                        fetched_at,
                    )
                    for row in payload
                ],
            )
            return conn.total_changes - before

    def upsert_forecast_features(self, rows: Iterable[dict[str, Any]]) -> int:
        payload = list(rows)
        if not payload:
            return 0
        fetched_at = _now()
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO dataset_forecast_features (
                    source, model, station_id, issued_at, target_date, valid_time, lead_hours,
                    latitude, longitude, variable, value, units, source_url, raw_json, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, model, station_id, issued_at, target_date, valid_time, variable) DO UPDATE SET
                    lead_hours = excluded.lead_hours,
                    latitude = excluded.latitude,
                    longitude = excluded.longitude,
                    value = excluded.value,
                    units = excluded.units,
                    source_url = excluded.source_url,
                    raw_json = excluded.raw_json,
                    fetched_at = excluded.fetched_at
                """,
                [
                    (
                        row["source"],
                        row["model"],
                        row.get("station_id", "KSFO"),
                        row["issued_at"],
                        row["target_date"],
                        row["valid_time"],
                        row.get("lead_hours"),
                        row.get("latitude"),
                        row.get("longitude"),
                        row["variable"],
                        row["value"],
                        row.get("units"),
                        row.get("source_url"),
                        json.dumps(row.get("raw", {}), sort_keys=True),
                        fetched_at,
                    )
                    for row in payload
                ],
            )
            return conn.total_changes - before

    def upsert_kalshi_markets(self, rows: Iterable[dict[str, Any]]) -> int:
        payload = list(rows)
        if not payload:
            return 0
        fetched_at = _now()
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO dataset_kalshi_markets (
                    ticker, event_ticker, target_date, market_status, result,
                    expiration_value, open_time, close_time, settlement_ts,
                    settlement_value_dollars, yes_bid, yes_ask, no_bid, no_ask,
                    liquidity_dollars, volume, volume_24h, open_interest,
                    strike_type, floor_strike, cap_strike, raw_json, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    event_ticker = excluded.event_ticker,
                    target_date = excluded.target_date,
                    market_status = excluded.market_status,
                    result = excluded.result,
                    expiration_value = excluded.expiration_value,
                    open_time = excluded.open_time,
                    close_time = excluded.close_time,
                    settlement_ts = excluded.settlement_ts,
                    settlement_value_dollars = excluded.settlement_value_dollars,
                    yes_bid = excluded.yes_bid,
                    yes_ask = excluded.yes_ask,
                    no_bid = excluded.no_bid,
                    no_ask = excluded.no_ask,
                    liquidity_dollars = excluded.liquidity_dollars,
                    volume = excluded.volume,
                    volume_24h = excluded.volume_24h,
                    open_interest = excluded.open_interest,
                    strike_type = excluded.strike_type,
                    floor_strike = excluded.floor_strike,
                    cap_strike = excluded.cap_strike,
                    raw_json = excluded.raw_json,
                    fetched_at = excluded.fetched_at
                """,
                [
                    (
                        row["ticker"],
                        row["event_ticker"],
                        row.get("target_date"),
                        row.get("market_status"),
                        row.get("result"),
                        row.get("expiration_value"),
                        row.get("open_time"),
                        row.get("close_time"),
                        row.get("settlement_ts"),
                        row.get("settlement_value_dollars"),
                        row.get("yes_bid"),
                        row.get("yes_ask"),
                        row.get("no_bid"),
                        row.get("no_ask"),
                        row.get("liquidity_dollars"),
                        row.get("volume"),
                        row.get("volume_24h"),
                        row.get("open_interest"),
                        row.get("strike_type"),
                        row.get("floor_strike"),
                        row.get("cap_strike"),
                        json.dumps(row.get("raw", {}), sort_keys=True),
                        fetched_at,
                    )
                    for row in payload
                ],
            )
            return conn.total_changes - before

    def upsert_kalshi_candles(self, ticker: str, period_interval: int, rows: Iterable[dict[str, Any]]) -> int:
        payload = list(rows)
        if not payload:
            return 0
        fetched_at = _now()
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO dataset_kalshi_candles (
                    ticker, end_period_ts, period_interval,
                    yes_bid_open, yes_bid_low, yes_bid_high, yes_bid_close,
                    yes_ask_open, yes_ask_low, yes_ask_high, yes_ask_close,
                    price_open, price_low, price_high, price_close, price_mean,
                    price_previous, volume, open_interest, raw_json, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, end_period_ts, period_interval) DO UPDATE SET
                    yes_bid_open = excluded.yes_bid_open,
                    yes_bid_low = excluded.yes_bid_low,
                    yes_bid_high = excluded.yes_bid_high,
                    yes_bid_close = excluded.yes_bid_close,
                    yes_ask_open = excluded.yes_ask_open,
                    yes_ask_low = excluded.yes_ask_low,
                    yes_ask_high = excluded.yes_ask_high,
                    yes_ask_close = excluded.yes_ask_close,
                    price_open = excluded.price_open,
                    price_low = excluded.price_low,
                    price_high = excluded.price_high,
                    price_close = excluded.price_close,
                    price_mean = excluded.price_mean,
                    price_previous = excluded.price_previous,
                    volume = excluded.volume,
                    open_interest = excluded.open_interest,
                    raw_json = excluded.raw_json,
                    fetched_at = excluded.fetched_at
                """,
                [
                    (
                        ticker,
                        int(row["end_period_ts"]),
                        period_interval,
                        _nested_float(row, "yes_bid", "open"),
                        _nested_float(row, "yes_bid", "low"),
                        _nested_float(row, "yes_bid", "high"),
                        _nested_float(row, "yes_bid", "close"),
                        _nested_float(row, "yes_ask", "open"),
                        _nested_float(row, "yes_ask", "low"),
                        _nested_float(row, "yes_ask", "high"),
                        _nested_float(row, "yes_ask", "close"),
                        _nested_float(row, "price", "open"),
                        _nested_float(row, "price", "low"),
                        _nested_float(row, "price", "high"),
                        _nested_float(row, "price", "close"),
                        _nested_float(row, "price", "mean"),
                        _nested_float(row, "price", "previous"),
                        _as_float(row.get("volume"), None),
                        _as_float(row.get("open_interest"), None),
                        json.dumps(row, sort_keys=True),
                        fetched_at,
                    )
                    for row in payload
                ],
            )
            return conn.total_changes - before

    def upsert_kalshi_trades(self, rows: Iterable[dict[str, Any]]) -> int:
        payload = list(rows)
        if not payload:
            return 0
        fetched_at = _now()
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO dataset_kalshi_trades (
                    trade_id, ticker, created_time, count, yes_price, no_price,
                    is_block_trade, raw_json, fetched_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    ticker = excluded.ticker,
                    created_time = excluded.created_time,
                    count = excluded.count,
                    yes_price = excluded.yes_price,
                    no_price = excluded.no_price,
                    is_block_trade = excluded.is_block_trade,
                    raw_json = excluded.raw_json,
                    fetched_at = excluded.fetched_at
                """,
                [
                    (
                        row["trade_id"],
                        row["ticker"],
                        row["created_time"],
                        row.get("count"),
                        row.get("yes_price"),
                        row.get("no_price"),
                        1 if row.get("is_block_trade") else 0,
                        json.dumps(row.get("raw", {}), sort_keys=True),
                        fetched_at,
                    )
                    for row in payload
                ],
            )
            return conn.total_changes - before

    def upsert_kalshi_orderbook_events(self, rows: Iterable[dict[str, Any]]) -> int:
        """Persist REST snapshots or websocket deltas used as queue evidence."""

        payload = list(rows)
        if not payload:
            return 0
        fetched_at = _now()
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT INTO dataset_kalshi_orderbook_events (
                    event_id, ticker, observed_at, sequence, yes_bids_json,
                    no_bids_json, source, raw_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    ticker = excluded.ticker,
                    observed_at = excluded.observed_at,
                    sequence = excluded.sequence,
                    yes_bids_json = excluded.yes_bids_json,
                    no_bids_json = excluded.no_bids_json,
                    source = excluded.source,
                    raw_json = excluded.raw_json,
                    fetched_at = excluded.fetched_at
                """,
                [
                    (
                        row["event_id"], row["ticker"], row["observed_at"],
                        row.get("sequence"), json.dumps(row.get("yes_bids", []), separators=(",", ":")),
                        json.dumps(row.get("no_bids", []), separators=(",", ":")),
                        row.get("source", "unknown"),
                        json.dumps(row.get("raw", row), sort_keys=True), fetched_at,
                    )
                    for row in payload
                ],
            )
            return conn.total_changes - before


def backfill_noaa_isd(
    store: DatasetStore,
    *,
    stations: Iterable[str],
    start: date,
    end: date,
    timeout: int = 30,
) -> DatasetResult:
    rows: list[dict[str, Any]] = []
    station_list = list(stations)
    for station in station_list:
        for year in range(start.year, end.year + 1):
            url = f"{NOAA_GLOBAL_HOURLY_BASE_URL}/{year}/{station}.csv"
            rows.extend(fetch_noaa_isd_csv(url, timeout=timeout, start=start, end=end))
    written = store.upsert_station_observations(rows)
    return DatasetResult("noaa-isd", written, f"{len(rows)} parsed rows from {len(station_list)} station(s)")


def fetch_noaa_isd_csv(
    url: str,
    *,
    timeout: int,
    start: date | None = None,
    end: date | None = None,
) -> list[dict[str, Any]]:
    request = Request(url, headers={"accept": "text/csv", "user-agent": "weatheredge-dataset-backfill/0.1"})
    rows: list[dict[str, Any]] = []
    with urlopen(request, timeout=timeout) as response:
        text = TextIOWrapper(response, encoding="utf-8", newline="")
        for raw in csv.DictReader(text):
            parsed = parse_noaa_isd_row(raw, source_url=url)
            if parsed is None:
                continue
            local = date.fromisoformat(parsed["local_date"])
            if start is not None and local < start:
                continue
            if end is not None and local > end:
                break
            rows.append(parsed)
    return rows


def parse_noaa_isd_csv(
    text: str,
    *,
    source_url: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in csv.DictReader(StringIO(text)):
        parsed = parse_noaa_isd_row(raw, source_url=source_url)
        if parsed is None:
            continue
        local = date.fromisoformat(parsed["local_date"])
        if start is not None and local < start:
            continue
        if end is not None and local > end:
            continue
        rows.append(parsed)
    return rows


def parse_noaa_isd_row(row: dict[str, Any], *, source_url: str | None = None) -> dict[str, Any] | None:
    observed = row.get("DATE")
    station = row.get("STATION")
    if not observed or not station:
        return None
    observed_at = _iso_utc(observed)
    return {
        "source": "noaa-isd",
        "station_id": str(station),
        "observed_at": observed_at,
        "local_date": _local_date(observed_at),
        "temp_f": _tenths_c_to_f(_encoded_first(row.get("TMP"))),
        "dewpoint_f": _tenths_c_to_f(_encoded_first(row.get("DEW"))),
        "relative_humidity": None,
        "wind_speed_kt": _meters_per_second_to_knots(_encoded_part(row.get("WND"), 3)),
        "wind_direction_deg": _as_float(_encoded_part(row.get("WND"), 0), None),
        "altimeter_in": _altimeter_to_in(_encoded_part(row.get("MA1"), 0)),
        "sea_level_pressure_hpa": _pressure_to_hpa(_encoded_first(row.get("SLP"))),
        "source_url": source_url,
        "raw": row,
    }


def backfill_iem_asos(
    store: DatasetStore,
    *,
    stations: Iterable[str],
    start: date,
    end: date,
    canonical_station_id: str | None = None,
    standard_utc_offset_hours: int = -8,
    timeout: int = 30,
) -> DatasetResult:
    rows: list[dict[str, Any]] = []
    station_list = list(stations)
    for station in station_list:
        params = {
            "station": station,
            "data": ["tmpf", "dwpf", "relh", "sknt", "drct", "alti", "skyc1", "skyc2", "skyc3", "skyc4"],
            "year1": start.year,
            "month1": start.month,
            "day1": start.day,
            "year2": end.year,
            "month2": end.month,
            "day2": end.day,
            # Fixed PST so observation timestamps and daily bucketing match the
            # NWS/Kalshi settlement day; also removes DST fall-back ambiguity.
            "tz": f"Etc/GMT+{-standard_utc_offset_hours}",
            "format": "onlycomma",
            "latlon": "yes",
            "elev": "yes",
            "missing": "null",
            "trace": "null",
            "direct": "no",
            "report_type": ["1", "2"],
        }
        url = f"{IEM_ASOS_URL}?{urlencode(params, doseq=True)}"
        text = _http_text(url, timeout=timeout)
        rows.extend(
            parse_iem_asos_csv(
                text, source_url=url, canonical_station_id=canonical_station_id,
                settlement_timezone=timezone(timedelta(hours=standard_utc_offset_hours)),
            )
        )
    written = store.upsert_station_observations(rows)
    return DatasetResult("iem-asos", written, f"{len(rows)} parsed rows from {len(station_list)} station(s)")


def parse_iem_asos_csv(
    text: str, *, source_url: str | None = None,
    canonical_station_id: str | None = None,
    settlement_timezone=PACIFIC_STANDARD_TZ,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in csv.DictReader(StringIO(text)):
        observed_at = _local_naive_to_utc(row.get("valid"), local_timezone=settlement_timezone)
        if not observed_at:
            continue
        output.append(
            {
                "source": "iem-asos",
                "station_id": canonical_station_id or row.get("station") or "",
                "observed_at": observed_at,
                "local_date": _local_date(observed_at, settlement_timezone=settlement_timezone),
                "temp_f": _as_float(row.get("tmpf"), None),
                "dewpoint_f": _as_float(row.get("dwpf"), None),
                "relative_humidity": _as_float(row.get("relh"), None),
                "wind_speed_kt": _as_float(row.get("sknt"), None),
                "wind_direction_deg": _as_float(row.get("drct"), None),
                "cloud_cover_fraction": _cloud_cover_fraction(row),
                "altimeter_in": _as_float(row.get("alti"), None),
                "sea_level_pressure_hpa": None,
                "source_url": source_url,
                "raw": row,
            }
        )
    return output


def backfill_open_meteo_previous_runs(
    store: DatasetStore,
    *,
    start: date,
    end: date,
    model: str = "best_match",
    previous_days: int = 7,
    station_id: str = "KSFO",
    latitude: float = KSFO_LATITUDE,
    longitude: float = KSFO_LONGITUDE,
    standard_utc_offset_hours: int = -8,
    timeout: int = 30,
) -> DatasetResult:
    models = _open_meteo_previous_run_models(model)
    variables = ["temperature_2m", *[f"temperature_2m_previous_day{day}" for day in range(1, previous_days + 1)]]
    rows: list[dict[str, Any]] = []
    for model_name in models:
        params: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": ",".join(variables),
            "temperature_unit": "fahrenheit",
            # Hourly rows are grouped into daily highs by their local date label,
            # so the request timezone defines the settlement-day bucketing.
            "timezone": f"Etc/GMT+{-standard_utc_offset_hours}",
        }
        if model_name and model_name != "best_match":
            params["models"] = model_name
        url = f"{OPEN_METEO_PREVIOUS_RUNS_URL}?{urlencode(params)}"
        payload = _http_json(url, timeout=timeout)
        rows.extend(
            open_meteo_hourly_daily_high_features(
                payload,
                source="open-meteo-previous-runs",
                model=model_name,
                source_url=url,
                station_id=station_id,
                settlement_timezone=timezone(timedelta(hours=standard_utc_offset_hours)),
            )
        )
    written = store.upsert_forecast_features(rows)
    return DatasetResult(
        "open-meteo-previous-runs",
        written,
        f"{len(rows)} daily high feature rows from {len(models)} model(s)",
    )


def _open_meteo_previous_run_models(model: str) -> tuple[str, ...]:
    normalized = (model or "best_match").strip()
    preset = OPEN_METEO_PREVIOUS_RUN_MODEL_PRESETS.get(normalized)
    if preset is not None:
        return preset
    models = tuple(item.strip() for item in normalized.split(",") if item.strip())
    return models or ("best_match",)


def backfill_open_meteo_historical_forecast(
    store: DatasetStore,
    *,
    start: date,
    end: date,
    model: str = "best_match",
    station_id: str = "KSFO",
    latitude: float = KSFO_LATITUDE,
    longitude: float = KSFO_LONGITUDE,
    standard_utc_offset_hours: int = -8,
    timeout: int = 30,
) -> DatasetResult:
    params: dict[str, Any] = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        # Daily maxima are aggregated over the request timezone; fixed PST
        # matches the NWS/Kalshi settlement day.
        "timezone": f"Etc/GMT+{-standard_utc_offset_hours}",
    }
    if model and model != "best_match":
        params["models"] = model
    url = f"{OPEN_METEO_HISTORICAL_FORECAST_URL}?{urlencode(params)}"
    payload = _http_json(url, timeout=timeout)
    rows = open_meteo_daily_features(
        payload, source="open-meteo-historical-forecast", model=model,
        source_url=url, station_id=station_id,
        settlement_timezone=timezone(timedelta(hours=standard_utc_offset_hours)),
    )
    written = store.upsert_forecast_features(rows)
    return DatasetResult("open-meteo-historical-forecast", written, f"{len(rows)} daily feature rows")


def open_meteo_daily_features(
    payload: dict[str, Any],
    *,
    source: str,
    model: str,
    source_url: str | None = None,
    station_id: str | None = None,
    settlement_timezone=PACIFIC_STANDARD_TZ,
) -> list[dict[str, Any]]:
    daily = payload.get("daily") if isinstance(payload, dict) else None
    if not isinstance(daily, dict):
        return []
    times = daily.get("time")
    if not isinstance(times, list):
        return []
    units = payload.get("daily_units") if isinstance(payload.get("daily_units"), dict) else {}
    rows: list[dict[str, Any]] = []
    for key, values in daily.items():
        if key == "time" or not isinstance(values, list):
            continue
        for idx, target in enumerate(times):
            if idx >= len(values) or values[idx] is None:
                continue
            target_date = date.fromisoformat(str(target))
            row = {
                    "source": source,
                    "model": model,
                    "issued_at": datetime.combine(target_date, datetime.min.time(), tzinfo=settlement_timezone).astimezone(UTC).isoformat(),
                    "target_date": target_date.isoformat(),
                    "valid_time": target_date.isoformat(),
                    "lead_hours": None,
                    "latitude": _as_float(payload.get("latitude"), None),
                    "longitude": _as_float(payload.get("longitude"), None),
                    "variable": key,
                    "value": float(values[idx]),
                    "units": units.get(key),
                    "source_url": source_url,
                    "raw": {"time": target, key: values[idx]},
                }
            if station_id:
                row["station_id"] = station_id
            rows.append(row)
    return rows


def open_meteo_hourly_daily_high_features(
    payload: dict[str, Any],
    *,
    source: str,
    model: str,
    source_url: str | None = None,
    station_id: str | None = None,
    settlement_timezone=PACIFIC_STANDARD_TZ,
) -> list[dict[str, Any]]:
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    if not isinstance(hourly, dict):
        return []
    times = hourly.get("time")
    if not isinstance(times, list):
        return []
    units = payload.get("hourly_units") if isinstance(payload.get("hourly_units"), dict) else {}
    grouped: dict[tuple[str, str], list[float]] = {}
    raw_examples: dict[tuple[str, str], dict[str, Any]] = {}
    for variable, values in hourly.items():
        if variable == "time" or not isinstance(values, list):
            continue
        for idx, valid in enumerate(times):
            if idx >= len(values) or values[idx] is None:
                continue
            target = str(valid)[:10]
            key = (target, variable)
            grouped.setdefault(key, []).append(float(values[idx]))
            raw_examples.setdefault(key, {"time": valid, variable: values[idx]})

    rows: list[dict[str, Any]] = []
    for (target, variable), values in sorted(grouped.items()):
        if not values:
            continue
        target_date = date.fromisoformat(target)
        lead_hours = _open_meteo_previous_lead_hours(variable)
        issued_at = datetime.combine(
            target_date,
            datetime.min.time(),
            tzinfo=settlement_timezone,
        ) - timedelta(hours=lead_hours or 0)
        row = {
                "source": source,
                "model": model,
                "issued_at": issued_at.astimezone(UTC).isoformat(),
                "target_date": target_date.isoformat(),
                "valid_time": target_date.isoformat(),
                "lead_hours": lead_hours,
                "latitude": _as_float(payload.get("latitude"), None),
                "longitude": _as_float(payload.get("longitude"), None),
                "variable": variable.replace("temperature_2m", "temperature_2m_max", 1),
                "value": max(values),
                "units": units.get(variable),
                "source_url": source_url,
                "raw": {
                    **raw_examples.get((target, variable), {}),
                    "aggregation": "local_date_max",
                    "n": len(values),
                },
            }
        if station_id:
            row["station_id"] = station_id
        rows.append(row)
    return rows


def backfill_lamp(
    store: DatasetStore,
    *,
    start: date,
    end: date,
    station_id: str = "KSFO",
    standard_utc_offset_hours: int = -8,
    timeout: int = 30,
) -> DatasetResult:
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for cycle_date in _date_range(start, end):
        for cycle_hour in NOAA_LAMP_CYCLES_3H:
            url = _lamp_text_url(cycle_date, cycle_hour)
            try:
                text = _http_text(url, timeout=timeout)
            except HTTPError as exc:
                if exc.code in NOAA_GUIDANCE_UNAVAILABLE_HTTP_CODES:
                    failures.append(
                        f"{cycle_date.isoformat()}T{cycle_hour:02d}:30Z HTTP {exc.code}"
                    )
                    continue
                raise
            rows.extend(
                parse_noaa_station_guidance_text(
                    text,
                    source="noaa-lamp",
                    model="lamp",
                    source_url=url,
                    station_id=station_id,
                    settlement_timezone=timezone(timedelta(hours=standard_utc_offset_hours)),
                )
            )
    rows = _dedupe_feature_rows(rows)
    written = store.upsert_forecast_features(rows)
    detail = f"{len(rows)} station guidance feature rows for {station_id}"
    if failures:
        detail += f"; skipped {len(failures)} unavailable cycle(s): {_summarize_failures(failures)}"
    return DatasetResult("noaa-lamp", written, detail)


def backfill_gfs_mos(
    store: DatasetStore,
    *,
    start: date,
    end: date,
    station_id: str = "KSFO",
    standard_utc_offset_hours: int = -8,
    timeout: int = 30,
) -> DatasetResult:
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for cycle_date in _date_range(start, end):
        for cycle_hour in NOAA_GUIDANCE_CYCLES_6H:
            url = _gfs_mos_text_url(cycle_date, cycle_hour)
            try:
                text = _http_text(url, timeout=timeout)
            except HTTPError as exc:
                if exc.code in NOAA_GUIDANCE_UNAVAILABLE_HTTP_CODES:
                    failures.append(
                        f"{cycle_date.isoformat()}T{cycle_hour:02d}:00Z HTTP {exc.code}"
                    )
                    continue
                raise
            rows.extend(
                parse_noaa_station_guidance_text(
                    text,
                    source="gfs-mos",
                    model="gfs-mos",
                    source_url=url,
                    station_id=station_id,
                    settlement_timezone=timezone(timedelta(hours=standard_utc_offset_hours)),
                )
            )
    rows = _dedupe_feature_rows(rows)
    written = store.upsert_forecast_features(rows)
    detail = f"{len(rows)} station guidance feature rows for {station_id}"
    if failures:
        detail += f"; skipped {len(failures)} unavailable cycle(s): {_summarize_failures(failures)}"
    return DatasetResult("gfs-mos", written, detail)


def _summarize_failures(failures: list[str], *, limit: int = 3) -> str:
    shown = failures[:limit]
    suffix = ", ..." if len(failures) > limit else ""
    return ", ".join(shown) + suffix


def backfill_nbm(
    store: DatasetStore,
    *,
    start: date,
    end: date,
    station_id: str = "KSFO",
    latitude: float = KSFO_LATITUDE,
    longitude: float = KSFO_LONGITUDE,
    standard_utc_offset_hours: int = -8,
    timeout: int = 30,
) -> DatasetResult:
    return backfill_open_meteo_previous_runs(
        store,
        start=start,
        end=end,
        model="ncep_nbm_conus",
        previous_days=0,
        station_id=station_id,
        latitude=latitude,
        longitude=longitude,
        standard_utc_offset_hours=standard_utc_offset_hours,
        timeout=timeout,
    )


def backfill_hrrr(
    store: DatasetStore,
    *,
    start: date,
    end: date,
    station_id: str = "KSFO",
    latitude: float = KSFO_LATITUDE,
    longitude: float = KSFO_LONGITUDE,
    standard_utc_offset_hours: int = -8,
    timeout: int = 30,
) -> DatasetResult:
    return backfill_open_meteo_previous_runs(
        store,
        start=start,
        end=end,
        model="gfs_hrrr",
        previous_days=0,
        station_id=station_id,
        latitude=latitude,
        longitude=longitude,
        standard_utc_offset_hours=standard_utc_offset_hours,
        timeout=timeout,
    )


def parse_noaa_station_guidance_text(
    text: str,
    *,
    source: str,
    model: str,
    source_url: str | None = None,
    station_id: str = "KSFO",
    settlement_timezone=PACIFIC_STANDARD_TZ,
) -> list[dict[str, Any]]:
    """Parse NOAA station guidance text into hourly temperature and daily highs."""

    block = _station_guidance_block(text, station_id)
    if not block:
        return []
    issued_at = _guidance_issue_time(block[0])
    if issued_at is None:
        return []
    forecast_hours: list[int] = []
    temperatures: list[float | None] = []
    for line in block[1:]:
        tokens = line.strip().split()
        if not tokens:
            continue
        label = tokens[0].upper()
        if label == "HR":
            forecast_hours = [
                int(token)
                for token in tokens[1:]
                if re.fullmatch(r"-?\d+", token)
            ]
        elif label == "TMP":
            temperatures = [_guidance_temp_value(token) for token in tokens[1:]]
    if not forecast_hours or not temperatures:
        return []

    hourly_rows: list[dict[str, Any]] = []
    for lead_hour, temp_f in zip(forecast_hours, temperatures):
        if temp_f is None:
            continue
        valid_at = issued_at + timedelta(hours=lead_hour)
        target = valid_at.astimezone(settlement_timezone).date()
        hourly_rows.append(
            {
                "source": source,
                "model": model,
                "station_id": station_id,
                "issued_at": issued_at.isoformat(),
                "target_date": target.isoformat(),
                "valid_time": valid_at.isoformat(),
                "lead_hours": float(lead_hour),
                "latitude": None,
                "longitude": None,
                "variable": "temperature_2m",
                "value": temp_f,
                "units": "degF",
                "source_url": source_url,
                "raw": {
                    "station_id": station_id,
                    "forecast_hour": lead_hour,
                    "source_product": "station_guidance_text",
                },
            }
        )

    daily_rows = _daily_high_rows_from_hourly(hourly_rows, source_url=source_url)
    return [*hourly_rows, *daily_rows]


def _station_guidance_block(text: str, station_id: str) -> list[str]:
    station = station_id.upper()
    block: list[str] = []
    collecting = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^K[A-Z0-9]{3}\b", stripped) and "GUIDANCE" in stripped.upper():
            if stripped.upper().startswith(station):
                block = [stripped]
                collecting = True
                continue
            if collecting:
                break
        elif collecting:
            block.append(stripped)
    return block


def _guidance_issue_time(header: str) -> datetime | None:
    match = re.search(
        r"(\d{1,2})/(\d{1,2})/(\d{2,4})\s+(\d{4})\s+UTC",
        header,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    month, day, year, hhmm = match.groups()
    year_num = int(year)
    if year_num < 100:
        year_num += 2000
    return datetime(
        year_num,
        int(month),
        int(day),
        int(hhmm[:2]),
        int(hhmm[2:]),
        tzinfo=UTC,
    )


def _guidance_temp_value(token: str) -> float | None:
    value = token.strip()
    if value in {"", "M", "MM", "NA", "--"}:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _daily_high_rows_from_hourly(
    hourly_rows: list[dict[str, Any]],
    *,
    source_url: str | None,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in hourly_rows:
        grouped.setdefault(str(row["target_date"]), []).append(row)
    daily: list[dict[str, Any]] = []
    for target, rows in sorted(grouped.items()):
        values = [float(row["value"]) for row in rows if _as_float(row.get("value"), None) is not None]
        if not values:
            continue
        first = rows[0]
        daily.append(
            {
                "source": first["source"],
                "model": first["model"],
                "station_id": first.get("station_id", "KSFO"),
                "issued_at": first["issued_at"],
                "target_date": target,
                "valid_time": target,
                "lead_hours": min(float(row["lead_hours"]) for row in rows),
                "latitude": None,
                "longitude": None,
                "variable": "temperature_2m_max",
                "value": max(values),
                "units": "degF",
                "source_url": source_url,
                "raw": {
                    "station_id": (first.get("raw") or {}).get("station_id"),
                    "aggregation": "local_date_max",
                    "n": len(values),
                    "source_product": "station_guidance_text",
                },
            }
        )
    return daily


def _date_range(start: date, end: date) -> list[date]:
    days = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def _lamp_text_url(cycle_date: date, cycle_hour: int) -> str:
    ymd = cycle_date.strftime("%Y%m%d")
    return f"{NOAA_NOMADS_BASE_URL}/lamp/prod/lmp.{ymd}/lmp_lavtxt.t{cycle_hour:02d}30z"


def _gfs_mos_text_url(cycle_date: date, cycle_hour: int) -> str:
    ymd = cycle_date.strftime("%Y%m%d")
    return f"{NOAA_NOMADS_BASE_URL}/gfs_mos/prod/gfs_mos.{ymd}/mdl_gfsmav.t{cycle_hour:02d}z"


def _dedupe_feature_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[object, ...], dict[str, Any]] = {}
    for row in rows:
        key = (
            row["source"],
            row["model"],
            row.get("station_id", "KSFO"),
            row["issued_at"],
            row["target_date"],
            row["valid_time"],
            row["variable"],
        )
        deduped[key] = row
    return list(deduped.values())


def backfill_kalshi_history(
    store: DatasetStore,
    *,
    start: date | None,
    end: date | None,
    include_candles: bool,
    include_trades: bool = False,
    candle_interval: int = 60,
    limit: int = 1000,
    max_pages: int = 20,
    max_trade_pages: int = 20,
    series_tickers: Iterable[str] = (SERIES_TICKER,),
    timeout: int = 20,
) -> DatasetResult:
    client = KalshiPublicClient(timeout=timeout)
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    for series_ticker in series_tickers:
        cursor = None
        for page_idx in range(max_pages):
            try:
                payload = client.list_historical_markets(
                    series_ticker=series_ticker, limit=limit, cursor=cursor
                )
            except HTTPError as exc:
                if exc.code == 429:
                    notes.append(
                        f"{series_ticker} market pagination halted by HTTP 429 after {page_idx} page(s)"
                    )
                    break
                raise
            markets = payload.get("markets", [])
            rows.extend(
                _kalshi_market_row(row)
                for row in markets
                if _market_in_date_window(row, start, end)
            )
            cursor = payload.get("cursor")
            if not cursor:
                break
    written = store.upsert_kalshi_markets(rows)
    candle_rows = 0
    if include_candles:
        for market_idx, row in enumerate(rows):
            open_ts = _timestamp(row.get("open_time"))
            close_ts = _timestamp(row.get("close_time"))
            if open_ts is None or close_ts is None:
                continue
            try:
                payload = client.get_historical_market_candlesticks(
                    row["ticker"],
                    start_ts=open_ts,
                    end_ts=close_ts,
                    period_interval=candle_interval,
                )
            except HTTPError as exc:
                if exc.code == 429:
                    notes.append(f"candles halted by HTTP 429 after {market_idx} of {len(rows)} market(s)")
                    break
                raise
            candle_rows += store.upsert_kalshi_candles(
                row["ticker"],
                candle_interval,
                payload.get("candlesticks", []),
            )
    trade_rows = 0
    if include_trades:
        trade_halted = False
        for market_idx, row in enumerate(rows):
            open_ts = _timestamp(row.get("open_time"))
            close_ts = _timestamp(row.get("close_time"))
            if open_ts is None or close_ts is None:
                continue
            cursor = None
            for page_idx in range(max_trade_pages):
                try:
                    payload = client.get_historical_trades(
                        ticker=row["ticker"],
                        min_ts=open_ts,
                        max_ts=close_ts,
                        limit=limit,
                        cursor=cursor,
                    )
                except HTTPError as exc:
                    if exc.code == 429:
                        notes.append(
                            "trades halted by HTTP 429 "
                            f"at market {market_idx + 1} of {len(rows)}, page {page_idx + 1}"
                        )
                        trade_halted = True
                        break
                    raise
                trade_rows += store.upsert_kalshi_trades(
                    _kalshi_trade_row(trade) for trade in payload.get("trades", [])
                )
                cursor = payload.get("cursor")
                if not cursor:
                    break
            if trade_halted:
                break
    total = written + candle_rows + trade_rows
    detail = f"{len(rows)} market rows"
    if include_candles:
        detail += f", {candle_rows} candle rows"
    if include_trades:
        detail += f", {trade_rows} trade rows"
    if notes:
        detail += "; " + "; ".join(notes)
    return DatasetResult("kalshi-history", total, detail)


def _kalshi_market_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticker": row["ticker"],
        "event_ticker": row.get("event_ticker", ""),
        "target_date": _target_date_from_event_ticker(row.get("event_ticker", "")),
        "market_status": row.get("status"),
        "result": row.get("result"),
        "expiration_value": _as_float(row.get("expiration_value"), None),
        "open_time": row.get("open_time"),
        "close_time": row.get("close_time"),
        "settlement_ts": row.get("settlement_ts"),
        "settlement_value_dollars": _as_float(row.get("settlement_value_dollars"), None),
        "yes_bid": _as_float(row.get("yes_bid_dollars"), None),
        "yes_ask": _as_float(row.get("yes_ask_dollars"), None),
        "no_bid": _as_float(row.get("no_bid_dollars"), None),
        "no_ask": _as_float(row.get("no_ask_dollars"), None),
        "liquidity_dollars": _as_float(row.get("liquidity_dollars"), None),
        "volume": _as_float(row.get("volume_fp"), None),
        "volume_24h": _as_float(row.get("volume_24h_fp"), None),
        "open_interest": _as_float(row.get("open_interest_fp"), None),
        "strike_type": row.get("strike_type"),
        "floor_strike": _as_float(row.get("floor_strike"), None),
        "cap_strike": _as_float(row.get("cap_strike"), None),
        "raw": row,
    }


def _kalshi_trade_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_id": row["trade_id"],
        "ticker": row["ticker"],
        "count": _as_float(row.get("count_fp"), None),
        "yes_price": _as_float(row.get("yes_price_dollars"), None),
        "no_price": _as_float(row.get("no_price_dollars"), None),
        "created_time": row.get("created_time"),
        "is_block_trade": bool(row.get("is_block_trade")),
        "raw": row,
    }


def _market_in_date_window(row: dict[str, Any], start: date | None, end: date | None) -> bool:
    target = _target_date_from_event_ticker(row.get("event_ticker", ""))
    if target is None:
        return True
    parsed = date.fromisoformat(target)
    if start is not None and parsed < start:
        return False
    if end is not None and parsed > end:
        return False
    return True


def _target_date_from_event_ticker(event_ticker: str) -> str | None:
    try:
        suffix = event_ticker.rsplit("-", 1)[1]
        parsed = datetime.strptime(suffix, "%y%b%d").date()
    except (IndexError, ValueError):
        return None
    return parsed.isoformat()


def _open_meteo_previous_lead_hours(variable: str) -> float | None:
    marker = "_previous_day"
    if marker not in variable:
        return 0.0
    try:
        return float(int(variable.rsplit(marker, 1)[1]) * 24)
    except ValueError:
        return None


def _http_json(url: str, *, timeout: int) -> Any:
    request = Request(url, headers={"accept": "application/json", "user-agent": "weatheredge-dataset-backfill/0.1"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_text(url: str, *, timeout: int) -> str:
    request = Request(url, headers={"accept": "text/plain", "user-agent": "weatheredge-dataset-backfill/0.1"})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _iso_utc(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _local_naive_to_utc(
    value: str | None, *, local_timezone=PACIFIC_STANDARD_TZ
) -> str | None:
    """Interpret an IEM timestamp in the fixed-standard request timezone."""

    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_timezone)
    return parsed.astimezone(UTC).isoformat()


def _local_date(observed_at: str, *, settlement_timezone=PACIFIC_STANDARD_TZ) -> str:
    """Settlement-day date on the station's fixed-standard clock."""

    parsed = datetime.fromisoformat(observed_at)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(settlement_timezone).date().isoformat()


def _cloud_cover_fraction(row: dict[str, Any]) -> float | None:
    fractions = {"CLR": 0.0, "SKC": 0.0, "FEW": 0.25, "SCT": 0.5, "BKN": 0.75, "OVC": 1.0}
    values = [
        fractions[str(row.get(f"skyc{layer}") or "").upper()]
        for layer in range(1, 5)
        if str(row.get(f"skyc{layer}") or "").upper() in fractions
    ]
    return max(values) if values else None


def _as_float(value: Any, default: float | None = 0.0) -> float | None:
    if value in (None, "", "null", "M"):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return default
    return numeric


def _encoded_first(value: Any) -> float | None:
    return _encoded_part(value, 0)


def _encoded_part(value: Any, index: int) -> float | None:
    if not value:
        return None
    parts = str(value).split(",")
    if index >= len(parts):
        return None
    return _as_float(parts[index], None)


def _tenths_c_to_f(value: float | None) -> float | None:
    if value is None or abs(value) >= 9999:
        return None
    return value / 10.0 * 9.0 / 5.0 + 32.0


def _meters_per_second_to_knots(value: float | None) -> float | None:
    if value is None or value >= 9999:
        return None
    return value / 10.0 * 1.94384449


def _pressure_to_hpa(value: float | None) -> float | None:
    if value is None or abs(value) >= 99999:
        return None
    return value / 10.0


def _altimeter_to_in(value: float | None) -> float | None:
    pressure = _pressure_to_hpa(value)
    if pressure is None:
        return None
    return pressure * 0.0295299830714


def _nested_float(row: dict[str, Any], key: str, nested: str) -> float | None:
    value = row.get(key)
    if not isinstance(value, dict):
        return None
    return _as_float(value.get(nested), None)


def _timestamp(value: str | None) -> int | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())
