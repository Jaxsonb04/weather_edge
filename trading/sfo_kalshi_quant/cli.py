from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
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
from ._cli import data as _data
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


def _is_retryable_sqlite_lock(exc: sqlite3.OperationalError) -> bool:
    error_code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(error_code, int):
        base_error_code = error_code & 0xFF
        return base_error_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}

    # Older Python/SQLite combinations may not expose sqlite_errorcode. Keep
    # that compatibility path deliberately narrow so unrelated SQL mentioning
    # a column or table named "locked" remains fail-fast.
    message = str(exc).strip().casefold()
    legacy_lock_messages = {
        "database is locked",
        "database table is locked",
        "database schema is locked",
    }
    return any(
        message == lock_message or message.startswith(f"{lock_message}: ")
        for lock_message in legacy_lock_messages
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ForecastDataError as exc:
        print(f"forecast data error: {exc}", file=sys.stderr)
        return 2
    except sqlite3.OperationalError as exc:
        if _is_retryable_sqlite_lock(exc):
            print(f"temporary sqlite lock: {exc}", file=sys.stderr)
            return 75
        print(f"error: {exc}", file=sys.stderr)
        return 1
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


def _scan_command_dependencies() -> _scan.ScanCommandDependencies:
    return _scan.ScanCommandDependencies(
        cities_for_args=_cities_for_args,
        config_for_args=_config,
        resolve_targets=_resolve_analysis_targets,
        client_factory=KalshiPublicClient,
        store_factory=PaperStore,
        city_config_factory=config_for_city,
        adapter_factory=SfoForecasterAdapter,
        calibrator_factory=ResidualCalibrator,
        sizing_model_factory=_build_sizing_model,
        analyze_target=_analyze_one_target,
        tail_basket_target=_tail_basket_one_target,
        arbitrage_target=_arbitrage_one_target,
        portfolio_target=_portfolio_scan_one_target,
        city_lookup=get_city,
    )


def cmd_analyze(args: argparse.Namespace) -> int:
    return _scan.cmd_analyze(args, dependencies=_scan_command_dependencies())


def cmd_tail_basket(args: argparse.Namespace) -> int:
    return _scan.cmd_tail_basket(args, dependencies=_scan_command_dependencies())


def cmd_arbitrage(args: argparse.Namespace) -> int:
    return _scan.cmd_arbitrage(args, dependencies=_scan_command_dependencies())


def cmd_portfolio_scan(args: argparse.Namespace) -> int:
    return _scan.cmd_portfolio_scan(args, dependencies=_scan_command_dependencies())


_collect_one_city = _data._collect_one_city
_combine_dataset_results = _data._combine_dataset_results
_dataset_date_range = _data._dataset_date_range
_dataset_run_params = _data._dataset_run_params
_dataset_sources = _data._dataset_sources


def _sync_data_bindings() -> None:
    for name in (
        "DatasetStore",
        "KalshiPublicClient",
        "PaperStore",
        "SfoForecasterAdapter",
        "_cities_for_args",
        "_config",
        "backfill_gfs_mos",
        "backfill_hrrr",
        "backfill_iem_asos",
        "backfill_kalshi_history",
        "backfill_lamp",
        "backfill_nbm",
        "backfill_noaa_isd",
        "backfill_open_meteo_historical_forecast",
        "backfill_open_meteo_previous_runs",
        "build_daily_report",
        "build_dataset_research",
        "build_strategy_research",
        "parse_target_dates",
        "write_dataset_research",
        "write_report",
        "write_strategy_research",
    ):
        setattr(_data, name, globals()[name])


def _data_dispatch(name: str, args: argparse.Namespace) -> int:
    _sync_data_bindings()
    return getattr(_data, name)(args)


def cmd_collect(args: argparse.Namespace) -> int:
    return _data_dispatch("cmd_collect", args)


def cmd_dataset_backfill(args: argparse.Namespace) -> int:
    return _data_dispatch("cmd_dataset_backfill", args)


def cmd_dataset_status(args: argparse.Namespace) -> int:
    return _data_dispatch("cmd_dataset_status", args)


def cmd_dataset_research(args: argparse.Namespace) -> int:
    return _data_dispatch("cmd_dataset_research", args)


def cmd_daily_report(args: argparse.Namespace) -> int:
    return _data_dispatch("cmd_daily_report", args)


def cmd_strategy_research(args: argparse.Namespace) -> int:
    return _data_dispatch("cmd_strategy_research", args)


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
