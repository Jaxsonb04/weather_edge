from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ._util import (
    _json_list,
    _json_object,
    _parse_timestamp,
    _row_value as _sqlite_row_value,
    _table_exists,
)
from .backtest import run_walk_forward_calibration_backtest
from .backtest_rescore import compute_real_money_readiness, run_rescore
from .cities import CITIES
from .config import (
    DEFAULT_DB_PATH,
    DEFAULT_FORECASTER_ROOT,
    SFO_TZ,
    StrategyConfig,
    normalize_risk_profile_name,
    strategy_config_for_profile,
)
from .db import PaperStore
from .dataset_research import (
    DEFAULT_MIN_AFTER_COST_TRADES,
    DEFAULT_MIN_MATCHED_ROWS,
    build_dataset_research as build_dataset_research_payload,
)
from .exits import (
    DEFAULT_NO_STOP_LOSS_PCT,
    DEFAULT_NO_TAKE_PROFIT_PCT,
    DEFAULT_RESEARCH_NO_SETTLEMENT_FIRST_MIN_COST,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
    DEFAULT_YES_STOP_LOSS_PCT,
    DEFAULT_YES_TAKE_PROFIT_PCT,
    convergence_take_profit_net,
    decide_exit,
    exit_bid_for_net,
)
from .fees import quadratic_fee_average_per_contract
from .forecast import ForecastDataError, SfoForecasterAdapter
from .forecast_scorecards import build_forecast_scorecards
from .live_execution import LiveExecutionPolicy, readiness_status_from_checks
from .research_shadow import build_research_shadow_report
from .replay import replay_from_database
from .settlement_day import settlement_today
from .settlement_truth import is_pre_resolution_decision as _is_strategy_pre_resolution
from .summary import build_paper_summary
from .synthetic_blend import build_synthetic_blend_calibration


ACTIVE_CALIBRATION_SOURCE = "lstm"
_sqlite_table_exists = _table_exists
CHALLENGER_CALIBRATION_SOURCE = "clean-blend/combined"
MIN_CLEAN_WINNER_SAMPLE = 60
# Exit-threshold percentage defaults are owned by exits.py (the single source
# shared with the live monitor); imported above.
DEFAULT_MODEL_VETO_MAX_LOSS_PCT = 60.0
DEFAULT_MODEL_VETO_BUFFER = 0.08
PRIMARY_PROFILE = "live"
EXPERIMENTAL_PROFILES = {"research"}
FORECAST_HEALTH_ROLLING_DAYS = 3
FORECAST_HEALTH_MIN_NWP_MODELS = 6
FORECAST_HEALTH_MAX_NWP_AGE = timedelta(hours=36)
FORECAST_HEALTH_MAX_EMOS_AGE = timedelta(hours=6)
FORECAST_HEALTH_MAX_CLISFO_LAG_DAYS = 2
FORECAST_LEAD_MODE_LABELS = {
    "day_ahead": "Day-ahead forecast",
    "same_day_prelock": "Same-day pre-lock forecast",
    "intraday_high_so_far": "Intraday observed-high edge",
    "post_resolution_excluded": "Post-resolution excluded",
    "unknown": "Unknown lead mode",
}


def build_strategy_research(
    *,
    forecaster_root: Path = DEFAULT_FORECASTER_ROOT,
    db_path: Path = DEFAULT_DB_PATH,
    config: StrategyConfig | None = None,
    calibration_min_train: int = 180,
) -> dict[str, Any]:
    """Build the public Strategy Lab artifact from existing runtime state.

    This is diagnostic-only. It reads the AWS-side forecast archive, public
    trading signal, and paper-trading database when present; it does not place,
    close, or settle paper orders.
    """

    forecaster_root = Path(forecaster_root)
    db_path = Path(db_path)
    cfg = config or strategy_config_for_profile(None)
    adapter = SfoForecasterAdapter(forecaster_root)
    trading_signal = _load_json_optional(forecaster_root / "trading_signal.json")
    dataset_research = _load_or_build_dataset_research(
        forecaster_root=forecaster_root,
        db_path=db_path,
    )
    settlements: dict[object, float] = {}
    sampled_decision_rows: list[sqlite3.Row] = []
    try:
        settlements = adapter.load_cli_settlement_truth()
    except (ForecastDataError, FileNotFoundError, KeyError, ValueError, sqlite3.Error):
        pass
    if db_path.exists() and _db_table_exists(db_path, "decision_snapshots"):
        try:
            sampled_decision_rows = PaperStore(db_path, init=False).sampled_decision_rows(
                sample_mode="entry-per-market-side"
            )
        except sqlite3.Error:
            pass

    active_calibration = _calibration_payload(
        adapter,
        source="lstm",
        label="lstm",
        role="Active execution calibration",
        min_train=calibration_min_train,
    )
    challenger_calibration = _calibration_payload(
        adapter,
        source="clean-blend",
        label=CHALLENGER_CALIBRATION_SOURCE,
        role="Challenger research calibration",
        min_train=calibration_min_train,
    )
    comparison = _comparison_summary(active_calibration, challenger_calibration)
    prediction_replay = _prediction_replay_payload(forecaster_root, cfg)
    backtest = _signal_backtest_payload(
        adapter,
        db_path,
        settlements=settlements,
        sampled_rows=sampled_decision_rows,
    )
    config_rescore = _config_rescore_payload(
        adapter,
        db_path,
        settlements=settlements,
        sampled_rows=sampled_decision_rows,
    )
    chronological_replay = replay_from_database(
        db_path,
        settlements,
        initial_capital=cfg.paper_bankroll,
    )
    real_money_readiness = _real_money_readiness_payload(
        config_rescore, active_calibration, chronological_replay
    )
    live_frequency_tuning = _live_frequency_tuning_payload(config_rescore, strategy_config_for_profile("live"))
    research_shadow = _research_shadow_payload(adapter, db_path, settlements=settlements)
    signal_quality = _signal_quality_payload(db_path, trading_signal)
    paper = _paper_payload(db_path)
    forecast_health = _forecast_health_payload(forecaster_root, config=cfg)
    forecast_scorecards = build_forecast_scorecards(forecaster_root / "weather.db")
    daily_summary = _daily_summary_payload(
        db_path=db_path,
        forecaster_root=forecaster_root,
        config=cfg,
    )
    profiles = _profile_views(
        daily_summary=daily_summary,
        paper=paper,
        signal_quality=signal_quality,
    )
    status = _status_payload(
        config=cfg,
        db_path=db_path,
        trading_signal=trading_signal,
        backtest=backtest,
        signal_quality=signal_quality,
        paper=paper,
        forecast_health=forecast_health,
    )
    accounting = _accounting_payload(daily_summary, paper, db_path=db_path)

    return {
        "schema_version": 1,
        "available": True,
        "mode": "paper_research_only",
        "live_orders_enabled": False,
        "default_profile": _default_profile(profiles),
        "generated_at": datetime.now(UTC).isoformat(),
        "source_of_truth": "AWS EC2 runtime artifacts after sync and refresh",
        "status": status,
        "daily_summary": daily_summary,
        "accounting": accounting,
        "calibration_comparison": {
            "active": active_calibration,
            "challenger": challenger_calibration,
            "comparison": comparison,
        },
        "prediction_replay": prediction_replay,
        "forecast_health": forecast_health,
        "forecast_scorecards": forecast_scorecards,
        "signal_quality": signal_quality,
        "backtest_summary": backtest,
        "config_rescore": config_rescore,
        "chronological_replay": chronological_replay,
        "real_money_readiness": real_money_readiness,
        "live_frequency_tuning": live_frequency_tuning,
        "research_shadow": research_shadow,
        "paper_trading": paper,
        "profiles": profiles,
        "dataset_research": _dataset_research_summary(dataset_research),
        "research_notes": _research_notes(),
        "disclaimer": (
            "Paper-trading research only — no real-money orders are ever placed. "
            "Forecasts use a per-city NWP-ensemble EMOS model (San Francisco adds an LSTM blend)."
        ),
    }


def _daily_summary_payload(
    *,
    db_path: Path,
    forecaster_root: Path,
    config: StrategyConfig,
) -> dict[str, Any]:
    try:
        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            config=config,
            days=7,
        )
    except Exception as exc:  # diagnostics artifact must not fail the refresh
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"available": True, **payload}


def write_strategy_research(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write (temp + os.replace): the strategy-lab-refresh and
    # forecaster-refresh timers build into the same dir, and the publisher copies
    # this file, so a plain truncate-write could be read half-written.
    import os

    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _accounting_payload(
    daily_summary: dict[str, Any],
    paper: dict[str, Any],
    *,
    db_path: Path,
) -> dict[str, Any]:
    """One account-level reconciliation; profiles are attribution only."""

    daily_totals = daily_summary.get("totals") or {}
    paper_summary = paper.get("summary") or {}
    initial_capital = _to_float(
        daily_summary.get("starting_bankroll", daily_summary.get("bankroll")),
        default=1000.0,
    )
    all_time_realized = _to_float(
        daily_totals.get("cumulative_realized_pnl", paper_summary.get("realized_pnl"))
    )
    window_realized = _to_float(daily_totals.get("realized_pnl"))
    realized_equity = initial_capital + all_time_realized
    open_cost_basis = _to_float(paper_summary.get("open_risk"))
    reservations = _to_float(paper_summary.get("pending_limit_risk"))
    shared_state = PaperStore(db_path, init=False).shared_account_state() if db_path.exists() else None
    cash_balance = (
        _to_float(shared_state.get("cash_balance"))
        if shared_state is not None
        else realized_equity - open_cost_basis
    )
    reservations = (
        _to_float(shared_state.get("reservations"))
        if shared_state is not None
        else reservations
    )
    available_cash = (
        _to_float(shared_state.get("available_cash"))
        if shared_state is not None
        else cash_balance - reservations
    )
    open_positions = int(_to_float(paper_summary.get("open_positions")))
    marked_open = int(_to_float(paper_summary.get("marked_open_positions")))
    open_value_raw = paper_summary.get("open_value")
    unrealized_raw = paper_summary.get("unrealized_pnl")
    marks_complete = open_positions == 0 or marked_open == open_positions
    if open_positions == 0:
        mark_coverage = "complete_no_open_positions"
    elif marks_complete:
        mark_coverage = "complete"
    elif marked_open:
        mark_coverage = "partial"
    else:
        mark_coverage = "unavailable"
    unrealized_pnl = _round(unrealized_raw, 2) if marks_complete and unrealized_raw is not None else None
    marked_equity = (
        _round(cash_balance + _to_float(open_value_raw), 2)
        if marks_complete and open_value_raw is not None
        else (_round(realized_equity, 2) if open_positions == 0 else None)
    )
    resolved_capital = _to_float(paper_summary.get("capital_at_risk"))
    profile_attribution = sum(
        _to_float(row.get("realized_pnl")) for row in paper.get("profiles") or []
    )
    final_curve_pnl = (
        _to_float((daily_summary.get("days") or [])[-1].get("cumulative_realized"))
        if daily_summary.get("days")
        else all_time_realized
    )
    reconciled = (
        abs(profile_attribution - all_time_realized) < 0.005
        and abs(final_curve_pnl - all_time_realized) < 0.005
        and available_cash >= -0.005
    )
    return {
        "schema_version": 1,
        "account_id": (
            str(shared_state.get("account_id")) if shared_state is not None else "legacy-paper-account"
        ),
        "accounting_cohort": (
            "shared_account_v2" if shared_state is not None else "legacy_independent_sizing"
        ),
        "initial_capital": _round(initial_capital, 2),
        "all_time_realized_pnl": _round(all_time_realized, 2),
        "window_realized_pnl": _round(window_realized, 2),
        "realized_equity": _round(realized_equity, 2),
        "cash_balance": _round(cash_balance, 2),
        "reservations": _round(reservations, 2),
        "available_cash": _round(available_cash, 2),
        "open_cost_basis": _round(open_cost_basis, 2),
        "unrealized_pnl": unrealized_pnl,
        "marked_equity": marked_equity,
        "mark_coverage": mark_coverage,
        "resolved_capital": _round(resolved_capital, 2),
        "return_on_initial_capital": (
            _round(all_time_realized / initial_capital, 6) if initial_capital > 0 else None
        ),
        "roi_on_resolved_capital": (
            _round(all_time_realized / resolved_capital, 6) if resolved_capital > 0 else None
        ),
        "profile_attributed_pnl": _round(profile_attribution, 2),
        "reconciliation_status": "reconciled" if reconciled else "mismatch",
        "reconciliation_difference": _round(profile_attribution - all_time_realized, 2),
    }


def _load_or_build_dataset_research(*, forecaster_root: Path, db_path: Path) -> dict[str, Any] | None:
    published = _load_json_optional(forecaster_root / "dataset_research.json")
    if published:
        return published
    try:
        return build_dataset_research_payload(
            db_path=db_path,
            forecaster_root=forecaster_root,
        )
    except Exception as exc:  # diagnostics artifact must not fail Strategy Lab
        return {
            "available": False,
            "reason": f"dataset research unavailable: {type(exc).__name__}: {exc}",
        }


def _profile_views(
    *,
    daily_summary: dict[str, Any],
    paper: dict[str, Any],
    signal_quality: dict[str, Any],
) -> list[dict[str, Any]]:
    names = _profile_names(daily_summary, paper, signal_quality)
    return [
        _profile_view(
            name,
            daily_summary=daily_summary,
            paper=paper,
            signal_quality=signal_quality,
        )
        for name in names
    ]


def _profile_names(
    daily_summary: dict[str, Any],
    paper: dict[str, Any],
    signal_quality: dict[str, Any],
) -> list[str]:
    names: set[str] = set()
    for row in daily_summary.get("profiles") or []:
        names.add(_profile_key(row.get("risk_profile")))
    for row in paper.get("profiles") or []:
        names.add(_profile_key(row.get("risk_profile")))
    for bucket in ("open_positions", "closed_positions", "recent_monitor_actions"):
        for row in paper.get(bucket) or []:
            names.add(_profile_key(row.get("risk_profile")))
    for row in paper.get("pending_limit_orders") or []:
        names.add(_profile_key(row.get("risk_profile")))
    for name in (signal_quality.get("latest_candidates_by_profile") or {}):
        names.add(_profile_key(name))
    for row in signal_quality.get("latest_candidates") or []:
        names.add(_profile_key(row.get("risk_profile")))
    names.discard("unknown")
    return sorted(names, key=_profile_sort_key)


def _profile_view(
    name: str,
    *,
    daily_summary: dict[str, Any],
    paper: dict[str, Any],
    signal_quality: dict[str, Any],
) -> dict[str, Any]:
    profile_daily = _profile_daily_summary(daily_summary, paper, name)
    profile_paper = _profile_paper_payload(paper, name)
    profile_signal = _profile_signal_quality(signal_quality, name)
    learnings = _profile_learnings(
        name,
        daily_summary=profile_daily,
        paper=profile_paper,
        signal_quality=profile_signal,
    )
    recommendations = _profile_recommendations(name, profile_daily)
    profile_daily["learnings"] = learnings
    profile_daily["recommended_changes"] = recommendations
    return {
        "risk_profile": name,
        "label": _profile_label(name),
        "profile_type": "experimental" if name in EXPERIMENTAL_PROFILES else "primary",
        "daily_summary": profile_daily,
        "signal_quality": profile_signal,
        "paper_trading": profile_paper,
        "learnings": learnings,
        "recommended_changes": recommendations,
        "status": _profile_status(name, profile_daily, profile_paper, profile_signal),
    }


def _profile_daily_summary(
    daily_summary: dict[str, Any],
    paper: dict[str, Any],
    name: str,
) -> dict[str, Any]:
    profile_total = _profile_row(daily_summary.get("profiles") or [], name)
    paper_total = _profile_row(paper.get("profiles") or [], name)
    window_pnl = _to_float(profile_total.get("realized_pnl"))
    all_time_pnl = _to_float(paper_total.get("realized_pnl", window_pnl))
    cumulative = all_time_pnl - window_pnl
    days = []
    for row in daily_summary.get("days") or []:
        profile = ((row.get("profiles") or {}).get(name) or {})
        realized = _to_float(profile.get("realized_pnl"))
        opening_attribution = cumulative
        cumulative += realized
        days.append(
            {
                "date": row.get("date"),
                "opened": int(_to_float(profile.get("opened"))),
                "closed": int(_to_float(profile.get("closed"))),
                "settled": int(_to_float(profile.get("settled"))),
                "resolved": int(_to_float(profile.get("resolved"))),
                "wins": int(_to_float(profile.get("wins"))),
                "losses": int(_to_float(profile.get("losses"))),
                "hit_rate": profile.get("hit_rate"),
                "realized_pnl": _round(realized, 2),
                "opening_attributed_pnl": _round(opening_attribution, 2),
                "cumulative_realized": _round(cumulative, 2),
                "closing_attributed_pnl": _round(cumulative, 2),
                "opened_spend": _round(profile.get("opened_spend"), 2),
                "resolved_spend": _round(profile.get("resolved_spend"), 2),
                "roi": profile.get("roi"),
                "signals": int(_to_float(profile.get("signals"))),
                "approved_signals": int(_to_float(profile.get("approved_signals"))),
                "forecast_predicted_high_f": row.get("forecast_predicted_high_f"),
                "forecast_actual_high_f": row.get("forecast_actual_high_f"),
                "forecast_error_f": row.get("forecast_error_f"),
            }
        )
    resolved = int(_to_float(profile_total.get("resolved")))
    wins = int(_to_float(profile_total.get("wins")))
    losses = int(_to_float(profile_total.get("losses")))
    opened = sum(int(row["opened"]) for row in days)
    closed = sum(int(row["closed"]) for row in days)
    settled = sum(int(row["settled"]) for row in days)
    resolved_capital = _to_float(profile_total.get("capital_resolved"))
    totals = {
        "trades_opened": opened,
        "trades_closed": closed,
        "trades_settled": settled,
        "open_positions": int(_to_float(paper_total.get("open_positions"))),
        "open_risk": _round(paper_total.get("open_risk"), 2),
        "realized_pnl": _round(window_pnl, 2),
        "cumulative_realized_pnl": _round(all_time_pnl, 2),
        "all_time_attributed_pnl": _round(all_time_pnl, 2),
        "window_attributed_pnl": _round(window_pnl, 2),
        "capital_resolved": _round(resolved_capital, 2),
        "roi": _round(window_pnl / resolved_capital, 4) if resolved_capital > 0 else None,
        "wins": wins,
        "losses": losses,
        "hit_rate": _round(wins / (wins + losses), 4) if (wins + losses) else None,
        "mean_abs_forecast_error_f": (daily_summary.get("totals") or {}).get(
            "mean_abs_forecast_error_f"
        ),
    }
    return {
        "available": bool(daily_summary.get("available", True)),
        "schema_version": daily_summary.get("schema_version"),
        "generated_at": daily_summary.get("generated_at"),
        "window_days": daily_summary.get("window_days"),
        "window_start": daily_summary.get("window_start"),
        "window_end": daily_summary.get("window_end"),
        "bankroll": daily_summary.get("bankroll"),
        # Profiles contribute P&L to one shared account. They do not each own a
        # separate $1,000 bankroll or equity curve.
        "opening_attributed_pnl": _round(all_time_pnl - window_pnl, 2),
        "current_attributed_pnl": _round(all_time_pnl, 2),
        # Profile-scoped YES/NO split and exit-reason mix so these cards render on
        # a profile tab, not just the All-profiles overview (the template reads
        # these field names directly).
        "side_performance": (daily_summary.get("side_performance_by_profile") or {}).get(name)
        or {},
        "exit_reasons": (daily_summary.get("exit_reasons_by_profile") or {}).get(name) or {},
        "risk_profile": name,
        "days": days,
        "totals": totals,
        "profiles": [profile_total] if profile_total else [],
        "gate_behavior": _profile_gate_behavior(daily_summary, name),
        "model_vs_market": daily_summary.get("model_vs_market") or {},
        "data_collected": daily_summary.get("data_collected") or {},
        "biggest_winners": [
            row
            for row in daily_summary.get("biggest_winners") or []
            if _profile_key(row.get("risk_profile")) == name
        ],
        "biggest_losers": [
            row
            for row in daily_summary.get("biggest_losers") or []
            if _profile_key(row.get("risk_profile")) == name
        ],
    }


def _profile_paper_payload(paper: dict[str, Any], name: str) -> dict[str, Any]:
    open_rows = [
        row
        for row in paper.get("open_positions") or []
        if _profile_key(row.get("risk_profile")) == name
    ]
    pending_limit_rows = [
        row
        for row in paper.get("pending_limit_orders") or []
        if _profile_key(row.get("risk_profile")) == name
    ]
    closed_rows = [
        row
        for row in paper.get("closed_positions") or []
        if _profile_key(row.get("risk_profile")) == name
    ]
    action_rows = [
        row
        for row in paper.get("recent_monitor_actions") or []
        if _profile_key(row.get("risk_profile")) == name
    ]
    monitor_action_rows = [
        row
        for row in action_rows
        if row.get("status") not in {"OPEN", "LIMIT_RESTING"}
    ]
    profile = _profile_row(paper.get("profiles") or [], name)
    duplicate_rows = [
        row
        for row in paper.get("duplicate_open_groups") or []
        if _profile_key(row.get("risk_profile")) == name
    ]
    open_positions = int(_to_float(profile.get("open_positions")))
    pending_limit_count = int(_to_float(profile.get("pending_limit_orders")))
    marked_open = [row for row in open_rows if row.get("unrealized_pnl") is not None]
    unrealized_pnl = (
        _round(sum(_to_float(row.get("unrealized_pnl")) for row in marked_open), 2)
        if marked_open
        else None
    )
    open_value = (
        _round(sum(_to_float(row.get("current_value")) for row in marked_open), 2)
        if marked_open
        else None
    )
    summary = {
        "open_positions": open_positions,
        "published_open_positions": len(open_rows),
        "hidden_open_positions": max(0, open_positions - len(open_rows)),
        "pending_limit_orders": pending_limit_count,
        "published_pending_limit_orders": len(pending_limit_rows),
        "hidden_pending_limit_orders": max(0, pending_limit_count - len(pending_limit_rows)),
        "pending_limit_risk": _round(profile.get("pending_limit_risk"), 2),
        "duplicate_open_groups": len(duplicate_rows),
        "largest_duplicate_open_group": max(
            [row["open_orders"] for row in duplicate_rows],
            default=0,
        ),
        "unresolved_past_targets": [],
        "latest_opened_at": open_rows[0].get("created_at") if open_rows else None,
        "latest_monitor_action_at": (
            monitor_action_rows[0].get("time") if monitor_action_rows else None
        ),
        "closed_positions": int(_to_float(profile.get("orders"))),
        "realized_pnl": _round(profile.get("realized_pnl"), 2),
        "unrealized_pnl": unrealized_pnl,
        "marked_open_positions": len(marked_open),
        "open_risk": _round(profile.get("open_risk"), 2),
        "open_value": open_value,
        "capital_at_risk": _round(profile.get("capital_resolved"), 2),
        "roi": profile.get("roi"),
        "hit_rate": profile.get("hit_rate"),
        "win_count": int(_to_float(profile.get("wins"))),
        "loss_count": int(_to_float(profile.get("losses"))),
    }
    return {
        "available": bool(paper.get("available")),
        "monitor": paper.get("monitor") or {},
        "summary": summary,
        "open_positions": open_rows,
        "pending_limit_orders": pending_limit_rows,
        "closed_positions": closed_rows,
        "recent_monitor_actions": action_rows,
        "duplicate_open_groups": duplicate_rows,
        "profiles": [profile] if profile else [],
    }


def _profile_signal_quality(signal_quality: dict[str, Any], name: str) -> dict[str, Any]:
    by_profile = signal_quality.get("latest_candidates_by_profile") or {}
    rows = by_profile.get(name)
    if rows is None:
        rows = [
            row
            for row in signal_quality.get("latest_candidates") or []
            if _profile_key(row.get("risk_profile")) == name
        ]
    return {
        "available": bool(rows),
        "source": signal_quality.get("source"),
        "latest_candidates": rows,
        # The Kalshi market consensus is the same ladder for every paper profile
        # (it is the crowd's view of the settlement high, not a per-book metric),
        # so pass the parent block straight through to each profile view.
        "market_consensus": signal_quality.get("market_consensus") or {"available": False},
        "charts": {
            "probability_vs_market": _probability_market_points(rows),
            "edge_by_market_bucket": _edge_by_market_bucket(rows),
            "quality_distribution": _quality_distribution(rows),
        },
    }


def _profile_gate_behavior(daily_summary: dict[str, Any], name: str) -> dict[str, Any]:
    gate = daily_summary.get("gate_behavior") or {}
    row = _profile_row(gate.get("by_profile") or [], name)
    return {
        "approved": int(_to_float(row.get("approved"))),
        "rejected": max(0, int(_to_float(row.get("signals"))) - int(_to_float(row.get("approved")))),
        "top_rejections": row.get("top_rejections") or [],
        "top_rejections_all": row.get("top_rejections_all") or [],
        "rejection_categories": row.get("rejection_categories") or {},
        "entry_block_reasons": row.get("entry_block_reasons") or [],
        "by_profile": [row] if row else [],
    }


def _profile_learnings(
    name: str,
    *,
    daily_summary: dict[str, Any],
    paper: dict[str, Any],
    signal_quality: dict[str, Any],
) -> list[str]:
    totals = daily_summary.get("totals") or {}
    resolved = int(_to_float(totals.get("wins"))) + int(_to_float(totals.get("losses")))
    notes: list[str] = []
    if resolved:
        notes.append(
            f"{name} resolved {resolved} trade(s): "
            f"{int(_to_float(totals.get('wins')))}W / "
            f"{int(_to_float(totals.get('losses')))}L, "
            f"net ${_to_float(totals.get('realized_pnl')):+.2f}."
        )
    else:
        notes.append(f"{name} has no resolved trades in this window yet.")
    open_risk = _to_float((paper.get("summary") or {}).get("open_risk"))
    if open_risk:
        notes.append(f"{name} currently has ${open_risk:.2f} of paper open risk.")
    signal_count = len(signal_quality.get("latest_candidates") or [])
    if signal_count:
        notes.append(f"{name} has {signal_count} current signal candidate(s) in the latest artifact.")
    if name in EXPERIMENTAL_PROFILES:
        notes.append(
            f"{name} is experimental paper-data collection; its P&L is isolated from the balanced headline."
        )
    return notes


def _profile_recommendations(name: str, daily_summary: dict[str, Any]) -> list[str]:
    totals = daily_summary.get("totals") or {}
    resolved = int(_to_float(totals.get("wins"))) + int(_to_float(totals.get("losses")))
    roi = totals.get("roi")
    if resolved == 0:
        return [f"Keep collecting {name} scans before changing this profile's gates."]
    if resolved < 15:
        return [
            f"{name} has only {resolved} resolved trade(s); wait for at least 15 before changing gates."
        ]
    if roi is not None and _to_float(roi) < -0.05:
        return [f"{name} ROI is materially negative; inspect losers before loosening this profile."]
    return [f"No rule-based {name} gate change is indicated by this window."]


def _profile_status(
    name: str,
    daily_summary: dict[str, Any],
    paper: dict[str, Any],
    signal_quality: dict[str, Any],
) -> dict[str, Any]:
    totals = daily_summary.get("totals") or {}
    paper_summary = paper.get("summary") or {}
    entry_block_reason = _entry_block_reason(signal_quality.get("latest_candidates") or [])
    alerts = _strategy_alerts(
        paper=paper,
        entry_block_reason=entry_block_reason,
    )
    open_count = int(_to_float(paper_summary.get("open_positions")))
    pending_count = int(_to_float(paper_summary.get("pending_limit_orders")))
    if open_count and pending_count:
        paper_status = (
            f"{open_count} open {name} paper position(s); "
            f"{pending_count} resting limit order(s)"
        )
    elif open_count:
        paper_status = f"{open_count} open {name} paper position(s)"
    elif pending_count:
        paper_status = f"{pending_count} resting limit order(s) for {name}"
    else:
        paper_status = f"no open {name} paper positions"
    return {
        "risk_profile": name,
        "profile_label": _profile_label(name),
        "profile_type": "experimental" if name in EXPERIMENTAL_PROFILES else "primary",
        "paper_trading_status": paper_status,
        "open_risk": _round(paper_summary.get("open_risk"), 2),
        "pending_limit_risk": _round(paper_summary.get("pending_limit_risk"), 2),
        "realized_pnl": _round(totals.get("realized_pnl"), 2),
        "hit_rate": totals.get("hit_rate"),
        "latest_signal_count": len(signal_quality.get("latest_candidates") or []),
        "entry_scanner_reason": entry_block_reason,
        "alerts": alerts,
        "alert_level": _alert_level(alerts),
    }


def _profile_row(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for row in rows:
        if _profile_key(row.get("risk_profile")) == name:
            return dict(row)
    return {
        "risk_profile": name,
        "orders": 0,
        "resolved": 0,
        "wins": 0,
        "losses": 0,
        "hit_rate": None,
        "realized_pnl": 0.0,
        "capital_resolved": 0.0,
        "roi": None,
        "open_positions": 0,
        "open_risk": 0.0,
        "pending_limit_orders": 0,
        "pending_limit_risk": 0.0,
        "signals": 0,
        "approved": 0,
    }


def _default_profile(profiles: list[dict[str, Any]]) -> str:
    names = {row["risk_profile"] for row in profiles}
    if PRIMARY_PROFILE in names:
        return PRIMARY_PROFILE
    return profiles[0]["risk_profile"] if profiles else PRIMARY_PROFILE


def _profile_key(value: object) -> str:
    return str(value or "unknown").strip().lower() or "unknown"


def _profile_sort_key(name: str) -> tuple[int, str]:
    order = {
        "live": 0,
        "research": 1,
        "unknown": 9,
    }
    return order.get(name, 8), name


def _profile_label(name: str) -> str:
    if name == "live":
        return "Live (real-money candidate)"
    if name == "research":
        return "Research (experimental)"
    return name


def _calibration_payload(
    adapter: SfoForecasterAdapter,
    *,
    source: str,
    label: str,
    role: str,
    min_train: int,
) -> dict[str, Any]:
    try:
        outcomes = (
            adapter.load_clean_blend_outcomes()
            if source == "clean-blend"
            else adapter.load_lstm_outcomes()
        )
    except (ForecastDataError, FileNotFoundError, KeyError, ValueError) as exc:
        return {
            "available": False,
            "source": label,
            "role": role,
            "outcome_count": 0,
            "sample_size": 0,
            "minimum_train_rows": min_train,
            "reason": str(exc),
            "buckets": [],
            "cohorts": _empty_cohorts(),
        }

    if len(outcomes) <= min_train:
        return {
            "available": False,
            "source": label,
            "role": role,
            "outcome_count": len(outcomes),
            "sample_size": 0,
            "minimum_train_rows": min_train,
            "reason": (
                f"{label} has {len(outcomes)} outcome rows; needs more than "
                f"{min_train} rows for this walk-forward comparison."
            ),
            "buckets": [],
            "cohorts": _empty_cohorts(),
        }

    try:
        result = run_walk_forward_calibration_backtest(outcomes, min_train=min_train)
    except ValueError as exc:
        return {
            "available": False,
            "source": label,
            "role": role,
            "outcome_count": len(outcomes),
            "sample_size": 0,
            "minimum_train_rows": min_train,
            "reason": str(exc),
            "buckets": [],
            "cohorts": _empty_cohorts(),
        }

    return {
        "available": True,
        "source": label,
        "role": role,
        "outcome_count": len(outcomes),
        "sample_size": result.n,
        "minimum_train_rows": min_train,
        "brier_score": _round(result.brier_score, 4),
        "climatology_brier_score": _round(result.climatology_brier_score, 4),
        "brier_skill": _round(result.brier_skill, 4),
        "ranked_probability_score": _round(result.ranked_probability_score, 4),
        "climatology_ranked_probability_score": _round(
            result.climatology_ranked_probability_score,
            4,
        ),
        "ranked_probability_skill": _round(result.ranked_probability_skill, 4),
        "log_loss": _round(result.log_loss, 4),
        "top_bin_accuracy": _round(result.top_bin_accuracy, 4),
        "avg_winning_probability": _round(result.avg_winning_probability, 4),
        "avg_entropy": _round(result.avg_entropy, 4),
        "cache_hit": result.cache_hit,
        "buckets": [
            {
                "range": f"{bucket.lower:.1f}-{bucket.upper:.1f}",
                "lower": bucket.lower,
                "upper": bucket.upper,
                "count": bucket.count,
                "avg_probability": _round(bucket.avg_probability, 4),
                "observed_frequency": _round(bucket.observed_frequency, 4),
                "calibration_gap": _round(bucket.observed_frequency - bucket.avg_probability, 4),
                "brier_score": _round(bucket.brier_score, 4),
            }
            for bucket in result.calibration_buckets
        ],
        "cohorts": _cohort_rows(result.cohorts),
    }


def _comparison_summary(active: dict[str, Any], challenger: dict[str, Any]) -> dict[str, Any]:
    recommendation = "Keep LSTM as the live model for now."
    if not active.get("available"):
        return {
            "winner": "not_available",
            "label": "Active calibration data unavailable",
            "recommendation": recommendation,
        }
    if not challenger.get("available"):
        return {
            "winner": "not_enough_clean_data",
            "label": "Not enough clean challenger data yet",
            "recommendation": recommendation,
        }
    if float(challenger.get("sample_size") or 0) < MIN_CLEAN_WINNER_SAMPLE:
        return {
            "winner": "not_enough_clean_data",
            "label": "Challenger sample is still too small",
            "recommendation": recommendation,
        }

    challenger_brier = float(challenger["brier_score"])
    active_brier = float(active["brier_score"])
    challenger_log = float(challenger["log_loss"])
    active_log = float(active["log_loss"])
    if challenger_brier < active_brier and challenger_log <= active_log:
        return {
            "winner": "challenger",
            "label": "Challenger leads on clean metrics",
            "recommendation": (
                "Do not switch automatically. Review point-in-time signal "
                "quality, paper PnL, and tail calibration before changing AWS."
            ),
        }
    if active_brier <= challenger_brier and active_log <= challenger_log:
        return {
            "winner": "active",
            "label": "Active lstm still leads",
            "recommendation": recommendation,
        }
    return {
        "winner": "mixed",
        "label": "Metrics are mixed",
        "recommendation": recommendation,
    }


def _prediction_replay_payload(forecaster_root: Path, config: StrategyConfig) -> dict[str, Any]:
    """Run the tracked historical LSTM/XGBoost replay as a Strategy Lab diagnostic."""

    ab_test_path = Path(forecaster_root) / "ab_test_results.json"
    if not ab_test_path.exists():
        return {
            "available": False,
            "reason": f"Historical replay input not found: {ab_test_path}",
        }
    try:
        payload = build_synthetic_blend_calibration(
            ab_test_path,
            config=config,
        )
    except (KeyError, ValueError, OSError, json.JSONDecodeError) as exc:
        return {
            "available": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    return {
        "available": True,
        "source": payload.get("source"),
        "mode": payload.get("mode"),
        "status": payload.get("status"),
        "configuration": payload.get("configuration") or {},
        "summary": payload.get("summary") or {},
        "models": payload.get("models") or {},
        "ridge_alpha_sweep": (payload.get("ridge_alpha_sweep") or [])[:8],
        "recent_predictions": payload.get("recent_predictions") or [],
        "warnings": payload.get("warnings") or [],
    }


def _config_rescore_payload(
    adapter: SfoForecasterAdapter,
    db_path: Path,
    *,
    settlements: dict[object, float] | None = None,
    sampled_rows: list[sqlite3.Row] | None = None,
) -> dict[str, Any]:
    """Re-score recorded decision snapshots under each scanning profile's CURRENT
    config and settle vs the official integer KSFO highs.

    This is the counterfactual the validation report needed and the signal
    backtest cannot give: signal_backtest replays the OLD config's recorded
    approve/size flags, while this re-runs every gate + Kelly sizing from scratch
    under today's StrategyConfig (see backtest_rescore.run_rescore). Diagnostic
    only -- never places, closes, or settles orders. Keyed by profile so the card
    can show the rescore for the active profile tab.
    """

    empty = {"available": False, "by_profile": {}, "settlement_days": 0, "sampled_snapshots": 0}
    if not db_path.exists():
        return {**empty, "reason": f"Paper-trading DB not found: {db_path}"}
    if not _db_table_exists(db_path, "decision_snapshots"):
        return {**empty, "reason": "decision_snapshots table is not available yet."}
    try:
        if settlements is None:
            settlements = adapter.load_cli_settlement_truth()
        store = PaperStore(db_path, init=False)
        rows = (
            sampled_rows
            if sampled_rows is not None
            else store.sampled_decision_rows(sample_mode="entry-per-market-side")
        )
        by_profile: dict[str, Any] = {}
        for name in ("live", "research"):
            cfg = strategy_config_for_profile(name)
            result = run_rescore(
                rows,
                settlements,
                cfg,
                bankroll=cfg.paper_bankroll,
                bootstrap_samples=1000,
            )
            # Drop the per-day list from the published artifact: the card shows
            # rollups (candidate vs recorded, per-side, per-cohort, CI), and
            # three profiles' worth of daily rows would bloat the JSON.
            result.pop("per_day", None)
            by_profile[name] = result
        return {
            "available": True,
            "settlement_days": len(settlements),
            "sampled_snapshots": len(rows),
            "by_profile": by_profile,
        }
    except Exception as exc:  # diagnostics artifact must not fail the refresh
        return {**empty, "reason": f"{type(exc).__name__}: {exc}"}


def _research_shadow_payload(
    adapter: SfoForecasterAdapter,
    db_path: Path,
    *,
    settlements: dict[object, float] | None = None,
) -> dict[str, Any]:
    empty = {
        "available": False,
        "summary": {},
        "paper_executed": {},
        "shadow_hold_to_settlement": {},
        "shadow_current_exit_policy": {},
    }
    if not db_path.exists():
        return {**empty, "reason": f"Paper-trading DB not found: {db_path}"}
    try:
        if settlements is None:
            settlements = adapter.load_cli_settlement_truth()
        store = PaperStore(db_path, init=False)
        return build_research_shadow_report(store, settlements=settlements)
    except Exception as exc:  # diagnostics artifact must not fail Strategy Lab
        return {**empty, "reason": f"{type(exc).__name__}: {exc}"}


def _real_money_readiness_payload(
    config_rescore: dict[str, Any],
    active_calibration: dict[str, Any],
    chronological_replay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Single go/no-go gauge for promoting the LIVE profile to real money.

    Collapses the live walk-forward rescore plus the walk-forward per-cohort
    Brier and worst calibration-bucket gap into a readiness percentage and a
    per-check breakdown. Judges ONLY the live (real-money-intent) profile; the
    research collector is never promoted. Diagnostic only.
    """

    if not config_rescore.get("available"):
        return {
            "available": False,
            "status": "NOT_READY",
            "status_reasons": [config_rescore.get("reason", "config rescore unavailable")],
            "reason": config_rescore.get("reason", "config rescore unavailable"),
        }
    live_rescore = dict((config_rescore.get("by_profile") or {}).get("live") or {})
    if not live_rescore:
        return {
            "available": False,
            "status": "NOT_READY",
            "status_reasons": ["no live-profile rescore available"],
            "reason": "no live-profile rescore available",
        }

    cohort_brier = {
        row.get("name"): row.get("brier_score")
        for row in (active_calibration.get("cohorts") or [])
        if row.get("name") and row.get("brier_score") is not None
    }
    # Brier Skill Score per cohort (model vs climatology). The readiness gate
    # judges SKILL (> 0 = beats the no-skill prior), not absolute Brier, because
    # a flat absolute bar is unachievable on interior 2F bins by any calibrated
    # model and would refuse real money for the wrong reason.
    cohort_brier_skill = {
        row.get("name"): row.get("brier_skill")
        for row in (active_calibration.get("cohorts") or [])
        if row.get("name") and row.get("brier_skill") is not None
    }
    gaps = [
        abs(bucket["calibration_gap"])
        for bucket in (active_calibration.get("buckets") or [])
        if bucket.get("calibration_gap") is not None
    ]
    max_gap = max(gaps) if gaps else None

    replay = chronological_replay or {}
    live_rescore["evidence_kind"] = replay.get(
        "evidence_kind", live_rescore.get("evidence_kind")
    )
    live_rescore["promotion_eligible"] = bool(replay.get("promotion_eligible"))
    live_rescore["promotion_block_reason"] = "; ".join(
        str(reason) for reason in replay.get("promotion_block_reasons") or []
    )
    readiness = compute_real_money_readiness(
        live_rescore,
        calibration_cohort_brier=cohort_brier or None,
        calibration_cohort_brier_skill=cohort_brier_skill or None,
        max_abs_calibration_gap=max_gap,
    )
    policy = LiveExecutionPolicy.from_env()
    pilot_pnl_value = _env_float("SFO_LIVE_REALIZED_PILOT_PNL")
    pilot_pnl = pilot_pnl_value if pilot_pnl_value is not None else 0.0
    failed = [
        str(check.get("label") or check.get("name"))
        for check in readiness.get("checks", [])
        if not check.get("passed")
    ]
    operational = readiness_status_from_checks(
        evidence_passed=bool(readiness.get("ready")),
        software_passed=True,
        paper_ready=True,
        pilot_loss_remaining=policy.pilot_max_loss + pilot_pnl,
        failing_checks=failed,
    )
    return {
        "available": True,
        "profile": "live",
        **readiness,
        "status": (
            "REPLAY_REQUIRED"
            if readiness.get("status") == "REPLAY_REQUIRED"
            else operational.status
        ),
        "status_reasons": operational.failing_checks,
        "pilot_loss_remaining": round(max(0.0, policy.pilot_max_loss + pilot_pnl), 2),
        "live_policy": {
            "enabled": policy.enabled,
            "dry_run": policy.dry_run,
            "pilot_max_loss": policy.pilot_max_loss,
            "daily_loss": policy.daily_loss,
            "per_trade_risk": policy.per_trade_risk,
        },
    }


def _live_frequency_tuning_payload(
    config_rescore: dict[str, Any],
    live_config: StrategyConfig,
) -> dict[str, Any]:
    """Guarded frequency report for the live real-money-candidate profile.

    This does not loosen gates. It reports whether the current live config is
    producing the desired paper frequency and explicitly publishes the guardrails
    that a future retune must preserve.
    """

    target_min = 2.0
    target_max = 3.0
    guardrails = {
        "min_edge_lcb": _round(live_config.min_edge_lcb, 4),
        "blocked_forecast_cohorts": list(live_config.blocked_forecast_cohorts),
        "paper_pause_enabled": True,
        "max_source_spread_f": _round(live_config.max_source_spread_f, 2),
    }
    if not config_rescore.get("available"):
        return {
            "available": False,
            "status": "RESCORE_UNAVAILABLE",
            "reason": config_rescore.get("reason", "config rescore unavailable"),
            "target_trades_per_day": [target_min, target_max],
            "safe_config_change": None,
            "guardrails": guardrails,
        }
    live = (config_rescore.get("by_profile") or {}).get("live") or {}
    counts = live.get("counts") or {}
    approved = int(_to_float(counts.get("approved_under_candidate_config")))
    days = int(_to_float(counts.get("independent_days")))
    considered = int(_to_float(counts.get("considered")))
    approved_per_day = approved / days if days > 0 else 0.0
    if days <= 0 or approved < 30:
        status = "BELOW_TARGET_COLLECT_ONLY"
        recommendation = (
            "Current live gates do not yet produce enough settled, independent "
            "evidence to tune toward 2-3 paper entries/day without weakening "
            "the lower-bound edge or pause guardrails."
        )
    elif target_min <= approved_per_day <= target_max:
        status = "ON_TARGET"
        recommendation = "Current live gates are within the target paper frequency band."
    elif approved_per_day < target_min:
        status = "BELOW_TARGET_COLLECT_ONLY"
        recommendation = (
            "Live frequency is below target; inspect profile-scoped rejection "
            "diagnostics before considering any guarded retune."
        )
    else:
        status = "ABOVE_TARGET_REVIEW_RISK"
        recommendation = (
            "Live frequency is above target; review drawdown and exposure before "
            "raising any limits."
        )
    return {
        "available": True,
        "status": status,
        "target_trades_per_day": [target_min, target_max],
        "approved_under_current_live_config": approved,
        "considered_snapshots": considered,
        "independent_days": days,
        "approved_per_independent_day": _round(approved_per_day, 4),
        "safe_config_change": None,
        "guardrails": guardrails,
        "recommendation": recommendation,
    }


def _signal_backtest_payload(
    adapter: SfoForecasterAdapter,
    db_path: Path,
    *,
    settlements: dict[object, float] | None = None,
    sampled_rows: list[sqlite3.Row] | None = None,
) -> dict[str, Any]:
    empty = {
        "available": False,
        "sample_mode": "latest-per-market-side",
        "pre_resolution_only": True,
        "counts": {
            "raw_signals": 0,
            "pre_resolution_signals": 0,
            "deduped_signals": 0,
            "excluded_post_resolution_signals": 0,
            "settled_signals": 0,
            "approved_signals": 0,
            "approved_raw_signals": 0,
            "approved_pre_resolution_signals": 0,
        },
        "metrics_available": False,
        "metrics": {},
        "quality_buckets": [],
    }
    if not db_path.exists():
        return {**empty, "reason": f"Paper-trading DB not found: {db_path}"}
    if not _db_table_exists(db_path, "decision_snapshots"):
        return {**empty, "reason": "decision_snapshots table is not available yet."}

    store = PaperStore(db_path, init=False)
    if settlements is None:
        settlements = adapter.load_cli_settlement_truth()
    # Score the FIRST approved snapshot per market-side (the actual entry), not
    # the latest pre-close scan. The latest row has decayed to ~0 edge and is
    # almost never the approved one, so latest-per-market-side discarded 100% of
    # approved entries and published approved_signals=0 / approved PnL/ROI=0 -- a
    # dedupe artifact that flatly contradicted the real paper book sitting beside
    # it. Entry mode makes the approved metrics reflect trades actually taken.
    summary = store.signal_backtest_summary(
        settlements,
        pre_resolution_only=True,
        sample_mode="entry-per-market-side",
        sampled_rows=sampled_rows,
    )
    counts = {
        "raw_signals": int(summary["raw_signals"]),
        "pre_resolution_signals": int(summary["pre_resolution_signals"]),
        "deduped_signals": int(summary["signals"]),
        "excluded_post_resolution_signals": int(summary["excluded_post_resolution_signals"]),
        "settled_signals": int(summary["settled_signals"]),
        "approved_signals": int(summary["approved_signals"]),
        "approved_raw_signals": int(summary.get("approved_raw_signals", summary["approved_signals"])),
        "approved_pre_resolution_signals": int(
            summary.get("approved_pre_resolution_signals", summary["approved_signals"])
        ),
    }
    metrics_available = counts["settled_signals"] > 0
    metric = _round if metrics_available else _null_metric
    return {
        "available": counts["raw_signals"] > 0,
        "metrics_available": metrics_available,
        "sample_mode": summary["sample_mode"],
        "pre_resolution_only": bool(summary["pre_resolution_only"]),
        "dedupe_explanation": (
            "Repeated 15-minute AWS scans are counted once per target, market, "
            "and side, using the first approved (entry) snapshot so approved "
            "metrics reflect trades actually taken, not the decayed last scan."
        ),
        "counts": counts,
        "metrics": {
            "approval_rate": _round(summary["approval_rate"], 4),
            "brier_score": metric(summary["brier_score"], 4),
            "log_loss": metric(summary["log_loss"], 4),
            "hit_rate": metric(summary["win_rate"], 4),
            "avg_probability": metric(summary["avg_probability"], 4),
            "avg_edge": metric(summary["avg_edge"], 4),
            "avg_edge_lcb": metric(summary["avg_edge_lcb"], 4),
            "avg_quality": metric(summary["avg_quality"], 1),
            "approved_paper_pnl": metric(summary["approved_paper_pnl"], 2),
            "approved_capital_at_risk": metric(summary["approved_capital_at_risk"], 2),
            "approved_roi": metric(summary["approved_roi"], 4),
            "approved_hit_rate": metric(summary["approved_hit_rate"], 4),
        },
        "lead_mode_counts": _decision_lead_mode_counts(db_path),
        "quality_buckets": [_round_dict(bucket) for bucket in summary["quality_buckets"]],
    }


def _is_live_candidate(row: dict[str, Any]) -> bool:
    """A candidate is "live" only if it could actually be traded right now.

    A resolved market sits at the price grid's extremes -- the winning side at
    ask ~ $1.00, the losing side at ask ~ $0.00 -- with no tradeable edge either
    way, so counting those as rejected candidates made the live snapshot read
    "0 of 24 approved" when ~8 of the 24 were already-resolved markets that were
    never tradeable: an unfair denominator. Dropping them makes the approval rate
    honest. This is a display/denominator fix only; it does not change which
    trades the scanner takes. See docs/trading_engine_diagnosis_2026-06-16.md
    (Finding 4).

    Both extremes are filtered (not just the $1.00 winner) so the denominator is
    symmetric -- a resolved market where the model's selected side LOST sits at
    ask ~ $0.00 and is just as untradeable. Live Kalshi asks live on the 1c..99c
    grid, so the 0.001 / 0.999 bounds never catch a genuinely live market. An
    ask ceiling is used instead of a wall-clock target-date cut on purpose: a
    date comparison would wrongly empty the candidate list overnight before the
    day's market opens, and _latest_decision_rows already restricts to the most
    recent target dates.
    """
    ask = row.get("ask")
    if isinstance(ask, (int, float)) and (ask >= 0.999 or ask <= 0.001):
        return False
    return True


def _signal_quality_payload(db_path: Path, trading_signal: dict[str, Any] | None) -> dict[str, Any]:
    decisions = _latest_decision_rows(db_path)
    source = "decision_snapshots"
    if not decisions:
        decisions = _decisions_from_trading_signal(trading_signal)
        source = "trading_signal.json"

    pre_filter_count = len(decisions)
    decisions = [row for row in decisions if _is_live_candidate(row)]
    stale_filtered = pre_filter_count - len(decisions)

    latest_target = max(
        (str(row.get("target_date")) for row in decisions if row.get("target_date")),
        default=None,
    )

    decisions.sort(
        key=lambda row: (
            str(row.get("target_date") or ""),
            str(row.get("created_at") or ""),
            bool(row.get("approved")),
            _to_float(row.get("quality_score")),
            _to_float(row.get("edge_lcb")),
            _to_float(row.get("edge")),
        ),
        reverse=True,
    )
    latest_candidates_by_profile = {
        name: rows[:24]
        for name, rows in _candidate_rows_by_profile(decisions).items()
    }
    published_decisions = decisions[:24]
    return {
        "available": bool(published_decisions),
        "source": source,
        "stale_candidates_filtered": stale_filtered,
        "latest_target_date": latest_target,
        "latest_candidates": published_decisions,
        "latest_candidates_by_profile": latest_candidates_by_profile,
        "lead_mode_counts": _lead_mode_counts(decisions),
        "market_consensus": _market_consensus_payload(db_path),
        "charts": {
            "probability_vs_market": _probability_market_points(published_decisions),
            "edge_by_market_bucket": _edge_by_market_bucket(published_decisions),
            "quality_distribution": _quality_distribution(published_decisions),
        },
    }


def _candidate_rows_by_profile(decisions: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_profile: dict[str, list[dict[str, Any]]] = {}
    for row in decisions:
        by_profile.setdefault(_profile_key(row.get("risk_profile")), []).append(row)
    return dict(sorted(by_profile.items(), key=lambda item: _profile_sort_key(item[0])))


def _paper_payload(db_path: Path) -> dict[str, Any]:
    monitor = _paper_monitor_config()
    empty = {
        "available": False,
        "monitor": monitor,
        "summary": {
            "open_positions": 0,
            "published_open_positions": 0,
            "hidden_open_positions": 0,
            "pending_limit_orders": 0,
            "published_pending_limit_orders": 0,
            "hidden_pending_limit_orders": 0,
            "pending_limit_risk": 0.0,
            "duplicate_open_groups": 0,
            "largest_duplicate_open_group": 0,
            "unresolved_past_targets": [],
            "latest_monitor_action_at": None,
            "closed_positions": 0,
            "realized_pnl": 0.0,
            "unrealized_pnl": None,
            "marked_open_positions": 0,
            "open_risk": 0.0,
            "open_value": None,
            "win_count": 0,
            "loss_count": 0,
        },
        "open_positions": [],
        "pending_limit_orders": [],
        "closed_positions": [],
        "recent_monitor_actions": [],
        "diagnostics": _empty_paper_diagnostics(),
        "profiles": [],
    }
    if not db_path.exists():
        return {**empty, "reason": f"Paper-trading DB not found: {db_path}"}
    if not _db_table_exists(db_path, "paper_orders"):
        return {**empty, "reason": "paper_orders table is not available yet."}

    store = PaperStore(db_path, init=False)
    summary = store.market_backtest_summary()
    monitor_marks = _latest_monitor_marks(db_path)
    decision_marks = _latest_position_marks(db_path)
    has_monitor_snapshots = _db_table_exists(db_path, "paper_monitor_snapshots")
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        open_rows = conn.execute(
            """
            SELECT *
            FROM paper_orders
            WHERE status = 'PAPER_FILLED'
              AND settled_at IS NULL
              AND closed_at IS NULL
            ORDER BY created_at DESC
            LIMIT 30
            """
        ).fetchall()
        pending_limit_rows = conn.execute(
            """
            SELECT *
            FROM paper_orders
            WHERE status = 'PAPER_LIMIT_RESTING'
              AND settled_at IS NULL
              AND closed_at IS NULL
            ORDER BY created_at DESC
            LIMIT 30
            """
        ).fetchall()
        pending_limit_summary = conn.execute(
            """
            SELECT COUNT(*) AS pending_orders,
                   COALESCE(SUM(contracts * cost_per_contract), 0.0) AS pending_risk
            FROM paper_orders
            WHERE status = 'PAPER_LIMIT_RESTING'
              AND settled_at IS NULL
              AND closed_at IS NULL
            """
        ).fetchone()
        closed_rows = conn.execute(
            """
            SELECT *
            FROM paper_orders
            WHERE status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
              AND realized_pnl IS NOT NULL
            ORDER BY COALESCE(closed_at, settled_at, created_at) DESC
            LIMIT 30
            """
        ).fetchall()
        closed_action_rows = conn.execute(
            """
            SELECT *
            FROM paper_orders
            WHERE closed_at IS NOT NULL OR settled_at IS NOT NULL
            ORDER BY COALESCE(closed_at, settled_at, created_at) DESC
            LIMIT 12
            """
        ).fetchall()
        monitor_rows = []
        if has_monitor_snapshots:
            # Keep only the LATEST snapshot per order. The monitor writes one row
            # per open order every ~2-minute cycle, so without this dedup a single
            # position that HOLDs for a few cycles shows up as several identical
            # "actions" (the repeated 0.59/0.40/0.11 @ 0.99 the owner saw). The
            # window-function filter collapses those to one inspection mark per
            # order. See docs/trading_engine_diagnosis_2026-06-16.md (Finding 5).
            monitor_rows = conn.execute(
                """
                SELECT m.*, p.label, p.risk_profile
                FROM (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY order_id
                               ORDER BY created_at DESC, id DESC
                           ) AS rn
                    FROM paper_monitor_snapshots
                ) m
                LEFT JOIN paper_orders p ON p.id = m.order_id
                WHERE m.rn = 1
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT 12
                """
            ).fetchall()
        duplicate_rows = conn.execute(
            """
            SELECT target_date,
                   COALESCE(risk_profile, 'live') AS risk_profile,
                   market_ticker,
                   UPPER(COALESCE(side, 'YES')) AS side,
                   COUNT(*) AS open_orders
            FROM paper_orders
            WHERE status = 'PAPER_FILLED'
              AND settled_at IS NULL
              AND closed_at IS NULL
            GROUP BY target_date,
                     COALESCE(risk_profile, 'live'),
                     market_ticker,
                     UPPER(COALESCE(side, 'YES'))
            HAVING COUNT(*) > 1
            ORDER BY open_orders DESC, target_date, risk_profile, market_ticker
            LIMIT 5
            """
        ).fetchall()
        target_rows = conn.execute(
            """
            SELECT target_date, COUNT(*) AS open_orders
            FROM paper_orders
            WHERE status = 'PAPER_FILLED'
              AND settled_at IS NULL
              AND closed_at IS NULL
            GROUP BY target_date
            ORDER BY target_date
            """
        ).fetchall()
        scanning_profiles = []
        if _db_table_exists(db_path, "decision_snapshots"):
            scanning_profiles = [
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT DISTINCT COALESCE(risk_profile, 'unknown')
                    FROM decision_snapshots
                    WHERE created_at >= datetime('now', '-7 days')
                    ORDER BY 1
                    """
                ).fetchall()
            ]
        profile_rows = conn.execute(
            """
            SELECT COALESCE(risk_profile, 'unknown') AS risk_profile,
                   SUM(CASE WHEN status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
                            THEN 1 ELSE 0 END) AS orders,
                   SUM(CASE WHEN status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
                            AND realized_pnl IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
                   SUM(CASE WHEN status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
                            AND realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
                            AND realized_pnl < 0 THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
                            THEN COALESCE(realized_pnl, 0.0) ELSE 0.0 END) AS realized_pnl,
                   SUM(CASE WHEN status = 'PAPER_FILLED'
                             AND settled_at IS NULL
                             AND closed_at IS NULL
                            THEN 1 ELSE 0 END) AS open_positions,
                   SUM(CASE WHEN status = 'PAPER_FILLED'
                             AND settled_at IS NULL
                             AND closed_at IS NULL
                            THEN contracts * cost_per_contract ELSE 0.0 END) AS open_risk,
                   SUM(CASE WHEN status = 'PAPER_LIMIT_RESTING'
                             AND settled_at IS NULL
                             AND closed_at IS NULL
                            THEN 1 ELSE 0 END) AS pending_limit_orders,
                   SUM(CASE WHEN status = 'PAPER_LIMIT_RESTING'
                             AND settled_at IS NULL
                             AND closed_at IS NULL
                            THEN contracts * cost_per_contract ELSE 0.0 END) AS pending_limit_risk,
                   SUM(CASE WHEN status IN ('PAPER_SETTLED', 'PAPER_CLOSED')
                             AND realized_pnl IS NOT NULL
                            THEN contracts * cost_per_contract ELSE 0.0 END) AS capital_resolved
            FROM paper_orders
            WHERE status != 'REJECTED'
            GROUP BY COALESCE(risk_profile, 'unknown')
            ORDER BY risk_profile
            """
        ).fetchall()

    open_positions = [
        _paper_row(row, _position_mark_for(row, monitor_marks, decision_marks), monitor)
        for row in open_rows
    ]
    # Mark resting limit orders with the latest scanned bid/ask too, so the
    # card can show the current market price and how far the ask is from the
    # resting limit. The monitor skips resting orders, so there is no monitor
    # mark; the decision-snapshot mark (recorded every scan) is the live price.
    pending_limit_orders = [
        _paper_row(
            row,
            decision_marks.get((row["market_ticker"], _side_from_row(row))),
            monitor,
        )
        for row in pending_limit_rows
    ]
    closed_positions = [_paper_row(row, None, monitor) for row in closed_rows]
    monitor_action_rows = [_paper_monitor_snapshot_row(row) for row in monitor_rows] + [
        _paper_action_row(row) for row in closed_action_rows
    ]
    monitor_action_rows = sorted(
        monitor_action_rows,
        key=lambda row: str(row.get("time") or ""),
        reverse=True,
    )
    open_action_rows = [_paper_open_action_row(row) for row in open_rows]
    pending_action_rows = [_paper_limit_action_row(row) for row in pending_limit_rows]
    open_reserve = min(4, len(open_action_rows) + len(pending_action_rows))
    action_rows = (
        monitor_action_rows[: max(0, 12 - open_reserve)]
        + pending_action_rows[:open_reserve]
        + open_action_rows[: max(0, open_reserve - len(pending_action_rows))]
    )
    action_rows = sorted(action_rows, key=lambda row: str(row.get("time") or ""), reverse=True)[:12]
    duplicate_groups = [_duplicate_group_row(row) for row in duplicate_rows]
    today = settlement_today()
    unresolved_past_targets = [
        {
            "target_date": str(row["target_date"]),
            "open_orders": int(row["open_orders"]),
        }
        for row in target_rows
        if _date_from_string(row["target_date"]) is not None
        and _date_from_string(row["target_date"]) < today
    ]
    marked_open_positions = [
        row for row in open_positions if row.get("unrealized_pnl") is not None
    ]
    unrealized_pnl = (
        _round(sum(_to_float(row.get("unrealized_pnl")) for row in marked_open_positions), 2)
        if marked_open_positions
        else None
    )
    open_value = (
        _round(sum(_to_float(row.get("current_value")) for row in marked_open_positions), 2)
        if marked_open_positions
        else None
    )
    win_count = int(_to_float(summary.get("wins"), default=0.0))
    loss_count = int(_to_float(summary.get("losses"), default=0.0))
    pending_limit_count = int(pending_limit_summary["pending_orders"] or 0) if pending_limit_summary else 0
    pending_limit_risk = _to_float(pending_limit_summary["pending_risk"]) if pending_limit_summary else 0.0
    return {
        "available": True,
        "monitor": monitor,
        "summary": {
            "open_positions": int(summary["open_orders"]),
            "published_open_positions": len(open_positions),
            "hidden_open_positions": max(0, int(summary["open_orders"]) - len(open_positions)),
            "pending_limit_orders": pending_limit_count,
            "published_pending_limit_orders": len(pending_limit_orders),
            "hidden_pending_limit_orders": max(0, pending_limit_count - len(pending_limit_orders)),
            "pending_limit_risk": _round(pending_limit_risk, 2),
            "duplicate_open_groups": len(duplicate_groups),
            "largest_duplicate_open_group": max(
                [row["open_orders"] for row in duplicate_groups],
                default=0,
            ),
            "unresolved_past_targets": unresolved_past_targets,
            "latest_opened_at": open_rows[0]["created_at"] if open_rows else None,
            "latest_monitor_action_at": (
                monitor_action_rows[0].get("time") if monitor_action_rows else None
            ),
            "closed_positions": int(summary["orders"]),
            "realized_pnl": _round(summary["realized_pnl"], 2),
            "unrealized_pnl": unrealized_pnl,
            "marked_open_positions": len(marked_open_positions),
            "open_risk": _round(summary["open_capital_at_risk"], 2),
            "open_value": open_value,
            "capital_at_risk": _round(summary["capital_at_risk"], 2),
            "roi": _round(summary["roi"], 4),
            "hit_rate": _round(summary["hit_rate"], 4),
            "win_count": win_count,
            "loss_count": loss_count,
        },
        "open_positions": open_positions,
        "pending_limit_orders": pending_limit_orders,
        "closed_positions": closed_positions,
        "recent_monitor_actions": action_rows,
        "duplicate_open_groups": duplicate_groups,
        "diagnostics": _paper_diagnostics(db_path),
        "profiles": _profiles_with_scanners(
            [_profile_summary_row(row) for row in profile_rows],
            scanning_profiles,
        ),
    }


def _paper_diagnostics(db_path: Path) -> dict[str, Any]:
    if not db_path.exists() or not _db_table_exists(db_path, "paper_orders"):
        return _empty_paper_diagnostics()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM paper_orders
            WHERE status != 'REJECTED'
            """
        ).fetchall()

    resolved = [
        row
        for row in rows
        if row["realized_pnl"] is not None and row["status"] != "PAPER_EXPIRED"
    ]
    return {
        "resolved_positions": len(resolved),
        "by_profile": _paper_group_diagnostics(resolved, lambda row: _row_risk_profile(row) or "unknown"),
        "by_side": _paper_group_diagnostics(resolved, _side_from_row),
        "by_exit_reason": _paper_group_diagnostics(resolved, _paper_exit_reason),
        "worst_segments": _worst_paper_segments(resolved),
    }


def _empty_paper_diagnostics() -> dict[str, Any]:
    return {
        "resolved_positions": 0,
        "by_profile": {},
        "by_side": {},
        "by_exit_reason": {},
        "worst_segments": [],
    }


def _paper_group_diagnostics(
    rows: list[sqlite3.Row],
    key_fn,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(str(key_fn(row)), []).append(row)
    return {key: _paper_group_summary(group) for key, group in sorted(groups.items())}


def _paper_group_summary(rows: list[sqlite3.Row]) -> dict[str, Any]:
    wins = sum(1 for row in rows if _paper_order_won(row))
    losses = sum(1 for row in rows if _paper_order_decided(row) and not _paper_order_won(row))
    pnl = sum(_to_float(row["realized_pnl"]) for row in rows)
    capital = sum(_to_float(row["contracts"]) * _to_float(row["cost_per_contract"]) for row in rows)
    return {
        "resolved": len(rows),
        "wins": wins,
        "losses": losses,
        "realized_pnl": _round(pnl, 2),
        "capital_resolved": _round(capital, 2),
        "roi": _round(pnl / capital, 4) if capital > 0 else None,
        "hit_rate": _round(wins / (wins + losses), 4) if (wins + losses) else None,
    }


def _worst_paper_segments(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (_row_risk_profile(row) or "unknown", _side_from_row(row), _paper_exit_reason(row))
        grouped.setdefault(key, []).append(row)
    segments = []
    for (profile, side, exit_reason), group in grouped.items():
        summary = _paper_group_summary(group)
        segments.append(
            {
                "risk_profile": profile,
                "side": side,
                "exit_reason": exit_reason,
                **summary,
            }
        )
    segments.sort(key=lambda row: (_to_float(row.get("realized_pnl")), -int(row.get("resolved") or 0)))
    return segments[:6]


def _paper_exit_reason(row: sqlite3.Row) -> str:
    if row["status"] == "PAPER_EXPIRED":
        return "limit_expired"
    if row["closed_at"]:
        return "monitor_exit"
    if row["settled_at"]:
        return "held_to_settlement"
    return "unresolved"


def _paper_order_won(row: sqlite3.Row) -> bool:
    try:
        resolved_yes = row["resolved_yes"]
    except (IndexError, KeyError):
        resolved_yes = None
    if resolved_yes is None:
        return _to_float(row["realized_pnl"]) > 0.0
    side = _side_from_row(row)
    return bool(resolved_yes) if side == "YES" else not bool(resolved_yes)


def _paper_order_decided(row: sqlite3.Row) -> bool:
    try:
        resolved_yes = row["resolved_yes"]
    except (IndexError, KeyError):
        resolved_yes = None
    if resolved_yes is not None:
        return True
    return abs(_to_float(row["realized_pnl"])) > 1e-9


def _profiles_with_scanners(
    profiles: list[dict[str, Any]],
    scanning_profiles: list[str],
) -> list[dict[str, Any]]:
    """A profile that scans but never trades must still appear in the lab —
    'balanced placed nothing while fast-feedback lost' is the key diagnostic."""

    present = {row["risk_profile"] for row in profiles}
    for name in scanning_profiles:
        if name in present:
            continue
        profiles.append(
            {
                "risk_profile": name,
                "orders": 0,
                "resolved": 0,
                "wins": 0,
                "losses": 0,
                "hit_rate": None,
                "realized_pnl": 0.0,
                "roi": None,
                "open_positions": 0,
                "open_risk": 0.0,
                "pending_limit_orders": 0,
                "pending_limit_risk": 0.0,
            }
        )
    profiles.sort(key=lambda row: row["risk_profile"])
    return profiles


def _profile_summary_row(row: sqlite3.Row) -> dict[str, Any]:
    resolved = int(row["resolved"] or 0)
    wins = int(row["wins"] or 0)
    losses = int(row["losses"] or 0)
    pnl = _to_float(row["realized_pnl"])
    capital = _to_float(row["capital_resolved"])
    return {
        "risk_profile": str(row["risk_profile"]),
        "orders": int(row["orders"] or 0),
        "resolved": resolved,
        "wins": wins,
        "losses": losses,
        "hit_rate": _round(wins / (wins + losses), 4) if (wins + losses) else None,
        "realized_pnl": _round(pnl, 2),
        "roi": _round(pnl / capital, 4) if capital > 0 else None,
        "open_positions": int(row["open_positions"] or 0),
        "open_risk": _round(_to_float(row["open_risk"]), 2),
        "pending_limit_orders": int(row["pending_limit_orders"] or 0),
        "pending_limit_risk": _round(_to_float(row["pending_limit_risk"]), 2),
    }


def _dataset_research_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Compact Strategy Lab view of the daily dataset-research verdict.

    The backfill timer collects IEM/Open-Meteo/Kalshi history every night; this
    block is what proves the collection is feeding the promotion decision
    rather than sitting unused in the DB.
    """

    if not payload:
        return {
            "available": False,
            "reason": "dataset_research.json not published yet; the nightly backfill writes it after collection.",
        }
    # dataset_research.py publishes candidates under accuracy_gate with MAE
    # metrics nested in each row's holdout block (the decision basis); legacy
    # top-level payloads are kept readable as a fallback.
    accuracy_gate = payload.get("accuracy_gate")
    if not isinstance(accuracy_gate, dict):
        accuracy_gate = {}
    candidates = accuracy_gate.get("candidates", payload.get("candidates"))
    candidate_rows = candidates if isinstance(candidates, list) else []
    top = [
        _dataset_candidate_row(row)
        for row in candidate_rows[:6]
        if isinstance(row, dict)
    ]
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    candidate_count = _dataset_candidate_count(
        accuracy_gate=accuracy_gate,
        payload=payload,
        candidate_rows=candidate_rows,
    )
    accuracy_candidates = _dataset_accuracy_candidate_count(
        accuracy_gate=accuracy_gate,
        payload=payload,
        candidate_rows=candidate_rows,
    )
    minimum_matched_rows = int(
        _to_float(accuracy_gate.get("minimum_matched_rows"), DEFAULT_MIN_MATCHED_ROWS)
    )
    profitability_gate = payload.get("profitability_gate")
    if not isinstance(profitability_gate, dict):
        profitability_gate = {}
    dataset_stack = _dataset_stack_summary(
        payload.get("dataset_stack") if isinstance(payload.get("dataset_stack"), dict) else {},
        candidate_rows=candidate_rows,
        minimum_matched_rows=minimum_matched_rows,
    )
    dataset_coverage = payload.get("dataset_coverage")
    if not isinstance(dataset_coverage, dict):
        dataset_coverage = {}
    headline = summary.get("headline") or _dataset_research_headline(
        candidate_count=candidate_count,
        accuracy_candidate_count=accuracy_candidates,
        candidate_rows=candidate_rows,
    )
    blocking_gates = summary.get("blocking_gates") or _dataset_blocking_gates(
        candidate_count=candidate_count,
        accuracy_candidate_count=accuracy_candidates,
        candidate_rows=candidate_rows,
        minimum_matched_rows=minimum_matched_rows,
        profitability_gate=profitability_gate,
    )
    action_items = summary.get("action_items") or _dataset_action_items(
        candidate_count=candidate_count,
        candidate_rows=candidate_rows,
        minimum_matched_rows=minimum_matched_rows,
        profitability_gate=profitability_gate,
    )
    return {
        "available": True,
        "generated_at": payload.get("generated_at"),
        "status": payload.get("status"),
        "headline": headline,
        "promotion_rule": payload.get("promotion_rule") or _dataset_promotion_rule(),
        "candidate_count": candidate_count,
        "accuracy_candidate_count": accuracy_candidates,
        "blocking_gates": blocking_gates,
        "action_items": action_items,
        "baseline": payload.get("baseline") or {},
        "dataset_coverage": dataset_coverage,
        "dataset_stack": dataset_stack,
        "profitability_gate": profitability_gate,
        "candidates": top,
    }


def _dataset_candidate_row(row: dict[str, Any]) -> dict[str, Any]:
    holdout = row.get("holdout")
    metrics = holdout if isinstance(holdout, dict) else row
    return {
        "dataset_key": row.get("dataset_key"),
        "decision": row.get("decision"),
        "reason": row.get("reason"),
        "next_use": row.get("next_use")
        or _dataset_candidate_next_use(row.get("decision"), row.get("reason")),
        "matched_rows": row.get("matched_rows"),
        "dataset_mae_f": metrics.get("dataset_mae_f"),
        "baseline_mae_f": metrics.get("baseline_mae_f"),
        "mae_delta_vs_baseline_f": metrics.get("mae_delta_vs_baseline_f"),
    }


def _dataset_candidate_count(
    *,
    accuracy_gate: dict[str, Any],
    payload: dict[str, Any],
    candidate_rows: list[Any],
) -> int:
    value = accuracy_gate.get("candidate_count", payload.get("candidate_count"))
    if value is None:
        return len(candidate_rows)
    return int(_to_float(value))


def _dataset_accuracy_candidate_count(
    *,
    accuracy_gate: dict[str, Any],
    payload: dict[str, Any],
    candidate_rows: list[Any],
) -> int:
    value = accuracy_gate.get("accuracy_candidate_count", payload.get("accuracy_candidate_count"))
    if value is not None:
        return int(_to_float(value))
    return sum(
        1
        for row in candidate_rows
        if isinstance(row, dict) and row.get("decision") == "accuracy_candidate"
    )


def _dataset_stack_summary(
    stack: dict[str, Any],
    *,
    candidate_rows: list[Any],
    minimum_matched_rows: int,
) -> dict[str, Any]:
    if stack:
        return stack
    max_matched = _dataset_max_matched_rows(candidate_rows)
    return {
        "available": False,
        "decision": "collect_only",
        "reason": (
            "Combined dataset stack is waiting for matched settlement rows; "
            f"best published feature has {max_matched} and needs {minimum_matched_rows}."
        ),
        "minimum_matched_rows": minimum_matched_rows,
        "max_matched_rows": max_matched,
        "feature_keys": [],
    }


def _dataset_research_headline(
    *,
    candidate_count: int,
    accuracy_candidate_count: int,
    candidate_rows: list[Any],
) -> str:
    max_matched = _dataset_max_matched_rows(candidate_rows)
    if accuracy_candidate_count > 0:
        return "Some dataset views show forecast lift, but trade weighting is still gated."
    if candidate_count <= 0:
        return "Dataset research is waiting for backfilled forecast feature rows."
    if max_matched <= 0:
        return "Dataset collection is live, but forecast features are not yet matched to settled SFO highs."
    return "Dataset collection is live; no source has cleared the holdout improvement gate yet."


def _dataset_blocking_gates(
    *,
    candidate_count: int,
    accuracy_candidate_count: int,
    candidate_rows: list[Any],
    minimum_matched_rows: int,
    profitability_gate: dict[str, Any],
) -> list[str]:
    gates = []
    max_matched = _dataset_max_matched_rows(candidate_rows)
    if candidate_count <= 0:
        gates.append("dataset gate: no forecast feature candidates published yet")
    if max_matched < minimum_matched_rows:
        gates.append(
            f"accuracy gate: best dataset feature has {max_matched} matched settlement rows; "
            f"needs {minimum_matched_rows}"
        )
    elif accuracy_candidate_count <= 0:
        gates.append("accuracy gate: no dataset feature has beaten LSTM on holdout yet")

    market_history = profitability_gate.get("market_history")
    trade_rows = 0
    if isinstance(market_history, dict):
        trade_rows = int(_to_float(market_history.get("trades")))
    minimum_trades = int(
        _to_float(profitability_gate.get("minimum_after_cost_trades"), DEFAULT_MIN_AFTER_COST_TRADES)
    )
    if trade_rows < minimum_trades:
        gates.append(f"market gate: {trade_rows} after-cost trade rows; needs {minimum_trades}")
    return gates


def _dataset_action_items(
    *,
    candidate_count: int,
    candidate_rows: list[Any],
    minimum_matched_rows: int,
    profitability_gate: dict[str, Any],
) -> list[str]:
    items = []
    max_matched = _dataset_max_matched_rows(candidate_rows)
    if candidate_count <= 0:
        items.append("Run the compact dataset backfill so forecast feature candidates publish into Strategy Lab.")
    if max_matched < minimum_matched_rows:
        items.append(
            f"Keep nightly dataset backfill running until each candidate has {minimum_matched_rows} "
            "matched settled highs."
        )
    market_history = profitability_gate.get("market_history")
    trade_rows = 0
    if isinstance(market_history, dict):
        trade_rows = int(_to_float(market_history.get("trades")))
    minimum_trades = int(
        _to_float(profitability_gate.get("minimum_after_cost_trades"), DEFAULT_MIN_AFTER_COST_TRADES)
    )
    if trade_rows < minimum_trades:
        items.append("Backfill or enable Kalshi trade history before using datasets for trading weight.")
    items.append("Keep LSTM as the live model until both accuracy and market gates pass.")
    return items


def _dataset_max_matched_rows(candidate_rows: list[Any]) -> int:
    return max(
        (
            int(_to_float(row.get("matched_rows")))
            for row in candidate_rows
            if isinstance(row, dict)
        ),
        default=0,
    )


def _dataset_candidate_next_use(decision: Any, reason: Any) -> str:
    if decision == "accuracy_candidate":
        return (
            "Eligible for challenger-model research; keep live trading weight at zero "
            "until the after-cost market gate passes."
        )
    if reason:
        return f"Keep collecting; {reason}"
    return "Keep collecting until the accuracy and market gates pass."


def _dataset_promotion_rule() -> str:
    return (
        "Collect broadly, but do not give a new source live model weight or loosen "
        "paper-trading gates until it improves held-out forecast error and then "
        "survives an after-cost market backtest with enough trades."
    )


def _forecast_health_payload(
    forecaster_root: Path,
    *,
    config: StrategyConfig,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_utc = _coerce_utc(now)
    today = settlement_today(current_utc)
    rolling_targets = [
        (today + timedelta(days=offset)).isoformat()
        for offset in range(FORECAST_HEALTH_ROLLING_DAYS)
    ]
    db_path = Path(forecaster_root) / "weather.db"
    warnings: list[dict[str, Any]] = []
    payload: dict[str, Any] = {
        "available": False,
        "db_path_hint": str(db_path),
        "generated_at": current_utc.isoformat(),
        "rolling_targets": rolling_targets,
        "nwp": {"available": False, "targets": []},
        "emos": {
            "available": False,
            "profiles_using_emos": _emos_enabled_profiles(config),
            "live_targets": [],
        },
        "clisfo": {"available": False},
        "nws_ground_truth": {"available": False},
        "warnings": warnings,
    }
    if not db_path.exists():
        warnings.append(
            _health_warning(
                "critical",
                "weather-db-missing",
                "Weather DB missing",
                f"Strategy Lab cannot inspect NWP, EMOS, or CLISFO health at {db_path}.",
                "Verify the AWS forecaster runtime path and refresh service.",
            )
        )
        return payload

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            payload["available"] = True
            payload["nwp"] = _nwp_health(conn, rolling_targets, current_utc, warnings)
            payload["emos"] = _emos_health(
                conn,
                rolling_targets,
                current_utc,
                warnings,
                profiles_using_emos=_emos_enabled_profiles(config),
            )
            payload["clisfo"] = _clisfo_health(conn, current_utc, warnings)
            payload["nws_ground_truth"] = _nws_ground_truth_health(conn, today)
    except sqlite3.Error as exc:
        payload["available"] = False
        warnings.append(
            _health_warning(
                "critical",
                "weather-db-unreadable",
                "Weather DB unreadable",
                f"{type(exc).__name__}: {exc}",
                "Inspect weather.db permissions and SQLite integrity on AWS.",
            )
        )
    return payload


def _nwp_health(
    conn: sqlite3.Connection,
    rolling_targets: list[str],
    current_utc: datetime,
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    if not _sqlite_table_exists(conn, "nwp_model_forecasts"):
        warnings.append(
            _health_warning(
                "warning",
                "nwp-table-missing",
                "NWP archive missing",
                "The nwp_model_forecasts table is not present in weather.db.",
                "Run the forecaster NWP archive refresh and check its logs.",
            )
        )
        return {"available": False, "targets": [], "reason": "nwp_model_forecasts table missing"}

    # Availability calendar (why each rolling target is checked differently):
    # the previous-runs ARCHIVE is refreshed by the nightly maintenance unit
    # with a fetch window ending at today+1, so the only target with reliably
    # complete archive rows around the clock is TODAY at lead 1 -- tomorrow's
    # rows appear only after the nightly run (and cover a handful of stations
    # until the day's model runs publish), and today+2 is never inside the
    # fetch window at all. Demanding lead-1 archive rows for future targets is
    # what produced the daily false "nwp-thin-target" for today+2. Tomorrow and
    # the 2-day-out target are instead checked against the LIVE serve's model
    # coverage (forecast_emos_daily_high.n_models, refreshed every tick from
    # the current-run forecast API) -- the models actually feeding those
    # markets' distributions.
    targets: list[dict[str, Any]] = []
    for offset, target in enumerate(rolling_targets):
        if offset == 0:
            row = conn.execute(
                """
                SELECT target_date,
                       lead_days,
                       COUNT(DISTINCT model) AS model_count,
                       MAX(fetched_at) AS latest_fetched_at,
                       GROUP_CONCAT(DISTINCT source) AS sources
                FROM nwp_model_forecasts
                WHERE target_date = ? AND lead_days = 1
                GROUP BY target_date, lead_days
                """,
                (target,),
            ).fetchone()
            target_health = _nwp_target_health(row, current_utc, target)
            target_health["check"] = "archive_lead1"
            targets.append(target_health)
            if target_health["model_count"] < FORECAST_HEALTH_MIN_NWP_MODELS:
                warnings.append(
                    _health_warning(
                        "warning",
                        "nwp-thin-target",
                        "Thin NWP target",
                        (
                            f"{target} has {target_health['model_count']} model(s), below "
                            f"the {FORECAST_HEALTH_MIN_NWP_MODELS}-model health floor."
                        ),
                        "Check the NWP archive refresh log for source outages.",
                        target_date=target,
                    )
                )
            if target_health.get("latest_age_hours") is not None and (
                target_health["latest_age_hours"]
                > FORECAST_HEALTH_MAX_NWP_AGE.total_seconds() / 3600
            ):
                warnings.append(
                    _health_warning(
                        "warning",
                        "nwp-stale-target",
                        "Stale NWP target",
                        f"{target} NWP data is {target_health['latest_age_hours']:.1f} hours old.",
                        "Check sfo-forecaster-refresh.service and the NWP archive step.",
                        target_date=target,
                    )
                )
            continue
        target_health = _nwp_live_serve_health(conn, target, current_utc)
        targets.append(target_health)
        model_count = target_health["model_count"]
        # A missing live serve for the target is _emos_health's
        # "emos-live-missing" alarm; only a PRESENT-but-thin serve is an NWP
        # coverage problem, so model_count None never warns here.
        if model_count is not None and model_count < FORECAST_HEALTH_MIN_NWP_MODELS:
            warnings.append(
                _health_warning(
                    "warning",
                    "nwp-thin-target",
                    "Thin NWP target",
                    (
                        f"{target} live serve has a station with {model_count} model(s), "
                        f"below the {FORECAST_HEALTH_MIN_NWP_MODELS}-model health floor."
                    ),
                    "Check the live EMOS serve log for current-run model outages.",
                    target_date=target,
                )
            )

    recent = [
        _round_dict(dict(row))
        for row in conn.execute(
            """
            SELECT target_date,
                   lead_days,
                   COUNT(DISTINCT model) AS model_count,
                   MAX(fetched_at) AS latest_fetched_at,
                   GROUP_CONCAT(DISTINCT source) AS sources
            FROM nwp_model_forecasts
            GROUP BY target_date, lead_days
            ORDER BY target_date DESC, lead_days
            LIMIT 21
            """
        ).fetchall()
    ]
    return {
        "available": True,
        "min_healthy_models": FORECAST_HEALTH_MIN_NWP_MODELS,
        "max_stale_hours": FORECAST_HEALTH_MAX_NWP_AGE.total_seconds() / 3600,
        "targets": targets,
        "recent_targets": recent,
    }


def _nwp_target_health(row: sqlite3.Row | None, current_utc: datetime, target: str) -> dict[str, Any]:
    if row is None:
        return {
            "target_date": target,
            "lead_days": 1,
            "model_count": 0,
            "latest_fetched_at": None,
            "latest_age_hours": None,
            "sources": [],
        }
    latest = _parse_timestamp(row["latest_fetched_at"])
    return {
        "target_date": str(row["target_date"]),
        "lead_days": int(row["lead_days"]),
        "model_count": int(row["model_count"] or 0),
        "latest_fetched_at": row["latest_fetched_at"],
        "latest_age_hours": _age_hours(current_utc, latest),
        "sources": _split_csv(row["sources"]),
    }


def _nwp_live_serve_health(
    conn: sqlite3.Connection, target: str, current_utc: datetime
) -> dict[str, Any]:
    """Model coverage for a future rolling target via the live EMOS serve.

    The archive cannot hold rows for targets past today+1 (nightly fetch
    window), so the model count that matters for those markets is n_models on
    the freshest live serve per station: the worst station's coverage is the
    honest number. ``model_count`` is None when no live rows exist yet -- that
    absence is reported by _emos_health, not here.
    """

    empty = {
        "target_date": target,
        "lead_days": None,
        "check": "live_serve_models",
        "model_count": None,
        "latest_fetched_at": None,
        "latest_age_hours": None,
        "sources": [],
    }
    if not _sqlite_table_exists(conn, "forecast_emos_daily_high"):
        return empty
    station_expr = (
        "station_id"
        if "station_id" in _sqlite_columns(conn, "forecast_emos_daily_high")
        else "'KSFO' AS station_id"
    )
    rows = conn.execute(
        f"""
        SELECT {station_expr}, n_models, fetched_at
        FROM forecast_emos_daily_high
        WHERE source = 'live' AND target_date = ?
        ORDER BY fetched_at DESC
        """,
        (target,),
    ).fetchall()
    freshest: dict[str, sqlite3.Row] = {}
    for row in rows:
        freshest.setdefault(str(row["station_id"]), row)
    if not freshest:
        return empty
    latest_fetched = max(str(row["fetched_at"]) for row in freshest.values())
    return {
        "target_date": target,
        "lead_days": None,
        "check": "live_serve_models",
        "model_count": min(int(row["n_models"] or 0) for row in freshest.values()),
        "latest_fetched_at": latest_fetched,
        "latest_age_hours": _age_hours(current_utc, _parse_timestamp(latest_fetched)),
        "sources": ["live_emos_serve"],
    }


def _emos_health(
    conn: sqlite3.Connection,
    rolling_targets: list[str],
    current_utc: datetime,
    warnings: list[dict[str, Any]],
    *,
    profiles_using_emos: list[str],
) -> dict[str, Any]:
    if not _sqlite_table_exists(conn, "forecast_emos_daily_high"):
        warnings.append(
            _health_warning(
                "warning",
                "emos-table-missing",
                "EMOS table missing",
                "The forecast_emos_daily_high table is not present in weather.db.",
                "Run the EMOS archive and live serving step.",
            )
        )
        return {
            "available": False,
            "profiles_using_emos": profiles_using_emos,
            "live_targets": [],
            "reason": "forecast_emos_daily_high table missing",
        }

    # Per-station, per-target: the serve writes one live row per city per
    # rolling target per tick (15 cities x 3 targets at leads 0..2), so a
    # global "latest 12 rows" slice conflated cities and fired a false
    # "emos-live-missing" for today/today+1 on every scan. The freshest row
    # per (station, target) -- at ANY lead, which is how the trader reads it
    # and how the lead-0 same-day serve lands -- is the unit of health.
    station_keyed = "station_id" in _sqlite_columns(conn, "forecast_emos_daily_high")
    expected_stations = (
        [city.nws_station_id for city in CITIES] if station_keyed else ["KSFO"]
    )
    station_expr = "station_id" if station_keyed else "'KSFO' AS station_id"
    placeholders = ",".join("?" for _ in rolling_targets)
    live_rows = conn.execute(
        f"""
        SELECT {station_expr}, target_date, lead_days, predicted_high_f, sigma_f,
               n_models, fetched_at, method, source
        FROM forecast_emos_daily_high
        WHERE source = 'live' AND target_date IN ({placeholders})
        ORDER BY fetched_at DESC
        """,
        tuple(rolling_targets),
    ).fetchall()
    latest_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in live_rows:
        key = (str(row["station_id"]), str(row["target_date"]))
        latest_by_key.setdefault(key, _emos_row(row, current_utc))

    # A settled (station, target) is done: the serve refuses to overwrite a
    # settled day by design, so neither its absence nor its age is a failure.
    settled: set[tuple[str, str]] = set()
    if _sqlite_table_exists(conn, "cli_settlements"):
        final_filter = (
            "AND is_final = 1"
            if "is_final" in _sqlite_columns(conn, "cli_settlements")
            else ""
        )
        settled = {
            (str(row["station_id"]), str(row["local_date"]))
            for row in conn.execute(
                f"""
                SELECT station_id, local_date FROM cli_settlements
                WHERE local_date IN ({placeholders}) AND max_temperature_f IS NOT NULL
                  {final_filter}
                """,
                tuple(rolling_targets),
            ).fetchall()
        }

    live_targets = [
        latest_by_key[(station, target)]
        for target in rolling_targets
        for station in expected_stations
        if (station, target) in latest_by_key
    ]
    max_age_hours = FORECAST_HEALTH_MAX_EMOS_AGE.total_seconds() / 3600
    for station in expected_stations:
        open_targets = [
            target for target in rolling_targets if (station, target) not in settled
        ]
        missing = [
            target for target in open_targets if (station, target) not in latest_by_key
        ]
        if missing and profiles_using_emos:
            warnings.append(
                _health_warning(
                    "warning",
                    "emos-live-missing",
                    "Live EMOS target missing",
                    (
                        f"{station} has no live EMOS distribution for "
                        f"{', '.join(missing)}; EMOS-enabled profiles degrade to "
                        f"residual calibration there."
                    ),
                    "Run emos_forecast.py --serve-rolling --cities all and inspect its output.",
                    target_date=missing[0],
                    station=station,
                )
            )
            continue
        stale = [
            (target, latest_by_key[(station, target)]["latest_age_hours"])
            for target in open_targets
            if (station, target) in latest_by_key
            and latest_by_key[(station, target)]["latest_age_hours"] is not None
            and latest_by_key[(station, target)]["latest_age_hours"] > max_age_hours
        ]
        if stale:
            worst_target, worst_age = max(stale, key=lambda item: item[1])
            extra = f" ({len(stale)} targets stale)" if len(stale) > 1 else ""
            warnings.append(
                _health_warning(
                    "warning",
                    "emos-live-stale",
                    "Live EMOS target stale",
                    f"{station} live EMOS for {worst_target} is "
                    f"{worst_age:.1f} hours old{extra}.",
                    "Check the forecaster refresh timer and EMOS serve-rolling step.",
                    target_date=worst_target,
                    station=station,
                )
            )

    archive = conn.execute(
        """
        SELECT COUNT(*) AS rows,
               MAX(target_date) AS latest_target_date,
               MAX(fetched_at) AS latest_fetched_at
        FROM forecast_emos_daily_high
        WHERE source != 'live'
        """
    ).fetchone()
    return {
        "available": True,
        "profiles_using_emos": profiles_using_emos,
        "max_stale_hours": FORECAST_HEALTH_MAX_EMOS_AGE.total_seconds() / 3600,
        "stations_checked": expected_stations,
        "live_targets": live_targets,
        "recent_live_targets": live_targets,
        "rolling_archive": {
            "rows": int(archive["rows"] or 0),
            "latest_target_date": archive["latest_target_date"],
            "latest_fetched_at": archive["latest_fetched_at"],
        },
    }


def _emos_row(row: sqlite3.Row, current_utc: datetime) -> dict[str, Any]:
    fetched = _parse_timestamp(row["fetched_at"])
    return {
        "station_id": str(row["station_id"]),
        "target_date": str(row["target_date"]),
        "lead_days": int(row["lead_days"]),
        "mu_f": _round(row["predicted_high_f"], 2),
        "sigma_f": _round(row["sigma_f"], 2),
        "n_models": int(row["n_models"] or 0),
        "fetched_at": row["fetched_at"],
        "latest_age_hours": _age_hours(current_utc, fetched),
        "method": row["method"],
        "source": row["source"],
    }


def _clisfo_health(
    conn: sqlite3.Connection,
    current_utc: datetime,
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Per-station CLI settlement freshness (the settlement instrument).

    The legacy single-city ``clisfo_settlements`` table was dropped in the
    15-city rearchitecture; truth lives in the station-keyed
    ``cli_settlements``. Checking the legacy name fired "table missing" on
    every scan while real per-station staleness went undetected. Each
    station's lag is measured against ITS OWN settlement day (fixed standard
    time), because an eastern station's climate day rolls hours before SFO's.
    """

    if not _sqlite_table_exists(conn, "cli_settlements"):
        warnings.append(
            _health_warning(
                "warning",
                "clisfo-table-missing",
                "CLI settlements missing",
                "The cli_settlements table is not present in weather.db.",
                "Run the CLI settlement refresh and inspect source access.",
            )
        )
        return {"available": False, "reason": "cli_settlements table missing"}
    final_filter = (
        "AND is_final = 1"
        if "is_final" in _sqlite_columns(conn, "cli_settlements")
        else ""
    )
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS rows,
               MAX(local_date) AS latest_date,
               MAX(fetched_at) AS latest_fetched_at
        FROM cli_settlements
        WHERE max_temperature_f IS NOT NULL
          {final_filter}
        """
    ).fetchone()
    total_rows = int(row["rows"] or 0)
    if total_rows == 0:
        warnings.append(
            _health_warning(
                "critical",
                "clisfo-empty",
                "CLI truth empty",
                "No CLI settlement rows with max_temperature_f are available.",
                "Check CLI fetch logs before trusting calibration or settlement.",
            )
        )
        return {
            "available": True,
            "rows": 0,
            "latest_date": None,
            "latest_fetched_at": None,
            "lag_days": None,
            "max_lag_days": FORECAST_HEALTH_MAX_CLISFO_LAG_DAYS,
            "stations": [],
        }
    latest_by_station = {
        str(station): latest
        for station, latest in conn.execute(
            f"""
            SELECT station_id, MAX(local_date)
            FROM cli_settlements
            WHERE max_temperature_f IS NOT NULL
              {final_filter}
            GROUP BY station_id
            """
        ).fetchall()
    }
    stations: list[dict[str, Any]] = []
    worst_lag: int | None = None
    for city in CITIES:
        station = city.nws_station_id
        station_today = settlement_today(current_utc, city)
        latest = latest_by_station.get(station)
        latest_date = _date_from_string(latest)
        lag_days = (station_today - latest_date).days if latest_date is not None else None
        stations.append(
            {"station_id": station, "latest_date": latest, "lag_days": lag_days}
        )
        if lag_days is None:
            warnings.append(
                _health_warning(
                    "warning",
                    "clisfo-stale",
                    "CLI truth missing for station",
                    f"{station} has no settled CLI rows in cli_settlements.",
                    "Check the CLI settlement fetch for that station.",
                    station=station,
                )
            )
        elif lag_days > FORECAST_HEALTH_MAX_CLISFO_LAG_DAYS:
            warnings.append(
                _health_warning(
                    "warning",
                    "clisfo-stale",
                    "CLI truth stale",
                    (
                        f"{station} latest CLI truth ({latest}) is {lag_days} "
                        f"settlement day(s) behind."
                    ),
                    "Run paper-auto-settle or inspect the CLI/NWS source fetch.",
                    station=station,
                )
            )
        if lag_days is not None:
            worst_lag = lag_days if worst_lag is None else max(worst_lag, lag_days)
    return {
        "available": True,
        "rows": total_rows,
        "latest_date": row["latest_date"],
        "latest_fetched_at": row["latest_fetched_at"],
        # Worst station lag: the honest headline number for a 15-city truth feed.
        "lag_days": worst_lag,
        "max_lag_days": FORECAST_HEALTH_MAX_CLISFO_LAG_DAYS,
        "stations": stations,
    }


def _nws_ground_truth_health(conn: sqlite3.Connection, today: object) -> dict[str, Any]:
    if not _sqlite_table_exists(conn, "nws_daily_high_ground_truth"):
        return {"available": False, "reason": "nws_daily_high_ground_truth table missing"}
    columns = _sqlite_columns(conn, "nws_daily_high_ground_truth")
    observed_expr = "MAX(high_observed_at)" if "high_observed_at" in columns else "NULL"
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS rows,
               MAX(local_date) AS latest_date,
               {observed_expr} AS latest_observed_at
        FROM nws_daily_high_ground_truth
        WHERE high_f IS NOT NULL
        """
    ).fetchone()
    latest_date = _date_from_string(row["latest_date"])
    lag_days = (today - latest_date).days if latest_date is not None else None
    return {
        "available": True,
        "rows": int(row["rows"] or 0),
        "latest_date": row["latest_date"],
        "latest_observed_at": row["latest_observed_at"],
        "lag_days": lag_days,
    }


def _emos_enabled_profiles(config: StrategyConfig) -> list[str]:
    profiles: list[str] = []
    if config.emos_distribution_enabled:
        profiles.append(PRIMARY_PROFILE)
    for profile in sorted(EXPERIMENTAL_PROFILES):
        if strategy_config_for_profile(profile).emos_distribution_enabled:
            profiles.append(profile)
    return profiles


def _health_warning(
    level: str,
    code: str,
    title: str,
    detail: str,
    action: str,
    *,
    target_date: str | None = None,
    station: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "level": level,
        "code": code,
        "title": title,
        "detail": detail,
        "action": action,
    }
    if target_date is not None:
        row["target_date"] = target_date
    if station is not None:
        row["station_id"] = station
    return row


def _sqlite_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _coerce_utc(value: datetime | None) -> datetime:
    current_utc = value or datetime.now(UTC)
    if current_utc.tzinfo is None:
        return current_utc.replace(tzinfo=UTC)
    return current_utc.astimezone(UTC)


def _age_hours(current_utc: datetime, timestamp: datetime | None) -> float | None:
    if timestamp is None:
        return None
    return _round(max(0.0, (current_utc - timestamp).total_seconds() / 3600.0), 2)


def _split_csv(value: object) -> list[str]:
    if value is None:
        return []
    return [part for part in str(value).split(",") if part]


def _row_risk_profile(row: sqlite3.Row) -> str | None:
    try:
        value = row["risk_profile"]
    except (IndexError, KeyError):
        return None
    return str(value) if value else None


def _status_payload(
    *,
    config: StrategyConfig,
    db_path: Path,
    trading_signal: dict[str, Any] | None,
    backtest: dict[str, Any],
    signal_quality: dict[str, Any],
    paper: dict[str, Any],
    forecast_health: dict[str, Any],
) -> dict[str, Any]:
    latest_targets = [
        str(row.get("target_date"))
        for row in signal_quality.get("latest_candidates", [])
        if row.get("target_date") and row.get("market_available") is not False
    ]
    entry_block_reason = _entry_block_reason(signal_quality.get("latest_candidates", []))
    for row in paper.get("open_positions", []):
        if row.get("target_date") and not _is_probability_only_ticker(row.get("ticker")):
            latest_targets.append(str(row["target_date"]))
    latest_target = signal_quality.get("latest_target_date") or _status_target_date(
        latest_targets,
        entry_block_reason=entry_block_reason,
    ) or _target_from_signal(trading_signal)
    raw_count = backtest["counts"]["raw_signals"]
    settled_count = backtest["counts"]["settled_signals"]
    small_sample = settled_count < 30
    alerts = _strategy_alerts(
        paper=paper,
        entry_block_reason=entry_block_reason,
        forecast_health=forecast_health,
    )
    return {
        "active_calibration_source": ACTIVE_CALIBRATION_SOURCE,
        "active_calibration_label": "lstm = Active execution calibration",
        "challenger_calibration_source": CHALLENGER_CALIBRATION_SOURCE,
        "challenger_calibration_label": (
            "clean-blend/combined = Challenger research calibration"
        ),
        "aws_execution_calibration_locked": True,
        "paper_only": True,
        "automation_status": (
            "AWS timers generate forecast, public signal, Strategy Lab JSON, "
            "paper scans, and paper monitor state when enabled."
        ),
        "paper_trading_status": _paper_status(paper),
        "entry_scanner_status": (
            "Same-day entries blocked; rolling scanner is evaluating later target dates."
            if entry_block_reason
            else "Entry scanner active for eligible target dates."
        ),
        "entry_scanner_reason": entry_block_reason,
        "last_updated": datetime.now(UTC).isoformat(),
        "latest_target_date": latest_target,
        "latest_signal_targets": sorted(set(latest_targets)),
        "raw_signal_count": raw_count,
        "pre_resolution_signal_count": backtest["counts"]["pre_resolution_signals"],
        "deduped_signal_count": backtest["counts"]["deduped_signals"],
        "post_resolution_excluded_count": backtest["counts"]["excluded_post_resolution_signals"],
        "alerts": alerts,
        "alert_level": _alert_level(alerts),
        "sample_warning": (
            "Sample size is still small; treat calibration and ROI as research diagnostics."
            if small_sample
            else ""
        ),
        "bankroll": _round(config.paper_bankroll, 2),
        "target_exposure_cap": _round(config.paper_bankroll * config.max_target_exposure_pct, 2),
        "max_entries_per_market_side": int(config.max_entries_per_market_side),
        "open_risk": paper["summary"]["open_risk"],
        "db_path_hint": str(db_path),
    }


def _decision_lead_mode_counts(
    db_path: Path, *, retention_days: int = 45
) -> dict[str, dict[str, Any]]:
    if not db_path.exists() or not _db_table_exists(db_path, "decision_snapshots"):
        return _empty_lead_mode_counts()
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT
                    CASE
                        WHEN COALESCE(intraday_is_complete, 0) != 0
                             OR (created_at IS NOT NULL AND
                                 (market_close_time IS NULL OR created_at >= market_close_time))
                            THEN 'post_resolution_excluded'
                        WHEN forecast_observed_high_mode IS NOT NULL
                             AND forecast_observed_high_mode != ''
                             OR intraday_observed_high_f IS NOT NULL
                             OR LOWER(COALESCE(forecast_method, '')) LIKE '%observed high%'
                             OR LOWER(COALESCE(forecast_method, '')) LIKE '%intraday%'
                             OR LOWER(COALESCE(forecast_method, '')) LIKE '%high-so-far%'
                            THEN 'intraday_high_so_far'
                        WHEN forecast_lead_hours IS NULL THEN 'unknown'
                        WHEN forecast_lead_hours >= 18.0 THEN 'day_ahead'
                        ELSE 'same_day_prelock'
                    END AS lead_mode,
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN approved = 1 THEN 1 ELSE 0 END), 0) AS approved
                FROM decision_snapshots
                WHERE market_ticker NOT LIKE '%-PAPER%'
                  AND created_at >= ?
                GROUP BY lead_mode
                """,
                ((datetime.now(UTC) - timedelta(days=max(1, retention_days))).isoformat(),),
            ).fetchall()
    except sqlite3.Error:
        return _empty_lead_mode_counts()

    counts = _empty_lead_mode_counts()
    for lead_mode, total, approved in rows:
        mode = str(lead_mode or "unknown")
        if mode not in counts:
            mode = "unknown"
        counts[mode]["total"] += int(total or 0)
        counts[mode]["approved"] += int(approved or 0)
    return counts


def _lead_mode_counts(decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    counts = _empty_lead_mode_counts()
    for row in decisions:
        mode = str(row.get("lead_mode") or "unknown")
        if mode not in counts:
            counts[mode] = {
                "lead_mode": mode,
                "label": FORECAST_LEAD_MODE_LABELS["unknown"],
                "total": 0,
                "approved": 0,
            }
        counts[mode]["total"] += 1
        if row.get("approved"):
            counts[mode]["approved"] += 1
    return counts


def _empty_lead_mode_counts() -> dict[str, dict[str, Any]]:
    return {
        mode: {"lead_mode": mode, "label": label, "total": 0, "approved": 0}
        for mode, label in FORECAST_LEAD_MODE_LABELS.items()
    }


def _forecast_lead_mode(
    *,
    lead_hours: object,
    forecast_method: object,
    observed_high_mode: object,
    intraday_observed_high_f: object,
    intraday_is_complete: bool,
    pre_resolution: bool,
) -> str:
    if not pre_resolution or intraday_is_complete:
        return "post_resolution_excluded"
    method = str(forecast_method or "").lower()
    if (
        observed_high_mode
        or intraday_observed_high_f not in (None, "")
        or "observed high" in method
        or "intraday" in method
        or "high-so-far" in method
    ):
        return "intraday_high_so_far"
    lead = _to_float(lead_hours, default=math.nan)
    if not math.isfinite(lead):
        return "unknown"
    if lead >= 18.0:
        return "day_ahead"
    return "same_day_prelock"


def _latest_decision_rows(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists() or not _db_table_exists(db_path, "decision_snapshots"):
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            WITH recent_targets AS (
                SELECT target_date
                FROM decision_snapshots
                WHERE market_ticker NOT LIKE '%-PAPER%'
                GROUP BY target_date
                ORDER BY target_date DESC
                LIMIT 3
            ),
            latest_by_target AS (
                SELECT d.target_date,
                       COALESCE(d.risk_profile, 'unknown') AS risk_profile,
                       MAX(d.created_at) AS created_at
                FROM decision_snapshots d
                JOIN recent_targets rt ON rt.target_date = d.target_date
                WHERE d.market_ticker NOT LIKE '%-PAPER%'
                GROUP BY d.target_date, COALESCE(d.risk_profile, 'unknown')
            )
            SELECT d.*
            FROM decision_snapshots d
            JOIN latest_by_target latest
              ON latest.target_date = d.target_date
             AND latest.risk_profile = COALESCE(d.risk_profile, 'unknown')
             AND latest.created_at = d.created_at
            WHERE d.market_ticker NOT LIKE '%-PAPER%'
            ORDER BY d.target_date DESC, d.approved DESC,
                     d.trade_quality_score DESC, d.edge_lcb DESC, d.edge DESC
            """
        ).fetchall()
    return [_decision_row(row) for row in rows]


def _decision_row(row: sqlite3.Row) -> dict[str, Any]:
    reasons = _json_list(row["reasons_json"])
    approved = bool(row["approved"])
    raw_signal_approved = _sqlite_row_value(row, "signal_approved")
    signal_approved = bool(raw_signal_approved) if raw_signal_approved is not None else approved
    lead_hours = _sqlite_row_value(row, "forecast_lead_hours")
    lead_mode = _forecast_lead_mode(
        lead_hours=lead_hours,
        forecast_method=_sqlite_row_value(row, "forecast_method"),
        observed_high_mode=_sqlite_row_value(row, "forecast_observed_high_mode"),
        intraday_observed_high_f=_sqlite_row_value(row, "intraday_observed_high_f"),
        intraday_is_complete=bool(_sqlite_row_value(row, "intraday_is_complete", 0)),
        pre_resolution=_is_strategy_pre_resolution(row),
    )
    return {
        "created_at": row["created_at"],
        "target_date": row["target_date"],
        "ticker": row["market_ticker"],
        "market_available": not _is_probability_only_ticker(row["market_ticker"]),
        "label": row["label"],
        "side": row["side"],
        "risk_profile": _row_risk_profile(row) or "unknown",
        "approved": approved,
        "signal_approved": signal_approved,
        "entry_block_reason": _sqlite_row_value(row, "entry_block_reason"),
        "diagnostics_available": bool(_sqlite_row_value(row, "diagnostics_json")),
        "forecast_snapshot_id": _sqlite_row_value(row, "forecast_snapshot_id"),
        "market_snapshot_id": _sqlite_row_value(row, "market_snapshot_id"),
        "decision": "TRADE" if approved else "NO_TRADE",
        "probability": _round(row["probability"], 4),
        "probability_lcb": _round(row["probability_lcb"], 4),
        "model_probability": _round(row["model_probability"], 4),
        "market_probability": _round(row["market_probability"], 4),
        "residual_probability": _round(row["residual_probability"], 4),
        "ensemble_probability": _round(row["ensemble_probability"], 4),
        "intraday_probability": _round(row["intraday_probability"], 4),
        "remaining_heat_risk": _round(row["remaining_heat_risk"], 4),
        "bid": _round(row["entry_bid"], 4),
        "ask": _round(row["entry_ask"], 4),
        "spread": _round(row["spread"], 4),
        "edge": _round(row["edge"], 4),
        "edge_lcb": _round(row["edge_lcb"], 4),
        "quality_score": _round(row["trade_quality_score"], 1),
        "recommended_contracts": _round(row["recommended_contracts"], 4),
        "recommended_spend": _round(row["recommended_spend"], 2),
        "expected_profit": _round(row["expected_profit"], 2),
        "forecast_lead_hours": _round(lead_hours, 2),
        "forecast_method": _sqlite_row_value(row, "forecast_method"),
        "forecast_observed_high_mode": _sqlite_row_value(row, "forecast_observed_high_mode"),
        "lead_mode": lead_mode,
        "lead_mode_label": FORECAST_LEAD_MODE_LABELS.get(lead_mode, FORECAST_LEAD_MODE_LABELS["unknown"]),
        "reasons": reasons,
        "decision_reason": _decision_reason(approved, reasons, row["edge"], row["edge_lcb"]),
    }


def _decisions_from_trading_signal(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("local_runtime_placeholder"):
        return []
    decisions = []
    for target in payload.get("targets") or []:
        if target.get("market_available") is False:
            continue
        forecast = target.get("forecast") or {}
        lead_hours = forecast.get("lead_hours")
        forecast_method = forecast.get("method")
        observed_high_mode = (
            (forecast.get("observed_high_decision") or {}).get("mode")
            if isinstance(forecast.get("observed_high_decision"), dict)
            else None
        )
        lead_mode = _forecast_lead_mode(
            lead_hours=lead_hours,
            forecast_method=forecast_method,
            observed_high_mode=observed_high_mode,
            intraday_observed_high_f=(target.get("intraday") or {}).get("observed_high_f")
            if isinstance(target.get("intraday"), dict)
            else None,
            intraday_is_complete=bool((target.get("intraday") or {}).get("is_complete"))
            if isinstance(target.get("intraday"), dict)
            else False,
            pre_resolution=True,
        )
        for row in target.get("decisions") or []:
            if _is_probability_only_ticker(row.get("ticker")):
                continue
            reasons = list(row.get("reasons") or [])
            approved = bool(row.get("approved"))
            decisions.append(
                {
                    "created_at": payload.get("generated_at"),
                    "target_date": target.get("target_date"),
                    "ticker": row.get("ticker"),
                    "market_available": bool(target.get("market_available", True)),
                    "label": row.get("label"),
                    "side": row.get("side"),
                    "risk_profile": row.get("risk_profile") or payload.get("risk_profile") or PRIMARY_PROFILE,
                    "approved": approved,
                    "signal_approved": bool(row.get("signal_approved", approved)),
                    "entry_block_reason": row.get("entry_block_reason"),
                    "decision": row.get("decision") or ("TRADE" if approved else "NO_TRADE"),
                    "probability": row.get("probability"),
                    "probability_lcb": row.get("probability_lcb"),
                    "model_probability": row.get("model_probability"),
                    "market_probability": row.get("market_probability"),
                    "residual_probability": row.get("residual_probability"),
                    "ensemble_probability": row.get("ensemble_probability"),
                    "intraday_probability": row.get("intraday_probability"),
                    "remaining_heat_risk": row.get("remaining_heat_risk"),
                    "bid": row.get("bid"),
                    "ask": row.get("ask"),
                    "spread": row.get("spread"),
                    "edge": row.get("edge"),
                    "edge_lcb": row.get("edge_lcb"),
                    "quality_score": row.get("trade_quality_score"),
                    "recommended_contracts": row.get("recommended_contracts"),
                    "recommended_spend": row.get("recommended_spend"),
                    "expected_profit": row.get("expected_profit"),
                    "forecast_lead_hours": lead_hours,
                    "forecast_method": forecast_method,
                    "forecast_observed_high_mode": observed_high_mode,
                    "lead_mode": lead_mode,
                    "lead_mode_label": FORECAST_LEAD_MODE_LABELS.get(
                        lead_mode,
                        FORECAST_LEAD_MODE_LABELS["unknown"],
                    ),
                    "reasons": reasons,
                    "decision_reason": _decision_reason(
                        approved,
                        reasons,
                        row.get("edge"),
                        row.get("edge_lcb"),
                    ),
                }
            )
    return decisions


def _latest_monitor_marks(db_path: Path) -> dict[int, dict[str, Any]]:
    if not db_path.exists() or not _db_table_exists(db_path, "paper_monitor_snapshots"):
        return {}
    marks: dict[int, dict[str, Any]] = {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM paper_monitor_snapshots
            WHERE live_bid IS NOT NULL
            ORDER BY created_at DESC, id DESC
            LIMIT 5000
            """
        ).fetchall()
    for row in rows:
        order_id = int(row["order_id"])
        if order_id in marks:
            continue
        marks[order_id] = {
            "source": "paper_monitor_snapshot",
            "snapshot_id": row["id"],
            "created_at": row["created_at"],
            "bid": _round(row["live_bid"], 4),
            "ask": None,
            "bid_size": None,
            "ask_size": None,
            "spread": None,
            "market_probability": None,
            "quality_score": None,
            "monitor_action": row["action"],
            "monitor_reason": row["reason"],
        }
    return marks


def _latest_position_marks(db_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    if not db_path.exists() or not _db_table_exists(db_path, "decision_snapshots"):
        return {}
    marks: dict[tuple[str, str], dict[str, Any]] = {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, created_at, market_ticker, side, yes_bid, yes_ask,
                   entry_bid, entry_ask, entry_bid_size, entry_ask_size,
                   spread, probability, model_probability, market_probability,
                   trade_quality_score
            FROM decision_snapshots
            ORDER BY created_at DESC, id DESC
            LIMIT 5000
            """
        ).fetchall()

    now = datetime.now(UTC)
    for row in rows:
        side = _side_from_row(row)
        key = (row["market_ticker"], side)
        if key in marks:
            continue
        bid = row["entry_bid"]
        ask = row["entry_ask"]
        if side == "YES":
            bid = row["yes_bid"] if bid is None else bid
            ask = row["yes_ask"] if ask is None else ask
        marks[key] = {
            "source": "latest_decision_snapshot",
            "snapshot_id": row["id"],
            "created_at": row["created_at"],
            "bid": _round(bid, 4),
            "ask": _round(ask, 4),
            "bid_size": _round(row["entry_bid_size"], 4),
            "ask_size": _round(row["entry_ask_size"], 4),
            "spread": _round(row["spread"], 4),
            "market_probability": _round(row["market_probability"], 4),
            "model_side_probability": _fresh_model_side_probability(row, now),
            "quality_score": _round(row["trade_quality_score"], 1),
        }
    return marks


def _fresh_model_side_probability(row: sqlite3.Row, now: datetime) -> float | None:
    created_at = _parse_timestamp(row["created_at"])
    if created_at is None or now - created_at > timedelta(minutes=90):
        return None
    value = row["model_probability"] if row["model_probability"] is not None else row["probability"]
    if value is None:
        return None
    parsed = _to_float(value, default=math.nan)
    if not math.isfinite(parsed):
        return None
    return max(0.0, min(1.0, parsed))


def _position_mark_for(
    row: sqlite3.Row,
    monitor_marks: dict[int, dict[str, Any]],
    decision_marks: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    side = _side_from_row(row)
    decision_mark = decision_marks.get((row["market_ticker"], side))
    monitor_mark = monitor_marks.get(int(row["id"]))
    if monitor_mark is None:
        return decision_mark
    if decision_mark is None:
        return monitor_mark
    return {**decision_mark, **monitor_mark}


def _side_from_row(row: sqlite3.Row) -> str:
    try:
        side = row["side"]
    except (IndexError, KeyError):
        side = None
    if side:
        normalized = str(side).upper()
        if normalized in {"YES", "NO"}:
            return normalized
    try:
        action = str(row["action"]).upper()
    except (IndexError, KeyError):
        return "YES"
    return "NO" if "NO" in action else "YES"


def _paper_monitor_config() -> dict[str, Any]:
    take_profit = _env_float("PAPER_TAKE_PROFIT_PCT")
    stop_loss = _env_float("PAPER_STOP_LOSS_PCT")
    yes_take_profit = _env_float("PAPER_YES_TAKE_PROFIT_PCT")
    yes_stop_loss = _env_float("PAPER_YES_STOP_LOSS_PCT")
    no_take_profit = _env_float("PAPER_NO_TAKE_PROFIT_PCT")
    no_stop_loss = _env_float("PAPER_NO_STOP_LOSS_PCT")
    model_veto_max_loss = _env_float("PAPER_MODEL_VETO_MAX_LOSS_PCT")
    model_veto_buffer = _env_float("PAPER_MODEL_VETO_BUFFER")
    return {
        "take_profit_pct": take_profit if take_profit is not None else DEFAULT_TAKE_PROFIT_PCT,
        "stop_loss_pct": stop_loss if stop_loss is not None else DEFAULT_STOP_LOSS_PCT,
        "yes_take_profit_pct": (
            yes_take_profit if yes_take_profit is not None else DEFAULT_YES_TAKE_PROFIT_PCT
        ),
        "yes_stop_loss_pct": yes_stop_loss if yes_stop_loss is not None else DEFAULT_YES_STOP_LOSS_PCT,
        "no_take_profit_pct": no_take_profit if no_take_profit is not None else DEFAULT_NO_TAKE_PROFIT_PCT,
        "no_stop_loss_pct": no_stop_loss if no_stop_loss is not None else DEFAULT_NO_STOP_LOSS_PCT,
        "model_veto_max_loss_pct": (
            model_veto_max_loss
            if model_veto_max_loss is not None
            else DEFAULT_MODEL_VETO_MAX_LOSS_PCT
        ),
        "model_veto_buffer": model_veto_buffer if model_veto_buffer is not None else DEFAULT_MODEL_VETO_BUFFER,
    }


def _monitor_thresholds_for_side(monitor: dict[str, Any], side: str) -> tuple[float, float]:
    normalized = side.upper()
    if normalized == "YES":
        return (
            _to_float(monitor.get("yes_take_profit_pct"), DEFAULT_YES_TAKE_PROFIT_PCT),
            _to_float(monitor.get("yes_stop_loss_pct"), DEFAULT_YES_STOP_LOSS_PCT),
        )
    if normalized == "NO":
        return (
            _to_float(monitor.get("no_take_profit_pct"), DEFAULT_NO_TAKE_PROFIT_PCT),
            _to_float(monitor.get("no_stop_loss_pct"), DEFAULT_NO_STOP_LOSS_PCT),
        )
    return (
        _to_float(monitor.get("take_profit_pct"), DEFAULT_TAKE_PROFIT_PCT),
        _to_float(monitor.get("stop_loss_pct"), DEFAULT_STOP_LOSS_PCT),
    )


def _settlement_first_no_min_cost_for_row(row: sqlite3.Row) -> float | None:
    profile = _row_risk_profile(row)
    if profile is not None and normalize_risk_profile_name(profile) == "research":
        return DEFAULT_RESEARCH_NO_SETTLEMENT_FIRST_MIN_COST
    return None


def _position_mark_status(
    unrealized_pnl: float | None,
    unrealized_roi: float | None,
    monitor: dict[str, Any],
    side: str,
) -> dict[str, str]:
    if unrealized_pnl is None or unrealized_roi is None:
        return {
            "status": "MARK_PENDING",
            "label": "Mark pending",
            "tone": "warn",
            "monitor_action": "WAITING_FOR_MARK",
        }

    take_profit_pct, stop_loss_pct = _monitor_thresholds_for_side(monitor, side)
    take_profit = take_profit_pct / 100.0
    stop_loss = stop_loss_pct / 100.0
    if unrealized_roi >= take_profit:
        monitor_action = "TAKE_PROFIT_READY"
    elif unrealized_roi <= -stop_loss:
        monitor_action = "STOP_LOSS_READY"
    else:
        monitor_action = "HOLD"

    if unrealized_pnl > 0.005:
        return {
            "status": "WINNING",
            "label": "Winning",
            "tone": "good",
            "monitor_action": monitor_action,
        }
    if unrealized_pnl < -0.005:
        return {
            "status": "LOSING",
            "label": "Losing",
            "tone": "bad",
            "monitor_action": monitor_action,
        }
    return {
        "status": "FLAT",
        "label": "Flat",
        "tone": "warn",
        "monitor_action": monitor_action,
    }


def _paper_row(
    row: sqlite3.Row,
    mark: dict[str, Any] | None = None,
    monitor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reasons = _json_list(row["reasons_json"])
    outcome_diagnostics = _json_object(_sqlite_row_value(row, "outcome_diagnostics_json"))
    outcome = outcome_diagnostics.get("outcome") if isinstance(outcome_diagnostics, dict) else {}
    monitor = monitor or _paper_monitor_config()
    side = _side_from_row(row)
    contracts = _to_float(row["contracts"])
    entry_price = row["entry_price"] if row["entry_price"] is not None else row["yes_ask"]
    cost_per_contract = _to_float(row["cost_per_contract"])
    fee_per_contract = _to_float(row["fee_per_contract"])
    risk = contracts * cost_per_contract
    current_bid = _to_float(mark.get("bid"), None) if mark else None
    current_ask = _to_float(mark.get("ask"), None) if mark else None
    current_exit_fee = (
        quadratic_fee_average_per_contract(
            current_bid, contracts, series_ticker=str(row["market_ticker"])
        )
        if current_bid is not None and current_bid > 0
        else None
    )
    current_net_exit = (
        current_bid - current_exit_fee
        if current_bid is not None and current_exit_fee is not None
        else None
    )
    current_value = (
        contracts * current_net_exit
        if current_net_exit is not None
        else None
    )
    unrealized_pnl = (
        current_value - risk
        if current_value is not None
        else None
    )
    unrealized_roi = (
        unrealized_pnl / risk
        if unrealized_pnl is not None and risk > 0
        else None
    )
    take_profit_pct, stop_loss_pct = _monitor_thresholds_for_side(monitor, side)
    take_profit = take_profit_pct / 100.0
    stop_loss = stop_loss_pct / 100.0
    legacy_take_profit_net = cost_per_contract * (1.0 + take_profit)
    stop_loss_net = max(0.0, cost_per_contract * (1.0 - stop_loss))
    model_side_probability = _to_float(mark.get("model_side_probability"), None) if mark else None
    model_take_profit_net = convergence_take_profit_net(model_side_probability)
    if model_take_profit_net is not None:
        take_profit_net = model_take_profit_net
        take_profit_basis = "model_fair_value"
    else:
        take_profit_net = legacy_take_profit_net
        take_profit_basis = "legacy_percent"
    mark_status = _position_mark_status(unrealized_pnl, unrealized_roi, monitor, side)
    exit_rule_reason = None
    if current_net_exit is not None:
        signal = decide_exit(
            side=side,
            entry_cost=cost_per_contract,
            net_exit=current_net_exit,
            stop_loss_net=stop_loss_net,
            model_side_probability=model_side_probability,
            model_veto_buffer=_to_float(monitor.get("model_veto_buffer"), DEFAULT_MODEL_VETO_BUFFER),
            model_veto_max_loss_roi=(
                _to_float(
                    monitor.get("model_veto_max_loss_pct"),
                    DEFAULT_MODEL_VETO_MAX_LOSS_PCT,
                )
                / 100.0
            ),
            legacy_take_profit_net=legacy_take_profit_net,
            stop_loss_pct=stop_loss_pct,
            settlement_first_no_min_cost=_settlement_first_no_min_cost_for_row(row),
        )
        exit_rule_reason = signal.reason
        mark_status = {**mark_status, "monitor_action": signal.action}
    if row["status"] == "PAPER_LIMIT_RESTING":
        mark_status = {
            "status": "LIMIT_RESTING",
            "label": "Limit pending",
            "tone": "warn",
            "monitor_action": "LIMIT_RESTING",
        }
    elif row["status"] in {"PAPER_SETTLED", "PAPER_CLOSED"}:
        realized = _to_float(row["realized_pnl"], 0.0)
        mark_status = {
            "status": "RESOLVED",
            "label": "Resolved",
            "tone": "good" if realized > 0 else "bad" if realized < 0 else "warn",
            "monitor_action": "RESOLVED",
        }
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "filled_at": _sqlite_row_value(row, "filled_at"),
        "cancelled_at": _sqlite_row_value(row, "cancelled_at"),
        "expires_at": _sqlite_row_value(row, "expires_at"),
        "account_id": _sqlite_row_value(row, "account_id"),
        "strategy_fingerprint": (
            _sqlite_row_value(row, "strategy_fingerprint") or "legacy_independent_sizing"
        ),
        "sleeve": _sqlite_row_value(row, "sleeve"),
        "fill_model": _sqlite_row_value(row, "fill_model"),
        "target_date": row["target_date"],
        "ticker": row["market_ticker"],
        "label": row["label"],
        "side": side,
        "status": row["status"],
        "risk_profile": _row_risk_profile(row),
        "diagnostics_available": bool(
            _sqlite_row_value(row, "diagnostics_json")
            or _sqlite_row_value(row, "outcome_diagnostics_json")
        ),
        "entry_decision_snapshot_id": _sqlite_row_value(row, "entry_decision_snapshot_id"),
        "contracts": _round(contracts, 4),
        "entry_mode": row["entry_mode"] if "entry_mode" in row.keys() else "market",
        "limit_price": _round(row["limit_price"], 4) if "limit_price" in row.keys() else None,
        "limit_fee_per_contract": (
            _round(row["limit_fee_per_contract"], 4)
            if "limit_fee_per_contract" in row.keys()
            else None
        ),
        "limit_cost_per_contract": (
            _round(row["limit_cost_per_contract"], 4)
            if "limit_cost_per_contract" in row.keys()
            else None
        ),
        "limit_edge": _round(row["limit_edge"], 4) if "limit_edge" in row.keys() else None,
        "limit_edge_lcb": (
            _round(row["limit_edge_lcb"], 4)
            if "limit_edge_lcb" in row.keys()
            else None
        ),
        "entry_price": _round(entry_price, 4),
        "entry_fee_per_contract": _round(fee_per_contract, 4),
        "cost_per_contract": _round(cost_per_contract, 4),
        "initial_cost": _round(risk, 2),
        "risk": _round(risk, 2),
        "max_profit": _round(contracts * max(0.0, 1.0 - cost_per_contract), 2),
        "max_loss": _round(risk, 2),
        "probability": _round(row["probability"], 4),
        "probability_lcb": _round(row["probability_lcb"], 4),
        "edge": _round(row["edge"], 4),
        "edge_lcb": _round(row["edge_lcb"], 4),
        "quality_score": _round(row["trade_quality_score"], 1),
        "expected_profit": _round(row["expected_profit"], 2),
        "current_bid": _round(current_bid, 4),
        "current_ask": _round(current_ask, 4),
        "current_spread": _round(mark.get("spread"), 4) if mark else None,
        "current_market_probability": _round(mark.get("market_probability"), 4) if mark else None,
        "current_snapshot_at": mark.get("created_at") if mark else None,
        "current_source": mark.get("source") if mark else None,
        "model_side_probability": _round(model_side_probability, 4),
        "current_exit_fee_per_contract": _round(current_exit_fee, 4),
        "current_net_exit": _round(current_net_exit, 4),
        "current_value": _round(current_value, 2),
        "unrealized_pnl": _round(unrealized_pnl, 2),
        "unrealized_roi": _round(unrealized_roi, 4),
        "position_status": mark_status["status"],
        "position_status_label": mark_status["label"],
        "position_status_tone": mark_status["tone"],
        "monitor_action": mark_status["monitor_action"],
        "take_profit_pct": _round(take_profit_pct, 2),
        "stop_loss_pct": _round(stop_loss_pct, 2),
        "take_profit_basis": take_profit_basis,
        "stop_loss_basis": "legacy_percent_floor",
        "exit_rule_reason": exit_rule_reason,
        "take_profit_pnl": _round(risk * take_profit, 2),
        "stop_loss_pnl": _round(-(risk * stop_loss), 2),
        "take_profit_net_exit": _round(take_profit_net, 4),
        "stop_loss_net_exit": _round(stop_loss_net, 4),
        "take_profit_bid": exit_bid_for_net(take_profit_net, contracts),
        "stop_loss_bid": exit_bid_for_net(stop_loss_net, contracts),
        "settlement_high_f": _round(row["settlement_high_f"], 1),
        "resolved_yes": row["resolved_yes"],
        "exit_price": _round(row["exit_price"], 4),
        "exit_fee_per_contract": _round(row["exit_fee_per_contract"], 4),
        "realized_pnl": _round(row["realized_pnl"], 2),
        "realized_roi": (
            _round(_to_float(row["realized_pnl"]) / risk, 4)
            if row["realized_pnl"] is not None and risk > 0
            else None
        ),
        "closed_at": row["closed_at"],
        "settled_at": row["settled_at"],
        "outcome_reason": outcome.get("win_loss_reason") if isinstance(outcome, dict) else None,
        "forecast_error_f": (
            _round(outcome.get("forecast_error_f"), 2)
            if isinstance(outcome, dict)
            else None
        ),
        "reasons": reasons,
        "why_good": _why_trade_good(row, reasons),
    }


def _paper_action_row(row: sqlite3.Row) -> dict[str, Any]:
    action_time = row["closed_at"] or row["settled_at"] or row["created_at"]
    contracts = _to_float(row["contracts"])
    risk = contracts * _to_float(row["cost_per_contract"])
    return {
        "id": row["id"],
        "time": action_time,
        "ticker": row["market_ticker"],
        "label": row["label"],
        "target_date": row["target_date"],
        "side": row["side"],
        "risk_profile": _row_risk_profile(row),
        "contracts": _round(contracts, 4),
        "status": row["status"],
        "entry_price": _round(row["entry_price"] if row["entry_price"] is not None else row["yes_ask"], 4),
        "cost_per_contract": _round(row["cost_per_contract"], 4),
        "initial_cost": _round(risk, 2),
        "exit_price": _round(row["exit_price"], 4),
        "exit_fee_per_contract": _round(row["exit_fee_per_contract"], 4),
        "settlement_high_f": _round(row["settlement_high_f"], 1),
        "realized_pnl": _round(row["realized_pnl"], 2),
        "realized_roi": (
            _round(_to_float(row["realized_pnl"]) / risk, 4)
            if row["realized_pnl"] is not None and risk > 0
            else None
        ),
        "note": "closed by monitor" if row["closed_at"] else "settled against official high",
    }


def _paper_open_action_row(row: sqlite3.Row) -> dict[str, Any]:
    contracts = _to_float(row["contracts"])
    risk = contracts * _to_float(row["cost_per_contract"])
    return {
        "id": row["id"],
        "time": row["created_at"],
        "ticker": row["market_ticker"],
        "label": row["label"],
        "target_date": row["target_date"],
        "side": row["side"],
        "risk_profile": _row_risk_profile(row),
        "contracts": _round(contracts, 4),
        "status": "OPEN",
        "entry_price": _round(
            row["entry_price"] if row["entry_price"] is not None else row["yes_ask"],
            4,
        ),
        "cost_per_contract": _round(row["cost_per_contract"], 4),
        "initial_cost": _round(risk, 2),
        "exit_price": None,
        "exit_fee_per_contract": None,
        "settlement_high_f": None,
        "realized_pnl": None,
        "realized_roi": None,
        "note": "paper order opened",
    }


def _paper_limit_action_row(row: sqlite3.Row) -> dict[str, Any]:
    contracts = _to_float(row["contracts"])
    risk = contracts * _to_float(row["cost_per_contract"])
    return {
        "id": row["id"],
        "time": row["created_at"],
        "ticker": row["market_ticker"],
        "label": row["label"],
        "target_date": row["target_date"],
        "side": row["side"],
        "risk_profile": _row_risk_profile(row),
        "contracts": _round(contracts, 4),
        "status": "LIMIT_RESTING",
        "entry_price": _round(
            row["entry_price"] if row["entry_price"] is not None else row["yes_ask"],
            4,
        ),
        "cost_per_contract": _round(row["cost_per_contract"], 4),
        "initial_cost": _round(risk, 2),
        "exit_price": None,
        "exit_fee_per_contract": None,
        "settlement_high_f": None,
        "realized_pnl": None,
        "realized_roi": None,
        "note": "paper buy-limit resting until market ask trades at or below limit",
    }


def _paper_monitor_snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        label = row["label"] or row["market_ticker"]
    except (IndexError, KeyError):
        label = row["market_ticker"]
    return {
        "id": row["order_id"],
        "time": row["created_at"],
        "ticker": row["market_ticker"],
        "label": label,
        "target_date": row["target_date"],
        "side": row["side"],
        "risk_profile": _row_risk_profile(row),
        "contracts": None,
        "status": row["action"],
        "entry_price": None,
        "cost_per_contract": None,
        "initial_cost": None,
        "exit_price": _round(row["live_bid"], 4),
        "exit_fee_per_contract": _round(row["exit_fee_per_contract"], 4),
        "settlement_high_f": None,
        # These are an OPEN position's live mark, not a realized close: live_bid
        # is the current sell bid and unrealized_pnl is mark-to-market. The
        # `unrealized` flag lets the UI label them as such so a HOLD inspection
        # does not read as a closed trade (see Finding 5 in
        # docs/trading_engine_diagnosis_2026-06-16.md).
        "realized_pnl": _round(row["unrealized_pnl"], 2),
        "realized_roi": _round(row["unrealized_roi"], 4),
        "unrealized": True,
        "note": row["reason"] or "monitor inspection (open position)",
    }


def _duplicate_group_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "target_date": str(row["target_date"]),
        "risk_profile": str(row["risk_profile"]),
        "ticker": row["market_ticker"],
        "side": row["side"],
        "open_orders": int(row["open_orders"]),
    }


def _cohort_rows(cohorts) -> list[dict[str, Any]]:
    by_name = {
        cohort.name: {
            "name": cohort.name,
            "label": _cohort_label(cohort.name),
            "count": cohort.count,
            "brier_score": _round(cohort.brier_score, 4),
            "climatology_brier_score": _round(cohort.climatology_brier_score, 4),
            "brier_skill": _round(cohort.brier_skill, 4),
            "ranked_probability_score": _round(cohort.ranked_probability_score, 4),
            "climatology_ranked_probability_score": _round(
                cohort.climatology_ranked_probability_score,
                4,
            ),
            "ranked_probability_skill": _round(cohort.ranked_probability_skill, 4),
            "log_loss": _round(cohort.log_loss, 4),
            "top_bin_accuracy": _round(cohort.top_bin_accuracy, 4),
            "avg_winning_probability": _round(cohort.avg_winning_probability, 4),
        }
        for cohort in cohorts
    }
    rows = []
    for name in ("cold_below_60f", "normal_60_69f", "warm_70_79f", "hot_80f_plus"):
        rows.append(by_name.get(name) or _empty_cohort(name))
    return rows


def _empty_cohorts() -> list[dict[str, Any]]:
    return [
        _empty_cohort(name)
        for name in ("cold_below_60f", "normal_60_69f", "warm_70_79f", "hot_80f_plus")
    ]


def _empty_cohort(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "label": _cohort_label(name),
        "count": 0,
        "brier_score": None,
        "climatology_brier_score": None,
        "brier_skill": None,
        "ranked_probability_score": None,
        "climatology_ranked_probability_score": None,
        "ranked_probability_skill": None,
        "log_loss": None,
        "top_bin_accuracy": None,
        "avg_winning_probability": None,
    }


def _cohort_label(name: str) -> str:
    return {
        "cold_below_60f": "Cold",
        "normal_60_69f": "Normal",
        "warm_70_79f": "Warm",
        "hot_80f_plus": "Hot",
    }.get(name, name)


def _probability_market_points(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    points = []
    for row in decisions:
        market = row.get("market_probability")
        model = row.get("model_probability")
        probability = row.get("probability")
        if market is None or (model is None and probability is None):
            continue
        points.append(
            {
                "x": _round(market, 4),
                "y": _round(model if model is not None else probability, 4),
                "r": max(4, min(12, _to_float(row.get("quality_score")) / 10)),
                "label": row.get("label"),
                "side": row.get("side"),
                "approved": bool(row.get("approved")),
            }
        )
    return points


def _edge_by_market_bucket(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)]
    output = []
    for lower, upper in buckets:
        rows = [
            row
            for row in decisions
            if row.get("market_probability") is not None
            and lower / 100 <= _to_float(row["market_probability"]) < upper / 100
        ]
        output.append(
            {
                "range": f"{lower}-{upper}",
                "count": len(rows),
                "avg_edge": _round(
                    sum(_to_float(row.get("edge")) for row in rows) / len(rows),
                    4,
                )
                if rows
                else 0.0,
            }
        )
    return output


def _quality_distribution(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100.0001)]
    output = []
    for lower, upper in buckets:
        rows = [
            row
            for row in decisions
            if lower <= _to_float(row.get("quality_score")) < upper
        ]
        output.append({"range": f"{int(lower)}-{int(min(upper, 100))}", "count": len(rows)})
    return output


def _research_notes() -> list[dict[str, str]]:
    return [
        {"term": "Backtest", "note": "Replay historical rows to see how probabilities scored after outcomes were known."},
        {"term": "Look-ahead bias", "note": "Using information that was not available at trade time; Strategy Lab separates pre-resolution rows."},
        {"term": "Pre-resolution", "note": "A signal recorded before market close or an observed-high lock."},
        {"term": "Dedupe", "note": "Repeated 15-minute scans are reduced to one sampled row per target, market, and side."},
        {"term": "Calibration", "note": "How closely stated probabilities match observed win frequencies."},
        {"term": "Brier score", "note": "Squared probability error; lower is better."},
        {"term": "Log loss", "note": "Penalty for assigning low probability to what happened; lower is better."},
        {"term": "Paper trading", "note": "Simulated positions recorded for research. No live money is placed."},
        {"term": "Challenger model", "note": "A research calibration compared against the active execution calibration."},
    ]


def _decision_reason(approved: bool, reasons: list[str], edge: object, edge_lcb: object) -> str:
    if approved:
        return (
            f"Passed risk gates with edge {_to_float(edge):.3f} and "
            f"lower-bound edge {_to_float(edge_lcb):.3f}."
        )
    if reasons:
        return reasons[0]
    return "No trade gate reason was recorded."


def _why_trade_good(row: sqlite3.Row, reasons: list[str]) -> str:
    if reasons:
        return "; ".join(reasons[:2])
    return (
        f"Paper position passed gates with p={_to_float(row['probability']):.3f}, "
        f"edge={_to_float(row['edge']):.3f}, "
        f"edge_lcb={_to_float(row['edge_lcb']):.3f}."
    )


def _paper_status(paper: dict[str, Any]) -> str:
    if not paper.get("available"):
        return "paper database unavailable"
    summary = paper["summary"]
    open_count = int(_to_float(summary.get("open_positions"), default=0.0))
    pending_count = int(_to_float(summary.get("pending_limit_orders"), default=0.0))
    if open_count and pending_count:
        return f"{open_count} open paper position(s); {pending_count} resting limit order(s)"
    if open_count:
        return f"{open_count} open paper position(s)"
    if pending_count:
        return f"{pending_count} resting limit order(s)"
    return "no open paper positions"


def _strategy_alerts(
    *,
    paper: dict[str, Any],
    entry_block_reason: str | None,
    daily_budget: float | None = None,
    now: datetime | None = None,
    forecast_health: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    current_utc = now or datetime.now(UTC)
    if current_utc.tzinfo is None:
        current_utc = current_utc.replace(tzinfo=UTC)
    else:
        current_utc = current_utc.astimezone(UTC)
    summary = paper.get("summary") or {}
    if not paper.get("available"):
        alerts.append(
            _alert(
                "warning",
                "paper-db-unavailable",
                "Paper DB unavailable",
                str(paper.get("reason") or "Strategy Lab cannot read paper-trading state."),
                "Check the AWS paper DB path and strategy-research service logs.",
            )
        )
        return alerts

    open_count = int(_to_float(summary.get("open_positions"), default=0.0))
    unresolved_targets = summary.get("unresolved_past_targets") or []
    if unresolved_targets:
        target_text = ", ".join(
            f"{row.get('target_date')} ({int(row.get('open_orders') or 0)})"
            for row in unresolved_targets[:4]
        )
        # A paper position settles the MORNING AFTER its target date, once the NWS
        # CLISFO daily climate report for that date is published (it cannot exist
        # earlier -- the day's high is not known until the day ends). So a position
        # whose target was yesterday is in NORMAL settlement lag, not a failure: the
        # paper-settle timer clears it within hours. Flagging that benign, expected
        # state as a CRITICAL "backlog" is a false alarm. Escalate to critical only
        # when a target is >= 2 days stale, i.e. the settlement-high lookup genuinely
        # failed to resolve it. See docs/trading_engine_diagnosis_2026-06-16.md.
        # Use the injected clock (current_utc) so the age threshold is testable and
        # consistent with _entry_block_reason rather than reading the wall clock.
        today = settlement_today(current_utc)
        ages = []
        for row in unresolved_targets:
            parsed = _date_from_string(row.get("target_date"))
            if parsed is not None:
                ages.append((today - parsed).days)
        max_age = max(ages, default=1)
        if max_age >= 2:
            alerts.append(
                _alert(
                    "critical",
                    "settlement-backlog",
                    "Settlement backlog",
                    f"Paper positions are up to {max_age} days past settlement for completed "
                    f"target dates: {target_text}. The settlement-high lookup could not resolve "
                    f"them from CLISFO or WeatherEdge ground truth.",
                    "Run paper-auto-settle (it backfills older CLISFO versions) or inspect the "
                    "settlement source for those dates.",
                )
            )
        else:
            alerts.append(
                _alert(
                    "warning",
                    "settlement-pending",
                    "Settlement pending",
                    f"Positions for {target_text} are awaiting the official CLISFO high, which "
                    f"publishes the morning after the target date. Auto-settle resolves them on "
                    f"its next run.",
                    "No action needed; the paper-settle timer settles these automatically.",
                )
            )

    duplicate_groups = paper.get("duplicate_open_groups") or []
    if duplicate_groups:
        largest = duplicate_groups[0]
        alerts.append(
            _alert(
                "critical",
                "duplicate-open-markets",
                "Duplicate open markets",
                (
                    f"{len(duplicate_groups)} market/side group(s) have repeated open positions. "
                    f"Largest: {largest.get('open_orders')}x {largest.get('ticker')} {largest.get('side')}."
                ),
                "Clear legacy duplicates, then confirm the duplicate guard is deployed.",
            )
        )

    latest_monitor_at = summary.get("latest_monitor_action_at")
    latest_monitor_dt = _parse_timestamp(latest_monitor_at)
    if open_count and latest_monitor_dt is None:
        latest_opened_dt = _parse_timestamp(summary.get("latest_opened_at"))
        if latest_opened_dt is not None and current_utc - latest_opened_dt <= timedelta(minutes=10):
            alerts.append(
                _alert(
                    "info",
                    "monitor-pending",
                    "Monitor mark pending",
                    "A paper position was opened recently; the next monitor pass should mark it shortly.",
                    "Keep the paper monitor timer active and refresh Strategy Lab after the next monitor tick.",
                )
            )
        else:
            alerts.append(
                _alert(
                    "critical",
                    "monitor-not-recording",
                    "Monitor not recording",
                    "Open paper positions exist, but Strategy Lab has no monitor inspection rows.",
                    "Start the paper monitor service and refresh Strategy Lab.",
                )
            )
    elif open_count and latest_monitor_dt is not None:
        monitor_age = current_utc - latest_monitor_dt
        if monitor_age > timedelta(minutes=45):
            alerts.append(
                _alert(
                    "critical",
                    "monitor-stale",
                    "Monitor stale",
                    f"Latest paper monitor action is {_age_label(monitor_age)} old.",
                    "Check sfo-kalshi-paper-monitor.timer and service logs.",
                )
            )

    marked_count = int(_to_float(summary.get("marked_open_positions"), default=0.0))
    if open_count and marked_count == 0:
        alerts.append(
            _alert(
                "warning",
                "open-positions-unmarked",
                "Open positions unmarked",
                "Open paper positions have no current sell-bid marks yet.",
                "Confirm monitor snapshots include live bid data.",
            )
        )

    hidden_count = int(_to_float(summary.get("hidden_open_positions"), default=0.0))
    if hidden_count:
        alerts.append(
            _alert(
                "warning",
                "open-positions-hidden",
                "Open list truncated",
                f"{hidden_count} open paper position(s) are summarized but hidden from the card list.",
                "Use paper-report for the full ledger, or reduce stale open inventory.",
            )
        )

    open_risk = _to_float(summary.get("open_risk"), default=0.0)
    if daily_budget is not None and daily_budget > 0 and open_risk > daily_budget:
        alerts.append(
            _alert(
                "warning",
                "open-risk-over-budget",
                "Open risk over budget",
                f"Open paper risk ${open_risk:.2f} is above the daily budget ${daily_budget:.2f}.",
                "Review duplicate exposure and daily budget settings.",
            )
        )

    if entry_block_reason:
        alerts.append(
            _alert(
                "info",
                "same-day-entry-blocked",
                "Same-day entries blocked",
                entry_block_reason,
                "Monitor and settlement can still run; scanner shifts to later targets.",
            )
        )

    alerts.extend(_forecast_health_alerts(forecast_health))

    if not alerts:
        alerts.append(
            _alert(
                "ok",
                "strategy-lab-healthy",
                "Strategy Lab healthy",
                "No settlement, monitor, duplicate-position, or risk alerts are active.",
                "Keep monitoring after each AWS refresh.",
            )
        )
    return alerts


def _forecast_health_alerts(forecast_health: dict[str, Any] | None) -> list[dict[str, str]]:
    if not forecast_health:
        return []
    output: list[dict[str, str]] = []
    for warning in forecast_health.get("warnings") or []:
        if not isinstance(warning, dict):
            continue
        output.append(
            _alert(
                str(warning.get("level") or "warning"),
                str(warning.get("code") or "forecast-health-warning"),
                str(warning.get("title") or "Forecast health warning"),
                str(warning.get("detail") or "Forecast health check reported a warning."),
                str(warning.get("action") or "Inspect AWS forecast refresh logs."),
            )
        )
    return output


def _alert(level: str, code: str, title: str, detail: str, action: str) -> dict[str, str]:
    return {
        "level": level,
        "code": code,
        "title": title,
        "detail": detail,
        "action": action,
    }


def _alert_level(alerts: list[dict[str, str]]) -> str:
    order = {"critical": 4, "warning": 3, "info": 2, "ok": 1}
    return max((alert.get("level", "ok") for alert in alerts), key=lambda level: order.get(level, 0), default="ok")


def _entry_block_reason(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> str | None:
    today = settlement_today(now)
    for row in rows:
        target = _date_from_string(row.get("target_date"))
        if target is not None and target != today:
            continue
        explicit = row.get("entry_block_reason")
        if explicit:
            return str(explicit)
        for reason in row.get("reasons") or []:
            text = str(reason)
            if text.startswith("same-day entry disabled:"):
                return text
    return None


def _status_target_date(
    targets: list[str],
    *,
    entry_block_reason: str | None,
    now: datetime | None = None,
) -> str | None:
    parsed = sorted({
        parsed
        for target in targets
        if (parsed := _date_from_string(target)) is not None
    })
    if not parsed:
        return None

    today = settlement_today(now)

    if entry_block_reason:
        future = [target for target in parsed if target > today]
        if future:
            return future[0].isoformat()
    elif today in parsed:
        return today.isoformat()

    current_or_future = [target for target in parsed if target >= today]
    if current_or_future:
        return current_or_future[0].isoformat()
    return parsed[-1].isoformat()


def _target_from_signal(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    targets = [
        target.get("target_date")
        for target in payload.get("targets") or []
        if target.get("target_date") and target.get("market_available") is not False
    ]
    return max(targets) if targets else None


def _is_probability_only_ticker(ticker: object) -> bool:
    return "-PAPER" in str(ticker or "")


def _date_from_string(value: object):
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def _age_label(delta: timedelta) -> str:
    total_minutes = max(0, int(delta.total_seconds() // 60))
    if total_minutes < 60:
        return f"{total_minutes} minute(s)"
    hours, minutes = divmod(total_minutes, 60)
    if hours < 24:
        return f"{hours} hour(s) {minutes} minute(s)"
    days, hours = divmod(hours, 24)
    return f"{days} day(s) {hours} hour(s)"


def _load_json_optional(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _db_table_exists(db_path: Path, table_name: str) -> bool:
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


class _ModelProbabilityRef:
    """Minimal duck-type wrapper exposing ``.model_probability`` for reuse.

    ``consensus_to_dict`` (report.py) reads ``getattr(probability,
    "model_probability", None)`` off each ladder bin's probability object. Here
    the only model-probability source is the float persisted on the latest
    decision snapshot per ticker, so wrap each value in this tiny ref to reuse
    the report's serializer rather than fork its JSON shape.
    """

    __slots__ = ("model_probability",)

    def __init__(self, model_probability: float) -> None:
        self.model_probability = model_probability


def _latest_consensus_inputs(db_path: Path) -> tuple[str, float, dict[str, float]] | None:
    """Latest live target's (target_date, model high, per-ticker model prob).

    Mirrors ``_latest_decision_rows``' "most recent real (non-PAPER) target"
    selection, then pulls that scan tick's model predicted high and the per-bin
    model probabilities so the consensus block can compute model-minus-market and
    overlay the model curve. Returns None when no live decision snapshot exists.
    """

    if not db_path.exists() or not _db_table_exists(db_path, "decision_snapshots"):
        return None
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            WITH latest_target AS (
                SELECT target_date
                FROM decision_snapshots
                WHERE market_ticker NOT LIKE '%-PAPER%'
                ORDER BY target_date DESC
                LIMIT 1
            ),
            latest_tick AS (
                SELECT MAX(d.created_at) AS created_at
                FROM decision_snapshots d
                JOIN latest_target lt ON lt.target_date = d.target_date
                WHERE d.market_ticker NOT LIKE '%-PAPER%'
            )
            SELECT d.target_date, d.market_ticker,
                   d.side, d.model_probability, d.forecast_predicted_high_f
            FROM decision_snapshots d
            JOIN latest_target lt ON lt.target_date = d.target_date
            JOIN latest_tick lk ON lk.created_at = d.created_at
            WHERE d.market_ticker NOT LIKE '%-PAPER%'
            """
        ).fetchall()
    if not rows:
        return None

    target_date = str(rows[0]["target_date"])
    predicted_high_f: float | None = None
    model_probabilities: dict[str, float] = {}
    for row in rows:
        if predicted_high_f is None and row["forecast_predicted_high_f"] is not None:
            predicted_high_f = _to_float(row["forecast_predicted_high_f"], default=math.nan)
            if not math.isfinite(predicted_high_f):
                predicted_high_f = None
        ticker = row["market_ticker"]
        if ticker in model_probabilities or row["model_probability"] is None:
            continue
        # decision_snapshots stores model_probability per SIDE; flip a NO row back
        # to the YES frame so it matches the market ladder's YES-bin convention.
        value = _to_float(row["model_probability"], default=math.nan)
        if not math.isfinite(value):
            continue
        if str(row["side"]).upper() == "NO":
            value = 1.0 - value
        model_probabilities[ticker] = max(0.0, min(1.0, value))

    if predicted_high_f is None:
        return None
    return target_date, predicted_high_f, model_probabilities


def _market_consensus_payload(db_path: Path) -> dict[str, Any]:
    """Reconstruct the Kalshi market consensus for the latest live target.

    The Strategy Lab artifact is built offline from stored runtime state, so the
    consensus is rebuilt from the freshest persisted market-snapshot ladder
    (``PaperStore.latest_market_snapshot``) rather than a live Kalshi call. The
    serialized shape mirrors ``report.consensus_to_dict`` exactly so the
    dashboard and the daily report agree. Returns ``{"available": False}`` when
    there is no recent stored ladder or no live forecast to compare against.
    """

    inputs = _latest_consensus_inputs(db_path)
    if inputs is None:
        return {"available": False}
    target_date, predicted_high_f, model_probabilities = inputs

    if not _db_table_exists(db_path, "market_snapshots"):
        return {"available": False}
    try:
        event = PaperStore(db_path, init=False).latest_market_snapshot(target_date)
    except sqlite3.Error:
        return {"available": False}
    if event is None:
        return {"available": False}

    markets = event.active_markets or event.markets
    if not markets:
        return {"available": False}

    # Local imports: report.py pulls the live Kalshi/ensemble clients, so defer
    # the import to call time to keep this diagnostics builder's import light.
    from .consensus import build_market_consensus
    from .report import consensus_to_dict

    consensus = build_market_consensus(markets)
    probabilities = {
        ticker: _ModelProbabilityRef(value) for ticker, value in model_probabilities.items()
    }
    payload = consensus_to_dict(consensus, predicted_high_f, probabilities)
    if payload.get("available"):
        payload = {**payload, "target_date": target_date}
    return payload


def _round_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _round(value, 4) if isinstance(value, float) else value for key, value in row.items()}


def _round(value: object, digits: int = 4):
    if value is None:
        return None
    number = _to_float(value, default=math.nan)
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _null_metric(value: object, digits: int = 4):
    return None


def _to_float(value: object, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _env_float(name: str) -> float | None:
    value = os.getenv(name)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None
