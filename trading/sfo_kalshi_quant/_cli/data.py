"""Data collection, dataset, and report commands behind the CLI facade."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import date
from urllib.error import URLError

from ..cities import parse_city_slugs
from ..config import StrategyConfig, strategy_config_for_profile
from ..dataset_research import build_dataset_research, write_dataset_research
from ..datasets import (
    KSFO_ASOS_STATION,
    KSFO_ISD_STATION,
    DatasetResult,
    DatasetStore,
    backfill_gfs_mos,
    backfill_hrrr,
    backfill_iem_asos,
    backfill_kalshi_history,
    backfill_lamp,
    backfill_nbm,
    backfill_noaa_isd,
    backfill_open_meteo_historical_forecast,
    backfill_open_meteo_previous_runs,
)
from ..db import PaperStore
from ..forecast import ForecastDataError, SfoForecasterAdapter, parse_target_dates
from ..kalshi import KalshiPublicClient
from ..report import build_daily_report, write_report
from ..strategy_research import build_strategy_research, write_strategy_research


def _config(args: argparse.Namespace) -> StrategyConfig:
    base = strategy_config_for_profile(getattr(args, "risk_profile", None))
    if args.bankroll is None:
        return base
    return replace(base, paper_bankroll=args.bankroll)


def _cities_for_args(args: argparse.Namespace):
    value = getattr(args, "cities", None) or os.getenv("PAPER_CITIES", "all")
    return parse_city_slugs(value)


def cmd_collect(args: argparse.Namespace) -> int:
    targets = parse_target_dates(args.target_date)
    client = KalshiPublicClient()
    store = PaperStore(args.db_path)
    for city in _cities_for_args(args):
        adapter = SfoForecasterAdapter(args.forecaster_root, city=city)
        _collect_one_city(args, city, adapter, client, store, targets)
    return 0


def _collect_one_city(args, city, adapter, client, store, targets) -> None:
    for target in targets:
        try:
            forecast = adapter.latest_blend(target)
        except ForecastDataError as exc:
            print(f"warning: [{city.slug}] no forecast for {target.isoformat()} ({exc})", file=sys.stderr)
            continue
        try:
            event = client.find_event_by_date(target, series_ticker=city.series_ticker)
        except (URLError, OSError) as exc:
            print(f"warning: live Kalshi lookup failed for {target.isoformat()} ({exc})", file=sys.stderr)
            event = None
        forecast_id = store.record_forecast(forecast)
        market_id = None
        if event:
            market_id = store.record_market(event)
        print(f"stored forecast snapshot {forecast_id} for {target.isoformat()}")
        if market_id:
            print(f"stored market snapshot {market_id} for {event.event_ticker}")
        else:
            print("no Kalshi event found for that date yet")


def cmd_dataset_backfill(args: argparse.Namespace) -> int:
    start, end = _dataset_date_range(args)
    store = DatasetStore(args.db_path)
    sources = _dataset_sources(args.source)
    cities = parse_city_slugs(args.cities)
    total_rows = 0
    for source in sources:
        params = _dataset_run_params(args, source, start, end)
        run_id = store.start_run(source, params)
        try:
            if source == "noaa-isd":
                result = backfill_noaa_isd(
                    store,
                    stations=args.isd_stations or [KSFO_ISD_STATION],
                    start=start,
                    end=end,
                    timeout=args.timeout,
                )
            elif source == "iem-asos":
                if args.asos_stations:
                    result = backfill_iem_asos(
                        store, stations=args.asos_stations, start=start, end=end,
                        timeout=args.timeout,
                    )
                else:
                    result = _combine_dataset_results(
                        backfill_iem_asos(
                            store, stations=[city.nws_station_id.removeprefix("K")],
                            canonical_station_id=city.nws_station_id,
                            standard_utc_offset_hours=city.standard_utc_offset_hours,
                            start=start, end=end, timeout=args.timeout,
                        )
                        for city in cities
                    )
            elif source == "open-meteo-previous-runs":
                result = _combine_dataset_results(
                    backfill_open_meteo_previous_runs(
                        store, start=start, end=end, model=args.open_meteo_model,
                        previous_days=args.previous_days, station_id=city.nws_station_id,
                        latitude=city.latitude, longitude=city.longitude,
                        standard_utc_offset_hours=city.standard_utc_offset_hours,
                        timeout=args.timeout,
                    )
                    for city in cities
                )
            elif source == "open-meteo-historical-forecast":
                result = _combine_dataset_results(
                    backfill_open_meteo_historical_forecast(
                        store, start=start, end=end, model=args.open_meteo_model,
                        station_id=city.nws_station_id, latitude=city.latitude,
                        longitude=city.longitude,
                        standard_utc_offset_hours=city.standard_utc_offset_hours,
                        timeout=args.timeout,
                    )
                    for city in cities
                )
            elif source == "lamp":
                result = _combine_dataset_results(
                    backfill_lamp(
                        store, start=start, end=end, station_id=city.nws_station_id,
                        standard_utc_offset_hours=city.standard_utc_offset_hours,
                        timeout=args.timeout,
                    )
                    for city in cities
                )
            elif source == "gfs-mos":
                result = _combine_dataset_results(
                    backfill_gfs_mos(
                        store, start=start, end=end, station_id=city.nws_station_id,
                        standard_utc_offset_hours=city.standard_utc_offset_hours,
                        timeout=args.timeout,
                    )
                    for city in cities
                )
            elif source == "nbm":
                result = _combine_dataset_results(
                    backfill_nbm(
                        store, start=start, end=end, station_id=city.nws_station_id,
                        latitude=city.latitude, longitude=city.longitude,
                        standard_utc_offset_hours=city.standard_utc_offset_hours,
                        timeout=args.timeout,
                    )
                    for city in cities
                )
            elif source == "hrrr":
                result = _combine_dataset_results(
                    backfill_hrrr(
                        store, start=start, end=end, station_id=city.nws_station_id,
                        latitude=city.latitude, longitude=city.longitude,
                        standard_utc_offset_hours=city.standard_utc_offset_hours,
                        timeout=args.timeout,
                    )
                    for city in cities
                )
            elif source == "kalshi-history":
                result = backfill_kalshi_history(
                    store,
                    start=start,
                    end=end,
                    include_candles=args.kalshi_candles,
                    include_trades=args.kalshi_trades,
                    candle_interval=args.candle_interval,
                    max_pages=args.kalshi_max_pages,
                    max_trade_pages=args.kalshi_max_trade_pages,
                    series_tickers=[city.series_ticker for city in cities],
                    timeout=args.timeout,
                )
            else:  # pragma: no cover - argparse choices guard this
                raise ValueError(f"unknown dataset source: {source}")
        except Exception as exc:
            store.finish_run(run_id, status="failed", rows_written=0, message=str(exc))
            raise
        store.finish_run(run_id, status="success", rows_written=result.rows_written, message=result.detail)
        total_rows += result.rows_written
        print(f"{result.source}: wrote {result.rows_written} row(s) ({result.detail})")
    print(f"dataset backfill complete: {total_rows} total row(s)")
    return 0


def cmd_dataset_status(args: argparse.Namespace) -> int:
    store = DatasetStore(args.db_path)
    tables = (
        "dataset_runs",
        "dataset_station_observations",
        "dataset_forecast_features",
        "dataset_kalshi_markets",
        "dataset_kalshi_candles",
        "dataset_kalshi_trades",
        "dataset_kalshi_orderbook_events",
    )
    with store.connect() as conn:
        for table in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table}: {count}")
        print("")
        print("recent dataset runs:")
        rows = conn.execute(
            """
            SELECT id, source, status, rows_written, started_at, completed_at, message
            FROM dataset_runs
            ORDER BY id DESC
            LIMIT 10
            """
        ).fetchall()
    if not rows:
        print("(none)")
        return 0
    for row in rows:
        run_id, source, status, rows_written, started_at, completed_at, message = row
        completed = completed_at or "running"
        detail = f" - {message}" if message else ""
        print(f"{run_id}: {source} {status} rows={rows_written} {started_at} -> {completed}{detail}")
    return 0


def cmd_dataset_research(args: argparse.Namespace) -> int:
    payload = build_dataset_research(
        db_path=args.db_path,
        forecaster_root=args.forecaster_root,
        min_matched_rows=args.min_matched_rows,
        min_mae_improvement_f=args.min_mae_improvement,
        holdout_fraction=args.holdout_fraction,
    )
    if args.output:
        write_dataset_research(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _dataset_date_range(args: argparse.Namespace) -> tuple[date, date]:
    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date) if args.end_date else start
    if end < start:
        raise ValueError("--end-date must be on or after --start-date")
    return start, end


def _combine_dataset_results(results) -> DatasetResult:
    rows = list(results)
    if not rows:
        return DatasetResult("station-aware", 0, "no cities selected")
    return DatasetResult(
        rows[0].source,
        sum(row.rows_written for row in rows),
        f"{len(rows)} cities; " + "; ".join(row.detail for row in rows),
    )


def _dataset_sources(source: str) -> list[str]:
    if source == "tier1":
        return [
            "noaa-isd",
            "iem-asos",
            "open-meteo-previous-runs",
            "open-meteo-historical-forecast",
            "lamp",
            "gfs-mos",
            "nbm",
            "hrrr",
            "kalshi-history",
        ]
    return [source]


def _dataset_run_params(args: argparse.Namespace, source: str, start: date, end: date) -> dict[str, object]:
    return {
        "source": source,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "cities": args.cities,
        "isd_stations": args.isd_stations or [KSFO_ISD_STATION],
        "asos_stations": args.asos_stations or [KSFO_ASOS_STATION],
        "open_meteo_model": args.open_meteo_model,
        "previous_days": args.previous_days,
        "kalshi_candles": args.kalshi_candles,
        "kalshi_trades": args.kalshi_trades,
        "candle_interval": args.candle_interval,
        "kalshi_max_pages": args.kalshi_max_pages,
        "kalshi_max_trade_pages": args.kalshi_max_trade_pages,
    }


def cmd_daily_report(args: argparse.Namespace) -> int:
    config = _config(args)
    payload = build_daily_report(
        forecaster_root=args.forecaster_root,
        targets=parse_target_dates(args.target_date),
        config=config,
        side=args.side,
        offline_events=args.offline_events,
        observed_high=args.observed_high,
        no_ensemble=args.no_ensemble,
        ensemble_timeout=args.ensemble_timeout,
        allow_live_market=not args.no_live_market,
        calibration_source=args.calibration_source,
    )
    if args.output:
        write_report(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_strategy_research(args: argparse.Namespace) -> int:
    config = _config(args)
    payload = build_strategy_research(
        forecaster_root=args.forecaster_root,
        db_path=args.db_path,
        config=config,
        calibration_min_train=args.calibration_min_train,
    )
    if args.output:
        write_strategy_research(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0
