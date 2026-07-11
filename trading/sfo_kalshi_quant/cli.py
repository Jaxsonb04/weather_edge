from __future__ import annotations

import argparse
import json
import math
import os
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError

from .backtest import run_walk_forward_calibration_backtest
from .backtest_rescore import run_rescore
from .arbitrage import ArbitrageOpportunity, build_arbitrage_opportunities
from .colors import Color
from .cities import CITIES, CityConfig, city_for_market_ticker, get_city, parse_city_slugs
from .config import (
    DEFAULT_DB_PATH,
    DEFAULT_FORECASTER_ROOT,
    SFO_TZ,
    SERIES_TICKER,
    StrategyConfig,
    config_for_city,
    intraday_timezone_for_city,
    normalize_risk_profile_name,
    strategy_config_for_profile,
)
from .consensus import MarketConsensus, build_market_consensus
from .db import PaperStore
from .dataset_research import build_dataset_research, write_dataset_research
from .datasets import (
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
from .ensemble import OpenMeteoEnsembleError, SfoEnsembleClient
from .exits import (
    DEFAULT_NO_STOP_LOSS_PCT,
    DEFAULT_NO_TAKE_PROFIT_PCT,
    DEFAULT_RESEARCH_NO_SETTLEMENT_FIRST_MIN_COST,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
    DEFAULT_YES_STOP_LOSS_PCT,
    DEFAULT_YES_TAKE_PROFIT_PCT,
    decide_exit,
)
from .fees import quadratic_fee_average_per_contract
from .forecast import (
    ForecastDataError,
    SfoForecasterAdapter,
    has_forecaster_observed_high_adjustment,
    parse_target_date,
    parse_target_dates,
)
from .kalshi import KalshiPublicClient, KalshiUnavailable, load_event_snapshots
from .models import (
    BucketProbability,
    EnsembleSnapshot,
    EventSnapshot,
    ForecastSnapshot,
    IntradaySnapshot,
    MarketBin,
    TradeDecision,
    format_event_date_token,
    target_date_from_event_ticker,
)
from .paper import ArbitrageContainmentError, PaperTrader
from .portfolio import PortfolioPlan, allocate_portfolio
from .probability import ResidualCalibrator
from .report import build_daily_report, write_report
from .posterior_kelly import load_posterior_kelly_model
from .risk import TradeEvaluator
from .settlement_day import settlement_clock, settlement_today
from .standard_bins import fallback_bins
from .strategy_research import build_strategy_research, write_strategy_research
from .summary import build_paper_summary, write_paper_summary, write_paper_summary_csv
from .synthetic_blend import build_synthetic_blend_calibration, write_synthetic_blend_calibration
from .tail_basket import TailBasket, build_tail_basket
from . import monitor as _monitor
from ._cli import scan as _scan
from ._cli import paper as _paper
from ._cli import backtest as _backtest_cli
from ._cli import parser as _parser
from ._cli.format import (
    _color_edge,
    _color_prob,
    _color_prob_optional,
    _color_status,
    _fmt_opt,
    _forecast_context_pieces,
    _format_pnl,
    _print_analysis,
    _print_arbitrage,
    _print_consensus_line,
    _print_portfolio_scan,
    _print_tail_basket,
)


# Hours are measured on the fixed-PST settlement clock, so 14 is 15:00 PDT
# civil time during DST — the same wall-clock cutoff the gate used before the
# settlement-day unification (and one hour earlier in winter, which only
# tightens the gate).
DEFAULT_SAME_DAY_ENTRY_CUTOFF_HOUR = 14
DEFAULT_MODEL_VETO_MAX_LOSS_PCT = 60.0
DEFAULT_MODEL_VETO_BUFFER = 0.08
SAME_DAY_HEARTBEAT_OBSERVATION_MAX_AGE_MINUTES = 90.0
_UNSET = _scan._UNSET

# Public compatibility aliases for callers that historically imported monitor
# helpers from this CLI module. The implementations now live with the execution
# engine, while this module remains the stable parser/wiring facade.
_all_public_trades_for_ticker = _monitor._all_public_trades_for_ticker
_fill_resting_orders_against_live_book = _monitor._fill_resting_orders_against_live_book
_heartbeat_timestamp_is_fresh = _monitor._heartbeat_timestamp_is_fresh
_is_guaranteed_payoff_group_row = _monitor._is_guaranteed_payoff_group_row
_monitor_market_lookup = _monitor._monitor_market_lookup
_monitor_thresholds_for_side = _monitor._monitor_thresholds_for_side
_same_day_no_basket_veto_reason = _monitor._same_day_no_basket_veto_reason
_settlement_first_no_min_cost_for_order = _monitor._settlement_first_no_min_cost_for_order
_validate_monitor_args = _monitor._validate_monitor_args


def _refresh_same_day_model_reads(
    store: PaperStore,
    rows,
    *,
    forecaster_root: Path,
    log=print,
) -> int:
    return _monitor._refresh_same_day_model_reads(
        store,
        rows,
        forecaster_root=forecaster_root,
        log=log,
        clock=settlement_clock,
        adapter_factory=SfoForecasterAdapter,
    )


def cmd_paper_monitor(args: argparse.Namespace) -> int:
    return _monitor.run_paper_monitor(
        args,
        client_factory=KalshiPublicClient,
        strategy_config_factory=strategy_config_for_profile,
        decide_exit_fn=decide_exit,
        refresh_model_reads=_refresh_same_day_model_reads,
    )


# Stable import facade for scan helpers. Command tests and downstream scripts
# historically patch dependencies on this module, so dispatch refreshes only
# those dependency bindings before entering the extracted scan engine.
ScanContext = _scan.ScanContext
_block_entry_decisions = _scan._block_entry_decisions
_build_sizing_model = _scan._build_sizing_model
_cached_paper_entry_pause_reason = _scan._cached_paper_entry_pause_reason
_clamp_sizing_equity = _scan._clamp_sizing_equity
_decision_signal_approved = _scan._decision_signal_approved
_entry_blocked_shadow_decisions = _scan._entry_blocked_shadow_decisions
_intraday_for_target = _scan._intraday_for_target
_place_portfolio_orders = _scan._place_portfolio_orders
_portfolio_decision_key = _scan._portfolio_decision_key
_portfolio_decisions_for_recording = _scan._portfolio_decisions_for_recording
_rolling_targets_count = _scan._rolling_targets_count
_sizing_bankroll = _scan._sizing_bankroll


def _sync_scan_bindings() -> None:
    for name in (
        "KalshiPublicClient",
        "ResidualCalibrator",
        "SfoForecasterAdapter",
        "TradeEvaluator",
        "_build_sizing_model",
        "_default_calibration_source",
        "_enforce_live_forecast_freshness",
        "_intraday_for_target",
        "_risk_profile_name",
        "_sizing_bankroll",
        "build_market_consensus",
        "settlement_clock",
    ):
        setattr(_scan, name, globals()[name])


def _scan_dispatch(name: str, *args, **kwargs):
    _sync_scan_bindings()
    return getattr(_scan, name)(*args, **kwargs)


def _resolve_analysis_targets(*args, **kwargs):
    return _scan_dispatch("_resolve_analysis_targets", *args, **kwargs)


def _rolling_live_event_targets(*args, **kwargs):
    return _scan_dispatch("_rolling_live_event_targets", *args, **kwargs)


def build_scan_context(*args, **kwargs):
    return _scan_dispatch("build_scan_context", *args, **kwargs)


def _analyze_one_target(*args, **kwargs):
    return _scan_dispatch("_analyze_one_target", *args, **kwargs)


def _portfolio_scan_one_target(*args, **kwargs):
    return _scan_dispatch("_portfolio_scan_one_target", *args, **kwargs)


def _arbitrage_one_target(*args, **kwargs):
    return _scan_dispatch("_arbitrage_one_target", *args, **kwargs)


def _tail_basket_one_target(*args, **kwargs):
    return _scan_dispatch("_tail_basket_one_target", *args, **kwargs)


def _ensemble_for_target(*args, **kwargs):
    return _scan_dispatch("_ensemble_for_target", *args, **kwargs)


def _paper_entry_gate_for_target(*args, **kwargs):
    return _scan_dispatch("_paper_entry_gate_for_target", *args, **kwargs)


def _same_day_entry_cutoff_hour() -> int:
    return _scan_dispatch("_same_day_entry_cutoff_hour")


def _sync_paper_bindings() -> None:
    for name in (
        "KalshiPublicClient",
        "SfoForecasterAdapter",
        "_cities_for_args",
        "_config",
        "settlement_clock",
        "settlement_today",
    ):
        setattr(_paper, name, globals()[name])


def _paper_dispatch(name: str, *args, **kwargs):
    _sync_paper_bindings()
    return getattr(_paper, name)(*args, **kwargs)


def cmd_paper_summary(args: argparse.Namespace) -> int:
    return _paper_dispatch("cmd_paper_summary", args)


def cmd_paper_report(args: argparse.Namespace) -> int:
    return _paper_dispatch("cmd_paper_report", args)


def cmd_paper_buy(args: argparse.Namespace) -> int:
    return _paper_dispatch("cmd_paper_buy", args)


def cmd_paper_close(args: argparse.Namespace) -> int:
    return _paper_dispatch("cmd_paper_close", args)


def cmd_paper_settle(args: argparse.Namespace) -> int:
    return _paper_dispatch("cmd_paper_settle", args)


def cmd_paper_resettle(args: argparse.Namespace) -> int:
    return _paper_dispatch("cmd_paper_resettle", args)


def cmd_paper_prune(args: argparse.Namespace) -> int:
    return _paper_dispatch("cmd_paper_prune", args)


def cmd_paper_check_foreign_keys(args: argparse.Namespace) -> int:
    return _paper_dispatch("cmd_paper_check_foreign_keys", args)


def cmd_paper_auto_settle(args: argparse.Namespace) -> int:
    return _paper_dispatch("cmd_paper_auto_settle", args)


def _completed_open_target_dates(*args, **kwargs):
    return _paper_dispatch("_completed_open_target_dates", *args, **kwargs)


def cmd_paper_archive(args: argparse.Namespace) -> int:
    return _paper_dispatch("cmd_paper_archive", args)


def cmd_paper_features(args: argparse.Namespace) -> int:
    return _paper_dispatch("cmd_paper_features", args)


def _sync_backtest_bindings() -> None:
    for name in ("SfoForecasterAdapter", "_config", "_risk_profile_name"):
        setattr(_backtest_cli, name, globals()[name])


def _backtest_dispatch(name: str, args: argparse.Namespace) -> int:
    _sync_backtest_bindings()
    return getattr(_backtest_cli, name)(args)


def cmd_backtest_calibration(args: argparse.Namespace) -> int:
    return _backtest_dispatch("cmd_backtest_calibration", args)


def cmd_synthetic_blend_calibration(args: argparse.Namespace) -> int:
    return _backtest_dispatch("cmd_synthetic_blend_calibration", args)


def cmd_backtest_market(args: argparse.Namespace) -> int:
    return _backtest_dispatch("cmd_backtest_market", args)


def cmd_backtest_signals(args: argparse.Namespace) -> int:
    return _backtest_dispatch("cmd_backtest_signals", args)


def cmd_backtest_rescore(args: argparse.Namespace) -> int:
    return _backtest_dispatch("cmd_backtest_rescore", args)


def build_parser() -> argparse.ArgumentParser:
    return _parser.build_parser(command_module=sys.modules[__name__])


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ForecastDataError as exc:
        print(f"forecast data error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _default_calibration_source() -> str:
    """Match the AWS deploy default (pinned lstm) unless explicitly overridden.

    A silent local default of auto made local research runs incomparable to
    the deployed paper scanner.
    """

    return os.getenv("SFO_TRADING_SIGNAL_CALIBRATION_SOURCE", "lstm")


def _default_cities() -> str:
    return os.getenv("PAPER_CITIES", "all")


def _cities_for_args(args: argparse.Namespace) -> tuple[CityConfig, ...]:
    return parse_city_slugs(getattr(args, "cities", None) or _default_cities())


def _default_paper_entry_mode() -> str:
    raw = os.getenv("PAPER_ENTRY_MODE", "market").strip().lower().replace("_", "-")
    if raw in {"limit", "limit-order", "paper-limit"}:
        return "limit"
    return "market"


def _env_float_default(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def cmd_analyze(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    if args.paper_stake is not None and args.daily_budget is not None:
        raise ValueError("use either --paper-stake or --daily-budget, not both")
    base_config = _config(args)
    kalshi_client = KalshiPublicClient()
    store = PaperStore(args.db_path)
    scanned_any = False
    for city_idx, city in enumerate(_cities_for_args(args)):
        if city_idx:
            print("")
            print("#" * 92)
            print("")
        config = config_for_city(base_config, city)
        targets, live_events_by_target = _resolve_analysis_targets(
            args, color, kalshi_client, city
        )
        if not targets:
            print(color.yellow(f"[{city.slug}] no eligible target dates found"))
            continue
        adapter = SfoForecasterAdapter(args.forecaster_root, city=city)
        # Fail-soft per city: a city with no calibration history yet (archive
        # still backfilling) or a stale forecast must not stall the other
        # fourteen books.
        try:
            outcomes = adapter.load_calibration_outcomes(args.calibration_source)
            calibrator = ResidualCalibrator(outcomes, config)
        except (ForecastDataError, ValueError) as exc:
            print(
                color.yellow(f"[{city.slug}] skipped: calibration unavailable ({exc})"),
                file=sys.stderr,
            )
            continue

        emos_lookup = (
            adapter.load_emos_mu_sigma(lead_days=None)
            if config.emos_distribution_enabled
            else {}
        )
        sizing_model = _build_sizing_model(config, store)
        pause_reasons: dict[tuple[str, str], str | None] = {}

        for idx, target in enumerate(targets):
            if idx:
                print("")
                print("=" * 92)
                print("")
            try:
                _analyze_one_target(
                    args,
                    target,
                    adapter,
                    calibrator,
                    config,
                    store,
                    color,
                    city=city,
                    event_hint=live_events_by_target.get(target),
                    event_lookup_done=target in live_events_by_target,
                    kalshi_client=kalshi_client,
                    emos_lookup=emos_lookup,
                    sizing_model=sizing_model,
                    pause_reasons=pause_reasons,
                )
                scanned_any = True
            except ForecastDataError as exc:
                print(
                    color.yellow(f"[{city.slug}] {target.isoformat()}: skipped ({exc})"),
                    file=sys.stderr,
                )
    if not scanned_any:
        print(color.yellow("no city produced an analyzable target"))
    return 0


def cmd_tail_basket(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    city = get_city(getattr(args, "city", None) or "sfo")
    config = config_for_city(_config(args), city)
    kalshi_client = KalshiPublicClient()
    targets, live_events_by_target = _resolve_analysis_targets(args, color, kalshi_client, city)
    if not targets:
        print(color.yellow("no eligible target dates found"))
        return 0
    adapter = SfoForecasterAdapter(args.forecaster_root, city=city)
    outcomes = adapter.load_calibration_outcomes(args.calibration_source)
    calibrator = ResidualCalibrator(outcomes, config)
    store = PaperStore(args.db_path)
    emos_lookup = (
        adapter.load_emos_mu_sigma(lead_days=None)
        if config.emos_distribution_enabled
        else {}
    )
    sizing_model = _build_sizing_model(config, store)
    pause_reasons: dict[tuple[str, str], str | None] = {}

    for idx, target in enumerate(targets):
        if idx:
            print("")
            print("=" * 92)
            print("")
        _tail_basket_one_target(
            args,
            target,
            adapter,
            calibrator,
            config,
            store,
            color,
            city=city,
            event_hint=live_events_by_target.get(target),
            event_lookup_done=target in live_events_by_target,
            kalshi_client=kalshi_client,
            emos_lookup=emos_lookup,
            sizing_model=sizing_model,
            pause_reasons=pause_reasons,
        )
    return 0


def cmd_arbitrage(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    city = get_city(getattr(args, "city", None) or "sfo")
    config = config_for_city(_config(args), city)
    kalshi_client = KalshiPublicClient()
    targets, live_events_by_target = _resolve_analysis_targets(args, color, kalshi_client, city)
    if not targets:
        print(color.yellow("no eligible target dates found"))
        return 0
    store = PaperStore(args.db_path)
    pause_reasons: dict[tuple[str, str], str | None] = {}

    for idx, target in enumerate(targets):
        if idx:
            print("")
            print("=" * 92)
            print("")
        _arbitrage_one_target(
            args,
            target,
            config,
            store,
            color,
            city=city,
            event_hint=live_events_by_target.get(target),
            event_lookup_done=target in live_events_by_target,
            kalshi_client=kalshi_client,
            pause_reasons=pause_reasons,
        )
    return 0


def cmd_portfolio_scan(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    base_config = _config(args)
    kalshi_client = KalshiPublicClient()
    store = PaperStore(args.db_path)
    scanned_any = False
    fatal_containment = False
    for city_idx, city in enumerate(_cities_for_args(args)):
        if city_idx:
            print("")
            print("#" * 92)
            print("")
        config = config_for_city(base_config, city)
        targets, live_events_by_target = _resolve_analysis_targets(
            args, color, kalshi_client, city
        )
        if not targets:
            print(color.yellow(f"[{city.slug}] no eligible target dates found"))
            continue
        adapter = SfoForecasterAdapter(args.forecaster_root, city=city)
        try:
            outcomes = adapter.load_calibration_outcomes(args.calibration_source)
            calibrator = ResidualCalibrator(outcomes, config)
        except (ForecastDataError, ValueError) as exc:
            print(
                color.yellow(f"[{city.slug}] skipped: calibration unavailable ({exc})"),
                file=sys.stderr,
            )
            continue

        emos_lookup = (
            adapter.load_emos_mu_sigma(lead_days=None)
            if config.emos_distribution_enabled
            else {}
        )
        sizing_model = _build_sizing_model(config, store)
        pause_reasons: dict[tuple[str, str], str | None] = {}

        for idx, target in enumerate(targets):
            if idx:
                print("")
                print("=" * 92)
                print("")
            try:
                _portfolio_scan_one_target(
                    args,
                    target,
                    adapter,
                    calibrator,
                    config,
                    store,
                    color,
                    city=city,
                    event_hint=live_events_by_target.get(target),
                    event_lookup_done=target in live_events_by_target,
                    kalshi_client=kalshi_client,
                    emos_lookup=emos_lookup,
                    sizing_model=sizing_model,
                    pause_reasons=pause_reasons,
                )
                scanned_any = True
            except ArbitrageContainmentError as exc:
                fatal_containment = True
                print(
                    color.yellow(f"[{city.slug}] {target.isoformat()}: skipped ({exc})"),
                    file=sys.stderr,
                )
            except ForecastDataError as exc:
                print(
                    color.yellow(f"[{city.slug}] {target.isoformat()}: skipped ({exc})"),
                    file=sys.stderr,
                )
    if not scanned_any:
        print(color.yellow("no city produced a scannable target"))
    return 1 if fatal_containment else 0


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


def _config(args: argparse.Namespace) -> StrategyConfig:
    base = strategy_config_for_profile(getattr(args, "risk_profile", None))
    if args.bankroll is None:
        return base
    return replace(base, paper_bankroll=args.bankroll)


def _risk_profile_name(args: argparse.Namespace) -> str:
    explicit = getattr(args, "risk_profile", None)
    return normalize_risk_profile_name(str(explicit) if explicit else None)


def _analysis_sides(side_arg: str) -> tuple[str, ...]:
    if side_arg == "both":
        return ("YES", "NO")
    return (side_arg.upper(),)


def _enforce_live_forecast_freshness(forecast, config: StrategyConfig) -> None:
    today = parse_target_date("today")
    if forecast.target_date < today:
        return
    age_hours = forecast.age_hours()
    if age_hours is None:
        raise ForecastDataError("forecast snapshot has no readable fetched_at timestamp")
    if age_hours > config.max_forecast_age_hours:
        raise ForecastDataError(
            f"forecast snapshot for {forecast.target_date.isoformat()} is stale "
            f"({age_hours:.1f}h old; max {config.max_forecast_age_hours:.1f}h)"
        )




if __name__ == "__main__":
    raise SystemExit(main())
