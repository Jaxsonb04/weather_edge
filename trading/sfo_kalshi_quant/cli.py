from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError

from .backtest import run_walk_forward_calibration_backtest
from .backtest_rescore import run_rescore
from .arbitrage import ArbitrageOpportunity, build_arbitrage_opportunities
from .colors import Color
from .config import (
    DEFAULT_DB_PATH,
    DEFAULT_FORECASTER_ROOT,
    SFO_TZ,
    SERIES_TICKER,
    StrategyConfig,
    normalize_risk_profile_name,
    strategy_config_for_profile,
)
from .consensus import MarketConsensus, build_market_consensus
from .db import PaperStore
from .dataset_research import build_dataset_research, write_dataset_research
from .datasets import (
    KSFO_ASOS_STATION,
    KSFO_ISD_STATION,
    DatasetStore,
    backfill_iem_asos,
    backfill_kalshi_history,
    backfill_noaa_isd,
    backfill_open_meteo_historical_forecast,
    backfill_open_meteo_previous_runs,
)
from .ensemble import OpenMeteoEnsembleError, SfoEnsembleClient
from .exits import (
    DEFAULT_NO_STOP_LOSS_PCT,
    DEFAULT_NO_TAKE_PROFIT_PCT,
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
    EnsembleSnapshot,
    EventSnapshot,
    IntradaySnapshot,
    format_event_date_token,
    target_date_from_event_ticker,
)
from .paper import PaperTrader
from .portfolio import PortfolioPlan, allocate_portfolio
from .probability import ResidualCalibrator
from .report import build_daily_report, write_report
from .risk import TradeEvaluator
from .settlement_day import settlement_clock, settlement_today
from .settlement import fetch_latest_clisfo, fetch_recent_clisfo_settlements
from .standard_bins import standard_sfo_bins
from .strategy_research import build_strategy_research, write_strategy_research
from .summary import build_paper_summary, write_paper_summary, write_paper_summary_csv
from .synthetic_blend import build_synthetic_blend_calibration, write_synthetic_blend_calibration
from .tail_basket import TailBasket, build_tail_basket


# Hours are measured on the fixed-PST settlement clock, so 14 is 15:00 PDT
# civil time during DST — the same wall-clock cutoff the gate used before the
# settlement-day unification (and one hour earlier in winter, which only
# tightens the gate).
DEFAULT_SAME_DAY_ENTRY_CUTOFF_HOUR = 14
DEFAULT_MODEL_VETO_MAX_LOSS_PCT = 60.0
DEFAULT_MODEL_VETO_BUFFER = 0.08


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sfo-kalshi", description="SFO Kalshi weather paper trader")
    parser.add_argument("--forecaster-root", type=Path, default=DEFAULT_FORECASTER_ROOT)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--bankroll", type=float, default=None)
    parser.add_argument(
        "--risk-profile",
        # type normalizes legacy aliases (balanced/conservative -> live;
        # exploratory/fast-feedback/fast -> research) before the choices check,
        # so old env CSVs and muscle memory keep working while only the two
        # canonical names are advertised.
        type=normalize_risk_profile_name,
        choices=("live", "research"),
        default=None,
        metavar="{live,research}",
        help=(
            "Risk gate profile. Defaults to PAPER_RISK_PROFILE or live. "
            "'live' is the real-money-intent exploiter (paper-only until the "
            "readiness gate passes); 'research' is the loose, tiny-size data "
            "collector. Legacy names (balanced/exploratory/fast-feedback) are "
            "accepted and map onto these two."
        ),
    )
    parser.add_argument("--no-color", action="store_true", help="Disable colored command output")

    sub = parser.add_subparsers(required=True)

    analyze = sub.add_parser("analyze", help="Rank current/paper SFO market opportunities")
    analyze.add_argument(
        "--target-date",
        default="tomorrow",
        help="today, tomorrow, both, rolling, comma-list, or YYYY-MM-DD",
    )
    analyze.add_argument(
        "--side",
        choices=("yes", "no", "both"),
        default="both",
        help="Which contract side to rank. Default is both to include BUY_YES and BUY_NO candidates.",
    )
    analyze.add_argument("--offline-events", type=Path, help="Saved Kalshi events JSON")
    analyze.add_argument(
        "--observed-high",
        type=float,
        help="Override today's observed high-so-far in F, e.g. 67 if the current official/app high is 67F.",
    )
    analyze.add_argument("--place-paper", action="store_true", help="Record approved paper orders")
    analyze.add_argument(
        "--paper-entry-mode",
        choices=("market", "limit"),
        default=_default_paper_entry_mode(),
        help=(
            "How --place-paper books approved analyzer entries. market preserves "
            "the historical immediate paper fill; limit records a paper buy-limit "
            "at the lower-confidence reservation price. Default: PAPER_ENTRY_MODE or market."
        ),
    )
    analyze.add_argument(
        "--skip-context-snapshots",
        action="store_true",
        help=(
            "Skip forecast/probability/market snapshot writes; decision snapshots "
            "are still recorded. Use for the second and later profiles of a "
            "multi-profile scan so shared context is not duplicated."
        ),
    )
    analyze.add_argument(
        "--paper-stake",
        type=float,
        help="Paper dollars to spend per approved trade, e.g. 10 for a $10 paper bet.",
    )
    analyze.add_argument(
        "--daily-budget",
        type=float,
        help="Maximum paper dollars to risk for the target date; risk-sized trades are scaled down only if needed.",
    )
    analyze.add_argument(
        "--no-ensemble",
        action="store_true",
        help="Disable Open-Meteo GFS ensemble shape input and use residual calibration only.",
    )
    analyze.add_argument(
        "--calibration-source",
        choices=("auto", "lstm", "clean-blend"),
        default=_default_calibration_source(),
        help=(
            "Residual source for trading probabilities. Defaults to "
            "SFO_TRADING_SIGNAL_CALIBRATION_SOURCE or lstm, matching the AWS "
            "deploy so local research stays comparable. auto prefers "
            "clean-blend when enough rows exist."
        ),
    )
    analyze.add_argument(
        "--ensemble-timeout",
        type=float,
        default=12.0,
        help="Seconds to wait for the station-aligned Open-Meteo ensemble fetch.",
    )
    analyze.set_defaults(func=cmd_analyze)

    basket = sub.add_parser(
        "tail-basket",
        help="Build a forecast-centered paper basket: far-tail NOs plus a small center YES",
    )
    basket.add_argument(
        "--target-date",
        default="rolling",
        help=(
            "today, tomorrow, both, rolling, comma-list, or YYYY-MM-DD. "
            "Default rolling waits for the next active Kalshi events."
        ),
    )
    basket.add_argument("--offline-events", type=Path, help="Saved Kalshi events JSON")
    basket.add_argument(
        "--observed-high",
        type=float,
        help="Override today's observed high-so-far in F.",
    )
    basket.add_argument("--place-paper", action="store_true", help="Record approved paper basket legs")
    basket.add_argument(
        "--skip-context-snapshots",
        action="store_true",
        help="Skip forecast/probability/market snapshot writes; decision snapshots are still recorded.",
    )
    basket.add_argument(
        "--tail-distance",
        type=float,
        default=3.0,
        help="Only use edge buckets fully outside forecast +/- this many F. Default: 3.",
    )
    basket.add_argument(
        "--tail-stake",
        type=float,
        default=5.0,
        help="Paper dollars to spend on each approved far-tail NO leg. Default: 5.",
    )
    basket.add_argument(
        "--center-stake",
        type=float,
        default=1.0,
        help="Paper dollars to spend on the approved center YES leg. Use 0 to disable. Default: 1.",
    )
    basket.add_argument(
        "--basket-sizing",
        choices=("fixed", "kelly"),
        default="fixed",
        help=(
            "How to size approved far-tail NO legs. 'fixed' spends --tail-stake "
            "dollars per leg (the small bounded guardrail). 'kelly' instead sizes "
            "each leg off the evaluator's risk budget (quarter-Kelly + comfort "
            "boost, capped by max_position/event_risk_pct) so the basket deploys "
            "meaningful capital and swings -- still bounded by --max-basket-spend "
            "and --max-worst-case-loss, and still held to a non-negative "
            "lower-bound edge. Default: fixed."
        ),
    )
    basket.add_argument(
        "--max-tail-probability",
        type=float,
        default=0.20,
        help="Reject basket if selected tail buckets sum above this YES probability. Default: 0.20.",
    )
    basket.add_argument(
        "--max-basket-spend",
        type=float,
        default=12.0,
        help="Reject basket if sized paper spend exceeds this amount. Default: 12.",
    )
    basket.add_argument(
        "--max-worst-case-loss",
        type=float,
        default=8.0,
        help="Reject basket if any settlement bucket loses more than this amount. Default: 8.",
    )
    basket.add_argument(
        "--no-ensemble",
        action="store_true",
        help="Disable Open-Meteo GFS ensemble shape input and use residual calibration only.",
    )
    basket.add_argument(
        "--calibration-source",
        choices=("auto", "lstm", "clean-blend"),
        default=_default_calibration_source(),
        help=(
            "Residual source for trading probabilities. Defaults to "
            "SFO_TRADING_SIGNAL_CALIBRATION_SOURCE or lstm, matching the AWS "
            "deploy so local research stays comparable. auto prefers "
            "clean-blend when enough rows exist."
        ),
    )
    basket.add_argument(
        "--ensemble-timeout",
        type=float,
        default=12.0,
        help="Seconds to wait for the station-aligned Open-Meteo ensemble fetch.",
    )
    basket.set_defaults(func=cmd_tail_basket)

    arbitrage = sub.add_parser(
        "arbitrage",
        help="Scan active daily temperature bins for paper-only arbitrage portfolios",
    )
    arbitrage.add_argument(
        "--target-date",
        default="rolling",
        help=(
            "today, tomorrow, both, rolling, comma-list, or YYYY-MM-DD. "
            "Default rolling waits for the next active Kalshi events."
        ),
    )
    arbitrage.add_argument("--offline-events", type=Path, help="Saved Kalshi events JSON")
    arbitrage.add_argument("--place-paper", action="store_true", help="Record approved paper arbitrage legs")
    arbitrage.add_argument(
        "--skip-context-snapshots",
        action="store_true",
        help="Skip market snapshot writes; paper orders can still be recorded.",
    )
    arbitrage.add_argument(
        "--max-arb-spend",
        type=float,
        help="Maximum paper dollars to spend per arbitrage portfolio.",
    )
    arbitrage.add_argument(
        "--min-profit",
        type=float,
        default=0.01,
        help="Minimum guaranteed paper profit per arbitrage portfolio. Default: 0.01.",
    )
    arbitrage.set_defaults(func=cmd_arbitrage)

    portfolio = sub.add_parser(
        "portfolio-scan",
        help="Run the shared portfolio allocator for AWS paper placement",
    )
    portfolio.add_argument(
        "--target-date",
        default="rolling",
        help=(
            "today, tomorrow, both, rolling, comma-list, or YYYY-MM-DD. "
            "Default rolling waits for the next active Kalshi events."
        ),
    )
    portfolio.add_argument(
        "--side",
        choices=("yes", "no", "both"),
        default="both",
        help="Directional sides to consider after arbitrage funding. Default: both.",
    )
    portfolio.add_argument("--offline-events", type=Path, help="Saved Kalshi events JSON")
    portfolio.add_argument(
        "--observed-high",
        type=float,
        help="Override today's observed high-so-far in F.",
    )
    portfolio.add_argument("--place-paper", action="store_true", help="Record approved paper portfolio orders")
    portfolio.add_argument(
        "--paper-entry-mode",
        choices=("market", "limit"),
        default=_default_paper_entry_mode(),
        help="How --place-paper books approved directional entries. Default: PAPER_ENTRY_MODE or market.",
    )
    portfolio.add_argument(
        "--skip-context-snapshots",
        action="store_true",
        help="Skip forecast/probability/market snapshot writes; decision snapshots are still recorded.",
    )
    portfolio.add_argument(
        "--max-arb-spend",
        type=float,
        default=12.0,
        help="Maximum paper dollars to spend per arbitrage portfolio. Default: 12.",
    )
    portfolio.add_argument(
        "--min-profit",
        type=float,
        default=0.01,
        help="Minimum guaranteed paper profit per arbitrage portfolio. Default: 0.01.",
    )
    portfolio.add_argument(
        "--no-ensemble",
        action="store_true",
        help="Disable Open-Meteo GFS ensemble shape input and use residual calibration only.",
    )
    portfolio.add_argument(
        "--calibration-source",
        choices=("auto", "lstm", "clean-blend"),
        default=_default_calibration_source(),
        help=(
            "Residual source for trading probabilities. Defaults to "
            "SFO_TRADING_SIGNAL_CALIBRATION_SOURCE or lstm, matching the AWS deploy."
        ),
    )
    portfolio.add_argument(
        "--ensemble-timeout",
        type=float,
        default=12.0,
        help="Seconds to wait for the station-aligned Open-Meteo ensemble fetch.",
    )
    portfolio.set_defaults(func=cmd_portfolio_scan)

    collect = sub.add_parser("collect", help="Fetch and store live Kalshi event plus forecast snapshot")
    collect.add_argument("--target-date", default="today", help="today, tomorrow, both, comma-list, or YYYY-MM-DD")
    collect.set_defaults(func=cmd_collect)

    dataset_backfill = sub.add_parser(
        "dataset-backfill",
        help="Backfill compact external dataset features for forecast and market research",
    )
    dataset_backfill.add_argument(
        "--source",
        choices=(
            "tier1",
            "noaa-isd",
            "iem-asos",
            "open-meteo-previous-runs",
            "open-meteo-historical-forecast",
            "kalshi-history",
        ),
        default="tier1",
        help="Dataset source to backfill. tier1 runs the deployable Lightsail-safe sources.",
    )
    dataset_backfill.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    dataset_backfill.add_argument("--end-date", help="YYYY-MM-DD. Defaults to start date.")
    dataset_backfill.add_argument(
        "--isd-station",
        action="append",
        dest="isd_stations",
        help=f"NOAA ISD station id. Can repeat. Default: {KSFO_ISD_STATION}",
    )
    dataset_backfill.add_argument(
        "--asos-station",
        action="append",
        dest="asos_stations",
        help=f"Iowa Mesonet ASOS station id. Can repeat. Default: {KSFO_ASOS_STATION}",
    )
    dataset_backfill.add_argument(
        "--open-meteo-model",
        default="best_match",
        help="Open-Meteo model id. Default uses Open-Meteo best_match.",
    )
    dataset_backfill.add_argument(
        "--previous-days",
        type=int,
        default=7,
        help="Open-Meteo previous-run lead offsets to request. Default: 7.",
    )
    dataset_backfill.add_argument(
        "--kalshi-candles",
        action="store_true",
        help="Also fetch Kalshi historical candles for filtered markets.",
    )
    dataset_backfill.add_argument(
        "--kalshi-trades",
        action="store_true",
        help="Also fetch Kalshi historical trades for filtered markets.",
    )
    dataset_backfill.add_argument(
        "--candle-interval",
        type=int,
        choices=(1, 60, 1440),
        default=60,
        help="Kalshi candle interval in minutes. Default: 60.",
    )
    dataset_backfill.add_argument(
        "--kalshi-max-pages",
        type=int,
        default=20,
        help="Max historical market pages to inspect. Default: 20.",
    )
    dataset_backfill.add_argument(
        "--kalshi-max-trade-pages",
        type=int,
        default=20,
        help="Max historical trade pages to inspect per market. Default: 20.",
    )
    dataset_backfill.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")
    dataset_backfill.set_defaults(func=cmd_dataset_backfill)

    dataset_status = sub.add_parser("dataset-status", help="Show compact dataset table counts and recent runs")
    dataset_status.set_defaults(func=cmd_dataset_status)

    dataset_research = sub.add_parser(
        "dataset-research",
        help="Evaluate whether collected dataset sources are ready for forecast/trading promotion",
    )
    dataset_research.add_argument("--output", type=Path, help="Optional JSON output path")
    dataset_research.add_argument(
        "--min-matched-rows",
        type=int,
        default=30,
        help="Minimum matched settlement rows before a dataset can be an accuracy candidate.",
    )
    dataset_research.add_argument(
        "--min-mae-improvement",
        type=float,
        default=0.25,
        help="Required held-out MAE improvement in degrees F versus the baseline.",
    )
    dataset_research.add_argument(
        "--holdout-fraction",
        type=float,
        default=0.25,
        help="Most-recent matched fraction used as holdout.",
    )
    dataset_research.set_defaults(func=cmd_dataset_research)

    daily = sub.add_parser("daily-report", help="Build a read-only paper-research daily report")
    daily.add_argument(
        "--target-date",
        default="tomorrow",
        help="today, tomorrow, both, comma-list, or YYYY-MM-DD",
    )
    daily.add_argument(
        "--side",
        choices=("yes", "no", "both"),
        default="both",
        help="Which contract side to rank. Default is both to include BUY_YES and BUY_NO candidates.",
    )
    daily.add_argument("--offline-events", type=Path, help="Saved Kalshi events JSON")
    daily.add_argument(
        "--observed-high",
        type=float,
        help="Override today's observed high-so-far in F.",
    )
    daily.add_argument(
        "--no-ensemble",
        action="store_true",
        help="Disable Open-Meteo GFS ensemble shape input and use residual calibration only.",
    )
    daily.add_argument(
        "--ensemble-timeout",
        type=float,
        default=12.0,
        help="Seconds to wait for the station-aligned Open-Meteo ensemble fetch.",
    )
    daily.add_argument(
        "--no-live-market",
        action="store_true",
        help="Do not fetch live Kalshi markets; use the standard probability-only ladder.",
    )
    daily.add_argument(
        "--calibration-source",
        choices=("auto", "lstm", "clean-blend"),
        default=_default_calibration_source(),
        help=(
            "Residual source for trading probabilities. Defaults to "
            "SFO_TRADING_SIGNAL_CALIBRATION_SOURCE or lstm, matching the AWS "
            "deploy so local research stays comparable. auto prefers "
            "clean-blend when enough rows exist."
        ),
    )
    daily.add_argument(
        "--format",
        choices=("json",),
        default="json",
        help="Report output format. JSON is stable for dashboard ingestion.",
    )
    daily.add_argument(
        "--output",
        type=Path,
        help="Optional file path for the public paper-research artifact, e.g. forecaster/trading_signal.json.",
    )
    daily.set_defaults(func=cmd_daily_report)

    strategy = sub.add_parser(
        "strategy-research",
        help="Build the public Strategy Lab diagnostics artifact",
    )
    strategy.add_argument(
        "--calibration-min-train",
        type=int,
        default=180,
        help="Minimum walk-forward training rows before scoring calibration diagnostics.",
    )
    strategy.add_argument(
        "--output",
        type=Path,
        help="Optional file path for the public Strategy Lab artifact.",
    )
    strategy.set_defaults(func=cmd_strategy_research)

    backtest = sub.add_parser("backtest-calibration", help="Walk-forward probability calibration backtest")
    backtest.add_argument("--min-train", type=int, default=180)
    backtest.add_argument(
        "--source",
        choices=("lstm", "clean-blend"),
        default="lstm",
        help=(
            "Outcome source. clean-blend uses only archived next-day blend "
            "forecasts made before the target local day."
        ),
    )
    backtest.set_defaults(func=cmd_backtest_calibration)

    synthetic = sub.add_parser(
        "synthetic-blend-calibration",
        help="Research a walk-forward synthetic blend model before enough live blend history exists",
    )
    synthetic.add_argument(
        "--ab-test-path",
        type=Path,
        help="Historical LSTM/XGBoost comparison JSON. Defaults to forecaster/ab_test_results.json.",
    )
    synthetic.add_argument(
        "--stack-min-train",
        type=int,
        default=120,
        help="Rows required before the ridge stack starts making point-in-time predictions.",
    )
    synthetic.add_argument(
        "--calibration-min-train",
        type=int,
        default=120,
        help="Synthetic outcome rows required before scoring Kalshi-bin calibration.",
    )
    synthetic.add_argument(
        "--ridge-alpha",
        type=float,
        default=10.0,
        help="Ridge shrinkage strength for the synthetic stacker.",
    )
    synthetic.add_argument(
        "--output",
        type=Path,
        help="Optional JSON output path for the synthetic calibration artifact.",
    )
    synthetic.set_defaults(func=cmd_synthetic_blend_calibration)

    paper_summary = sub.add_parser(
        "paper-summary",
        help="Daily and rolling N-day paper-trading summary with forecast accuracy",
    )
    paper_summary.add_argument(
        "--days",
        type=int,
        default=7,
        help="Rolling window length in days. Default: 7.",
    )
    paper_summary.add_argument(
        "--output",
        type=Path,
        help="Optional JSON output path, e.g. forecaster/paper_summary.json.",
    )
    paper_summary.add_argument(
        "--csv",
        type=Path,
        help="Optional per-day CSV export path.",
    )
    paper_summary.set_defaults(func=cmd_paper_summary)

    report = sub.add_parser("paper-report", help="Show recent paper orders")
    report.add_argument("--limit", type=int, default=25)
    report.add_argument("--since", help="Only include paper trades with target_date >= YYYY-MM-DD")
    report.add_argument("--until", help="Only include paper trades with target_date <= YYYY-MM-DD")
    report.set_defaults(func=cmd_paper_report)

    buy = sub.add_parser("paper-buy", help="Buy paper YES or NO contracts with a specific paper-dollar amount")
    buy.add_argument("--ticker", required=True, help="Kalshi market ticker, e.g. KXHIGHTSFO-26JUN03-B68.5")
    buy.add_argument("--side", choices=("yes", "no"), default="yes", help="Paper side to buy; default yes")
    buy.add_argument("--amount", type=float, required=True, help="Paper dollars to put at risk, e.g. 10")
    buy.add_argument(
        "--price",
        type=float,
        help="Optional limit price. Fills only if live side ask is <= this price unless --force-fill is used.",
    )
    buy.add_argument(
        "--force-fill",
        action="store_true",
        help="Research-only override: fill exactly at --price even if that is not the live ask.",
    )
    buy.set_defaults(func=cmd_paper_buy)

    close = sub.add_parser(
        "paper-close",
        aliases=["paper-sell"],
        help="Close/sell one open paper position at the live Kalshi bid for its stored side",
    )
    close.add_argument("--order-id", type=int, required=True)
    close.add_argument(
        "--exit-price",
        type=float,
        help="Optional offline override. By default this uses the live Kalshi bid for the stored side.",
    )
    close.set_defaults(func=cmd_paper_close)

    monitor = sub.add_parser(
        "paper-monitor",
        help="Close open paper positions if live bid hits stop-loss or take-profit thresholds",
    )
    monitor.add_argument("--limit", type=int, default=50, help="Maximum open paper positions to inspect")
    monitor.add_argument(
        "--take-profit-pct",
        type=float,
        default=_env_float_default("PAPER_TAKE_PROFIT_PCT", DEFAULT_TAKE_PROFIT_PCT),
        help="Fallback close threshold when unrealized paper ROI is at/above this percent.",
    )
    monitor.add_argument(
        "--stop-loss-pct",
        type=float,
        default=_env_float_default("PAPER_STOP_LOSS_PCT", DEFAULT_STOP_LOSS_PCT),
        help="Fallback close threshold when unrealized paper ROI is at/below negative this percent.",
    )
    monitor.add_argument(
        "--yes-take-profit-pct",
        type=float,
        default=_env_float_default("PAPER_YES_TAKE_PROFIT_PCT", DEFAULT_YES_TAKE_PROFIT_PCT),
        help="YES-specific take-profit ROI percent. Default: 50.",
    )
    monitor.add_argument(
        "--yes-stop-loss-pct",
        type=float,
        default=_env_float_default("PAPER_YES_STOP_LOSS_PCT", DEFAULT_YES_STOP_LOSS_PCT),
        help="YES-specific stop-loss ROI percent. Default: 25.",
    )
    monitor.add_argument(
        "--no-take-profit-pct",
        type=float,
        default=_env_float_default("PAPER_NO_TAKE_PROFIT_PCT", DEFAULT_NO_TAKE_PROFIT_PCT),
        help="NO-specific take-profit ROI percent. Default: 35.",
    )
    monitor.add_argument(
        "--no-stop-loss-pct",
        type=float,
        default=_env_float_default("PAPER_NO_STOP_LOSS_PCT", DEFAULT_NO_STOP_LOSS_PCT),
        help="NO-specific stop-loss ROI percent. Default: 35.",
    )
    monitor.add_argument(
        "--model-veto-max-loss-pct",
        type=float,
        default=_env_float_default("PAPER_MODEL_VETO_MAX_LOSS_PCT", DEFAULT_MODEL_VETO_MAX_LOSS_PCT),
        help=(
            "Allow a fresh model snapshot to veto stop-loss exits only while the "
            "position is above negative this ROI percent. Default: 60."
        ),
    )
    monitor.add_argument(
        "--model-veto-buffer",
        type=float,
        default=_env_float_default("PAPER_MODEL_VETO_BUFFER", DEFAULT_MODEL_VETO_BUFFER),
        help="Extra model-probability cushion required to veto a NO stop-loss. Default: 0.08.",
    )
    monitor.add_argument("--dry-run", action="store_true", help="Print actions without closing paper positions")
    monitor.set_defaults(func=cmd_paper_monitor)

    settle = sub.add_parser("paper-settle", help="Settle paper orders for a date with final CLISFO high")
    settle.add_argument("--target-date", required=True)
    settle.add_argument("--settlement-high", type=float, required=True)
    settle.set_defaults(func=cmd_paper_settle)

    auto_settle = sub.add_parser(
        "paper-auto-settle",
        help="Settle open paper orders from CLISFO, with WeatherEdge ground truth as a fallback",
    )
    auto_settle.add_argument("--timeout", type=int, default=20, help="Seconds to wait for CLISFO")
    auto_settle.set_defaults(func=cmd_paper_auto_settle)

    market_backtest = sub.add_parser("backtest-market", help="Summarize settled paper-trading PnL")
    market_backtest.add_argument("--since", help="Only include paper trades with target_date >= YYYY-MM-DD")
    market_backtest.add_argument("--until", help="Only include paper trades with target_date <= YYYY-MM-DD")
    market_backtest.set_defaults(func=cmd_backtest_market)

    signal_backtest = sub.add_parser(
        "backtest-signals",
        help="Backtest recorded decision snapshots against official settled highs",
    )
    signal_backtest.add_argument("--since", help="Only include signals with target_date >= YYYY-MM-DD")
    signal_backtest.add_argument("--until", help="Only include signals with target_date <= YYYY-MM-DD")
    signal_backtest.add_argument("--approved-only", action="store_true", help="Only score rows that passed gates")
    signal_backtest.add_argument(
        "--min-quality",
        type=float,
        help="Only score rows with trade-quality score at or above this value",
    )
    signal_backtest.add_argument(
        "--sample-mode",
        choices=("latest-per-market-side", "entry-per-market-side", "all"),
        default="latest-per-market-side",
        help=(
            "How repeated scans are counted. Default keeps only the latest "
            "pre-resolution row for each target/market/side; entry-per-market-side "
            "keeps the first approved row, which is the decision that opened the "
            "position."
        ),
    )
    signal_backtest.add_argument(
        "--include-post-resolution",
        action="store_true",
        help="Include rows recorded after market close or official daily completion.",
    )
    signal_backtest.set_defaults(func=cmd_backtest_signals)

    rescore = sub.add_parser(
        "backtest-rescore",
        help=(
            "Re-score recorded decision snapshots under the current --risk-profile "
            "config (re-runs gates + Kelly sizing from scratch) and settle vs "
            "official highs, rolled up by independent weather day"
        ),
    )
    rescore.add_argument("--since", help="Only include snapshots with target_date >= YYYY-MM-DD")
    rescore.add_argument("--until", help="Only include snapshots with target_date <= YYYY-MM-DD")
    rescore.add_argument(
        "--sample-mode",
        choices=("latest-per-market-side", "entry-per-market-side", "all"),
        default="entry-per-market-side",
        help=(
            "Which snapshot represents each target/market/side. Default keeps the "
            "entry (first approved) row, the decision that opened the position; "
            "latest keeps the decayed last pre-close scan."
        ),
    )
    rescore.add_argument(
        "--include-post-resolution",
        action="store_true",
        help="Include rows recorded after market close or official daily completion.",
    )
    rescore.add_argument(
        "--bootstrap-samples",
        type=int,
        default=2000,
        help="Day-clustered bootstrap resamples for the ROI confidence interval.",
    )
    rescore.add_argument(
        "--json",
        dest="json_output",
        help="Optional path to write the full rescore result as JSON.",
    )
    rescore.set_defaults(func=cmd_backtest_rescore)
    return parser


def cmd_analyze(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    if args.paper_stake is not None and args.daily_budget is not None:
        raise ValueError("use either --paper-stake or --daily-budget, not both")
    config = _config(args)
    kalshi_client = KalshiPublicClient()
    targets, live_events_by_target = _resolve_analysis_targets(args, color, kalshi_client)
    if not targets:
        print(color.yellow("no eligible target dates found"))
        return 0
    adapter = SfoForecasterAdapter(args.forecaster_root)
    outcomes = adapter.load_calibration_outcomes(args.calibration_source)
    calibrator = ResidualCalibrator(outcomes, config)
    store = PaperStore(args.db_path)

    for idx, target in enumerate(targets):
        if idx:
            print("")
            print("=" * 92)
            print("")
        _analyze_one_target(
            args,
            target,
            adapter,
            calibrator,
            config,
            store,
            color,
            event_hint=live_events_by_target.get(target),
            event_lookup_done=target in live_events_by_target,
            kalshi_client=kalshi_client,
        )
    return 0


def cmd_tail_basket(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    config = _config(args)
    kalshi_client = KalshiPublicClient()
    targets, live_events_by_target = _resolve_analysis_targets(args, color, kalshi_client)
    if not targets:
        print(color.yellow("no eligible target dates found"))
        return 0
    adapter = SfoForecasterAdapter(args.forecaster_root)
    outcomes = adapter.load_calibration_outcomes(args.calibration_source)
    calibrator = ResidualCalibrator(outcomes, config)
    store = PaperStore(args.db_path)

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
            event_hint=live_events_by_target.get(target),
            event_lookup_done=target in live_events_by_target,
            kalshi_client=kalshi_client,
        )
    return 0


def cmd_arbitrage(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    config = _config(args)
    kalshi_client = KalshiPublicClient()
    targets, live_events_by_target = _resolve_analysis_targets(args, color, kalshi_client)
    if not targets:
        print(color.yellow("no eligible target dates found"))
        return 0
    store = PaperStore(args.db_path)

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
            event_hint=live_events_by_target.get(target),
            event_lookup_done=target in live_events_by_target,
            kalshi_client=kalshi_client,
        )
    return 0


def cmd_portfolio_scan(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    config = _config(args)
    kalshi_client = KalshiPublicClient()
    targets, live_events_by_target = _resolve_analysis_targets(args, color, kalshi_client)
    if not targets:
        print(color.yellow("no eligible target dates found"))
        return 0
    adapter = SfoForecasterAdapter(args.forecaster_root)
    outcomes = adapter.load_calibration_outcomes(args.calibration_source)
    calibrator = ResidualCalibrator(outcomes, config)
    store = PaperStore(args.db_path)

    for idx, target in enumerate(targets):
        if idx:
            print("")
            print("=" * 92)
            print("")
        _portfolio_scan_one_target(
            args,
            target,
            adapter,
            calibrator,
            config,
            store,
            color,
            event_hint=live_events_by_target.get(target),
            event_lookup_done=target in live_events_by_target,
            kalshi_client=kalshi_client,
        )
    return 0


def _resolve_analysis_targets(
    args: argparse.Namespace,
    color: Color,
    kalshi_client: KalshiPublicClient,
) -> tuple[list[date], dict[date, EventSnapshot]]:
    clock_targets = parse_target_dates(args.target_date)
    if args.offline_events or args.target_date != "rolling":
        return clock_targets, {}

    try:
        events = kalshi_client.list_event_snapshots(
            series_ticker=SERIES_TICKER,
            limit=20,
            with_nested_markets=True,
        )
    except URLError as exc:
        if args.place_paper:
            print(
                color.yellow(
                    f"warning: live Kalshi rolling target lookup failed ({exc}); "
                    "skipping paper scan instead of using clock-derived target dates"
                ),
                file=sys.stderr,
            )
            return [], {}
        print(
            color.yellow(
                f"warning: live Kalshi rolling target lookup failed ({exc}); "
                "using clock-derived probability targets"
            ),
            file=sys.stderr,
        )
        return clock_targets, {}

    targets, events_by_target = _rolling_live_event_targets(events)
    if targets:
        return targets, events_by_target

    if args.place_paper:
        print(
            color.yellow(
                "warning: no active Kalshi KXHIGHTSFO events found; skipping paper scan "
                "instead of using clock-derived target dates"
            ),
            file=sys.stderr,
        )
        return [], {}
    return clock_targets, {}


def _clamp_sizing_equity(equity: float, starting_bankroll: float) -> float:
    """Bound compounding to [0.5x, 2x] of the starting notional.

    Kelly sizes off current wealth, so a winning book should stake more and a
    losing one less. But on a tiny, noisy paper sample an early lucky (or unlucky)
    run would balloon (or zero) stakes off pure variance. Clamp the equity used
    for sizing so a drawdown cannot collapse stakes to nothing and a hot streak
    cannot run away before the sample is meaningful.
    """

    return min(max(equity, 0.5 * starting_bankroll), 2.0 * starting_bankroll)


def _sizing_bankroll(store: PaperStore, config: StrategyConfig, risk_profile: str | None) -> float:
    """Bankroll used for Kelly and the risk caps.

    Frozen notional by default (reproducible paper runs); live realized equity
    (clamped) when size_against_live_equity is set, so sizing fractions current
    wealth without letting a noisy early sample run away with the stake.
    """

    if config.size_against_live_equity:
        equity = store.paper_equity(config.paper_bankroll, risk_profile=risk_profile)
        return _clamp_sizing_equity(equity, config.paper_bankroll)
    return config.paper_bankroll


def _rolling_targets_count() -> int:
    # Kalshi lists SFO events several days out; scanning more of them grows the
    # distinct-candidate universe (and the paper sample) without touching any
    # edge gate. Bounded so a misconfig can't fan out unboundedly.
    raw = os.getenv("PAPER_ROLLING_TARGETS", "3")
    try:
        value = int(raw)
    except ValueError:
        return 3
    return max(1, min(value, 7))


def _rolling_live_event_targets(
    events: list[EventSnapshot],
    *,
    now: datetime | None = None,
    max_targets: int | None = None,
) -> tuple[list[date], dict[date, EventSnapshot]]:
    if max_targets is None:
        max_targets = _rolling_targets_count()
    local_now = settlement_clock(now)
    today = local_now.date()
    min_target = today
    if local_now.hour >= _same_day_entry_cutoff_hour():
        min_target = today + timedelta(days=1)
    events_by_target: dict[date, EventSnapshot] = {}
    for event in events:
        target = event.target_date
        if target is None or target < min_target or not event.active_markets:
            continue
        current = events_by_target.get(target)
        if current is None or len(event.active_markets) > len(current.active_markets):
            events_by_target[target] = event
    targets = sorted(events_by_target)[:max_targets]
    return targets, {target: events_by_target[target] for target in targets}


def _analyze_one_target(
    args: argparse.Namespace,
    target,
    adapter: SfoForecasterAdapter,
    calibrator: ResidualCalibrator,
    config: StrategyConfig,
    store: PaperStore,
    color: Color,
    *,
    event_hint: EventSnapshot | None = None,
    event_lookup_done: bool = False,
    kalshi_client: KalshiPublicClient | None = None,
) -> None:
    forecast = adapter.latest_blend(target)
    _enforce_live_forecast_freshness(forecast, config)
    intraday = _intraday_for_target(args, target, adapter)
    observed_high_f = intraday.observed_high_f if intraday else None
    if intraday is not None and not has_forecaster_observed_high_adjustment(forecast):
        forecast = adapter.apply_intraday_update(forecast, intraday)
    ensemble = _ensemble_for_target(args, target, forecast.predicted_high_f, color)
    event = event_hint
    if event_lookup_done:
        pass
    elif args.offline_events:
        events = load_event_snapshots(args.offline_events, target)
        event = events[0] if events else None
    else:
        try:
            client = kalshi_client or KalshiPublicClient()
            event = client.find_event_by_date(target, series_ticker=SERIES_TICKER)
        except (URLError, OSError) as exc:
            print(color.yellow(f"warning: live Kalshi lookup failed ({exc}); using probability-only ladder"), file=sys.stderr)
            event = None

    if event:
        markets = event.active_markets or event.markets
        event_title = event.title
    else:
        markets = standard_sfo_bins(f"{SERIES_TICKER}-{format_event_date_token(target)}-PAPER")
        event_title = "No live Kalshi event found; probability-only fallback ladder"

    emos_lookup = adapter.load_emos_mu_sigma() if config.emos_distribution_enabled else {}
    probabilities = calibrator.bucket_probabilities(
        markets,
        forecast.predicted_high_f,
        source_spread_f=forecast.source_spread_f,
        observed_high_f=observed_high_f,
        ensemble=ensemble,
        intraday=intraday,
        emos_mu_sigma=emos_lookup.get(target),
    )
    # The market's de-vigged bin ladder distilled into a consensus forecast
    # (implied high, distribution, confidence). Surfaced below and, when the
    # profile enables it, anchored into sizing via the consensus guard in rank().
    consensus = build_market_consensus(markets)
    risk_profile = _risk_profile_name(args)
    paper_bankroll = _sizing_bankroll(store, config, risk_profile)
    evaluator = TradeEvaluator(config)
    decisions = evaluator.rank(
        markets,
        probabilities,
        bankroll=paper_bankroll,
        sides=_analysis_sides(args.side),
        source_spread_f=forecast.source_spread_f,
        forecast_high_f=forecast.predicted_high_f,
        # The day's source disagreement is the comfort-edge uncertainty proxy:
        # on a calm (low-spread) day the band floors to ~3F block / ~6F full;
        # on a disagreement day it widens, so near-forecast NO is blocked further
        # out. Floored inside the assessment so it never collapses.
        forecast_sigma_f=forecast.source_spread_f,
        market_consensus=consensus,
    )
    entry_allowed = True
    entry_block_reason = None
    if args.place_paper:
        if event is None:
            entry_allowed = False
            entry_block_reason = (
                "paper entry disabled: target date is not listed as a live Kalshi event yet"
            )
        elif not event.active_markets:
            entry_allowed = False
            entry_block_reason = "paper entry disabled: Kalshi event has no active markets"
        else:
            entry_allowed, entry_block_reason = _paper_entry_gate_for_target(target, forecast, intraday)
    if args.place_paper and entry_allowed:
        pause_reason = store.paper_entry_pause_reason(
            risk_profile,
            bankroll=paper_bankroll,
            target_date=target.isoformat(),
        )
        if pause_reason is not None:
            entry_allowed = False
            entry_block_reason = pause_reason
    paper_trader = PaperTrader(
        store,
        config,
        risk_profile=risk_profile,
        entry_mode=args.paper_entry_mode,
    )
    display_decisions = paper_trader.with_paper_stakes(decisions, args.paper_stake)
    daily_budget_remaining = None
    if args.daily_budget is not None:
        daily_budget_remaining = store.remaining_daily_budget(
            target.isoformat(),
            args.daily_budget,
            risk_profile=risk_profile,
        )
        display_decisions = paper_trader.with_daily_budget(display_decisions, daily_budget_remaining)
    display_decisions = paper_trader.with_entry_mode(display_decisions)
    if not entry_allowed and entry_block_reason:
        display_decisions = _block_entry_decisions(display_decisions, entry_block_reason)

    if not getattr(args, "skip_context_snapshots", False):
        store.record_forecast(forecast)
        if event:
            store.record_market(event)
        store.record_probabilities(target.isoformat(), probabilities.values())
    store.record_decisions(
        target.isoformat(),
        display_decisions,
        forecast=forecast,
        intraday=intraday,
        event=event,
        market_consensus=consensus,
        risk_profile=risk_profile,
        bankroll=paper_bankroll,
    )

    order_ids = []
    if args.place_paper and entry_allowed:
        order_ids = paper_trader.place_approved(
            target.isoformat(),
            decisions,
            stake_dollars=args.paper_stake,
            daily_budget=daily_budget_remaining,
            bankroll=paper_bankroll,
        )

    _print_analysis(
        event_title,
        forecast,
        display_decisions,
        placed_ids=order_ids,
        market_available=event is not None,
        color=color,
        paper_stake=args.paper_stake,
        daily_budget=args.daily_budget,
        daily_budget_remaining=daily_budget_remaining,
        intraday=intraday,
        ensemble=ensemble,
        entry_block_reason=entry_block_reason,
        consensus=consensus,
    )


def _portfolio_scan_one_target(
    args: argparse.Namespace,
    target,
    adapter: SfoForecasterAdapter,
    calibrator: ResidualCalibrator,
    config: StrategyConfig,
    store: PaperStore,
    color: Color,
    *,
    event_hint: EventSnapshot | None = None,
    event_lookup_done: bool = False,
    kalshi_client: KalshiPublicClient | None = None,
) -> None:
    forecast = adapter.latest_blend(target)
    _enforce_live_forecast_freshness(forecast, config)
    intraday = _intraday_for_target(args, target, adapter)
    observed_high_f = intraday.observed_high_f if intraday else None
    if intraday is not None and not has_forecaster_observed_high_adjustment(forecast):
        forecast = adapter.apply_intraday_update(forecast, intraday)
    ensemble = _ensemble_for_target(args, target, forecast.predicted_high_f, color)
    event = event_hint
    if event_lookup_done:
        pass
    elif args.offline_events:
        events = load_event_snapshots(args.offline_events, target)
        event = events[0] if events else None
    else:
        try:
            client = kalshi_client or KalshiPublicClient()
            event = client.find_event_by_date(target, series_ticker=SERIES_TICKER)
        except (URLError, OSError) as exc:
            print(color.yellow(f"warning: live Kalshi lookup failed ({exc}); using probability-only ladder"), file=sys.stderr)
            event = None

    if event:
        markets = event.active_markets or event.markets
        event_title = event.title
        market_available = True
    else:
        markets = standard_sfo_bins(f"{SERIES_TICKER}-{format_event_date_token(target)}-PAPER")
        event_title = "No live Kalshi event found; portfolio scan is research-only"
        market_available = False

    emos_lookup = adapter.load_emos_mu_sigma() if config.emos_distribution_enabled else {}
    probabilities = calibrator.bucket_probabilities(
        markets,
        forecast.predicted_high_f,
        source_spread_f=forecast.source_spread_f,
        observed_high_f=observed_high_f,
        ensemble=ensemble,
        intraday=intraday,
        emos_mu_sigma=emos_lookup.get(target),
    )
    consensus = build_market_consensus(markets)
    risk_profile = _risk_profile_name(args)
    paper_bankroll = _sizing_bankroll(store, config, risk_profile)
    evaluator = TradeEvaluator(config)
    decisions = evaluator.rank(
        markets,
        probabilities,
        bankroll=paper_bankroll,
        sides=_analysis_sides(args.side),
        source_spread_f=forecast.source_spread_f,
        forecast_high_f=forecast.predicted_high_f,
        forecast_sigma_f=forecast.source_spread_f,
        market_consensus=consensus,
    )
    opportunities = build_arbitrage_opportunities(
        markets,
        config=config,
        bankroll=paper_bankroll,
        max_spend=args.max_arb_spend,
        min_profit=args.min_profit,
    )
    plan = allocate_portfolio(
        decisions,
        arbitrage_opportunities=opportunities,
        bankroll=paper_bankroll,
        risk_profile=risk_profile,
    )

    entry_allowed = True
    entry_block_reason = None
    if args.place_paper:
        if event is None:
            entry_allowed = False
            entry_block_reason = (
                "paper portfolio disabled: target date is not listed as a live Kalshi event yet"
            )
        elif not event.active_markets:
            entry_allowed = False
            entry_block_reason = "paper portfolio disabled: Kalshi event has no active markets"
        else:
            entry_allowed, entry_block_reason = _paper_entry_gate_for_target(target, forecast, intraday)
    paper_trader = PaperTrader(
        store,
        config,
        risk_profile=risk_profile,
        entry_mode=args.paper_entry_mode,
    )
    if args.place_paper and entry_allowed:
        pause_reason = store.paper_entry_pause_reason(
            risk_profile,
            bankroll=paper_bankroll,
            target_date=target.isoformat(),
        )
        if pause_reason is not None:
            entry_allowed = False
            entry_block_reason = pause_reason

    decisions_to_record = _portfolio_decisions_for_recording(decisions, plan)
    if not entry_allowed and entry_block_reason:
        decisions_to_record = _block_entry_decisions(decisions_to_record, entry_block_reason)

    if not getattr(args, "skip_context_snapshots", False):
        store.record_forecast(forecast)
        if event:
            store.record_market(event)
        store.record_probabilities(target.isoformat(), probabilities.values())
    store.record_decisions(
        target.isoformat(),
        decisions_to_record,
        forecast=forecast,
        intraday=intraday,
        event=event,
        market_consensus=consensus,
        risk_profile=risk_profile,
        bankroll=paper_bankroll,
    )

    placed_ids: list[int] = []
    if args.place_paper and entry_allowed and plan.approved:
        for opportunity in plan.arbitrage_opportunities:
            placed_ids.extend(
                paper_trader.place_arbitrage(
                    target.isoformat(),
                    opportunity,
                    bankroll=paper_bankroll,
                )
            )
        directional = [
            leg.decision
            for leg in plan.legs
            if leg.sleeve != "arbitrage"
        ]
        placed_ids.extend(
            paper_trader.place_approved(
                target.isoformat(),
                directional,
                bankroll=paper_bankroll,
            )
        )

    _print_portfolio_scan(
        event_title,
        forecast,
        plan,
        decisions_to_record,
        placed_ids=placed_ids,
        market_available=market_available,
        color=color,
        intraday=intraday,
        ensemble=ensemble,
        entry_block_reason=entry_block_reason,
        consensus=consensus,
    )


def _arbitrage_one_target(
    args: argparse.Namespace,
    target,
    config: StrategyConfig,
    store: PaperStore,
    color: Color,
    *,
    event_hint: EventSnapshot | None = None,
    event_lookup_done: bool = False,
    kalshi_client: KalshiPublicClient | None = None,
) -> None:
    event = event_hint
    if event_lookup_done:
        pass
    elif args.offline_events:
        events = load_event_snapshots(args.offline_events, target)
        event = events[0] if events else None
    else:
        try:
            client = kalshi_client or KalshiPublicClient()
            event = client.find_event_by_date(target, series_ticker=SERIES_TICKER)
        except (URLError, OSError) as exc:
            print(color.yellow(f"warning: live Kalshi lookup failed ({exc}); no active ladder available"), file=sys.stderr)
            event = None

    if event:
        markets = event.active_markets or event.markets
        event_title = event.title
        market_available = True
    else:
        markets = standard_sfo_bins(f"{SERIES_TICKER}-{format_event_date_token(target)}-PAPER")
        event_title = "No live Kalshi event found; arbitrage scan is research-only"
        market_available = False

    risk_profile = _risk_profile_name(args)
    paper_bankroll = _sizing_bankroll(store, config, risk_profile)
    opportunities = build_arbitrage_opportunities(
        markets,
        config=config,
        bankroll=paper_bankroll,
        max_spend=args.max_arb_spend,
        min_profit=args.min_profit,
    )

    entry_allowed = True
    entry_block_reason = None
    if args.place_paper:
        if event is None:
            entry_allowed = False
            entry_block_reason = (
                "paper arbitrage disabled: target date is not listed as a live Kalshi event yet"
            )
        elif not event.active_markets:
            entry_allowed = False
            entry_block_reason = "paper arbitrage disabled: Kalshi event has no active markets"
        else:
            entry_allowed, entry_block_reason = _paper_entry_gate_for_target(target, None, None)

    paper_trader = PaperTrader(store, config, risk_profile=risk_profile)
    if args.place_paper and entry_allowed:
        pause_reason = store.paper_entry_pause_reason(
            risk_profile,
            bankroll=paper_bankroll,
            target_date=target.isoformat(),
        )
        if pause_reason is not None:
            entry_allowed = False
            entry_block_reason = pause_reason

    if event and not getattr(args, "skip_context_snapshots", False):
        store.record_market(event)

    placed_ids: list[int] = []
    if args.place_paper and entry_allowed:
        for opportunity in opportunities:
            if not opportunity.approved:
                continue
            placed_ids.extend(
                paper_trader.place_arbitrage(
                    target.isoformat(),
                    opportunity,
                    bankroll=paper_bankroll,
                )
            )

    _print_arbitrage(
        event_title,
        target.isoformat(),
        opportunities,
        placed_ids=placed_ids,
        market_available=market_available,
        color=color,
        max_spend=args.max_arb_spend,
        min_profit=args.min_profit,
        entry_block_reason=entry_block_reason,
    )


def _tail_basket_one_target(
    args: argparse.Namespace,
    target,
    adapter: SfoForecasterAdapter,
    calibrator: ResidualCalibrator,
    config: StrategyConfig,
    store: PaperStore,
    color: Color,
    *,
    event_hint: EventSnapshot | None = None,
    event_lookup_done: bool = False,
    kalshi_client: KalshiPublicClient | None = None,
) -> None:
    forecast = adapter.latest_blend(target)
    _enforce_live_forecast_freshness(forecast, config)
    intraday = _intraday_for_target(args, target, adapter)
    observed_high_f = intraday.observed_high_f if intraday else None
    if intraday is not None and not has_forecaster_observed_high_adjustment(forecast):
        forecast = adapter.apply_intraday_update(forecast, intraday)
    ensemble = _ensemble_for_target(args, target, forecast.predicted_high_f, color)
    event = event_hint
    if event_lookup_done:
        pass
    elif args.offline_events:
        events = load_event_snapshots(args.offline_events, target)
        event = events[0] if events else None
    else:
        try:
            client = kalshi_client or KalshiPublicClient()
            event = client.find_event_by_date(target, series_ticker=SERIES_TICKER)
        except (URLError, OSError) as exc:
            print(color.yellow(f"warning: live Kalshi lookup failed ({exc}); using probability-only ladder"), file=sys.stderr)
            event = None

    if event:
        markets = event.active_markets or event.markets
        event_title = event.title
    else:
        markets = standard_sfo_bins(f"{SERIES_TICKER}-{format_event_date_token(target)}-PAPER")
        event_title = "No live Kalshi event found; probability-only fallback ladder"

    emos_lookup = adapter.load_emos_mu_sigma() if config.emos_distribution_enabled else {}
    probabilities = calibrator.bucket_probabilities(
        markets,
        forecast.predicted_high_f,
        source_spread_f=forecast.source_spread_f,
        observed_high_f=observed_high_f,
        ensemble=ensemble,
        intraday=intraday,
        emos_mu_sigma=emos_lookup.get(target),
    )
    risk_profile = _risk_profile_name(args)
    paper_bankroll = _sizing_bankroll(store, config, risk_profile)
    evaluator = TradeEvaluator(config)
    basket = build_tail_basket(
        markets,
        probabilities,
        predicted_high_f=forecast.predicted_high_f,
        evaluator=evaluator,
        bankroll=paper_bankroll,
        tail_distance_f=args.tail_distance,
        # In 'kelly' mode pass no fixed stake so each leg keeps the evaluator's
        # risk-budget (Kelly + comfort) size instead of a hardcoded few dollars.
        tail_stake=None if args.basket_sizing == "kelly" else args.tail_stake,
        center_stake=args.center_stake,
        max_tail_yes_probability=args.max_tail_probability,
        max_basket_spend=args.max_basket_spend,
        max_worst_case_loss=args.max_worst_case_loss,
        source_spread_f=forecast.source_spread_f,
    )

    entry_allowed = True
    entry_block_reason = None
    if args.place_paper:
        if event is None:
            entry_allowed = False
            entry_block_reason = (
                "paper entry disabled: target date is not listed as a live Kalshi event yet"
            )
        elif not event.active_markets:
            entry_allowed = False
            entry_block_reason = "paper entry disabled: Kalshi event has no active markets"
        else:
            entry_allowed, entry_block_reason = _paper_entry_gate_for_target(target, forecast, intraday)

    if args.place_paper and entry_allowed:
        pause_reason = store.paper_entry_pause_reason(
            risk_profile,
            bankroll=paper_bankroll,
            target_date=target.isoformat(),
        )
        if pause_reason is not None:
            entry_allowed = False
            entry_block_reason = pause_reason
    decisions_to_record = basket.decisions_for_recording()
    if not entry_allowed and entry_block_reason:
        decisions_to_record = _block_entry_decisions(decisions_to_record, entry_block_reason)

    if not getattr(args, "skip_context_snapshots", False):
        store.record_forecast(forecast)
        if event:
            store.record_market(event)
        store.record_probabilities(target.isoformat(), probabilities.values())
    store.record_decisions(
        target.isoformat(),
        decisions_to_record,
        forecast=forecast,
        intraday=intraday,
        event=event,
        risk_profile=risk_profile,
        bankroll=paper_bankroll,
    )

    order_ids = []
    if args.place_paper and entry_allowed and basket.approved:
        paper_trader = PaperTrader(store, config, risk_profile=risk_profile)
        # A tail basket is a worst-case-bounded structure meant to be held to
        # settlement. Tag its legs as one group so the monitor never closes a
        # single leg on an intraday take-profit/stop-loss and breaks the bound.
        order_ids = paper_trader.place_approved(
            target.isoformat(),
            basket.decisions,
            daily_budget=None,
            bankroll=paper_bankroll,
            group_id=f"BASKET-{uuid.uuid4().hex[:12]}",
        )

    _print_tail_basket(
        event_title,
        forecast,
        basket,
        placed_ids=order_ids,
        market_available=event is not None,
        color=color,
        intraday=intraday,
        ensemble=ensemble,
        entry_block_reason=entry_block_reason,
    )


def _ensemble_for_target(
    args: argparse.Namespace,
    target,
    station_center_high_f: float,
    color: Color,
) -> EnsembleSnapshot | None:
    if args.no_ensemble:
        return None
    try:
        return SfoEnsembleClient(timeout=args.ensemble_timeout).station_aligned_snapshot(
            target,
            station_center_high_f,
        )
    except (OpenMeteoEnsembleError, OSError, TimeoutError, URLError) as exc:
        print(
            color.yellow(f"warning: station-aligned ensemble lookup failed ({exc}); using residual calibration only"),
            file=sys.stderr,
        )
        return None


def _intraday_for_target(
    args: argparse.Namespace,
    target,
    adapter: SfoForecasterAdapter,
) -> IntradaySnapshot | None:
    today = parse_target_date("today")
    if target != today:
        return None
    intraday = adapter.intraday_snapshot(target)
    if args.observed_high is None:
        return intraday
    if intraday is None:
        return IntradaySnapshot(
            target_date=target,
            observed_high_f=args.observed_high,
            latest_temp_f=None,
            latest_observed_at=None,
            remaining_forecast_high_f=None,
            forecast_fetched_at=None,
            observation_count=0,
        )
    return replace(intraday, observed_high_f=args.observed_high)


def _paper_entry_gate_for_target(
    target,
    forecast,
    intraday: IntradaySnapshot | None,
    *,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    # A single-source forecast (Google-cache fallback when the multi-source
    # blend is unavailable) reports a 0.0 source spread, which silently passes
    # the disagreement gate and skips sigma widening. Refuse to open paper
    # positions on an uncorroborated point forecast on any target date.
    if forecast is not None and getattr(forecast, "source_count", 2) < 2:
        return False, (
            "paper entry disabled: single-source forecast (no multi-source "
            "corroboration); disagreement gate and sigma widening cannot engage"
        )
    local_now = settlement_clock(now)
    if target != local_now.date():
        return True, None
    if intraday is not None and intraday.is_complete:
        return False, "same-day entry disabled: official daily high is complete; monitor/settle only"

    cutoff_hour = _same_day_entry_cutoff_hour()
    if local_now.hour >= cutoff_hour:
        return (
            False,
            (
                "same-day entry disabled: local peak/high window has passed; "
                "rolling scanner shifts to later target dates"
            ),
        )
    return True, None


def _block_entry_decisions(decisions, reason: str):
    return [
        replace(
            decision,
            approved=False,
            recommended_contracts=0.0,
            expected_profit=0.0,
            reasons=[reason, *decision.reasons],
        )
        for decision in decisions
    ]


def _portfolio_decisions_for_recording(decisions, plan: PortfolioPlan):
    selected_by_key = {
        _portfolio_decision_key(leg.decision): leg.decision
        for leg in plan.legs
    }
    recorded = []
    seen: set[tuple[str, str]] = set()
    for decision in decisions:
        key = _portfolio_decision_key(decision)
        seen.add(key)
        selected = selected_by_key.get(key)
        if selected is not None:
            recorded.append(selected)
        elif decision.approved:
            recorded.append(
                replace(
                    decision,
                    approved=False,
                    recommended_contracts=0.0,
                    expected_profit=0.0,
                    reasons=[*decision.reasons, "portfolio not allocated by shared risk budget"],
                )
            )
        else:
            recorded.append(decision)
    for leg in plan.legs:
        key = _portfolio_decision_key(leg.decision)
        if key not in seen:
            recorded.append(leg.decision)
            seen.add(key)
    return recorded


def _portfolio_decision_key(decision) -> tuple[str, str]:
    return (str(decision.ticker), str(decision.side).upper())


def _same_day_entry_cutoff_hour() -> int:
    raw = os.getenv("PAPER_SAME_DAY_ENTRY_CUTOFF_HOUR", str(DEFAULT_SAME_DAY_ENTRY_CUTOFF_HOUR))
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SAME_DAY_ENTRY_CUTOFF_HOUR
    return min(23, max(0, value))


def cmd_collect(args: argparse.Namespace) -> int:
    targets = parse_target_dates(args.target_date)
    adapter = SfoForecasterAdapter(args.forecaster_root)
    client = KalshiPublicClient()
    store = PaperStore(args.db_path)
    for target in targets:
        forecast = adapter.latest_blend(target)
        try:
            event = client.find_event_by_date(target, series_ticker=SERIES_TICKER)
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
    return 0


def cmd_dataset_backfill(args: argparse.Namespace) -> int:
    start, end = _dataset_date_range(args)
    store = DatasetStore(args.db_path)
    sources = _dataset_sources(args.source)
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
                result = backfill_iem_asos(
                    store,
                    stations=args.asos_stations or [KSFO_ASOS_STATION],
                    start=start,
                    end=end,
                    timeout=args.timeout,
                )
            elif source == "open-meteo-previous-runs":
                result = backfill_open_meteo_previous_runs(
                    store,
                    start=start,
                    end=end,
                    model=args.open_meteo_model,
                    previous_days=args.previous_days,
                    timeout=args.timeout,
                )
            elif source == "open-meteo-historical-forecast":
                result = backfill_open_meteo_historical_forecast(
                    store,
                    start=start,
                    end=end,
                    model=args.open_meteo_model,
                    timeout=args.timeout,
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


def _dataset_sources(source: str) -> list[str]:
    if source == "tier1":
        return [
            "noaa-isd",
            "iem-asos",
            "open-meteo-previous-runs",
            "open-meteo-historical-forecast",
            "kalshi-history",
        ]
    return [source]


def _dataset_run_params(args: argparse.Namespace, source: str, start: date, end: date) -> dict[str, object]:
    return {
        "source": source,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
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


def cmd_backtest_calibration(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    config = _config(args)
    adapter = SfoForecasterAdapter(args.forecaster_root)
    outcomes = (
        adapter.load_clean_blend_outcomes()
        if args.source == "clean-blend"
        else adapter.load_lstm_outcomes()
    )
    result = run_walk_forward_calibration_backtest(outcomes, config=config, min_train=args.min_train)
    print(color.cyan(color.bold("walk-forward calibration backtest")))
    print(f"source: {args.source}")
    print(f"n: {result.n}")
    print(f"brier_score: {result.brier_score:.4f}")
    print(f"log_loss: {result.log_loss:.4f}")
    print(f"top_bin_accuracy: {result.top_bin_accuracy:.3f}")
    print(f"avg_winning_probability: {result.avg_winning_probability:.3f}")
    print(f"avg_entropy: {result.avg_entropy:.3f}")
    print("")
    print(color.gray("calibration_bucket count avg_p win_rate brier"))
    print(color.gray("-" * 48))
    for bucket in result.calibration_buckets:
        if bucket.count == 0:
            continue
        print(
            f"{bucket.lower:.1f}-{bucket.upper:.1f} "
            f"{bucket.count:5d} "
            f"{bucket.avg_probability:5.3f} "
            f"{bucket.observed_frequency:8.3f} "
            f"{bucket.brier_score:5.3f}"
        )
    if result.cohorts:
        print("")
        print(color.gray("temperature_cohort count brier log_loss top_hit avg_win_p"))
        print(color.gray("-" * 64))
        for cohort in result.cohorts:
            print(
                f"{cohort.name:20s} "
                f"{cohort.count:5d} "
                f"{cohort.brier_score:5.3f} "
                f"{cohort.log_loss:8.3f} "
                f"{cohort.top_bin_accuracy:7.3f} "
                f"{cohort.avg_winning_probability:9.3f}"
            )
    return 0


def cmd_synthetic_blend_calibration(args: argparse.Namespace) -> int:
    config = _config(args)
    ab_test_path = args.ab_test_path or args.forecaster_root / "ab_test_results.json"
    payload = build_synthetic_blend_calibration(
        ab_test_path,
        config=config,
        stack_min_train=args.stack_min_train,
        calibration_min_train=args.calibration_min_train,
        ridge_alpha=args.ridge_alpha,
    )
    if args.output:
        write_synthetic_blend_calibration(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_paper_summary(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    if args.days < 1:
        raise ValueError("--days must be at least 1")
    config = _config(args)
    payload = build_paper_summary(
        db_path=args.db_path,
        forecaster_root=args.forecaster_root,
        config=config,
        days=args.days,
    )
    if args.output:
        write_paper_summary(args.output, payload)
    if args.csv:
        write_paper_summary_csv(args.csv, payload)

    totals = payload["totals"]
    print(color.cyan(color.bold(f"paper summary: {payload['window_start']} to {payload['window_end']}")))
    print(
        f"opened={totals['trades_opened']} closed={totals['trades_closed']} "
        f"settled={totals['trades_settled']} open_now={totals['open_positions']} "
        f"open_risk=${totals['open_risk']:.2f}"
    )
    realized = f"${totals['realized_pnl']:.2f}"
    realized = color.green(realized) if totals["realized_pnl"] >= 0 else color.red(realized)
    hit_rate = "-" if totals["hit_rate"] is None else f"{totals['hit_rate']:.3f}"
    roi = "-" if totals["roi"] is None else f"{totals['roi']:.3f}"
    print(
        f"window_realized={realized} cumulative=${totals['cumulative_realized_pnl']:.2f} "
        f"hit_rate={hit_rate} roi={roi}"
    )
    if totals["mean_abs_forecast_error_f"] is not None:
        print(f"mean_abs_forecast_error={totals['mean_abs_forecast_error_f']:.2f}F")
    print("")
    print(color.gray("date        opened closed settled wins losses realized cumulative hit  fc_err"))
    print(color.gray("-" * 84))
    for day in payload["days"]:
        hit = "-" if day["hit_rate"] is None else f"{day['hit_rate']:.2f}"
        err = "-" if day["forecast_error_f"] is None else f"{day['forecast_error_f']:.1f}F"
        print(
            f"{day['date']}  {day['opened']:5d} {day['closed']:6d} {day['settled']:7d} "
            f"{day['wins']:4d} {day['losses']:6d} {day['realized_pnl']:8.2f} "
            f"{day['cumulative_realized']:10.2f} {hit:>4s} {err:>6s}"
        )
    if payload["biggest_winners"]:
        print("")
        print(color.green("biggest winners:"))
        for row in payload["biggest_winners"]:
            print(f"  #{row['id']} {row['target_date']} {row['ticker']} {row['side']} ${row['realized_pnl']:+.2f}")
    if payload["biggest_losers"]:
        print("")
        print(color.red("biggest losers:"))
        for row in payload["biggest_losers"]:
            print(f"  #{row['id']} {row['target_date']} {row['ticker']} {row['side']} ${row['realized_pnl']:+.2f}")
    print("")
    print(color.cyan("learnings:"))
    for note in payload["learnings"]:
        print(f"  - {note}")
    print(color.cyan("recommended next changes:"))
    for note in payload["recommended_changes"]:
        print(f"  - {note}")
    return 0


def cmd_paper_report(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    store = PaperStore(args.db_path)
    rows = store.paper_orders(args.limit, since=args.since, until=args.until)
    if not rows:
        print(color.yellow("no paper orders recorded"))
        return 0
    for row in rows:
        status = _color_status(color, row["status"])
        pnl = _format_pnl(row["realized_pnl"])
        if row["realized_pnl"] is not None:
            pnl = color.green(pnl) if float(row["realized_pnl"]) >= 0 else color.red(pnl)
        entry_price = row["entry_price"] if row["entry_price"] is not None else row["yes_ask"]
        side = row["side"] if row["side"] else ("NO" if "NO" in str(row["action"]).upper() else "YES")
        print(
            f"id={row['id']} {row['created_at']} {row['target_date']} {row['market_ticker']} "
            f"{side} {row['contracts']:.4f} @ {float(entry_price):.2f} "
            f"spent=${float(row['contracts']) * float(row['cost_per_contract']):.2f} "
            f"edge={_color_edge(color, row['edge'])} "
            f"q={float(row['trade_quality_score']):4.1f} status={status} "
            f"exit={row['exit_price'] if row['exit_price'] is not None else '-'} "
            f"settle={row['settlement_high_f'] if row['settlement_high_f'] is not None else '-'} "
            f"pnl={pnl}"
        )
    return 0


def cmd_paper_buy(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    if args.amount <= 0:
        raise ValueError("amount must be positive")
    if args.force_fill and args.price is None:
        raise ValueError("--force-fill requires --price")
    side = args.side.upper()

    client = KalshiPublicClient()
    market = client.get_market(args.ticker)
    target = target_date_from_event_ticker(market.event_ticker)
    if target is None:
        raise ValueError(f"could not infer target date from {market.event_ticker}")

    if args.force_fill:
        entry_price = float(args.price)
        price_note = color.yellow("manual forced paper price; not a realistic fill")
        action = f"BUY_{side}_FORCE_PAPER"
        reason = "manual force-filled paper buy"
    else:
        if market.status != "active":
            raise ValueError(f"market {market.ticker} is {market.status}; cannot buy at a live ask")
        live_ask = market.side_ask(side)
        if live_ask <= 0 or live_ask >= 1:
            raise ValueError(f"market {market.ticker} has no live {side} ask to buy")
        if args.price is not None and live_ask > args.price:
            print(
                color.yellow(
                    f"limit not filled: live {side} ask is {live_ask:.2f}, "
                    f"above your limit price {args.price:.2f}"
                )
            )
            return 0
        entry_price = live_ask
        if args.price is None:
            price_note = f"live Kalshi {side} ask {live_ask:.2f}"
            action = f"BUY_{side}_LIVE_ASK_PAPER"
            reason = "manual paper buy at live ask"
        else:
            price_note = f"live Kalshi {side} ask {live_ask:.2f}, within limit {args.price:.2f}"
            action = f"BUY_{side}_LIMIT_PAPER"
            reason = "manual paper buy at live ask within limit"

    from .fees import quadratic_fee_per_contract

    fee = quadratic_fee_per_contract(entry_price)
    cost = entry_price + fee
    desired_contracts = args.amount / cost
    filled_contracts = desired_contracts
    amount_used = args.amount
    size_note = ""
    ask_size = market.side_ask_size(side)
    if not args.force_fill and ask_size > 0 and desired_contracts > ask_size:
        filled_contracts = ask_size
        amount_used = filled_contracts * cost
        size_note = f"; capped by top {side} ask size {ask_size:.4f}"

    store = PaperStore(args.db_path)
    order_id = store.record_manual_buy(
        target_date=target.isoformat(),
        market_ticker=market.ticker,
        label=market.yes_sub_title,
        amount=amount_used,
        entry_price=entry_price,
        side=side,
        action=action,
        reason=reason,
        strike_type=market.strike_type,
        floor_strike=market.floor_strike,
        cap_strike=market.cap_strike,
    )
    # Report the stored order, not the pre-rounding estimate: the DB rounds
    # down to whole contracts and averages the fee across them, so the
    # fractional CLI numbers can disagree with what actually got booked.
    order = store.paper_order(order_id)
    stored_contracts = float(order["contracts"])
    stored_fee = float(order["fee_per_contract"])
    stored_cost = float(order["cost_per_contract"])
    amount_at_risk = stored_contracts * stored_cost
    max_profit = stored_contracts * (1.0 - stored_cost)
    print(color.green(f"paper bought order id={order_id}"))
    print(f"ticker: {market.ticker} ({market.yes_sub_title})")
    print(f"paper amount at risk: ${amount_at_risk:.2f}{size_note}")
    print(f"entry: {price_note}")
    print(f"entry fee per contract: ${stored_fee:.2f}")
    print(f"all-in cost per contract: ${stored_cost:.2f}")
    print(f"contracts: {stored_contracts:.0f}")
    print(f"max profit if {side} wins: ${max_profit:.2f}")
    print(f"max loss if {side} loses: ${amount_at_risk:.2f}")
    return 0


def cmd_paper_close(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    store = PaperStore(args.db_path)
    open_order = store.open_paper_order(args.order_id)
    if open_order is None:
        raise ValueError(f"no open paper order found with id {args.order_id}")
    side = str(open_order["side"] or ("NO" if "NO" in str(open_order["action"]).upper() else "YES")).upper()

    if args.exit_price is None:
        market = KalshiPublicClient().get_market(open_order["market_ticker"])
        if market.status != "active":
            raise ValueError(f"market {market.ticker} is {market.status}; cannot use a live bid to close")
        live_bid = market.side_bid(side)
        if live_bid <= 0:
            raise ValueError(f"market {market.ticker} has no live {side} bid to sell into")
        exit_price = live_bid
        price_note = f"live Kalshi {side} bid for {market.ticker}"
    else:
        exit_price = args.exit_price
        price_note = "manual offline override"

    row = store.close_paper_order(args.order_id, exit_price)
    pnl = f"${row['realized_pnl']:.2f}"
    pnl = color.green(pnl) if row["realized_pnl"] >= 0 else color.red(pnl)
    print(
        f"{color.green('closed')} paper order {row['id']} at {row['exit_price']:.2f} using {price_note}; "
        f"exit_fee={row['exit_fee_per_contract']:.2f}; "
        f"realized_pnl={pnl}"
    )
    return 0


def _validate_monitor_args(args: argparse.Namespace) -> None:
    values = [
        args.take_profit_pct,
        args.stop_loss_pct,
        args.yes_take_profit_pct,
        args.yes_stop_loss_pct,
        args.no_take_profit_pct,
        args.no_stop_loss_pct,
        args.model_veto_max_loss_pct,
    ]
    if any(value <= 0 for value in values) or args.model_veto_buffer < 0:
        raise ValueError(
            "take-profit, stop-loss, model-veto loss percentages, and model-veto buffer must be non-negative; percentages must be greater than zero"
        )


def _monitor_thresholds_for_side(args: argparse.Namespace, side: str) -> tuple[float, float]:
    normalized = side.upper()
    if normalized == "YES":
        return float(args.yes_take_profit_pct), float(args.yes_stop_loss_pct)
    if normalized == "NO":
        return float(args.no_take_profit_pct), float(args.no_stop_loss_pct)
    return float(args.take_profit_pct), float(args.stop_loss_pct)


def cmd_paper_monitor(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    store = PaperStore(args.db_path)
    rows = store.open_paper_orders(args.limit)
    if not rows:
        print(color.yellow("no open paper positions"))
        return 0

    model_veto_max_loss = args.model_veto_max_loss_pct / 100.0
    _validate_monitor_args(args)

    client = KalshiPublicClient()
    closed = 0
    inspected = 0
    for row in rows:
        inspected += 1
        side = str(row["side"] or ("NO" if "NO" in str(row["action"]).upper() else "YES")).upper()
        group_id = row["group_id"] if "group_id" in row.keys() else None
        if group_id:
            # Legs of an arbitrage box/ladder or a tail basket form a single
            # guaranteed/worst-case-bounded payoff. Closing one leg early
            # converts the structure into naked directional risk, so hold every
            # grouped leg to settlement instead of applying intraday exits.
            store.record_monitor_snapshot(
                row,
                side=side,
                action="HOLD_GUARANTEED_LEG",
                reason=f"leg of guaranteed-payoff group {group_id}; held to settlement",
            )
            print(
                f"HOLD order {row['id']} {row['market_ticker']} {side}: "
                f"guaranteed-payoff group {group_id} (held to settlement)"
            )
            continue
        take_profit_pct, stop_loss_pct = _monitor_thresholds_for_side(args, side)
        take_profit = take_profit_pct / 100.0
        stop_loss = stop_loss_pct / 100.0
        try:
            market = client.get_market(row["market_ticker"])
        except HTTPError as exc:
            # An expired/invalid API key (401/403) must NOT be masked as a benign
            # transient HOLD -- that would silently leave every open position
            # unmanaged. Surface it loudly by re-raising; transient 4xx (e.g. a
            # 404 on a delisted market) stay a per-order FETCH_FAILED.
            if exc.code in (401, 403):
                raise
            reason = f"market fetch failed (HTTP {exc.code})"
            store.record_monitor_snapshot(row, side=side, action="FETCH_FAILED", reason=reason)
            print(f"HOLD order {row['id']} {row['market_ticker']} {side}: {reason}")
            continue
        except (KalshiUnavailable, URLError, OSError, TimeoutError) as exc:
            # Genuinely transient network failures: hold this position and move on.
            # Non-network exceptions (e.g. a programming bug) now propagate instead
            # of being swallowed into a phantom HOLD across the whole book.
            reason = f"market fetch failed ({type(exc).__name__})"
            store.record_monitor_snapshot(row, side=side, action="FETCH_FAILED", reason=reason)
            print(f"HOLD order {row['id']} {row['market_ticker']} {side}: {reason}")
            continue

        if market.status != "active":
            store.record_monitor_snapshot(
                row,
                side=side,
                action="HOLD_INACTIVE_MARKET",
                reason=f"market status {market.status}",
                market_status=market.status,
            )
            print(f"HOLD order {row['id']} {row['market_ticker']} {side}: market status {market.status}")
            continue

        live_bid = market.side_bid(side)
        if live_bid <= 0:
            store.record_monitor_snapshot(
                row,
                side=side,
                action="HOLD_NO_BID",
                reason="no live bid",
                market_status=market.status,
                live_bid=live_bid,
            )
            print(f"HOLD order {row['id']} {row['market_ticker']} {side}: no live bid")
            continue

        entry_cost = float(row["cost_per_contract"])
        contracts = float(row["contracts"])
        exit_fee = quadratic_fee_average_per_contract(live_bid, contracts)
        net_exit = live_bid - exit_fee
        pnl_pct = (net_exit - entry_cost) / entry_cost if entry_cost > 0 else 0.0
        pnl_dollars = contracts * (net_exit - entry_cost)

        # Edge-based exit decision, shared with the dashboard mirror via exits.py.
        # Take-profit fires when the net exit reaches the model's fair value for
        # the side -- always reachable, unlike the old %-of-cost target that
        # exceeded $1 for any favorite (cost > ~0.74) and silently rode every
        # favorite to settlement. When no fresh model read exists, the legacy
        # %-of-cost target is the reachable-for-cheap-positions fallback. The
        # stop-loss is the reachable downside price floor with the NO-side model
        # veto preserved (do not sell intraday noise the model still expects to win).
        model_yes_p = store.latest_model_probability(
            str(row["target_date"]), str(row["market_ticker"])
        )
        model_side_p = (
            (model_yes_p if side == "YES" else 1.0 - model_yes_p)
            if model_yes_p is not None
            else None
        )
        signal = decide_exit(
            side=side,
            entry_cost=entry_cost,
            net_exit=net_exit,
            stop_loss_net=entry_cost * (1.0 - stop_loss),
            model_side_probability=model_side_p,
            model_veto_buffer=args.model_veto_buffer,
            model_veto_max_loss_roi=model_veto_max_loss,
            legacy_take_profit_net=entry_cost * (1.0 + take_profit),
            stop_loss_pct=stop_loss_pct,
        )

        if signal.action in ("HOLD", "HOLD_MODEL_VETO", "HOLD_NO_MODEL_READ"):
            store.record_monitor_snapshot(
                row,
                side=side,
                action=signal.action,
                reason=signal.reason,
                market_status=market.status,
                live_bid=live_bid,
                exit_fee_per_contract=exit_fee,
                net_exit_per_contract=net_exit,
                unrealized_pnl=pnl_dollars,
                unrealized_roi=pnl_pct,
            )
            print(
                f"HOLD order {row['id']} {row['market_ticker']} {side}: "
                f"bid={live_bid:.2f} net={net_exit:.2f} unrealized={pnl_pct * 100:.1f}% "
                f"(${pnl_dollars:.2f}); {signal.reason}"
            )
            continue

        reason = signal.reason
        exit_kind = signal.action  # "TAKE_PROFIT" | "STOP_LOSS"

        if args.dry_run:
            store.record_monitor_snapshot(
                row,
                side=side,
                action="WOULD_CLOSE",
                reason=reason,
                market_status=market.status,
                live_bid=live_bid,
                exit_fee_per_contract=exit_fee,
                net_exit_per_contract=net_exit,
                unrealized_pnl=pnl_dollars,
                unrealized_roi=pnl_pct,
            )
            print(
                f"WOULD_CLOSE order {row['id']} {row['market_ticker']} {side}: "
                f"bid={live_bid:.2f} net={net_exit:.2f} unrealized={pnl_pct * 100:.1f}% "
                f"(${pnl_dollars:.2f}); {reason}"
            )
            continue

        action = "CLOSE_TAKE_PROFIT" if exit_kind == "TAKE_PROFIT" else "CLOSE_STOP_LOSS"
        store.record_monitor_snapshot(
            row,
            side=side,
            action=action,
            reason=reason,
            market_status=market.status,
            live_bid=live_bid,
            exit_fee_per_contract=exit_fee,
            net_exit_per_contract=net_exit,
            unrealized_pnl=pnl_dollars,
            unrealized_roi=pnl_pct,
        )
        try:
            closed_row = store.close_paper_order(int(row["id"]), live_bid)
        except (ValueError, RuntimeError) as exc:
            # A concurrent settle/close can win the race for this row. Log and
            # keep inspecting the rest of the book instead of aborting the run.
            print(
                color.yellow(
                    f"skip order {row['id']} {row['market_ticker']} {side}: "
                    f"close failed ({type(exc).__name__}: {exc})"
                ),
                file=sys.stderr,
            )
            continue
        closed += 1
        pnl = f"${closed_row['realized_pnl']:.2f}"
        pnl = color.green(pnl) if closed_row["realized_pnl"] >= 0 else color.red(pnl)
        print(
            f"{color.green('closed')} order {closed_row['id']} {row['market_ticker']} {side}: "
            f"bid={live_bid:.2f}; exit_fee={closed_row['exit_fee_per_contract']:.2f}; "
            f"realized_pnl={pnl}; {reason}"
        )

    print(color.cyan(f"paper monitor inspected {inspected}, closed {closed}"))
    return 0


def cmd_paper_settle(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    target = parse_target_date(args.target_date)
    store = PaperStore(args.db_path)
    count = store.settle_paper_orders(target.isoformat(), args.settlement_high)
    print(color.cyan(f"settled {count} paper orders for {target.isoformat()} at {args.settlement_high:.0f}F"))
    return 0


def cmd_paper_auto_settle(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    store = PaperStore(args.db_path)
    open_targets = _completed_open_target_dates(store.open_paper_target_dates())
    if not open_targets:
        print(color.yellow("auto-settle skipped: no completed open paper target dates"))
        return 0
    clisfo_settled = 0
    try:
        clisfo_settlements = {
            target.isoformat(): high
            for target, high in fetch_recent_clisfo_settlements(timeout=args.timeout).items()
        }
    except (OSError, TimeoutError, URLError) as exc:
        clisfo_settlements = {}
        print(color.yellow(f"recent CLISFO settlement lookup skipped: {type(exc).__name__}: {exc}"))
    for target_date in open_targets:
        if target_date not in clisfo_settlements:
            continue
        count = store.settle_paper_orders(target_date, float(clisfo_settlements[target_date]))
        clisfo_settled += count
        if count:
            print(color.cyan(f"settled {count} paper orders for {target_date} from CLISFO"))
    if clisfo_settled and not _completed_open_target_dates(store.open_paper_target_dates()):
        print(color.cyan(f"auto-settled {clisfo_settled} paper orders from CLISFO"))
        return 0

    adapter = SfoForecasterAdapter(args.forecaster_root)
    settlements = {target.isoformat(): high for target, high in adapter.load_ksfo_daily_highs().items()}
    db_settled = 0
    for target_date in store.open_paper_target_dates():
        if target_date not in open_targets:
            continue
        if target_date not in settlements:
            continue
        count = store.settle_paper_orders(target_date, settlements[target_date])
        db_settled += count
        if count:
            print(color.cyan(f"settled {count} paper orders for {target_date} from WeatherEdge ground truth"))
    if db_settled and not _completed_open_target_dates(store.open_paper_target_dates()):
        print(
            color.cyan(
                f"auto-settled {clisfo_settled + db_settled} paper orders "
                "from CLISFO/WeatherEdge ground truth"
            )
        )
        return 0

    try:
        report = fetch_latest_clisfo(timeout=args.timeout)
    except (OSError, TimeoutError, URLError) as exc:
        print(color.yellow(f"CLISFO settlement lookup skipped: {type(exc).__name__}: {exc}"))
        return 0
    if report.report_date is None or report.max_temperature_f is None:
        print(color.yellow("CLISFO settlement lookup skipped: report date or max temperature missing"))
        return 0
    if report.report_date.isoformat() not in _completed_open_target_dates(store.open_paper_target_dates()):
        print(color.yellow("CLISFO settlement skipped: latest report does not match a completed open target"))
        return 0
    count = store.settle_paper_orders(report.report_date.isoformat(), float(report.max_temperature_f))
    print(
        color.cyan(
            f"auto-settled {clisfo_settled + db_settled + count} paper orders; "
            f"latest CLISFO {report.report_date.isoformat()} at {report.max_temperature_f}F "
            f"settled {count}"
        )
    )
    return 0


def _completed_open_target_dates(target_dates: list[str], *, now: datetime | None = None) -> list[str]:
    local_today = settlement_today(now)
    completed = []
    for target_date in target_dates:
        try:
            target = parse_target_date(target_date)
        except ValueError:
            continue
        if target < local_today:
            completed.append(target_date)
    return completed


def cmd_backtest_market(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    config = _config(args)
    store = PaperStore(args.db_path)
    summary = store.market_backtest_summary(since=args.since, until=args.until)
    print(color.cyan(color.bold("settled paper-market PnL")))
    print(f"starting_bankroll: ${config.paper_bankroll:.2f}")
    print(f"orders: {summary['orders']:.0f}")
    print(f"contracts: {summary['contracts']:.4f}")
    print(f"capital_at_risk: ${summary['capital_at_risk']:.2f}")
    realized = f"${summary['realized_pnl']:.2f}"
    print(f"realized_pnl: {color.green(realized) if summary['realized_pnl'] >= 0 else color.red(realized)}")
    print(f"roi: {summary['roi']:.3f}")
    print(f"hit_rate: {summary['hit_rate']:.3f}")
    print(f"avg_edge: {summary['avg_edge']:.3f}")
    print(f"open_orders: {summary['open_orders']:.0f}")
    print(f"open_capital_at_risk: ${summary['open_capital_at_risk']:.2f}")
    print(f"ending_bankroll_realized: ${config.paper_bankroll + summary['realized_pnl']:.2f}")
    return 0


def cmd_backtest_signals(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    adapter = SfoForecasterAdapter(args.forecaster_root)
    settlements = adapter.load_ksfo_daily_highs()
    store = PaperStore(args.db_path)
    summary = store.signal_backtest_summary(
        settlements,
        since=args.since,
        until=args.until,
        approved_only=args.approved_only,
        min_quality=args.min_quality,
        pre_resolution_only=not args.include_post_resolution,
        sample_mode=args.sample_mode,
    )
    print(color.cyan(color.bold("recorded decision-signal backtest")))
    print(f"sample_mode: {summary['sample_mode']}")
    print(f"pre_resolution_only: {str(summary['pre_resolution_only']).lower()}")
    print(f"official_settlement_days: {len(settlements)}")
    print(f"raw_signals: {summary['raw_signals']:.0f}")
    print(f"pre_resolution_signals: {summary['pre_resolution_signals']:.0f}")
    print(f"excluded_post_resolution_signals: {summary['excluded_post_resolution_signals']:.0f}")
    print(f"sampled_signals: {summary['signals']:.0f}")
    print(f"settled_signals: {summary['settled_signals']:.0f}")
    print(f"approved_signals: {summary['approved_signals']:.0f}")
    print(f"approval_rate: {summary['approval_rate']:.3f}")
    print(f"brier_score: {summary['brier_score']:.4f}")
    print(f"log_loss: {summary['log_loss']:.4f}")
    print(f"avg_probability: {summary['avg_probability']:.3f}")
    print(f"win_rate: {summary['win_rate']:.3f}")
    print(f"avg_edge: {summary['avg_edge']:.3f}")
    print(f"avg_edge_lcb: {summary['avg_edge_lcb']:.3f}")
    print(f"avg_quality: {summary['avg_quality']:.1f}")
    print(f"approved_paper_pnl: ${summary['approved_paper_pnl']:.2f}")
    print(f"approved_capital_at_risk: ${summary['approved_capital_at_risk']:.2f}")
    print(f"approved_roi: {summary['approved_roi']:.3f}")
    print(f"approved_hit_rate: {summary['approved_hit_rate']:.3f}")
    streams = summary.get("probability_streams") or {}
    if streams:
        print("")
        print(color.gray("probability_stream settled avg_p win_rate brier log_loss"))
        print(color.gray("-" * 60))
        for name in ("weather_model", "market_prior", "traded"):
            stream = streams.get(name)
            if not stream:
                continue
            print(
                f"{name:>18s} "
                f"{stream['settled']:7.0f} "
                f"{stream['avg_probability']:5.3f} "
                f"{stream['win_rate']:8.3f} "
                f"{stream['brier_score']:5.3f} "
                f"{stream['log_loss']:8.4f}"
            )
    buckets = summary["quality_buckets"]
    if buckets:
        print("")
        print(color.gray("q_bucket count approved avg_p win_rate brier approved_roi"))
        print(color.gray("-" * 62))
        for bucket in buckets:
            print(
                f"{bucket['range']:>7s} "
                f"{bucket['count']:5.0f} "
                f"{bucket['approved']:8.0f} "
                f"{bucket['avg_probability']:5.3f} "
                f"{bucket['win_rate']:8.3f} "
                f"{bucket['brier_score']:5.3f} "
                f"{bucket['approved_roi']:12.3f}"
            )
    return 0


def cmd_backtest_rescore(args: argparse.Namespace) -> int:
    color = Color.from_no_color(args.no_color)
    config = _config(args)
    profile = _risk_profile_name(args)
    adapter = SfoForecasterAdapter(args.forecaster_root)
    settlements = adapter.load_ksfo_daily_highs()
    store = PaperStore(args.db_path)
    rows = store.sampled_decision_rows(
        since=args.since,
        until=args.until,
        pre_resolution_only=not args.include_post_resolution,
        sample_mode=args.sample_mode,
    )
    result = run_rescore(
        rows,
        settlements,
        config,
        bankroll=config.paper_bankroll,
        bootstrap_samples=args.bootstrap_samples,
    )

    if getattr(args, "json_output", None):
        Path(args.json_output).write_text(json.dumps(result, indent=2), encoding="utf-8")

    counts = result["counts"]
    cand = result["candidate"]
    rec = result["recorded_config_own_book"]

    print(color.cyan(color.bold(f"config rescore — risk_profile={profile}")))
    print(color.gray(f"basis: {result['config_basis']}"))
    print(f"official_settlement_days: {len(settlements)}")
    print(f"starting_bankroll: ${result['starting_bankroll']:.2f}")
    print(f"sampled_snapshots: {counts['considered']}")
    print(f"approved_under_recorded_config: {counts['approved_under_recorded_config']}")
    print(f"approved_under_candidate_config: {counts['approved_under_candidate_config']}")
    print(f"approved_without_settlement: {counts['approved_without_settlement']}")
    print(f"settled_decisions: {counts['settled_decisions']}")
    print(f"independent_days: {counts['independent_days']}")
    print("")
    print(color.bold("candidate config (after-fee, held-to-settlement):"))
    print(f"  realized_pnl: ${cand['realized_pnl']:.2f}")
    print(f"  capital_at_risk: ${cand['capital_at_risk']:.2f}")
    print(f"  roi: {_fmt_opt(cand['roi'], '{:.3%}')}")
    print(f"  wins/losses: {cand['wins']}/{cand['losses']}")
    print(f"  hit_rate_per_trade: {_fmt_opt(cand['hit_rate_per_trade'], '{:.3f}')}")
    print(f"  ending_equity: ${cand['ending_equity']:.2f}")
    print(
        "  log_growth_per_independent_day: "
        f"{_fmt_opt(cand['log_growth_per_independent_day'], '{:.5f}')}"
    )
    print(
        "  geometric_growth_per_independent_day: "
        f"{_fmt_opt(cand['geometric_growth_per_independent_day'], '{:.3%}')}"
    )
    portfolio = result.get("portfolio") or {}
    if portfolio:
        print(f"  portfolio_day_hit_rate: {_fmt_opt(portfolio.get('hit_rate_per_day'), '{:.3f}')}")
        print(f"  max_drawdown: ${float(portfolio.get('max_drawdown') or 0.0):.2f}")
        print(f"  max_drawdown_pct: {_fmt_opt(portfolio.get('max_drawdown_pct'), '{:.3%}')}")
    ci = cand["roi_ci95_day_clustered"]
    if ci is not None:
        print(f"  roi_ci95_day_clustered: [{ci[0]:.3%}, {ci[1]:.3%}]")
    else:
        print("  roi_ci95_day_clustered: n/a (need >= 2 settled days)")
    print("")
    print(
        color.gray(
            f"recorded config own book: pnl ${rec['realized_pnl']:.2f}, "
            f"roi {_fmt_opt(rec['roi'], '{:.3%}')}, "
            f"{rec['settled_decisions']} settled over {rec['independent_days']} days, "
            f"hit_rate {_fmt_opt(rec['hit_rate_per_trade'], '{:.3f}')}"
        )
    )

    by_side = result["by_side"]
    if by_side:
        print("")
        print(color.gray("side  trades days  pnl       roi      hit_rate"))
        print(color.gray("-" * 50))
        for name in ("NO", "YES"):
            bucket = by_side.get(name)
            if not bucket:
                continue
            print(
                f"{name:>4s} {bucket['trades']:6d} {bucket['independent_days']:4d} "
                f"${bucket['realized_pnl']:8.2f} {_fmt_opt(bucket['roi'], '{:7.3%}')} "
                f"{_fmt_opt(bucket['hit_rate'], '{:.3f}')}"
            )

    by_sleeve = (result.get("portfolio") or {}).get("by_sleeve") or {}
    if by_sleeve:
        print("")
        print(color.gray("sleeve              trades days  pnl       roi      hit_rate"))
        print(color.gray("-" * 64))
        for name, bucket in by_sleeve.items():
            print(
                f"{name[:18]:>18s} {bucket['trades']:6d} {bucket['independent_days']:4d} "
                f"${bucket['realized_pnl']:8.2f} {_fmt_opt(bucket['roi'], '{:7.3%}')} "
                f"{_fmt_opt(bucket['hit_rate'], '{:.3f}')}"
            )

    by_cohort = result["by_cohort"]
    if by_cohort:
        print("")
        print(color.gray("cohort (by settled high)  trades days  pnl       roi      hit_rate"))
        print(color.gray("-" * 64))
        for name, bucket in by_cohort.items():
            print(
                f"{name:>24s} {bucket['trades']:6d} {bucket['independent_days']:4d} "
                f"${bucket['realized_pnl']:8.2f} {_fmt_opt(bucket['roi'], '{:7.3%}')} "
                f"{_fmt_opt(bucket['hit_rate'], '{:.3f}')}"
            )
    return 0


def _fmt_opt(value, spec: str) -> str:
    if value is None:
        return "n/a"
    return spec.format(value)


def _format_pnl(value) -> str:
    if value is None:
        return "open"
    return f"${float(value):.2f}"


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


def _print_consensus_line(
    consensus: MarketConsensus | None,
    forecast_high_f: float,
    color: Color,
) -> None:
    """One-line "what the market forecasts" summary under the model forecast.

    This is the same headline number Kalshi prints on the market ("70.7
    forecast"), rebuilt from the ladder, shown with its spread, modal bin, and
    the signed gap to our model right where the model's own forecast prints.
    """

    if consensus is None or not consensus.available or consensus.implied_high_f is None:
        return
    pieces = [f"{consensus.implied_high_f:.1f}F"]
    if (
        consensus.p10_f is not None
        and consensus.median_f is not None
        and consensus.p90_f is not None
    ):
        pieces.append(
            f"P10/P50/P90={consensus.p10_f:.1f}/{consensus.median_f:.1f}/{consensus.p90_f:.1f}F"
        )
    if consensus.modal_bin_label:
        pieces.append(f"modal={consensus.modal_bin_label} {consensus.modal_probability:.0%}")
    if consensus.implied_stdev_f is not None:
        pieces.append(f"implied_spread={consensus.implied_stdev_f:.1f}F")
    gap = consensus.gap_to_forecast_f(forecast_high_f)
    line = color.cyan("kalshi forecast: " + " ".join(pieces))
    if gap is not None:
        direction = "warmer than" if gap > 0 else "cooler than" if gap < 0 else "level with"
        gap_text = f"model {gap:+.1f}F ({direction} market)"
        # Flag a material disagreement: that is both the edge source and the risk.
        gap_render = color.yellow(gap_text) if abs(gap) >= 2.0 else color.gray(gap_text)
        line = f"{line} {color.gray('|')} {gap_render}"
    print(line)


def _print_analysis(
    event_title,
    forecast,
    decisions,
    placed_ids: list[int],
    market_available: bool,
    color: Color,
    paper_stake: float | None = None,
    daily_budget: float | None = None,
    daily_budget_remaining: float | None = None,
    intraday: IntradaySnapshot | None = None,
    ensemble: EnsembleSnapshot | None = None,
    entry_block_reason: str | None = None,
    consensus: MarketConsensus | None = None,
) -> None:
    print(color.cyan(color.bold(event_title)))
    print(
        f"{color.bold('forecast')} {forecast.target_date.isoformat()}: {forecast.predicted_high_f:.2f}F "
        f"source_spread={forecast.source_spread_f:.2f}F method={forecast.method}"
    )
    _print_consensus_line(consensus, forecast.predicted_high_f, color)
    forecast_context = _forecast_context_pieces(forecast)
    if forecast_context:
        print(color.cyan("forecast context: " + "; ".join(forecast_context)))
    intraday_update = forecast.raw.get("intraday_update") if isinstance(forecast.raw, dict) else None
    if intraday is not None and intraday.observed_high_f is not None:
        pieces = [f"observed_high_so_far={intraday.observed_high_f:.1f}F"]
        if intraday.observed_high_source:
            pieces.append(f"source={intraday.observed_high_source}")
        if intraday.is_complete:
            pieces.append("complete_daily_high")
        if intraday.latest_temp_f is not None:
            pieces.append(f"latest_temp={intraday.latest_temp_f:.1f}F")
        if intraday.remaining_forecast_high_f is not None:
            pieces.append(f"remaining_hourly_high={intraday.remaining_forecast_high_f:.1f}F")
        if intraday_update:
            pieces.append(
                f"adjusted_from={float(intraday_update['pre_intraday_predicted_high_f']):.2f}F"
            )
        print(color.cyan("intraday: " + "; ".join(pieces)))
    observed_decision = forecast.raw.get("observed_high_decision") if isinstance(forecast.raw, dict) else None
    if isinstance(observed_decision, dict):
        mode = observed_decision.get("mode")
        reason = observed_decision.get("reason")
        high = observed_decision.get("highF")
        if mode and reason and high is not None:
            print(color.cyan(f"observed lock: {mode} at {float(high):.1f}F ({reason})"))
    if ensemble is not None:
        grid = "-"
        if ensemble.grid_latitude is not None and ensemble.grid_longitude is not None:
            grid = f"{ensemble.grid_latitude:.2f},{ensemble.grid_longitude:.2f}"
        print(
            color.cyan(
                "ensemble: "
                f"station_mean={ensemble.station_mean_high_f:.2f}F "
                f"raw_mean={ensemble.raw_mean_high_f:.2f}F "
                f"station_std={ensemble.station_std_high_f:.2f}F "
                f"members={ensemble.member_count} "
                f"cell={ensemble.cell_selection} "
                f"grid={grid} "
                f"station_shift={ensemble.station_bias_f:+.2f}F"
            )
        )
        if ensemble.warning:
            print(color.yellow(f"ensemble warning: {ensemble.warning}"))
    if paper_stake is not None:
        print(color.yellow(f"paper stake override: ${paper_stake:.2f} per approved trade"))
    if daily_budget is not None:
        remaining = daily_budget if daily_budget_remaining is None else daily_budget_remaining
        print(
            color.yellow(
                f"daily paper budget: ${daily_budget:.2f} total; "
                f"${remaining:.2f} remaining for this target date"
            )
        )
    if entry_block_reason:
        print(color.yellow(entry_block_reason))
    print("")
    if not market_available:
        print(color.gray("side label          resid  ens   intra model  p     p_lcb heat  q     note"))
        print(color.gray("-" * 103))
        for decision in decisions:
            print(
                f"{decision.side:4s} {decision.label[:13]:13s} "
                f"{_color_prob_optional(color, decision.residual_probability)} "
                f"{_color_prob_optional(color, decision.ensemble_probability)} "
                f"{_color_prob_optional(color, decision.intraday_probability)} "
                f"{_color_prob_optional(color, decision.model_probability)} "
                f"{_color_prob(color, decision.probability)} {_color_prob(color, decision.probability_lcb)} "
                f"{_color_prob_optional(color, decision.remaining_heat_risk)} "
                f"{decision.trade_quality_score:5.1f} "
                f"{color.yellow('no active Kalshi market')}"
            )
        return

    print(color.gray("side label          bid   ask resid  ens   intra model  mkt    p     p_lcb heat  edge  edge_lcb q     contracts spend    decision"))
    print(color.gray("-" * 158))
    for decision in decisions:
        status = color.green(color.bold("TRADE")) if decision.approved else color.red("NO")
        reason = "" if decision.approved else color.gray("; ".join(decision.reasons[:2]))
        spend = decision.recommended_contracts * decision.cost_per_contract
        print(
            f"{decision.side:4s} {decision.label[:13]:13s} "
            f"{decision.bid:5.2f} {decision.ask:5.2f} "
            f"{_color_prob_optional(color, decision.residual_probability)} "
            f"{_color_prob_optional(color, decision.ensemble_probability)} "
            f"{_color_prob_optional(color, decision.intraday_probability)} "
            f"{_color_prob_optional(color, decision.model_probability)} "
            f"{_color_prob_optional(color, decision.market_probability)} "
            f"{_color_prob(color, decision.probability)} {_color_prob(color, decision.probability_lcb)} "
            f"{_color_prob_optional(color, decision.remaining_heat_risk)} "
            f"{_color_edge(color, decision.edge)} {_color_edge(color, decision.edge_lcb)} "
            f"{decision.trade_quality_score:5.1f} "
            f"{decision.recommended_contracts:9.4f} ${spend:7.2f} {status} {reason}"
        )
    if placed_ids:
        print("")
        print(color.green(f"recorded paper orders: {', '.join(str(order_id) for order_id in placed_ids)}"))


def _print_portfolio_scan(
    event_title,
    forecast,
    plan: PortfolioPlan,
    decisions,
    *,
    placed_ids: list[int],
    market_available: bool,
    color: Color,
    intraday: IntradaySnapshot | None = None,
    ensemble: EnsembleSnapshot | None = None,
    entry_block_reason: str | None = None,
    consensus: MarketConsensus | None = None,
) -> None:
    print(color.cyan(color.bold(event_title)))
    print(
        f"{color.bold('portfolio scan')} {forecast.target_date.isoformat()}: "
        f"forecast={forecast.predicted_high_f:.2f}F "
        f"source_spread={forecast.source_spread_f:.2f}F method={forecast.method} "
        f"profile={plan.risk_profile}"
    )
    _print_consensus_line(consensus, forecast.predicted_high_f, color)
    forecast_context = _forecast_context_pieces(forecast)
    if forecast_context:
        print(color.cyan("forecast context: " + "; ".join(forecast_context)))
    if intraday is not None and intraday.observed_high_f is not None:
        pieces = [f"observed_high_so_far={intraday.observed_high_f:.1f}F"]
        if intraday.is_complete:
            pieces.append("complete_daily_high")
        if intraday.latest_temp_f is not None:
            pieces.append(f"latest_temp={intraday.latest_temp_f:.1f}F")
        print(color.cyan("intraday: " + "; ".join(pieces)))
    if ensemble is not None:
        print(
            color.cyan(
                "ensemble: "
                f"station_mean={ensemble.station_mean_high_f:.2f}F "
                f"station_std={ensemble.station_std_high_f:.2f}F "
                f"members={ensemble.member_count}"
            )
        )
        if ensemble.warning:
            print(color.yellow(f"ensemble warning: {ensemble.warning}"))
    if not market_available:
        print(color.yellow("no active Kalshi market; portfolio placement is disabled"))
    if entry_block_reason:
        print(color.yellow(entry_block_reason))

    if entry_block_reason:
        blocked_label = (
            "BLOCKED_BY_PAUSE"
            if "paused" in entry_block_reason.lower()
            else "BLOCKED"
        )
        status = color.yellow(color.bold(blocked_label))
    else:
        status = color.green(color.bold("APPROVED")) if plan.approved else color.red(color.bold("REJECTED"))
    print("")
    print(
        f"portfolio={status} run={plan.run_id} "
        f"spend=${plan.total_spend:.2f} expected=${plan.expected_profit:.2f} "
        f"worst_loss=${plan.worst_case_loss:.2f} "
        f"loss_cap=${plan.limits.max_daily_loss:.2f} "
        f"yes_sleeve=${plan.limits.yes_sleeve:.2f} "
        f"explore_sleeve=${plan.limits.explore_sleeve:.2f}"
    )
    for reason in plan.reasons:
        print(color.yellow(f"allocator: {reason}"))

    print("")
    print(color.gray("sleeve            side label          bid   ask    p   p_lcb  edge edge_lcb q     contracts spend    decision"))
    print(color.gray("-" * 124))
    sleeve_by_key = {
        _portfolio_decision_key(leg.decision): leg.sleeve
        for leg in plan.legs
    }
    for decision in decisions:
        sleeve = sleeve_by_key.get(_portfolio_decision_key(decision), "-")
        status_text = color.green("TRADE") if decision.approved else color.red("NO")
        reason = "" if decision.approved else color.gray("; ".join(decision.reasons[:2]))
        spend = decision.recommended_contracts * decision.cost_per_contract
        print(
            f"{sleeve[:16]:16s} {decision.side:4s} {decision.label[:13]:13s} "
            f"{decision.bid:5.2f} {decision.ask:5.2f} "
            f"{_color_prob(color, decision.probability)} {_color_prob(color, decision.probability_lcb)} "
            f"{_color_edge(color, decision.edge)} {_color_edge(color, decision.edge_lcb)} "
            f"{decision.trade_quality_score:5.1f} "
            f"{decision.recommended_contracts:9.4f} ${spend:7.2f} {status_text} {reason}"
        )

    if placed_ids:
        print("")
        print(color.green(f"recorded paper portfolio orders: {', '.join(str(order_id) for order_id in placed_ids)}"))


def _print_tail_basket(
    event_title,
    forecast,
    basket: TailBasket,
    *,
    placed_ids: list[int],
    market_available: bool,
    color: Color,
    intraday: IntradaySnapshot | None = None,
    ensemble: EnsembleSnapshot | None = None,
    entry_block_reason: str | None = None,
) -> None:
    print(color.cyan(color.bold(event_title)))
    print(
        f"{color.bold('tail basket')} {forecast.target_date.isoformat()}: "
        f"forecast={forecast.predicted_high_f:.2f}F "
        f"tail_band={basket.plausible_low_f:.1f}-{basket.plausible_high_f:.1f}F "
        f"source_spread={forecast.source_spread_f:.2f}F method={forecast.method}"
    )
    forecast_context = _forecast_context_pieces(forecast)
    if forecast_context:
        print(color.cyan("forecast context: " + "; ".join(forecast_context)))
    if intraday is not None and intraday.observed_high_f is not None:
        pieces = [f"observed_high_so_far={intraday.observed_high_f:.1f}F"]
        if intraday.is_complete:
            pieces.append("complete_daily_high")
        if intraday.latest_temp_f is not None:
            pieces.append(f"latest_temp={intraday.latest_temp_f:.1f}F")
        print(color.cyan("intraday: " + "; ".join(pieces)))
    if ensemble is not None:
        print(
            color.cyan(
                "ensemble: "
                f"station_mean={ensemble.station_mean_high_f:.2f}F "
                f"station_std={ensemble.station_std_high_f:.2f}F "
                f"members={ensemble.member_count}"
            )
        )
        if ensemble.warning:
            print(color.yellow(f"ensemble warning: {ensemble.warning}"))
    if entry_block_reason:
        print(color.yellow(entry_block_reason))
    if not market_available:
        print(color.yellow("no active Kalshi market; basket is research-only until the event is listed"))

    status = color.green(color.bold("APPROVED")) if basket.approved else color.red(color.bold("REJECTED"))
    print("")
    print(
        f"basket={status} center={basket.center_label or '-'} "
        f"tail_p={basket.tail_yes_probability:.3f} "
        f"spend=${basket.total_spend:.2f} "
        f"edge=${basket.expected_profit:.2f} "
        f"worst_loss=${basket.worst_case_loss:.2f}"
    )
    for reason in basket.reasons:
        print(color.yellow(f"guardrail: {reason}"))

    print("")
    print(color.gray("kind       side label          bid   ask    p   p_lcb  edge edge_lcb contracts spend    decision"))
    print(color.gray("-" * 112))
    for leg in basket.legs:
        decision = leg.decision
        leg_status = color.green("TRADE") if decision.approved and basket.approved else color.red("NO")
        reason = "" if decision.approved else color.gray("; ".join(decision.reasons[:2]))
        print(
            f"{leg.kind:10s} {decision.side:4s} {decision.label[:13]:13s} "
            f"{decision.bid:5.2f} {decision.ask:5.2f} "
            f"{_color_prob(color, decision.probability)} {_color_prob(color, decision.probability_lcb)} "
            f"{_color_edge(color, decision.edge)} {_color_edge(color, decision.edge_lcb)} "
            f"{decision.recommended_contracts:9.4f} ${leg.spend:7.2f} {leg_status} {reason}"
        )

    if basket.scenarios:
        print("")
        print(color.gray("settlement scenario       p_yes    basket_pnl"))
        print(color.gray("-" * 48))
        for scenario in basket.scenarios:
            pnl = f"${scenario.pnl:+.2f}"
            pnl = color.green(pnl) if scenario.pnl >= 0 else color.red(pnl)
            p = "-" if scenario.probability is None else f"{scenario.probability:5.3f}"
            print(f"{scenario.label[:22]:22s} {p:>6s} {pnl:>12s}")

    if placed_ids:
        print("")
        print(color.green(f"recorded paper basket orders: {', '.join(str(order_id) for order_id in placed_ids)}"))


def _print_arbitrage(
    event_title,
    target_date: str,
    opportunities: list[ArbitrageOpportunity],
    *,
    placed_ids: list[int],
    market_available: bool,
    color: Color,
    max_spend: float | None,
    min_profit: float,
    entry_block_reason: str | None = None,
) -> None:
    print(color.cyan(color.bold(event_title)))
    spend_text = "profile event cap" if max_spend is None else f"${max_spend:.2f}"
    print(
        f"{color.bold('arbitrage scan')} {target_date}: "
        f"max_spend={spend_text} min_profit=${min_profit:.2f}"
    )
    if not market_available:
        print(color.yellow("no active Kalshi market; arbitrage placement is disabled"))
    if entry_block_reason:
        print(color.yellow(entry_block_reason))

    print("")
    print(color.gray("kind             legs contracts spend    payout   profit   roi     decision"))
    print(color.gray("-" * 88))
    if not opportunities:
        print(color.yellow("no arbitrage portfolios could be evaluated"))
        return

    for opportunity in opportunities:
        status = color.green(color.bold("TRADE")) if opportunity.approved else color.red("NO")
        reason = "" if opportunity.approved else color.gray("; ".join(opportunity.reasons[:2]))
        roi = opportunity.return_on_spend * 100.0
        print(
            f"{opportunity.kind:16s} {len(opportunity.legs):4d} "
            f"{opportunity.contracts:9.4f} ${opportunity.total_spend:7.2f} "
            f"${opportunity.guaranteed_payout:7.2f} ${opportunity.guaranteed_profit:7.2f} "
            f"{roi:6.2f}% {status} {reason}"
        )
        if opportunity.approved:
            for leg in opportunity.legs:
                print(
                    color.gray(
                        f"  {leg.side:3s} {leg.market.yes_sub_title[:18]:18s} "
                        f"ask={leg.price:.2f} fee={leg.fee_per_contract:.4f} "
                        f"cost={leg.cost_per_contract:.4f}"
                    )
                )

    if placed_ids:
        print("")
        print(color.green(f"recorded paper arbitrage orders: {', '.join(str(order_id) for order_id in placed_ids)}"))


def _color_prob(color: Color, value: float) -> str:
    text = f"{float(value):5.3f}"
    if value >= 0.25:
        return color.green(text)
    if value >= 0.12:
        return color.yellow(text)
    return color.red(text)


def _forecast_context_pieces(forecast) -> list[str]:
    pieces: list[str] = []
    if forecast.lead_hours is not None:
        pieces.append(f"lead={forecast.lead_hours:.1f}h")
    if forecast.fresh_station_count is not None:
        pieces.append(f"fresh_stations={forecast.fresh_station_count}")
    google_api = forecast.raw.get("google_weather_api") if isinstance(forecast.raw, dict) else None
    if isinstance(google_api, dict):
        daily_used = google_api.get("daily_events_used")
        daily_budget = google_api.get("daily_event_budget")
        monthly_used = google_api.get("monthly_events_used")
        monthly_budget = google_api.get("monthly_event_budget")
        if daily_used is not None and daily_budget is not None:
            text = f"google_events={int(daily_used)}/{int(daily_budget)} day"
            if monthly_used is not None and monthly_budget is not None:
                text += f", {int(monthly_used)}/{int(monthly_budget)} month"
            pieces.append(text)
    elif forecast.calls_used_today is not None and forecast.max_calls_per_day is not None:
        pieces.append(f"google_events={forecast.calls_used_today}/{forecast.max_calls_per_day}")
    google_components = forecast.raw.get("google_components") if isinstance(forecast.raw, dict) else None
    if isinstance(google_components, dict):
        hourly = google_components.get("hourly_local_day_high_f")
        daily = google_components.get("daily_endpoint_high_f")
        gap = google_components.get("daily_minus_hourly_gap_f")
        if hourly is not None and daily is not None:
            pieces.append(
                f"google_hourly={float(hourly):.1f}F daily={float(daily):.1f}F gap={float(gap or 0):+.1f}F"
            )
        current = google_components.get("current_conditions")
        if isinstance(current, dict):
            current_temp = current.get("current_temp_f")
            last_24h_max = current.get("last_24h_max_temp_f")
            humidity = current.get("relative_humidity_pct")
            context = []
            if current_temp is not None:
                context.append(f"current={float(current_temp):.1f}F")
            if last_24h_max is not None:
                context.append(f"24h_max={float(last_24h_max):.1f}F")
            if humidity is not None:
                context.append(f"rh={int(humidity)}%")
            if context:
                pieces.append("google_current=" + ",".join(context))
    google_warning = forecast.raw.get("google_warning") if isinstance(forecast.raw, dict) else None
    if google_warning:
        pieces.append(f"google_warning={google_warning}")
    weights = [
        ("G", forecast.google_weight),
        ("NWS", forecast.nws_weight),
        ("OM", forecast.open_meteo_weight),
        ("Hist", forecast.history_weight),
    ]
    if any(value is not None for _, value in weights):
        pieces.append(
            "weights="
            + ",".join(
                f"{label}:{float(value):.2f}"
                for label, value in weights
                if value is not None
            )
        )
    blend_weighting = forecast.raw.get("blend_weighting") if isinstance(forecast.raw, dict) else None
    if isinstance(blend_weighting, dict) and blend_weighting.get("mode"):
        pieces.append(f"weight_mode={blend_weighting['mode']}")
    return pieces


def _color_prob_optional(color: Color, value: float | None) -> str:
    if value is None:
        return color.gray("  n/a")
    return _color_prob(color, value)


def _color_edge(color: Color, value: float) -> str:
    text = f"{float(value):7.3f}"
    if value > 0:
        return color.green(text)
    if value > -0.02:
        return color.yellow(text)
    return color.red(text)


def _color_status(color: Color, status: str) -> str:
    if status in {"PAPER_FILLED", "PAPER_SETTLED"}:
        return color.green(status)
    if status == "PAPER_LIMIT_RESTING":
        return color.yellow(status)
    if status == "PAPER_CLOSED":
        return color.cyan(status)
    if status == "REJECTED":
        return color.red(status)
    return color.yellow(status)


if __name__ == "__main__":
    raise SystemExit(main())
