from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .backtest import run_walk_forward_calibration_backtest
from .backtest_rescore import run_rescore
from .config import DEFAULT_DB_PATH, DEFAULT_FORECASTER_ROOT, SFO_TZ, StrategyConfig, strategy_config_for_profile
from .db import PaperStore
from .dataset_research import (
    DEFAULT_MIN_AFTER_COST_TRADES,
    DEFAULT_MIN_MATCHED_ROWS,
    build_dataset_research as build_dataset_research_payload,
)
from .exits import (
    DEFAULT_NO_STOP_LOSS_PCT,
    DEFAULT_NO_TAKE_PROFIT_PCT,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
    DEFAULT_YES_STOP_LOSS_PCT,
    DEFAULT_YES_TAKE_PROFIT_PCT,
)
from .fees import quadratic_fee_average_per_contract
from .forecast import ForecastDataError, SfoForecasterAdapter
from .settlement_day import settlement_today
from .summary import build_paper_summary
from .synthetic_blend import build_synthetic_blend_calibration


ACTIVE_CALIBRATION_SOURCE = "lstm"
CHALLENGER_CALIBRATION_SOURCE = "clean-blend/combined"
MIN_CLEAN_WINNER_SAMPLE = 60
# Exit-threshold percentage defaults are owned by exits.py (the single source
# shared with the live monitor); imported above.
DEFAULT_MODEL_VETO_MAX_LOSS_PCT = 60.0
DEFAULT_MODEL_VETO_BUFFER = 0.08
PRIMARY_PROFILE = "balanced"
EXPERIMENTAL_PROFILES = {"exploratory", "fast-feedback"}


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
    backtest = _signal_backtest_payload(adapter, db_path)
    config_rescore = _config_rescore_payload(adapter, db_path)
    signal_quality = _signal_quality_payload(db_path, trading_signal)
    paper = _paper_payload(db_path)
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
    )

    return {
        "schema_version": 1,
        "available": True,
        "mode": "paper_research_only",
        "live_orders_enabled": False,
        "default_profile": _default_profile(profiles),
        "generated_at": datetime.now(UTC).isoformat(),
        "source_of_truth": "AWS Lightsail runtime artifacts after sync and refresh",
        "status": status,
        "daily_summary": daily_summary,
        "calibration_comparison": {
            "active": active_calibration,
            "challenger": challenger_calibration,
            "comparison": comparison,
        },
        "prediction_replay": prediction_replay,
        "signal_quality": signal_quality,
        "backtest_summary": backtest,
        "config_rescore": config_rescore,
        "paper_trading": paper,
        "profiles": profiles,
        "dataset_research": _dataset_research_summary(dataset_research),
        "research_notes": _research_notes(),
        "disclaimer": (
            "Paper-trading research only. The active AWS execution calibration "
            "remains pinned to lstm; this artifact does not place live orders."
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
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    cumulative = 0.0
    days = []
    for row in daily_summary.get("days") or []:
        profile = ((row.get("profiles") or {}).get(name) or {})
        realized = _to_float(profile.get("realized_pnl"))
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
                "cumulative_realized": _round(cumulative, 2),
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
    window_pnl = _to_float(profile_total.get("realized_pnl"))
    totals = {
        "trades_opened": opened,
        "trades_closed": closed,
        "trades_settled": settled,
        "open_positions": int(_to_float(paper_total.get("open_positions"))),
        "open_risk": _round(paper_total.get("open_risk"), 2),
        "realized_pnl": _round(window_pnl, 2),
        "cumulative_realized_pnl": _round(paper_total.get("realized_pnl", window_pnl), 2),
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
        # Per-profile live equity = the shared starting notional + this profile's
        # all-time realized PnL (paper_total["realized_pnl"], the same value the
        # cumulative_realized_pnl total uses). Without these the equity card on a
        # profile tab fell back to "-" because only the aggregate carried them.
        "starting_bankroll": daily_summary.get(
            "starting_bankroll", daily_summary.get("bankroll")
        ),
        "current_equity": _round(
            _to_float(daily_summary.get("starting_bankroll", daily_summary.get("bankroll")))
            + _to_float(paper_total.get("realized_pnl")),
            2,
        ),
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
    rows = [
        row
        for row in signal_quality.get("latest_candidates") or []
        if _profile_key(row.get("risk_profile")) == name
    ]
    return {
        "available": bool(rows),
        "source": signal_quality.get("source"),
        "latest_candidates": rows,
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
        "top_rejections": [],
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
        "balanced": 0,
        "conservative": 1,
        "exploratory": 2,
        "fast-feedback": 3,
        "unknown": 9,
    }
    return order.get(name, 8), name


def _profile_label(name: str) -> str:
    if name == "balanced":
        return "Balanced"
    if name == "fast-feedback":
        return "Fast feedback (experimental)"
    if name == "exploratory":
        return "Exploratory (experimental)"
    if name == "conservative":
        return "Conservative"
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
        "log_loss": _round(result.log_loss, 4),
        "top_bin_accuracy": _round(result.top_bin_accuracy, 4),
        "avg_winning_probability": _round(result.avg_winning_probability, 4),
        "avg_entropy": _round(result.avg_entropy, 4),
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
    recommendation = "Keep AWS execution pinned to lstm."
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


def _config_rescore_payload(adapter: SfoForecasterAdapter, db_path: Path) -> dict[str, Any]:
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
        settlements = adapter.load_ksfo_daily_highs()
        store = PaperStore(db_path, init=False)
        rows = store.sampled_decision_rows(sample_mode="entry-per-market-side")
        by_profile: dict[str, Any] = {}
        for name in ("balanced", "fast-feedback", "exploratory"):
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


def _signal_backtest_payload(adapter: SfoForecasterAdapter, db_path: Path) -> dict[str, Any]:
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
    settlements = adapter.load_ksfo_daily_highs()
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

    decisions.sort(
        key=lambda row: (
            bool(row.get("approved")),
            _to_float(row.get("quality_score")),
            _to_float(row.get("edge_lcb")),
            _to_float(row.get("edge")),
        ),
        reverse=True,
    )
    decisions = decisions[:24]
    return {
        "available": bool(decisions),
        "source": source,
        "stale_candidates_filtered": stale_filtered,
        "latest_candidates": decisions,
        "charts": {
            "probability_vs_market": _probability_market_points(decisions),
            "edge_by_market_bucket": _edge_by_market_bucket(decisions),
            "quality_distribution": _quality_distribution(decisions),
        },
    }


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
            WHERE realized_pnl IS NOT NULL
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
                   COALESCE(risk_profile, 'balanced') AS risk_profile,
                   market_ticker,
                   UPPER(COALESCE(side, 'YES')) AS side,
                   COUNT(*) AS open_orders
            FROM paper_orders
            WHERE status = 'PAPER_FILLED'
              AND settled_at IS NULL
              AND closed_at IS NULL
            GROUP BY target_date,
                     COALESCE(risk_profile, 'balanced'),
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
                   COUNT(*) AS orders,
                   SUM(CASE WHEN realized_pnl IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) AS losses,
                   SUM(COALESCE(realized_pnl, 0.0)) AS realized_pnl,
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
                   SUM(CASE WHEN realized_pnl IS NOT NULL
                            THEN contracts * cost_per_contract ELSE 0.0 END) AS capital_resolved
            FROM paper_orders
            WHERE status != 'REJECTED'
            GROUP BY COALESCE(risk_profile, 'unknown')
            ORDER BY risk_profile
            """
        ).fetchall()

    open_positions = [
        _paper_row(
            row,
            monitor_marks.get(int(row["id"]))
            or decision_marks.get((row["market_ticker"], _side_from_row(row))),
            monitor,
        )
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
    win_count = sum(1 for row in closed_rows if _to_float(row["realized_pnl"]) > 0)
    loss_count = sum(1 for row in closed_rows if _to_float(row["realized_pnl"]) < 0)
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
        "profiles": _profiles_with_scanners(
            [_profile_summary_row(row) for row in profile_rows],
            scanning_profiles,
        ),
    }


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
    items.append("Keep live AWS execution pinned to LSTM until both accuracy and market gates pass.")
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
    latest_target = _status_target_date(
        latest_targets,
        entry_block_reason=entry_block_reason,
    ) or _target_from_signal(trading_signal)
    raw_count = backtest["counts"]["raw_signals"]
    settled_count = backtest["counts"]["settled_signals"]
    small_sample = settled_count < 30
    alerts = _strategy_alerts(
        paper=paper,
        entry_block_reason=entry_block_reason,
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
                SELECT d.target_date, MAX(d.created_at) AS created_at
                FROM decision_snapshots d
                JOIN recent_targets rt ON rt.target_date = d.target_date
                WHERE d.market_ticker NOT LIKE '%-PAPER%'
                GROUP BY d.target_date
            )
            SELECT d.*
            FROM decision_snapshots d
            JOIN latest_by_target latest
              ON latest.target_date = d.target_date
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
    return {
        "created_at": row["created_at"],
        "target_date": row["target_date"],
        "ticker": row["market_ticker"],
        "market_available": not _is_probability_only_ticker(row["market_ticker"]),
        "label": row["label"],
        "side": row["side"],
        "risk_profile": _row_risk_profile(row) or "unknown",
        "approved": approved,
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
                   spread, market_probability, trade_quality_score
            FROM decision_snapshots
            ORDER BY created_at DESC, id DESC
            LIMIT 5000
            """
        ).fetchall()

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
            "quality_score": _round(row["trade_quality_score"], 1),
        }
    return marks


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


def _net_exit_per_contract(bid: float, contracts: float) -> float:
    if bid <= 0 or bid >= 1 or contracts <= 0:
        return 0.0
    return bid - quadratic_fee_average_per_contract(bid, contracts)


def _exit_bid_for_net(target_net: float, contracts: float) -> float | None:
    if contracts <= 0:
        return None
    if target_net <= _net_exit_per_contract(0.01, contracts):
        return _round(0.01, 4)
    if target_net > _net_exit_per_contract(0.99, contracts):
        return None
    lo = 0.01
    hi = 0.99
    for _ in range(48):
        mid = (lo + hi) / 2.0
        if _net_exit_per_contract(mid, contracts) >= target_net:
            hi = mid
        else:
            lo = mid
    return _round(hi, 4)


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
        quadratic_fee_average_per_contract(current_bid, contracts)
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
    take_profit_net = cost_per_contract * (1.0 + take_profit)
    stop_loss_net = max(0.0, cost_per_contract * (1.0 - stop_loss))
    mark_status = _position_mark_status(unrealized_pnl, unrealized_roi, monitor, side)
    if row["status"] == "PAPER_LIMIT_RESTING":
        mark_status = {
            "status": "LIMIT_RESTING",
            "label": "Limit pending",
            "tone": "warn",
            "monitor_action": "LIMIT_RESTING",
        }
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "target_date": row["target_date"],
        "ticker": row["market_ticker"],
        "label": row["label"],
        "side": side,
        "status": row["status"],
        "risk_profile": _row_risk_profile(row),
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
        "take_profit_pnl": _round(risk * take_profit, 2),
        "stop_loss_pnl": _round(-(risk * stop_loss), 2),
        "take_profit_net_exit": _round(take_profit_net, 4),
        "stop_loss_net_exit": _round(stop_loss_net, 4),
        "take_profit_bid": _exit_bid_for_net(take_profit_net, contracts),
        "stop_loss_bid": _exit_bid_for_net(stop_loss_net, contracts),
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


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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


def _json_list(value: object) -> list[str]:
    if not value:
        return []
    try:
        payload = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        return [str(item) for item in payload]
    return []


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
