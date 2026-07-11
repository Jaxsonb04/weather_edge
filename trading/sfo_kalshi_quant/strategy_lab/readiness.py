from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .._util import (
    _date_from_string,
    _db_table_exists,
    _env_float,
    _json_list,
    _json_object,
    _load_json_optional,
    _null_metric,
    _parse_timestamp,
    _round,
    _round_dict,
    _row_value as _sqlite_row_value,
    _table_exists,
    _to_float,
)
from ..backtest import run_walk_forward_calibration_backtest
from ..backtest_rescore import compute_real_money_readiness, run_rescore
from ..cities import CITIES
from ..config import (
    DEFAULT_DB_PATH,
    DEFAULT_FORECASTER_ROOT,
    SFO_TZ,
    StrategyConfig,
    normalize_risk_profile_name,
    strategy_config_for_profile,
)
from ..db import PaperStore
from ..dataset_research import (
    DEFAULT_MIN_AFTER_COST_TRADES,
    DEFAULT_MIN_MATCHED_ROWS,
    build_dataset_research as build_dataset_research_payload,
)
from ..exits import (
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
from ..fees import quadratic_fee_average_per_contract
from ..forecast import ForecastDataError, SfoForecasterAdapter
from ..forecast_scorecards import build_forecast_scorecards
from ..live_execution import LiveExecutionPolicy, readiness_status_from_checks
from ..research_shadow import build_research_shadow_report
from ..replay import replay_from_database
from ..settlement_day import settlement_today
from ..settlement_truth import is_pre_resolution_decision as _is_strategy_pre_resolution
from ..summary import build_paper_summary
from ..synthetic_blend import build_synthetic_blend_calibration
from . import (
    ACTIVE_CALIBRATION_SOURCE,
    CHALLENGER_CALIBRATION_SOURCE,
    DEFAULT_MODEL_VETO_BUFFER,
    DEFAULT_MODEL_VETO_MAX_LOSS_PCT,
    EXPERIMENTAL_PROFILES,
    FORECAST_HEALTH_MAX_CLISFO_LAG_DAYS,
    FORECAST_HEALTH_MAX_EMOS_AGE,
    FORECAST_HEALTH_MAX_NWP_AGE,
    FORECAST_HEALTH_MIN_NWP_MODELS,
    FORECAST_HEALTH_ROLLING_DAYS,
    FORECAST_LEAD_MODE_LABELS,
    MIN_CLEAN_WINNER_SAMPLE,
    PRIMARY_PROFILE,
)

_sqlite_table_exists = _table_exists

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

