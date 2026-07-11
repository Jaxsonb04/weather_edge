"""Backtest and model-rescore commands behind the stable CLI facade."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from ..backtest import run_walk_forward_calibration_backtest
from ..backtest_rescore import run_rescore
from ..colors import Color
from ..config import (
    StrategyConfig,
    normalize_risk_profile_name,
    strategy_config_for_profile,
)
from ..db import PaperStore
from ..forecast import SfoForecasterAdapter
from ..synthetic_blend import (
    build_synthetic_blend_calibration,
    write_synthetic_blend_calibration,
)
from .format import _fmt_opt


def _config(args: argparse.Namespace) -> StrategyConfig:
    base = strategy_config_for_profile(getattr(args, "risk_profile", None))
    if args.bankroll is None:
        return base
    return replace(base, paper_bankroll=args.bankroll)


def _risk_profile_name(args: argparse.Namespace) -> str:
    explicit = getattr(args, "risk_profile", None)
    return normalize_risk_profile_name(str(explicit) if explicit else None)


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
    print(f"cache_hit: {str(result.cache_hit).lower()}")
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
    settlements = adapter.load_cli_settlement_truth()
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
    settlements = adapter.load_cli_settlement_truth()
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
