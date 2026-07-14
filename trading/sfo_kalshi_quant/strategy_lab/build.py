from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
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
from ..account import (
    RESEARCH_ACCOUNT_ID,
    SHARED_ACCOUNT_ID,
    WEEKLY_GOAL_TZ,
    WEEKLY_RETURN_TARGET,
)
from ..db import PaperStore
from ..dataset_research import build_dataset_research as build_dataset_research_payload
from ..forecast import ForecastDataError, SfoForecasterAdapter
from ..forecast_scorecards import build_forecast_scorecards
from ..maker_fills import EXECUTION_MODEL_VERSION
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
    chronological_replay_full = replay_from_database(
        db_path,
        settlements,
        initial_capital=cfg.paper_bankroll,
    )
    chronological_replay = _bounded_replay(chronological_replay_full)
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
        "schema_version": 2,
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
        "_private_evidence": {
            "generated_at": datetime.now(UTC).isoformat(),
            "chronological_replay": chronological_replay_full,
        },
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

    public_payload = {key: value for key, value in payload.items() if not key.startswith("_private_")}
    text = json.dumps(public_payload, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    private = payload.get("_private_evidence")
    if isinstance(private, dict):
        private_path = path.with_name("strategy_research_evidence.private.json")
        private_tmp = private_path.with_name(f".{private_path.name}.tmp")
        private_tmp.write_text(
            json.dumps(private, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(private_tmp, private_path)


def _bounded_replay(replay: dict[str, Any], *, event_limit: int = 200) -> dict[str, Any]:
    events = list(replay.get("events") or [])
    return {
        **replay,
        "events": events[-event_limit:],
        "events_total": len(events),
        "events_truncated": max(0, len(events) - event_limit),
        "private_evidence_artifact": "strategy_research_evidence.private.json",
    }


def _accounting_payload(
    daily_summary: dict[str, Any],
    paper: dict[str, Any],
    *,
    db_path: Path,
) -> dict[str, Any]:
    """Publish account-scoped balances; never mix live cash with research P&L."""

    if not db_path.exists():
        return {
            "schema_version": 2,
            "available": False,
            "reason": "paper account database unavailable",
            "accounts": {},
        }

    store = PaperStore(db_path, init=False)
    live = _account_snapshot(store, SHARED_ACCOUNT_ID, role="live")
    research = _account_snapshot(store, RESEARCH_ACCOUNT_ID, role="research")
    if live is None:
        return {
            "schema_version": 2,
            "available": False,
            "reason": "live paper account unavailable",
            "accounts": {},
        }
    accounts: dict[str, Any] = {"live": live}
    if research is not None:
        accounts["research"] = research
    combined = _combined_account(live, research) if research is not None else None
    goal = _weekly_goal_payload(store, live)

    # Keep the v1 headline keys for one release. They are aliases of the LIVE
    # account only; callers can no longer accidentally combine research P&L
    # with live cash.
    return {
        "schema_version": 2,
        "available": True,
        "accounts": accounts,
        "combined": combined,
        "goal": goal,
        "account_id": live["account_id"],
        "accounting_cohort": "account_scoped_v4",
        "initial_capital": live["initial_equity"],
        "all_time_realized_pnl": live["realized_pnl"],
        "window_realized_pnl": goal["weekly_realized_pnl"],
        "realized_equity": live["realized_equity"],
        "cash_balance": live["cash_balance"],
        "reservations": live["reservations"],
        "available_cash": live["available_cash"],
        "open_cost_basis": live["open_cost_basis"],
        "unrealized_pnl": live["unrealized_pnl"],
        "marked_equity": live["marked_equity"],
        "mark_coverage": live["mark_coverage"],
        "resolved_capital": live["resolved_capital"],
        "return_on_initial_capital": live["return_on_initial_capital"],
        "roi_on_resolved_capital": live["roi_on_resolved_capital"],
        "profile_attributed_pnl": live["realized_pnl"],
        "reconciliation_status": live["reconciliation_status"],
        "reconciliation_difference": live["reconciliation_difference"],
    }


def _account_snapshot(
    store: PaperStore,
    account_id: str,
    *,
    role: str,
) -> dict[str, Any] | None:
    state = store._account_state(account_id)
    if state is None:
        return None
    open_statuses = (
        "PAPER_FILLED",
        "PAPER_PARTIALLY_FILLED",
        "PAPER_PARTIAL_EXPIRED",
    )
    placeholders = ",".join("?" for _ in open_statuses)
    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        resolved = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0), "
            "COALESCE(SUM(contracts * cost_per_contract),0) FROM paper_orders "
            "WHERE account_id=? AND status IN ('PAPER_SETTLED','PAPER_CLOSED')",
            (account_id,),
        ).fetchone()
        open_rows = conn.execute(
            f"SELECT id FROM paper_orders WHERE account_id=? "
            f"AND status IN ({placeholders}) AND settled_at IS NULL AND closed_at IS NULL",
            (account_id, *open_statuses),
        ).fetchall()
        unrealized_rows: list[sqlite3.Row] = []
        if open_rows and _db_table_exists(store.db_path, "paper_monitor_snapshots"):
            unrealized_rows = conn.execute(
                f"""
                SELECT p.id, m.unrealized_pnl
                FROM paper_orders p
                LEFT JOIN paper_monitor_snapshots m ON m.id = (
                    SELECT m2.id FROM paper_monitor_snapshots m2
                    WHERE m2.order_id=p.id ORDER BY m2.created_at DESC, m2.id DESC LIMIT 1
                )
                WHERE p.account_id=? AND p.status IN ({placeholders})
                  AND p.settled_at IS NULL AND p.closed_at IS NULL
                """,
                (account_id, *open_statuses),
            ).fetchall()
    open_count = len(open_rows)
    marked_count = sum(row["unrealized_pnl"] is not None for row in unrealized_rows)
    marks_complete = open_count == 0 or marked_count == open_count
    unrealized = (
        sum(float(row["unrealized_pnl"] or 0.0) for row in unrealized_rows)
        if marks_complete and open_count > 0
        else (0.0 if open_count == 0 else None)
    )
    realized_equity = _to_float(state["realized_equity"])
    initial = _to_float(state["initial_capital"])
    realized_pnl = realized_equity - initial
    resolved_pnl = float(resolved[0] or 0.0)
    resolved_capital = float(resolved[1] or 0.0)
    identity_difference = (
        _to_float(state["available_cash"])
        + _to_float(state["reservations"])
        + _to_float(state["open_cost_basis"])
        - realized_equity
    )
    return {
        "account_id": account_id,
        "role": role,
        "verification_scope": "exec-v3 fills only; legacy outcomes retained as unverified",
        "initial_equity": _round(initial, 2),
        "cash_balance": _round(state["cash_balance"], 2),
        "available_cash": _round(state["available_cash"], 2),
        "reservations": _round(state["reservations"], 2),
        "open_cost_basis": _round(state["open_cost_basis"], 2),
        "open_positions": open_count,
        "realized_equity": _round(realized_equity, 2),
        "realized_pnl": _round(realized_pnl, 2),
        "booked_resolved_pnl": _round(resolved_pnl, 2),
        "unrealized_pnl": _round(unrealized, 2) if unrealized is not None else None,
        "marked_equity": (
            _round(realized_equity + unrealized, 2) if unrealized is not None else None
        ),
        "mark_coverage": (
            "complete_no_open_positions" if open_count == 0
            else ("complete" if marks_complete else ("partial" if marked_count else "unavailable"))
        ),
        "resolved_capital": _round(resolved_capital, 2),
        "return_on_initial_capital": (
            _round(realized_pnl / initial, 6) if initial > 0 else None
        ),
        "roi_on_resolved_capital": (
            _round(resolved_pnl / resolved_capital, 6) if resolved_capital > 0 else None
        ),
        "reconciliation_status": (
            "reconciled" if abs(identity_difference) < 0.005 else "mismatch"
        ),
        "reconciliation_difference": _round(identity_difference, 2),
    }


def _combined_account(live: dict[str, Any], research: dict[str, Any]) -> dict[str, Any]:
    marked = (
        _to_float(live["marked_equity"]) + _to_float(research["marked_equity"])
        if live["marked_equity"] is not None and research["marked_equity"] is not None
        else None
    )
    return {
        "account_id": "paper-combined",
        "role": "combined_diagnostic_only",
        "initial_equity": _round(_to_float(live["initial_equity"]) + _to_float(research["initial_equity"]), 2),
        "cash_balance": _round(_to_float(live["cash_balance"]) + _to_float(research["cash_balance"]), 2),
        "available_cash": _round(_to_float(live["available_cash"]) + _to_float(research["available_cash"]), 2),
        "reservations": _round(_to_float(live["reservations"]) + _to_float(research["reservations"]), 2),
        "open_cost_basis": _round(_to_float(live["open_cost_basis"]) + _to_float(research["open_cost_basis"]), 2),
        "realized_equity": _round(_to_float(live["realized_equity"]) + _to_float(research["realized_equity"]), 2),
        "realized_pnl": _round(_to_float(live["realized_pnl"]) + _to_float(research["realized_pnl"]), 2),
        "unrealized_pnl": (
            _round(_to_float(live["unrealized_pnl"]) + _to_float(research["unrealized_pnl"]), 2)
            if live["unrealized_pnl"] is not None and research["unrealized_pnl"] is not None
            else None
        ),
        "marked_equity": _round(marked, 2) if marked is not None else None,
        "label": "Live plus research; never used for the weekly goal or readiness",
    }


def _weekly_goal_payload(store: PaperStore, live: dict[str, Any]) -> dict[str, Any]:
    now_local = datetime.now(WEEKLY_GOAL_TZ)
    start_local = (now_local - timedelta(days=now_local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_local = start_local + timedelta(days=7)
    with store.connect() as conn:
        resolved_rows = conn.execute(
            "SELECT COALESCE(closed_at,settled_at), COALESCE(realized_pnl,0) "
            "FROM paper_orders WHERE account_id=? "
            "AND status IN ('PAPER_SETTLED','PAPER_CLOSED') "
            "AND COALESCE(closed_at,settled_at) IS NOT NULL "
            "ORDER BY COALESCE(closed_at,settled_at), id",
            (SHARED_ACCOUNT_ID,),
        ).fetchall()
        boundary_row = conn.execute(
            "SELECT MIN(created_at) FROM paper_account_ledger "
            "WHERE event_type='EXECUTION_SEMANTICS_TRANSITION' "
            "AND idempotency_key=?",
            (f"execution:{EXECUTION_MODEL_VERSION}",),
        ).fetchone()

    evidence_boundary = boundary_row[0] if boundary_row else None
    first_full_evidence_week: datetime | None = None
    if evidence_boundary:
        try:
            boundary_at = datetime.fromisoformat(
                str(evidence_boundary).replace("Z", "+00:00")
            )
            if boundary_at.tzinfo is None:
                boundary_at = boundary_at.replace(tzinfo=UTC)
            boundary_local = boundary_at.astimezone(WEEKLY_GOAL_TZ)
            boundary_week = (
                boundary_local - timedelta(days=boundary_local.weekday())
            ).replace(hour=0, minute=0, second=0, microsecond=0)
            first_full_evidence_week = (
                boundary_week
                if boundary_local == boundary_week
                else boundary_week + timedelta(days=7)
            )
        except ValueError:
            pass

    pnl_by_week: dict[datetime, float] = {}
    for resolved_at, realized_pnl in resolved_rows:
        try:
            resolved = datetime.fromisoformat(str(resolved_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=UTC)
        resolved_local = resolved.astimezone(WEEKLY_GOAL_TZ)
        week_start = (resolved_local - timedelta(days=resolved_local.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        pnl_by_week[week_start] = pnl_by_week.get(week_start, 0.0) + float(
            realized_pnl or 0.0
        )

    weekly_pnl = pnl_by_week.get(start_local, 0.0)
    starting_equity = _to_float(live["realized_equity"]) - weekly_pnl
    target_pnl = starting_equity * WEEKLY_RETURN_TARGET
    weekly_return = weekly_pnl / starting_equity if starting_equity > 0 else None

    # The active week never counts toward the stability streak. Walk backward
    # from its opening realized equity and stop at the first missing or
    # sub-target completed week.
    completed_week_success_streak = 0
    ending_equity = starting_equity
    cursor = start_local - timedelta(days=7)
    while (
        first_full_evidence_week is not None
        and cursor >= first_full_evidence_week
        and cursor in pnl_by_week
    ):
        completed_pnl = pnl_by_week[cursor]
        opening_equity = ending_equity - completed_pnl
        completed_return = (
            completed_pnl / opening_equity if opening_equity > 0 else None
        )
        if completed_return is None or completed_return + 1e-12 < WEEKLY_RETURN_TARGET:
            break
        completed_week_success_streak += 1
        ending_equity = opening_equity
        cursor -= timedelta(days=7)

    return {
        "metric": "weekly_realized_return",
        "account_id": SHARED_ACCOUNT_ID,
        "timezone": str(WEEKLY_GOAL_TZ),
        "week_starts": "Monday 00:00",
        "period_start": start_local.isoformat(),
        "period_end": end_local.isoformat(),
        "target_return": WEEKLY_RETURN_TARGET,
        "starting_realized_equity": _round(starting_equity, 2),
        "current_realized_equity": live["realized_equity"],
        "weekly_realized_pnl": _round(weekly_pnl, 2),
        "weekly_realized_return": _round(weekly_return, 6) if weekly_return is not None else None,
        "target_realized_pnl": _round(target_pnl, 2),
        "remaining_pnl": _round(max(0.0, target_pnl - weekly_pnl), 2),
        "achieved": bool(weekly_return is not None and weekly_return >= WEEKLY_RETURN_TARGET),
        "completed_week_success_streak": completed_week_success_streak,
        "evidence_boundary": evidence_boundary,
        "first_full_evidence_week": (
            first_full_evidence_week.isoformat() if first_full_evidence_week else None
        ),
        "current_week_evidence_qualified": bool(
            first_full_evidence_week is not None and start_local >= first_full_evidence_week
        ),
        "execution_model_version": EXECUTION_MODEL_VERSION,
        "excludes": ["research-shadow", "unrealized-marks"],
        "disclaimer": "Research objective, not a guaranteed return; risk gates remain binding.",
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
