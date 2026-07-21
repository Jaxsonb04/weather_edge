from __future__ import annotations

import sqlite3
from datetime import date
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from sfo_kalshi_quant import datasets as datasets_module
from sfo_kalshi_quant.datasets import (
    DatasetStore,
    OPEN_METEO_PREVIOUS_RUN_RESEARCH_MODELS,
    backfill_gfs_mos,
    backfill_hrrr,
    backfill_lamp,
    backfill_open_meteo_previous_runs,
    backfill_nbm,
    backfill_kalshi_history,
    fetch_noaa_isd_csv,
    open_meteo_daily_features,
    open_meteo_hourly_daily_high_features,
    parse_noaa_station_guidance_text,
    parse_iem_asos_csv,
    parse_noaa_isd_row,
)


def test_dataset_store_releases_each_write_connection_without_gc(tmp_path, monkeypatch):
    opened: list[sqlite3.Connection] = []
    original_connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(datasets_module.sqlite3, "connect", tracked_connect)
    store = DatasetStore(tmp_path / "dataset.db")
    run_id = store.start_run("open-meteo-previous-runs", {})
    store.finish_run(run_id, status="success", rows_written=0)

    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            conn.execute("SELECT 1")


def test_dataset_store_clamps_busy_wait_to_expired_retry_deadline(tmp_path, monkeypatch):
    store = DatasetStore(tmp_path / "dataset.db")
    monkeypatch.setenv("SFO_DATASET_LOCK_RETRY_DEADLINE_MILLISECONDS", "0")

    conn = store.connect()
    try:
        busy_timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()

    assert busy_timeout_ms == 0


def test_parse_noaa_isd_row_converts_encoded_units():
    row = {
        "STATION": "72494023234",
        "DATE": "2020-01-01T00:56:00",
        "TMP": "+0128,5",
        "DEW": "+0067,5",
        "WND": "320,5,N,0015,5",
        "MA1": "10200,5,10194,5",
        "SLP": "10198,5",
    }

    parsed = parse_noaa_isd_row(row, source_url="https://example.test/noaa")

    assert parsed is not None
    assert parsed["source"] == "noaa-isd"
    assert parsed["station_id"] == "72494023234"
    assert parsed["observed_at"] == "2020-01-01T00:56:00+00:00"
    assert round(parsed["temp_f"], 1) == 55.0
    assert round(parsed["dewpoint_f"], 1) == 44.1
    assert round(parsed["wind_speed_kt"], 1) == 2.9
    assert round(parsed["sea_level_pressure_hpa"], 1) == 1019.8


def test_fetch_noaa_isd_csv_filters_requested_local_date_without_full_file_scan():
    text = """STATION,DATE,TMP,DEW,WND,MA1,SLP
72494023234,2020-01-01T07:56:00,"+0100,5","+0050,5","320,5,N,0015,5","10200,5,10194,5","10198,5"
72494023234,2020-01-01T08:56:00,"+0128,5","+0067,5","320,5,N,0015,5","10200,5,10194,5","10198,5"
72494023234,2020-01-02T08:56:00,"+0130,5","+0068,5","320,5,N,0015,5","10200,5,10194,5","10198,5"
"""

    with patch("sfo_kalshi_quant.datasets.urlopen", return_value=BytesIO(text.encode("utf-8"))):
        rows = fetch_noaa_isd_csv(
            "https://example.test/noaa.csv",
            timeout=1,
            start=date(2020, 1, 1),
            end=date(2020, 1, 1),
        )

    assert len(rows) == 1
    assert rows[0]["observed_at"] == "2020-01-01T08:56:00+00:00"
    assert rows[0]["local_date"] == "2020-01-01"


def test_parse_iem_asos_csv_keeps_local_date_and_weather_fields():
    text = """station,valid,lon,lat,elevation,tmpf,dwpf,relh,sknt,drct,alti
SFO,2020-01-01 00:00,-122.3749,37.6190,5.00,50.0,44.0,80.0,14.00,270.0,30.17
"""

    rows = parse_iem_asos_csv(text, source_url="https://example.test/iem")

    assert len(rows) == 1
    assert rows[0]["source"] == "iem-asos"
    assert rows[0]["station_id"] == "SFO"
    assert rows[0]["observed_at"] == "2020-01-01T08:00:00+00:00"
    assert rows[0]["local_date"] == "2020-01-01"
    assert rows[0]["temp_f"] == 50.0
    assert rows[0]["wind_direction_deg"] == 270.0


def test_open_meteo_previous_runs_reduce_hourly_offsets_to_daily_high_features():
    payload = {
        "latitude": 37.62612,
        "longitude": -122.400154,
        "hourly_units": {
            "time": "iso8601",
            "temperature_2m": "°F",
            "temperature_2m_previous_day1": "°F",
        },
        "hourly": {
            "time": [
                "2026-06-01T00:00",
                "2026-06-01T01:00",
                "2026-06-02T00:00",
                "2026-06-02T01:00",
            ],
            "temperature_2m": [51.0, 54.0, 55.0, 56.0],
            "temperature_2m_previous_day1": [50.0, 53.0, 60.0, 58.0],
        },
    }

    rows = open_meteo_hourly_daily_high_features(
        payload,
        source="open-meteo-previous-runs",
        model="best_match",
        source_url="https://example.test/open-meteo",
    )

    values = {(row["target_date"], row["variable"]): row for row in rows}
    assert values[("2026-06-01", "temperature_2m_max")]["value"] == 54.0
    assert values[("2026-06-02", "temperature_2m_max_previous_day1")]["value"] == 60.0
    assert values[("2026-06-02", "temperature_2m_max_previous_day1")]["lead_hours"] == 24.0
    assert values[("2026-06-02", "temperature_2m_max_previous_day1")]["issued_at"].startswith("2026-06-01")


def test_open_meteo_previous_runs_research_preset_keeps_models_and_leads_separate():
    payload = {
        "latitude": 37.62612,
        "longitude": -122.400154,
        "hourly_units": {
            "time": "iso8601",
            "temperature_2m": "°F",
            "temperature_2m_previous_day1": "°F",
        },
        "hourly": {
            "time": ["2026-06-02T00:00", "2026-06-02T01:00"],
            "temperature_2m": [55.0, 56.0],
            "temperature_2m_previous_day1": [60.0, 58.0],
        },
    }
    requested_urls = []

    def fake_json(url: str, *, timeout: int = 30):
        requested_urls.append(url)
        return payload

    with TemporaryDirectory() as tmp, patch("sfo_kalshi_quant.datasets._http_json", fake_json):
        store = DatasetStore(Path(tmp) / "dataset.db")
        result = backfill_open_meteo_previous_runs(
            store,
            start=date(2026, 6, 2),
            end=date(2026, 6, 2),
            model="research_core",
            previous_days=1,
            timeout=1,
        )

        assert result.rows_written == len(OPEN_METEO_PREVIOUS_RUN_RESEARCH_MODELS) * 2
        assert len(requested_urls) == len(OPEN_METEO_PREVIOUS_RUN_RESEARCH_MODELS)
        assert all("models=" in url for url in requested_urls)
        with sqlite3.connect(store.db_path) as conn:
            rows = conn.execute(
                """
                SELECT model, variable, lead_hours
                FROM dataset_forecast_features
                ORDER BY model, variable
                """
            ).fetchall()

    assert {row[0] for row in rows} == set(OPEN_METEO_PREVIOUS_RUN_RESEARCH_MODELS)
    assert {row[2] for row in rows} == {0.0, 24.0}


def test_parse_noaa_station_guidance_text_emits_hourly_and_daily_high_features():
    text = """
    FOUS11 KWNO 261230
    KSFO   GFS LAMP GUIDANCE   6/26/2026  1230 UTC
    HR     01 02 03 04 05
    TMP    58 60 MM 63 61
    """

    rows = parse_noaa_station_guidance_text(
        text,
        source="noaa-lamp",
        model="lamp",
        source_url="https://example.test/lamp",
        station_id="KSFO",
    )

    hourly = [row for row in rows if row["variable"] == "temperature_2m"]
    daily = [row for row in rows if row["variable"] == "temperature_2m_max"]
    assert [row["value"] for row in hourly] == [58.0, 60.0, 63.0, 61.0]
    assert {row["lead_hours"] for row in hourly} == {1.0, 2.0, 4.0, 5.0}
    assert daily == [
        {
            "source": "noaa-lamp",
            "model": "lamp",
            "station_id": "KSFO",
            "issued_at": "2026-06-26T12:30:00+00:00",
            "target_date": "2026-06-26",
            "valid_time": "2026-06-26",
            "lead_hours": 1.0,
            "latitude": None,
            "longitude": None,
            "variable": "temperature_2m_max",
            "value": 63.0,
            "units": "degF",
            "source_url": "https://example.test/lamp",
            "raw": {
                "station_id": "KSFO",
                "aggregation": "local_date_max",
                "n": 4,
                "source_product": "station_guidance_text",
            },
        }
    ]


def test_parse_noaa_station_guidance_text_ignores_other_stations_and_bad_headers():
    text = """
    KOAK   GFS LAMP GUIDANCE   6/26/2026  1230 UTC
    HR     01 02
    TMP    70 71
    KSFO   GUIDANCE WITHOUT ISSUE TIME
    HR     01 02
    TMP    58 59
    """

    rows = parse_noaa_station_guidance_text(
        text,
        source="noaa-lamp",
        model="lamp",
        station_id="KSFO",
    )

    assert rows == []


def test_lamp_and_gfs_mos_backfills_store_station_guidance_features():
    text = """
    KSFO   GFS LAMP GUIDANCE   6/26/2026  1230 UTC
    HR     01 02
    TMP    58 61
    """

    def fake_text(url: str, *, timeout: int = 30):
        return text

    with TemporaryDirectory() as tmp, patch("sfo_kalshi_quant.datasets._http_text", fake_text):
        store = DatasetStore(Path(tmp) / "dataset.db")
        lamp = backfill_lamp(store, start=date(2026, 6, 26), end=date(2026, 6, 26), timeout=1)
        mos = backfill_gfs_mos(store, start=date(2026, 6, 26), end=date(2026, 6, 26), timeout=1)
        with sqlite3.connect(store.db_path) as conn:
            rows = conn.execute(
                """
                SELECT source, model, variable, value
                FROM dataset_forecast_features
                ORDER BY source, variable
                """
            ).fetchall()

    assert lamp.rows_written == 3
    assert mos.rows_written == 3
    assert {row[0] for row in rows} == {"noaa-lamp", "gfs-mos"}
    assert ("noaa-lamp", "lamp", "temperature_2m_max", 61.0) in rows
    assert ("gfs-mos", "gfs-mos", "temperature_2m_max", 61.0) in rows


def test_station_guidance_backfills_skip_unavailable_http_cycles():
    def unavailable(url: str, *, timeout: int = 30):
        raise HTTPError(url=url, code=403, msg="Forbidden", hdrs=None, fp=None)

    with TemporaryDirectory() as tmp, patch("sfo_kalshi_quant.datasets._http_text", unavailable):
        store = DatasetStore(Path(tmp) / "dataset.db")
        lamp = backfill_lamp(store, start=date(2026, 6, 26), end=date(2026, 6, 26), timeout=1)
        mos = backfill_gfs_mos(store, start=date(2026, 6, 26), end=date(2026, 6, 26), timeout=1)

    assert lamp.rows_written == 0
    assert mos.rows_written == 0
    assert "skipped" in lamp.detail
    assert "HTTP 403" in lamp.detail
    assert "skipped" in mos.detail
    assert "HTTP 403" in mos.detail


def test_nbm_and_hrrr_backfills_use_model_specific_previous_run_sources():
    payload = {
        "latitude": 37.62612,
        "longitude": -122.400154,
        "hourly_units": {"time": "iso8601", "temperature_2m": "°F"},
        "hourly": {
            "time": ["2026-06-02T00:00", "2026-06-02T01:00"],
            "temperature_2m": [55.0, 56.0],
        },
    }
    requested_urls = []

    def fake_json(url: str, *, timeout: int = 30):
        requested_urls.append(url)
        return payload

    with TemporaryDirectory() as tmp, patch("sfo_kalshi_quant.datasets._http_json", fake_json):
        store = DatasetStore(Path(tmp) / "dataset.db")
        nbm = backfill_nbm(store, start=date(2026, 6, 2), end=date(2026, 6, 2), timeout=1)
        hrrr = backfill_hrrr(store, start=date(2026, 6, 2), end=date(2026, 6, 2), timeout=1)
        with sqlite3.connect(store.db_path) as conn:
            rows = conn.execute(
                """
                SELECT source, model, variable, value
                FROM dataset_forecast_features
                ORDER BY source
                """
            ).fetchall()

    assert nbm.rows_written == 1
    assert hrrr.rows_written == 1
    assert any("models=ncep_nbm_conus" in url for url in requested_urls)
    assert any("models=gfs_hrrr" in url for url in requested_urls)
    assert rows == [
        ("open-meteo-previous-runs", "gfs_hrrr", "temperature_2m_max", 56.0),
        ("open-meteo-previous-runs", "ncep_nbm_conus", "temperature_2m_max", 56.0),
    ]


def test_open_meteo_daily_features_keep_source_and_model_provenance():
    payload = {
        "latitude": 37.62612,
        "longitude": -122.400154,
        "daily_units": {"time": "iso8601", "temperature_2m_max": "°F"},
        "daily": {"time": ["2026-06-01"], "temperature_2m_max": [75.8]},
    }

    rows = open_meteo_daily_features(
        payload,
        source="open-meteo-historical-forecast",
        model="best_match",
        source_url="https://example.test/open-meteo",
    )

    assert rows == [
        {
            "source": "open-meteo-historical-forecast",
            "model": "best_match",
            # Midnight of the target settlement day on the fixed-PST clock.
            "issued_at": "2026-06-01T08:00:00+00:00",
            "target_date": "2026-06-01",
            "valid_time": "2026-06-01",
            "lead_hours": None,
            "latitude": 37.62612,
            "longitude": -122.400154,
            "variable": "temperature_2m_max",
            "value": 75.8,
            "units": "°F",
            "source_url": "https://example.test/open-meteo",
            "raw": {"time": "2026-06-01", "temperature_2m_max": 75.8},
        }
    ]


def test_dataset_store_upserts_compact_feature_tables():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "dataset.db"
        store = DatasetStore(db_path)
        obs_rows = parse_iem_asos_csv(
            """station,valid,lon,lat,elevation,tmpf,dwpf,relh,sknt,drct,alti
SFO,2020-01-01 00:00,-122.3749,37.6190,5.00,50.0,44.0,80.0,14.00,270.0,30.17
"""
        )
        assert store.upsert_station_observations(obs_rows) == 1
        assert store.upsert_station_observations(obs_rows) == 1
        assert store.upsert_forecast_features(
            [
                {
                    "source": "open-meteo-previous-runs",
                    "model": "best_match",
                    "issued_at": "2026-06-01T07:00:00+00:00",
                    "target_date": "2026-06-02",
                    "valid_time": "2026-06-02",
                    "lead_hours": 24.0,
                    "latitude": 37.62,
                    "longitude": -122.38,
                    "variable": "temperature_2m_max_previous_day1",
                    "value": 67.0,
                    "units": "°F",
                    "source_url": "https://example.test",
                    "raw": {"n": 24},
                }
            ]
        ) == 1
        assert store.upsert_kalshi_markets(
            [
                {
                    "ticker": "KXHIGHTSFO-26JUN02-B68.5",
                    "event_ticker": "KXHIGHTSFO-26JUN02",
                    "target_date": "2026-06-02",
                    "market_status": "settled",
                    "result": "yes",
                    "expiration_value": 69.0,
                    "yes_bid": 0.99,
                    "yes_ask": 1.0,
                    "raw": {"ticker": "KXHIGHTSFO-26JUN02-B68.5"},
                }
            ]
        ) == 1
        assert store.upsert_kalshi_candles(
            "KXHIGHTSFO-26JUN02-B68.5",
            60,
            [
                {
                    "end_period_ts": 1780400000,
                    "yes_bid": {"open": "0.10", "low": "0.10", "high": "0.20", "close": "0.20"},
                    "yes_ask": {"open": "0.12", "low": "0.12", "high": "0.22", "close": "0.22"},
                    "price": {"open": "0.11", "low": "0.11", "high": "0.21", "close": "0.21"},
                    "volume": "10.00",
                    "open_interest": "20.00",
                }
            ],
        ) == 1
        assert store.upsert_kalshi_trades(
            [
                {
                    "trade_id": "trade-1",
                    "ticker": "KXHIGHTSFO-26JUN02-B68.5",
                    "count": 10.0,
                    "yes_price": 0.21,
                    "no_price": 0.79,
                    "created_time": "2026-06-02T18:00:00Z",
                    "is_block_trade": False,
                    "raw": {"trade_id": "trade-1"},
                }
            ]
        ) == 1

        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM dataset_station_observations").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM dataset_forecast_features").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM dataset_kalshi_markets").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM dataset_kalshi_candles").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM dataset_kalshi_trades").fetchone()[0] == 1


def test_kalshi_history_backfill_can_collect_trade_rows():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "dataset.db"
        store = DatasetStore(db_path)
        fake_client = _FakeKalshiHistoryClient()

        with patch("sfo_kalshi_quant.datasets.KalshiPublicClient", lambda timeout=20: fake_client):
            result = backfill_kalshi_history(
                store,
                start=date(2026, 6, 2),
                end=date(2026, 6, 2),
                include_candles=False,
                include_trades=True,
                max_pages=1,
                max_trade_pages=1,
            )

        with sqlite3.connect(db_path) as conn:
            markets = conn.execute("SELECT ticker, target_date FROM dataset_kalshi_markets").fetchall()
            trades = conn.execute(
                "SELECT trade_id, ticker, count, yes_price, no_price FROM dataset_kalshi_trades"
            ).fetchall()

    assert result.rows_written == 2
    assert markets == [("KXHIGHTSFO-26JUN02-B68.5", "2026-06-02")]
    assert trades == [("trade-1", "KXHIGHTSFO-26JUN02-B68.5", 10.0, 0.21, 0.79)]


def test_kalshi_history_backfill_keeps_market_rows_when_optional_detail_rate_limited():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "dataset.db"
        store = DatasetStore(db_path)
        fake_client = _RateLimitedKalshiHistoryClient()

        with patch("sfo_kalshi_quant.datasets.KalshiPublicClient", lambda timeout=20: fake_client):
            result = backfill_kalshi_history(
                store,
                start=date(2026, 6, 2),
                end=date(2026, 6, 2),
                include_candles=True,
                include_trades=False,
                max_pages=1,
            )

        with sqlite3.connect(db_path) as conn:
            market_count = conn.execute("SELECT COUNT(*) FROM dataset_kalshi_markets").fetchone()[0]
            candle_count = conn.execute("SELECT COUNT(*) FROM dataset_kalshi_candles").fetchone()[0]

    assert result.rows_written == 1
    assert "candles halted by HTTP 429" in result.detail
    assert market_count == 1
    assert candle_count == 0


def test_forecast_features_keep_two_stations_for_the_same_forecast_key():
    with TemporaryDirectory() as tmp:
        store = DatasetStore(Path(tmp) / "dataset.db")
        common = {
            "source": "test-model",
            "model": "baseline",
            "issued_at": "2026-07-09T12:00:00+00:00",
            "target_date": "2026-07-10",
            "valid_time": "2026-07-10",
            "variable": "temperature_2m_max",
            "units": "degF",
        }

        store.upsert_forecast_features([
            {**common, "station_id": "KSFO", "value": 68.0},
            {**common, "station_id": "KNYC", "value": 87.0},
        ])

        with sqlite3.connect(store.db_path) as conn:
            rows = conn.execute(
                "SELECT station_id, value FROM dataset_forecast_features ORDER BY station_id"
            ).fetchall()

    assert rows == [("KNYC", 87.0), ("KSFO", 68.0)]


def test_forecast_feature_migration_preserves_legacy_rows_as_ksfo():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "dataset.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE dataset_forecast_features (
                    source TEXT NOT NULL, model TEXT NOT NULL, issued_at TEXT NOT NULL,
                    target_date TEXT NOT NULL, valid_time TEXT NOT NULL, lead_hours REAL,
                    latitude REAL, longitude REAL, variable TEXT NOT NULL, value REAL NOT NULL,
                    units TEXT, source_url TEXT, raw_json TEXT NOT NULL, fetched_at TEXT NOT NULL,
                    PRIMARY KEY (source, model, issued_at, target_date, valid_time, variable)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO dataset_forecast_features
                VALUES ('legacy', 'baseline', '2026-07-08T12:00:00+00:00',
                        '2026-07-09', '2026-07-09', 24, 37.6, -122.4,
                        'temperature_2m_max', 67, 'degF', NULL, '{}',
                        '2026-07-08T12:01:00+00:00')
                """
            )

        DatasetStore(db_path)

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT station_id, source, value FROM dataset_forecast_features"
            ).fetchone()

    assert row == ("KSFO", "legacy", 67.0)


def test_orderbook_events_are_idempotent_and_preserve_queue_evidence():
    with TemporaryDirectory() as tmp:
        store = DatasetStore(Path(tmp) / "dataset.db")
        event = {
            "event_id": "book-1",
            "ticker": "KXHIGHTSFO-26JUL10-B68.5",
            "observed_at": "2026-07-09T18:00:00+00:00",
            "sequence": 42,
            "yes_bids": [[30, 12], [29, 8]],
            "no_bids": [[69, 4]],
            "source": "rest_snapshot",
        }

        store.upsert_kalshi_orderbook_events([event])
        store.upsert_kalshi_orderbook_events([{**event, "yes_bids": [[30, 15]]}])

        with sqlite3.connect(store.db_path) as conn:
            row = conn.execute(
                "SELECT event_id, sequence, yes_bids_json, source "
                "FROM dataset_kalshi_orderbook_events"
            ).fetchone()

    assert row == ("book-1", 42, "[[30,15]]", "rest_snapshot")


class _FakeKalshiHistoryClient:
    def list_historical_markets(self, *, series_ticker, limit, cursor=None):
        return {
            "markets": [
                {
                    "ticker": "KXHIGHTSFO-26JUN02-B68.5",
                    "event_ticker": "KXHIGHTSFO-26JUN02",
                    "status": "settled",
                    "open_time": "2026-06-01T14:00:00Z",
                    "close_time": "2026-06-03T08:00:00Z",
                    "yes_bid_dollars": "0.2000",
                    "yes_ask_dollars": "0.2200",
                    "volume_fp": "10.00",
                    "open_interest_fp": "20.00",
                    "strike_type": "between",
                    "floor_strike": 68,
                    "cap_strike": 69,
                }
            ],
            "cursor": "",
        }

    def get_historical_trades(self, *, ticker, min_ts, max_ts, limit=1000, cursor=None):
        return {
            "trades": [
                {
                    "trade_id": "trade-1",
                    "ticker": ticker,
                    "count_fp": "10.00",
                    "yes_price_dollars": "0.2100",
                    "no_price_dollars": "0.7900",
                    "created_time": "2026-06-02T18:00:00Z",
                    "is_block_trade": False,
                }
            ],
            "cursor": "",
        }


class _RateLimitedKalshiHistoryClient(_FakeKalshiHistoryClient):
    def get_historical_market_candlesticks(
        self,
        market_ticker,
        *,
        start_ts,
        end_ts,
        period_interval=60,
    ):
        raise HTTPError(
            url=f"https://example.test/{market_ticker}/candlesticks",
            code=429,
            msg="Too Many Requests",
            hdrs=None,
            fp=None,
        )
