from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .._util import (
    _db_table_exists,
    _load_json_optional,
    _round,
    _to_float,
)
from ..config import (
    DEFAULT_DB_PATH,
    DEFAULT_FORECASTER_ROOT,
    StrategyConfig,
    strategy_config_for_profile,
)
from ..db import PaperStore
from ..dataset_research import build_dataset_research as build_dataset_research_payload
from ..forecast import ForecastDataError, SfoForecasterAdapter
from ..forecast_scorecards import build_forecast_scorecards
from ..replay import replay_from_database
from ..summary import build_paper_summary
from . import CHALLENGER_CALIBRATION_SOURCE
from .calibration import (
    _calibration_payload,
    _comparison_summary,
    _config_rescore_payload,
    _prediction_replay_payload,
    _research_shadow_payload,
    _signal_backtest_payload,
    _signal_quality_payload,
)
from .dataset_summary import _dataset_research_summary
from .forecast_health import _forecast_health_payload
from .paper_card import _paper_payload
from .profiles import _default_profile, _profile_views
from .readiness import _live_frequency_tuning_payload, _real_money_readiness_payload
from .status_alerts import _status_payload


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
