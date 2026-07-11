"""Argument registration for the stable CLI facade."""

from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path

from ..config import (
    DEFAULT_DB_PATH,
    DEFAULT_FORECASTER_ROOT,
    normalize_risk_profile_name,
)
from ..datasets import KSFO_ASOS_STATION, KSFO_ISD_STATION
from ..exits import (
    DEFAULT_NO_STOP_LOSS_PCT,
    DEFAULT_NO_TAKE_PROFIT_PCT,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
    DEFAULT_YES_STOP_LOSS_PCT,
    DEFAULT_YES_TAKE_PROFIT_PCT,
)
from ..monitor import DEFAULT_MODEL_VETO_BUFFER, DEFAULT_MODEL_VETO_MAX_LOSS_PCT


_COMMAND_NAMES = (
    "cmd_analyze",
    "cmd_arbitrage",
    "cmd_backtest_calibration",
    "cmd_backtest_market",
    "cmd_backtest_rescore",
    "cmd_backtest_signals",
    "cmd_collect",
    "cmd_daily_report",
    "cmd_dataset_backfill",
    "cmd_dataset_research",
    "cmd_dataset_status",
    "cmd_paper_archive",
    "cmd_paper_auto_settle",
    "cmd_paper_buy",
    "cmd_paper_check_foreign_keys",
    "cmd_paper_close",
    "cmd_paper_features",
    "cmd_paper_monitor",
    "cmd_paper_prune",
    "cmd_paper_report",
    "cmd_paper_resettle",
    "cmd_paper_settle",
    "cmd_paper_summary",
    "cmd_portfolio_scan",
    "cmd_strategy_research",
    "cmd_synthetic_blend_calibration",
    "cmd_tail_basket",
)


def _default_calibration_source() -> str:
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


def _bind_commands(command_module=None) -> None:
    if command_module is None:
        command_module = importlib.import_module("sfo_kalshi_quant.cli")
    for name in _COMMAND_NAMES:
        globals()[name] = getattr(command_module, name)


def build_parser(command_module=None) -> argparse.ArgumentParser:
    _bind_commands(command_module)
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

    register_scan_commands(sub)
    register_data_commands(sub)
    register_research_commands(sub)
    register_paper_commands(sub)
    register_backtest_commands(sub)
    return parser


def register_scan_commands(sub) -> None:

    analyze = sub.add_parser("analyze", help="Rank current/paper market opportunities across cities")
    analyze.add_argument(
        "--cities",
        default=None,
        help="'all' or comma-separated city slugs (default: env PAPER_CITIES or all)",
    )
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
    basket.add_argument("--city", default="sfo", help="city slug (single city)")
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
    arbitrage.add_argument("--city", default="sfo", help="city slug (single city)")
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
        "--cities",
        default=None,
        help="'all' or comma-separated city slugs (default: env PAPER_CITIES or all)",
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

def register_data_commands(sub) -> None:
    collect = sub.add_parser("collect", help="Fetch and store live Kalshi event plus forecast snapshot")
    collect.add_argument("--target-date", default="today", help="today, tomorrow, both, comma-list, or YYYY-MM-DD")
    collect.add_argument(
        "--cities",
        default=None,
        help="'all' or comma-separated city slugs (default: env PAPER_CITIES or all)",
    )
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
            "lamp",
            "gfs-mos",
            "nbm",
            "hrrr",
            "kalshi-history",
        ),
        default="tier1",
        help="Dataset source to backfill. tier1 runs the production-safe compact sources.",
    )
    dataset_backfill.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    dataset_backfill.add_argument("--end-date", help="YYYY-MM-DD. Defaults to start date.")
    dataset_backfill.add_argument(
        "--cities",
        default=os.getenv("PAPER_CITIES", "all"),
        help="'all' or comma-separated city slugs for station-aware sources.",
    )
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

def register_research_commands(sub) -> None:
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

def register_paper_commands(sub) -> None:
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
    monitor.add_argument(
        "--limit", type=int, default=0,
        help="Optional cap on open positions to inspect; 0 processes the complete active book",
    )
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

    settle = sub.add_parser("paper-settle", help="Settle one city's paper orders for a date with its final CLI high")
    settle.add_argument("--target-date", required=True)
    settle.add_argument("--settlement-high", type=float, required=True)
    settle.add_argument("--city", default="sfo", help="city slug whose orders this high settles")
    settle.set_defaults(func=cmd_paper_settle)

    resettle = sub.add_parser(
        "paper-resettle",
        help="Verify booked paper settlements against final CLI truth without rewriting P&L",
    )
    resettle.add_argument(
        "--verify",
        action="store_true",
        required=True,
        help="Record MATCH/MISMATCH audit results; never mutate settled orders",
    )
    resettle.add_argument("--days", type=int, default=14, help="Recent target days to verify")
    resettle.set_defaults(func=cmd_paper_resettle)

    prune = sub.add_parser(
        "paper-prune",
        help=(
            "Low-level/manual; use the archive-gated service for scheduled retention"
        ),
    )
    prune.add_argument("--full-days", type=int, default=7)
    prune.add_argument("--dedup-days", type=int, default=45)
    prune.set_defaults(func=cmd_paper_prune)

    fk_check = sub.add_parser(
        "paper-check-foreign-keys",
        help="Explicit capped foreign-key integrity audit for deploy/health checks",
    )
    fk_check.add_argument("--limit", type=int, default=100)
    fk_check.set_defaults(func=cmd_paper_check_foreign_keys)

    archive = sub.add_parser(
        "paper-archive",
        help="Append-only lossless day export of the journal; gates paper-prune (see archive.py)",
    )
    archive.add_argument("--archive-dir", type=Path, default=None,
                         help="Default: <db dir>/archive")
    archive.add_argument("--merge-db", type=Path, action="append", default=None,
                         help="Extra source DB (e.g. a backup) merged by id during export; repeatable")
    archive.add_argument("--check-gate", action="store_true",
                         help="Exit non-zero unless every complete UTC day is archived+verified")
    archive.add_argument("--upload", action="store_true",
                         help="Upload unuploaded archive files to S3 (env SFO_ARCHIVE_S3_BUCKET)")
    archive.add_argument("--cleanup", action="store_true",
                         help="Delete local files older than --keep-days that are verifiably uploaded")
    archive.add_argument("--keep-days", type=int, default=30)
    archive.add_argument("--skip-full", action="store_true",
                         help="Skip the nightly full-table snapshots of the small tables")
    archive.set_defaults(func=cmd_paper_archive)

    features = sub.add_parser(
        "paper-features",
        help="Distill archived decision ticks into market_side_day feature rows with settlement labels",
    )
    features.add_argument("--archive-dir", type=Path, default=None)
    features.add_argument("--features-db", type=Path, default=None,
                          help="Default: <archive dir>/features.db")
    features.add_argument("--weather-db", type=Path, default=None,
                          help="weather.db for CLI settlement labels (default: forecaster root)")
    features.add_argument("--days", type=int, default=9,
                          help="Trailing target-date window to (re)build")
    features.set_defaults(func=cmd_paper_features)

    auto_settle = sub.add_parser(
        "paper-auto-settle",
        help="Settle eligible paper orders only from durable is_final=1 CLI truth",
    )
    auto_settle.add_argument(
        "--timeout", type=int, default=20,
        help="Deprecated compatibility option; auto-settle performs no live CLI fetch",
    )
    auto_settle.add_argument(
        "--cities",
        default=None,
        help="'all' or comma-separated city slugs (default: env PAPER_CITIES or all)",
    )
    auto_settle.set_defaults(func=cmd_paper_auto_settle)

def register_backtest_commands(sub) -> None:
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
