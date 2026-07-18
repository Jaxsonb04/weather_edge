from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .._util import (
    _db_table_exists,
    _json_list,
    _null_metric,
    _round,
    _round_dict,
    _row_value as _sqlite_row_value,
    _to_float,
)
from ..backtest import run_walk_forward_calibration_backtest
from ..backtest_rescore import run_rescore
from ..config import (
    StrategyConfig,
    normalize_risk_profile_name,
    strategy_config_for_profile,
)
from ..db import PaperStore
from ..forecast import ForecastDataError, SfoForecasterAdapter
from ..research_shadow import build_research_shadow_report
from ..settlement_truth import is_pre_resolution_decision as _is_strategy_pre_resolution
from ..synthetic_blend import build_synthetic_blend_calibration
from . import FORECAST_LEAD_MODE_LABELS, MIN_CLEAN_WINNER_SAMPLE, PRIMARY_PROFILE
from .consensus_offline import _market_consensus_payload
from .paper_card import _row_risk_profile
from .profiles import (
    _edge_by_market_bucket,
    _probability_market_points,
    _profile_key,
    _profile_sort_key,
    _quality_distribution,
)
from .status_alerts import _decision_reason, _is_probability_only_ticker


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
    """Replay each profile's recorded decision snapshots under its current config.

    Live and research scans may construct different forecast probabilities, so
    each profile is replayed only from snapshots recorded for that normalized
    profile. This is diagnostic only and never places, closes, or settles orders.
    """

    evidence_kind = "profile_specific_snapshot_replay"
    empty = {
        "available": False,
        "evidence_kind": evidence_kind,
        "by_profile": {},
        "settlement_days": 0,
        "sampled_snapshots": 0,
    }
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
        rows_by_profile: dict[str, list[sqlite3.Row]] = {"live": [], "research": []}
        for row in rows:
            profile = normalize_risk_profile_name(_row_risk_profile(row) or "live")
            rows_by_profile[profile].append(row)
        by_profile: dict[str, Any] = {}
        for name in ("live", "research"):
            cfg = strategy_config_for_profile(name)
            result = run_rescore(
                rows_by_profile[name],
                settlements,
                cfg,
                bankroll=cfg.paper_bankroll,
                bootstrap_samples=1000,
            )
            # Drop the per-day list from the published artifact: the card shows
            # rollups (candidate vs recorded, per-side, per-cohort, CI), and
            # three profiles' worth of daily rows would bloat the JSON.
            result.pop("per_day", None)
            result["evidence_kind"] = evidence_kind
            by_profile[name] = result
        return {
            "available": True,
            "evidence_kind": evidence_kind,
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
