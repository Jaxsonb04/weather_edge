from __future__ import annotations

import hashlib
import json
import math
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
    SFO_TZ,
    StrategyConfig,
    config_for_city,
    strategy_config_for_profile,
)
from ..cities import CITIES
from ..account import (
    INITIAL_CAPITAL,
    LIVE_STABILITY_ACCOUNT_ID,
    RESEARCH_ACCOUNT_ID,
    SHARED_ACCOUNT_ID,
    WEEKLY_GOAL_TZ,
    WEEKLY_RETURN_TARGET,
    strategy_fingerprint,
)
from ..db import PaperStore
from ..dataset_research import build_dataset_research as build_dataset_research_payload
from ..forecast import ForecastDataError, SfoForecasterAdapter
from ..forecast_scorecards import build_forecast_scorecards
from ..maker_fills import EXECUTION_MODEL_VERSION
from ..research_policy import (
    MOTION_POLICY,
    TARGET_POLICY,
    TARGET_POLICY_V1,
    TARGET_POLICY_V2,
    TARGET_POLICY_V3,
    TARGET_POLICY_V4,
    TARGET_POLICY_V5,
)
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
    fast_publication = _env_truthy("SFO_STRATEGY_FAST_PUBLICATION")
    current_source_sha = _analysis_source_sha(forecaster_root)
    current_config_fingerprint = _analysis_config_fingerprint(
        calibration_min_train=calibration_min_train
    )
    analysis_cache = (
        _load_analysis_cache(
            forecaster_root,
            expected_source_sha=current_source_sha,
            expected_config_fingerprint=current_config_fingerprint,
        )
        if fast_publication
        else None
    )
    adapter = SfoForecasterAdapter(forecaster_root)
    trading_signal = _load_json_optional(forecaster_root / "trading_signal.json")
    dataset_research = _load_or_build_dataset_research(
        forecaster_root=forecaster_root,
        db_path=db_path,
        allow_build=not fast_publication,
    )
    settlements: dict[object, float] = {}
    sampled_decision_rows: list[sqlite3.Row] = []
    try:
        settlements = adapter.load_cli_settlement_truth()
    except (ForecastDataError, FileNotFoundError, KeyError, ValueError, sqlite3.Error):
        pass
    if (
        not fast_publication
        and db_path.exists()
        and _db_table_exists(db_path, "decision_snapshots")
    ):
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
    deferred_reason = (
        "Deferred from the recurring public artifact; run the offline full "
        "research job for historical analytics."
    )
    if fast_publication:
        if analysis_cache is not None:
            backtest = dict(analysis_cache["backtest_summary"])
            config_rescore = dict(analysis_cache["config_rescore"])
        else:
            backtest = _deferred_backtest_payload(deferred_reason)
            config_rescore = _deferred_config_rescore_payload(deferred_reason)
    else:
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
    if fast_publication:
        chronological_replay = _cached_analysis_section(
            analysis_cache,
            "chronological_replay",
            reason=deferred_reason,
        )
        chronological_replay_full = chronological_replay
    else:
        live_config = strategy_config_for_profile("live")
        entry_mode = _current_paper_entry_mode()
        chronological_replay_full = replay_from_database(
            db_path,
            settlements,
            initial_capital=cfg.paper_bankroll,
            required_strategy_fingerprint={
                city.series_ticker: strategy_fingerprint(
                    config_for_city(live_config, city),
                    entry_mode=entry_mode,
                )
                for city in CITIES
            },
        )
        chronological_replay = _bounded_replay(chronological_replay_full)
    readiness_now = datetime.now(UTC)
    if fast_publication:
        analysis_generated_at = (
            str(analysis_cache["analysis_generated_at"])
            if analysis_cache is not None
            else None
        )
    else:
        analysis_generated_at = readiness_now.isoformat()
    # These sections combine the expensive rescore with fresh calibration,
    # replay, policy environment, and pilot/account state. Cache only the
    # expensive input and always derive the public verdict on this refresh.
    real_money_readiness = _real_money_readiness_payload(
        config_rescore,
        active_calibration,
        chronological_replay,
        analysis_generated_at=analysis_generated_at,
        now=readiness_now,
    )
    live_frequency_tuning = _live_frequency_tuning_payload(
        config_rescore, strategy_config_for_profile("live")
    )
    if fast_publication:
        research_shadow = _cached_analysis_section(
            analysis_cache,
            "research_shadow",
            reason=deferred_reason,
        )
    else:
        research_shadow = _research_shadow_payload(
            adapter,
            db_path,
            settlements=settlements,
        )
    signal_quality = _signal_quality_payload(
        db_path,
        trading_signal,
        prefer_runtime_signal=fast_publication,
    )
    paper = _paper_payload(db_path)
    forecast_health = _forecast_health_payload(forecaster_root, config=cfg)
    if fast_publication:
        forecast_scorecards = _cached_analysis_section(
            analysis_cache,
            "forecast_scorecards",
            reason=deferred_reason,
        )
    else:
        forecast_scorecards = build_forecast_scorecards(
            forecaster_root / "weather.db"
        )
    daily_summary = _mark_deferred_decision_counts(
        _daily_summary_payload(
            db_path=db_path,
            forecaster_root=forecaster_root,
            config=cfg,
            allow_decision_scan=not fast_publication,
            decision_analysis=(
                analysis_cache.get("daily_summary_analysis")
                if isinstance(analysis_cache, dict)
                else None
            ),
        )
    )
    accounting = _accounting_payload(daily_summary, paper, db_path=db_path)
    if accounting.get("available") is False:
        accounting_reason = str(
            accounting.get("reason")
            or "fresh active paper ledgers are unavailable"
        )
        real_money_readiness = {
            "available": False,
            "profile": "live",
            "status": "NOT_READY",
            "status_reasons": [accounting_reason],
            "reason": (
                "live readiness suppressed because account validation failed: "
                f"{accounting_reason}"
            ),
        }
    profiles = _bind_accounting_to_profiles(
        _profile_views(
            daily_summary=daily_summary,
            paper=paper,
            signal_quality=signal_quality,
        ),
        accounting,
    )
    # The profile views re-derive their own counters from the parent summary
    # with ``0`` defaults, so the deferral marker has to be re-applied after
    # they are built or each book would re-publish the zeros.
    profiles = _clear_deferred_profile_counts(profiles, daily_summary)
    status = _status_payload(
        config=cfg,
        db_path=db_path,
        trading_signal=trading_signal,
        backtest=backtest,
        signal_quality=signal_quality,
        paper=paper,
        forecast_health=forecast_health,
    )
    generated_at = readiness_now.isoformat()
    if not fast_publication:
        analysis_cache = {
            "schema_version": 2,
            "source_sha": current_source_sha,
            "config_fingerprint": current_config_fingerprint,
            "analysis_generated_at": analysis_generated_at,
            "backtest_summary": backtest,
            "config_rescore": config_rescore,
            "chronological_replay": chronological_replay,
            "research_shadow": research_shadow,
            "forecast_scorecards": forecast_scorecards,
            "daily_summary_analysis": _daily_summary_analysis(
                daily_summary,
                analysis_generated_at=analysis_generated_at,
            ),
        }

    payload = {
        "schema_version": 3,
        "available": True,
        "mode": "paper_research_only",
        "live_orders_enabled": False,
        "default_profile": _default_profile(profiles),
        "generated_at": generated_at,
        "analysis_generated_at": analysis_generated_at,
        "analysis_source_sha": (
            analysis_cache.get("source_sha")
            if isinstance(analysis_cache, dict)
            else None
        ),
        "source_of_truth": "AWS EC2 runtime artifacts after sync and refresh",
        "publication_mode": "fast_public" if fast_publication else "full_research",
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
        "research_daily_target": paper.get("research_daily_target") or {
            "available": False,
            "reason": "target research has not activated yet",
        },
        "paper_trading": paper,
        "profiles": profiles,
        "dataset_research": _dataset_research_summary(dataset_research),
        "research_notes": _research_notes(),
        "disclaimer": (
            "Paper-trading research only — no real-money orders are ever placed. "
            "Forecasts use per-city NWP/EMOS; San Francisco can add its residual-calibration blend "
            "when optional inputs are available, and each target publishes its current served method."
        ),
    }
    # The frequent builder treats the cache as read-only input. Emitting a
    # staged copy would let an in-flight fast cycle replace a newer full rescore
    # after its atomic promotion.
    if not fast_publication:
        payload["_private_evidence"] = {
            "generated_at": generated_at,
            "analysis_generated_at": analysis_generated_at,
            "source_sha": current_source_sha,
            "config_fingerprint": current_config_fingerprint,
            "chronological_replay": chronological_replay_full,
        }
        payload["_private_analysis_cache"] = analysis_cache
    return payload


def _deferred_backtest_payload(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "metrics_available": False,
        "sample_mode": "entry-per-market-side",
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
        "metrics": {},
        "quality_buckets": [],
        "reason": reason,
    }


def _deferred_config_rescore_payload(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "evidence_kind": "profile_specific_snapshot_replay",
        "by_profile": {},
        "settlement_days": 0,
        "sampled_snapshots": 0,
        "reason": reason,
    }


# The recurring public cycle is permanently bounded (run_publication_cycle.sh
# forces the fast flag), so the decision journal is never scanned on this path.
# ``build_paper_summary`` still returns its empty accumulators, and publishing
# those as 0 next to real order counts reads as a measured "0 of 0 approved".
# The artifact has to say the number does not exist instead of printing a zero.
DEFERRED_DECISION_ANALYTICS_REASON = (
    "Decision-journal analytics are not computed on the bounded recurring "
    "public path; these fields are unpopulated, not zero."
)

_DEFERRED_DAY_COUNT_FIELDS = ("signals", "approved_signals")


def _deferred_gate_behavior_payload(
    gate: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Keep the section's shape but drop counters that were never measured.

    Every value under ``gate_behavior`` is decision-journal derived, so on the
    deferred path each count becomes null and each collection empties. Walking
    the existing keys instead of listing them keeps the summary-level and
    profile-level shapes stable as either one grows.
    """

    emptied: dict[str, Any] = {}
    for key, value in gate.items():
        if isinstance(value, bool):
            emptied[key] = value
        elif isinstance(value, (int, float)):
            emptied[key] = None
        elif isinstance(value, list):
            emptied[key] = []
        elif isinstance(value, dict):
            emptied[key] = {}
        else:
            emptied[key] = value
    return {
        **emptied,
        "available": False,
        "analysis_deferred": True,
        "reason": reason,
    }


def _decision_analytics_deferred(daily_summary: dict[str, Any]) -> bool:
    analytics = daily_summary.get("decision_analytics")
    return (
        isinstance(analytics, dict)
        and analytics.get("status") == "deferred"
    )


def _cached_counts_stale_from(daily_summary: dict[str, Any]) -> str | None:
    analytics = daily_summary.get("decision_analytics")
    if not isinstance(analytics, dict) or analytics.get("status") != "cached":
        return None
    raw = analytics.get("analysis_generated_at")
    if raw in (None, ""):
        return None
    try:
        generated = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=UTC)
    return generated.astimezone(SFO_TZ).date().isoformat()


def _day_without_deferred_counts(row: dict[str, Any]) -> dict[str, Any]:
    """Null one published day's decision counters, per-profile rows included."""

    if not isinstance(row, dict):
        return row
    cleared = {
        **row,
        **{field: None for field in _DEFERRED_DAY_COUNT_FIELDS if field in row},
    }
    profiles = row.get("profiles")
    if isinstance(profiles, dict):
        cleared["profiles"] = {
            name: (
                {
                    **profile,
                    **{
                        field: None
                        for field in _DEFERRED_DAY_COUNT_FIELDS
                        if field in profile
                    },
                }
                if isinstance(profile, dict)
                else profile
            )
            for name, profile in profiles.items()
        }
    return cleared


def _mark_deferred_decision_counts(daily_summary: dict[str, Any]) -> dict[str, Any]:
    """Replace unpopulated decision counters with an explicit unavailable marker.

    Only fires when the summary itself reports the journal as deferred, so a
    fresh full-research build and a cache-backed fast build both keep their real
    measured counts.
    """

    deferred = _decision_analytics_deferred(daily_summary)
    stale_from = _cached_counts_stale_from(daily_summary)
    if daily_summary.get("available") is not True or (not deferred and stale_from is None):
        return daily_summary
    payload = dict(daily_summary)
    if deferred:
        gate = payload.get("gate_behavior")
        if isinstance(gate, dict):
            payload["gate_behavior"] = _deferred_gate_behavior_payload(
                gate, DEFERRED_DECISION_ANALYTICS_REASON
            )
        if isinstance(payload.get("model_vs_market"), dict):
            payload["model_vs_market"] = {
                "available": False,
                "analysis_deferred": True,
                "reason": DEFERRED_DECISION_ANALYTICS_REASON,
            }
    elif stale_from is not None:
        analytics = dict(payload.get("decision_analytics") or {})
        analytics["counts_stale_from"] = stale_from
        analytics["reason"] = (
            f"Decision counts on and after {stale_from} are unavailable from "
            "the older analysis cache."
        )
        payload["decision_analytics"] = analytics
    if "days" in payload:
        payload["days"] = [
            (
                _day_without_deferred_counts(row)
                if deferred
                or (
                    stale_from is not None
                    and isinstance(row, dict)
                    and str(row.get("date") or "") >= stale_from
                )
                else row
            )
            for row in payload["days"] or []
        ]
    return payload


def _clear_deferred_profile_counts(
    profiles: list[dict[str, Any]],
    daily_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    """Carry the same deferral marker into every per-profile book."""

    deferred = _decision_analytics_deferred(daily_summary)
    stale_from = _cached_counts_stale_from(daily_summary)
    if not deferred and stale_from is None:
        return profiles
    rows: list[dict[str, Any]] = []
    for profile in profiles:
        daily = profile.get("daily_summary")
        if not isinstance(daily, dict):
            rows.append(profile)
            continue
        updated = dict(daily)
        gate = updated.get("gate_behavior")
        if deferred and isinstance(gate, dict):
            updated["gate_behavior"] = _deferred_gate_behavior_payload(
                gate, DEFERRED_DECISION_ANALYTICS_REASON
            )
        if "days" in updated:
            updated["days"] = [
                (
                    _day_without_deferred_counts(row)
                    if deferred
                    or (
                        stale_from is not None
                        and isinstance(row, dict)
                        and str(row.get("date") or "") >= stale_from
                    )
                    else row
                )
                for row in updated["days"] or []
            ]
        rows.append({**profile, "daily_summary": updated})
    return rows


def _cached_analysis_section(
    analysis_cache: dict[str, Any] | None,
    field: str,
    *,
    reason: str,
) -> dict[str, Any]:
    if isinstance(analysis_cache, dict) and isinstance(
        analysis_cache.get(field), dict
    ):
        return dict(analysis_cache[field])
    return {
        "available": False,
        "analysis_deferred": True,
        "reason": reason,
    }


def _env_truthy(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _current_paper_entry_mode() -> str:
    """Mirror the scanner's canonical entry mode for strategy identity."""

    raw = os.getenv("PAPER_ENTRY_MODE", "market").strip().lower().replace("_", "-")
    if raw in {"limit", "limit-order", "paper-limit"}:
        return "limit"
    return "market"


def _analysis_source_sha(forecaster_root: Path) -> str | None:
    build_info = _load_json_optional(forecaster_root / "build_info.json")
    if not isinstance(build_info, dict):
        return None
    source_sha = build_info.get("source_sha")
    if not isinstance(source_sha, str) or not source_sha.strip():
        return None
    suffix = ":dirty" if build_info.get("source_dirty") is True else ""
    return f"{source_sha.strip()}{suffix}"


def _analysis_config_fingerprint(*, calibration_min_train: int) -> str:
    entry_mode = _current_paper_entry_mode()
    payload = {
        "cache_contract": "strategy-analysis-v5-exact-city-fingerprints",
        "calibration_min_train": calibration_min_train,
        "entry_mode": entry_mode,
        "execution_model_version": EXECUTION_MODEL_VERSION,
        "live_strategy": strategy_fingerprint(
            strategy_config_for_profile("live"),
            entry_mode=entry_mode,
        ),
        "research_strategy": strategy_fingerprint(
            strategy_config_for_profile("research"),
            entry_mode=entry_mode,
        ),
        "target_policy": TARGET_POLICY.policy_fingerprint,
        "motion_policy": MOTION_POLICY.policy_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()[:24]


def _load_analysis_cache(
    forecaster_root: Path,
    *,
    expected_source_sha: str | None,
    expected_config_fingerprint: str,
) -> dict[str, Any] | None:
    cached = _normalize_analysis_cache(
        _load_json_optional(forecaster_root / "strategy_analysis_cache.json"),
        expected_source_sha=expected_source_sha,
        expected_config_fingerprint=expected_config_fingerprint,
    )
    if cached is not None:
        return cached
    # A legacy public artifact has no immutable source identity. It is an
    # acceptable one-time seed only in an unversioned local environment; a
    # deployed build_info.json requires a source-matched dedicated cache.
    if expected_source_sha is not None:
        return None
    legacy = _load_json_optional(forecaster_root / "strategy_research.json")
    if legacy is None or legacy.get("publication_mode") == "fast_public":
        return None
    return _normalize_analysis_cache(
        legacy,
        expected_source_sha=None,
        expected_config_fingerprint=expected_config_fingerprint,
        legacy_public_artifact=True,
    )


def _normalize_analysis_cache(
    payload: dict[str, Any] | None,
    *,
    expected_source_sha: str | None,
    expected_config_fingerprint: str,
    legacy_public_artifact: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if not legacy_public_artifact and (
        payload.get("schema_version") != 2
        or payload.get("source_sha") != expected_source_sha
        or payload.get("config_fingerprint") != expected_config_fingerprint
    ):
        return None
    generated_at = payload.get(
        "generated_at" if legacy_public_artifact else "analysis_generated_at"
    )
    if not isinstance(generated_at, str) or not generated_at.strip():
        return None
    fields = (
        "backtest_summary",
        "config_rescore",
    )
    if any(not isinstance(payload.get(field), dict) for field in fields):
        return None
    backtest = payload["backtest_summary"]
    counts = backtest.get("counts")
    required_counts = (
        "raw_signals",
        "pre_resolution_signals",
        "deduped_signals",
        "excluded_post_resolution_signals",
        "settled_signals",
    )
    if (
        not isinstance(counts, dict)
        or any(
            isinstance(counts.get(field), bool)
            or not isinstance(counts.get(field), (int, float))
            for field in required_counts
        )
        or not isinstance(payload["config_rescore"].get("by_profile"), dict)
    ):
        return None
    additional_fields = (
        "chronological_replay",
        "research_shadow",
        "forecast_scorecards",
        "daily_summary_analysis",
    )
    if not legacy_public_artifact:
        if any(
            not isinstance(payload.get(field), dict)
            for field in additional_fields
        ):
            return None
        if payload["daily_summary_analysis"].get("available") is not True:
            return None
    normalized = {
        "schema_version": 2,
        "source_sha": expected_source_sha,
        "config_fingerprint": expected_config_fingerprint,
        "analysis_generated_at": generated_at,
        **{field: dict(payload[field]) for field in fields},
    }
    for field in additional_fields:
        if isinstance(payload.get(field), dict):
            normalized[field] = dict(payload[field])
    return normalized


def _daily_summary_payload(
    *,
    db_path: Path,
    forecaster_root: Path,
    config: StrategyConfig,
    allow_decision_scan: bool = True,
    decision_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        payload = build_paper_summary(
            db_path=db_path,
            forecaster_root=forecaster_root,
            config=config,
            days=7,
            allow_decision_scan=allow_decision_scan,
            decision_analysis=decision_analysis,
        )
    except Exception as exc:  # diagnostics artifact must not fail the refresh
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"available": True, **payload}


def _daily_summary_analysis(
    daily_summary: dict[str, Any],
    *,
    analysis_generated_at: str,
) -> dict[str, Any]:
    if daily_summary.get("available") is not True:
        return {
            "available": False,
            "analysis_generated_at": analysis_generated_at,
            "reason": str(
                daily_summary.get("reason")
                or "full daily summary analysis is unavailable"
            ),
        }
    per_day = {}
    for row in daily_summary.get("days") or []:
        key = row.get("date")
        if not isinstance(key, str) or not key:
            continue
        per_day[key] = {
            "signals": int(row.get("signals") or 0),
            "approved": int(row.get("approved_signals") or 0),
            "avg_model_probability": row.get("avg_model_probability"),
            "avg_market_probability": row.get("avg_market_probability"),
            "profiles": {
                str(name): {
                    "signals": int(profile.get("signals") or 0),
                    "approved": int(profile.get("approved_signals") or 0),
                }
                for name, profile in (row.get("profiles") or {}).items()
                if isinstance(profile, dict)
            },
        }
    return {
        "available": True,
        "analysis_generated_at": analysis_generated_at,
        "per_day": per_day,
        "gate_behavior": dict(daily_summary.get("gate_behavior") or {}),
        "model_vs_market": dict(daily_summary.get("model_vs_market") or {}),
        "data_collected": dict(daily_summary.get("data_collected") or {}),
    }


def write_strategy_research(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write (temp + os.replace): the strategy-lab-refresh and
    # forecaster-refresh timers build into the same dir, and the publisher copies
    # this file, so a plain truncate-write could be read half-written.
    import os

    public_payload = {
        key: value
        for key, value in payload.items()
        if not key.startswith("_private_")
    }
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
    analysis = payload.get("_private_analysis_cache")
    if isinstance(analysis, dict):
        analysis_path = path.with_name("strategy_analysis_cache.json")
        analysis_tmp = analysis_path.with_name(f".{analysis_path.name}.tmp")
        analysis_tmp.write_text(
            json.dumps(analysis, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(analysis_tmp, analysis_path)


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
    """Publish the two active ledgers and a separate read-only archive."""

    if not db_path.exists():
        return {
            "schema_version": 3,
            "available": False,
            "reason": "paper account database unavailable",
            "accounts": {},
            "active_ledgers": {},
            "archived_accounts": [],
        }

    store = PaperStore(db_path, init=False)
    live = _account_snapshot(
        store,
        LIVE_STABILITY_ACCOUNT_ID,
        role="live_stability",
        daily_summary=daily_summary,
    )
    research_target = _account_snapshot(
        store,
        TARGET_POLICY.account_id,
        role="research_roi",
        daily_summary=daily_summary,
    )
    active_snapshots_valid = (
        _active_account_snapshot_is_valid(
            store,
            live,
            expected_account_id=LIVE_STABILITY_ACCOUNT_ID,
        )
        and _active_account_snapshot_is_valid(
            store,
            research_target,
            expected_account_id=TARGET_POLICY.account_id,
        )
    )
    if not active_snapshots_valid:
        return {
            "schema_version": 3,
            "available": False,
            "reason": "fresh active paper ledgers invalid or unavailable",
            "accounts": {},
            "active_ledgers": {},
            "archived_accounts": [],
        }

    archive_specs = (
        (
            "legacy_live",
            "legacy-shared-account",
            "Legacy shared paper account",
            SHARED_ACCOUNT_ID,
            "legacy_shared",
            "live-legacy",
        ),
        (
            "legacy_research",
            "legacy-research-shadow-account",
            "Legacy research shadow account",
            RESEARCH_ACCOUNT_ID,
            "legacy_research",
            "research",
        ),
        (
            "research_target_v1",
            "research-target-v1",
            "Research target v1 achieved performance",
            TARGET_POLICY_V1.account_id,
            "research_target_archived",
            "research-target-v1",
        ),
        (
            "research_target_v2",
            "research-target-v2",
            "Research target v2 achieved performance",
            TARGET_POLICY_V2.account_id,
            "research_target_archived",
            "research-target-v2",
        ),
        (
            "research_target_v3",
            "research-target-v3",
            "Research target v3 achieved performance",
            TARGET_POLICY_V3.account_id,
            "research_target_archived",
            "research-target-v3",
        ),
        (
            "research_target_v4",
            "research-target-v4",
            "Research target v4 achieved performance",
            TARGET_POLICY_V4.account_id,
            "research_target_archived",
            "research-target-v4",
        ),
        (
            "research_target_v5",
            "research-target-v5",
            "Research target v5 achieved performance",
            TARGET_POLICY_V5.account_id,
            "research_target_archived",
            "research-target-v5",
        ),
        (
            "research_motion",
            "research-motion",
            "Research motion achieved performance",
            MOTION_POLICY.account_id,
            "research_motion_archived",
            "research-motion",
        ),
    )
    archived_accounts: list[dict[str, Any]] = []
    accounts: dict[str, Any] = {
        "live": live,
        "research_target": research_target,
    }
    for (
        key,
        profile_key,
        label,
        account_id,
        role,
        attribution_profile_key,
    ) in archive_specs:
        snapshot = _account_snapshot(
            store,
            account_id,
            role=role,
            daily_summary=None,
        )
        if snapshot is None:
            continue
        archived = {
            **snapshot,
            "key": key,
            "profile_key": profile_key,
            "attribution_profile_key": attribution_profile_key,
            "label": label,
            "status": (
                "ARCHIVED_SETTLING"
                if (
                    int(snapshot.get("open_positions") or 0) > 0
                    or int(snapshot.get("pending_limit_orders") or 0) > 0
                )
                else "ARCHIVED"
            ),
        }
        archived_accounts.append(archived)
        accounts[key] = archived

    active_ledgers = {
        "live_stability": live,
        "research_roi": research_target,
    }
    combined = _combined_account(live, research_target)
    goal = _weekly_goal_payload(store, live)

    # Headline compatibility aliases continue to mean the active live account
    # only. Archived and research balances never flow through these fields.
    return {
        "schema_version": 3,
        "available": True,
        "accounts": accounts,
        "active_ledgers": active_ledgers,
        "archived_accounts": archived_accounts,
        "combined": combined,
        "goal": goal,
        "account_id": live["account_id"],
        "accounting_cohort": "fresh_ledgers_v1",
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
        "research_accounts_excluded_from_live_goal_and_readiness": [
            RESEARCH_ACCOUNT_ID,
            TARGET_POLICY_V1.account_id,
            TARGET_POLICY_V2.account_id,
            TARGET_POLICY_V3.account_id,
            TARGET_POLICY_V4.account_id,
            TARGET_POLICY_V5.account_id,
            TARGET_POLICY.account_id,
            MOTION_POLICY.account_id,
        ],
    }


def _active_account_snapshot_is_valid(
    store: PaperStore,
    snapshot: dict[str, Any] | None,
    *,
    expected_account_id: str,
) -> bool:
    """Fail publication closed unless a fresh ledger matches its fixed policy."""

    if (
        snapshot is None
        or snapshot.get("account_id") != expected_account_id
        or snapshot.get("status") != "ACTIVE"
        or snapshot.get("reconciliation_status") != "reconciled"
        or not math.isclose(
            _to_float(snapshot.get("initial_equity")),
            INITIAL_CAPITAL,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    ):
        return False
    with store.connect() as conn:
        row = conn.execute(
            "SELECT status, initial_capital, opening_cash FROM paper_accounts "
            "WHERE account_id=?",
            (expected_account_id,),
        ).fetchone()
    account_row_valid = bool(
        row is not None
        and str(row[0]) == "ACTIVE"
        and math.isclose(
            float(row[1]),
            INITIAL_CAPITAL,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        and math.isclose(
            float(row[2]),
            INITIAL_CAPITAL,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    )
    if not account_row_valid:
        return False
    today = datetime.now(WEEKLY_GOAL_TZ).date().isoformat()
    today_rows = [
        day
        for day in list(snapshot.get("days") or [])
        if str(day.get("date") or "") == today
    ]
    if today_rows and not math.isclose(
        _to_float(today_rows[-1].get("closing_equity")),
        _to_float(snapshot.get("realized_equity")),
        rel_tol=0.0,
        abs_tol=0.005,
    ):
        return False
    return True


def _account_snapshot(
    store: PaperStore,
    account_id: str,
    *,
    role: str,
    daily_summary: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    state = store._account_state(
        account_id,
        persist_high_water=False,
    )
    if state is None:
        return None
    open_statuses = (
        "PAPER_FILLED",
        "PAPER_PARTIALLY_FILLED",
        "PAPER_PARTIAL_EXPIRED",
    )
    placeholders = ",".join("?" for _ in open_statuses)
    pending_statuses = (
        "PAPER_LIMIT_RESTING",
        "PAPER_PARTIALLY_FILLED",
    )
    pending_placeholders = ",".join("?" for _ in pending_statuses)
    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        account_row = conn.execute(
            "SELECT created_at, status, cutover_note FROM paper_accounts "
            "WHERE account_id=?",
            (account_id,),
        ).fetchone()
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
        pending_rows = conn.execute(
            f"SELECT id, COALESCE(reserved_cost, 0) AS reserved_cost "
            f"FROM paper_orders WHERE account_id=? "
            f"AND status IN ({pending_placeholders}) "
            "AND settled_at IS NULL AND closed_at IS NULL",
            (account_id, *pending_statuses),
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
    pending_count = len(pending_rows)
    pending_risk = sum(float(row["reserved_cost"] or 0.0) for row in pending_rows)
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
    reconciliation = store.account_order_ledger_reconciliation(account_id)
    snapshot = {
        "account_id": account_id,
        "role": role,
        "created_at": (
            str(account_row["created_at"])
            if account_row is not None and account_row["created_at"] is not None
            else None
        ),
        "status": (
            str(account_row["status"])
            if account_row is not None and account_row["status"] is not None
            else "UNKNOWN"
        ),
        "cutover_note": (
            str(account_row["cutover_note"])
            if account_row is not None and account_row["cutover_note"] is not None
            else None
        ),
        "verification_scope": (
            f"{EXECUTION_MODEL_VERSION} fills only; "
            "legacy outcomes retained as unverified"
        ),
        "initial_equity": _round(initial, 2),
        "cash_balance": _round(state["cash_balance"], 2),
        "available_cash": _round(state["available_cash"], 2),
        "reservations": _round(state["reservations"], 2),
        "open_cost_basis": _round(state["open_cost_basis"], 2),
        "open_positions": open_count,
        "pending_limit_orders": pending_count,
        "pending_limit_risk": _round(pending_risk, 2),
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
            str(reconciliation["status"])
        ),
        "reconciliation_difference": _round(
            reconciliation.get("difference"),
            2,
        ),
        "reconciliation_mismatched_order_ids": list(
            reconciliation.get("mismatched_order_ids") or []
        ),
    }
    if daily_summary is not None:
        snapshot["days"] = _account_daily_balance_rows(
            store,
            account_id,
            initial_equity=initial,
            account_created_at=snapshot["created_at"],
            day_rows=list(daily_summary.get("days") or []),
        )
    return snapshot


def _account_daily_balance_rows(
    store: PaperStore,
    account_id: str,
    *,
    initial_equity: float,
    account_created_at: object,
    day_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return an account-only realized balance series for the published days."""

    day_keys = sorted(
        {
            str(row.get("date"))
            for row in day_rows
            if row.get("date")
        }
    )
    if not day_keys:
        return []

    created_day: str | None = None
    if account_created_at:
        try:
            created = datetime.fromisoformat(
                str(account_created_at).replace("Z", "+00:00")
            )
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            created_day = created.astimezone(WEEKLY_GOAL_TZ).date().isoformat()
        except ValueError:
            created_day = None

    with store.connect() as conn:
        resolved_rows = conn.execute(
            "SELECT COALESCE(closed_at, settled_at), COALESCE(realized_pnl, 0) "
            "FROM paper_orders WHERE account_id=? "
            "AND status IN ('PAPER_SETTLED','PAPER_CLOSED') "
            "AND COALESCE(closed_at, settled_at) IS NOT NULL "
            "ORDER BY COALESCE(closed_at, settled_at), id",
            (account_id,),
        ).fetchall()

    pnl_by_day: dict[str, float] = {}
    for resolved_at, realized_pnl in resolved_rows:
        try:
            resolved = datetime.fromisoformat(
                str(resolved_at).replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=UTC)
        key = resolved.astimezone(WEEKLY_GOAL_TZ).date().isoformat()
        pnl_by_day[key] = pnl_by_day.get(key, 0.0) + float(realized_pnl or 0.0)

    first_day = day_keys[0]
    cumulative = float(initial_equity) + sum(
        pnl for key, pnl in pnl_by_day.items() if key < first_day
    )
    output: list[dict[str, Any]] = []
    for key in day_keys:
        if created_day is not None and key < created_day:
            output.append(
                {
                    "date": key,
                    "opening_equity": None,
                    "daily_realized_pnl": 0.0,
                    "realized_pnl": 0.0,
                    "closing_equity": None,
                    "cumulative_realized": 0.0,
                }
            )
            continue
        opening = cumulative
        daily_pnl = pnl_by_day.get(key, 0.0)
        cumulative += daily_pnl
        output.append(
            {
                "date": key,
                "opening_equity": _round(opening, 2),
                "daily_realized_pnl": _round(daily_pnl, 2),
                "realized_pnl": _round(daily_pnl, 2),
                "closing_equity": _round(cumulative, 2),
                "cumulative_realized": _round(cumulative - initial_equity, 2),
            }
        )
    return output


def _empty_bound_profile(
    *,
    label: str,
    risk_profile: str,
    profile_type: str,
) -> dict[str, Any]:
    return {
        "label": label,
        "risk_profile": risk_profile,
        "profile_type": profile_type,
        "daily_summary": {"days": [], "totals": {}},
        "paper_trading": {
            "available": True,
            "summary": {
                "closed_positions": 0,
                "win_count": 0,
                "loss_count": 0,
                "hit_rate": None,
                "realized_pnl": 0.0,
                "roi": None,
                "open_positions": 0,
                "pending_limit_orders": 0,
                "open_risk": 0.0,
            },
        },
        "learnings": [],
        "recommended_changes": [],
        "excluded_from": [],
    }


def _bind_accounting_to_profiles(
    profiles: list[dict[str, Any]],
    accounting: dict[str, Any],
) -> list[dict[str, Any]]:
    """Attach true balances to active profiles and mark all prior books read-only."""

    rows = [dict(profile) for profile in profiles]
    archive_order = {
        "live-legacy": 0,
        "research-target-v1": 1,
        "research-target-v2": 2,
        "research-target-v3": 3,
        "research-target-v4": 4,
        "research-target-v5": 5,
        "research-motion": 6,
        "research": 7,
    }
    if accounting.get("available") is False:
        archived_rows = [
            row
            for row in rows
            if row.get("archived") is True
            or str(row.get("risk_profile")) in archive_order
        ]
        return sorted(
            archived_rows,
            key=lambda row: (
                archive_order.get(str(row.get("risk_profile")), 99),
                str(row.get("risk_profile")),
            ),
        )

    by_name = {
        str(profile.get("risk_profile")): profile
        for profile in rows
        if profile.get("risk_profile")
    }
    active_specs = (
        ("live", "Live Stability", "primary", "live_stability"),
        (
            "research-target",
            "Research ROI · fixed daily objective",
            "experimental",
            "research_roi",
        ),
    )
    active_ledgers = accounting.get("active_ledgers") or {}
    for profile_key, label, profile_type, account_key in active_specs:
        if profile_key not in by_name and active_ledgers.get(account_key):
            profile = _empty_bound_profile(
                label=label,
                risk_profile=profile_key,
                profile_type=profile_type,
            )
            rows.append(profile)
            by_name[profile_key] = profile

    archived_accounts = list(accounting.get("archived_accounts") or [])
    for account in archived_accounts:
        profile_key = str(
            account.get("attribution_profile_key")
            or account.get("profile_key")
            or ""
        )
        if not profile_key or profile_key not in archive_order:
            continue
        if profile_key not in by_name:
            profile = _empty_bound_profile(
                label=str(account.get("label") or profile_key),
                risk_profile=profile_key,
                profile_type=(
                    "primary" if profile_key == "live-legacy" else "experimental"
                ),
            )
            rows.append(profile)
            by_name[profile_key] = profile
        by_name[profile_key]["archived"] = True

    profile_to_ledger = {
        "live": "live_stability",
        "research-target": "research_roi",
    }
    for profile_key, ledger_key in profile_to_ledger.items():
        profile = by_name.get(profile_key)
        account = active_ledgers.get(ledger_key)
        if profile is None or not isinstance(account, dict):
            continue
        profile["archived"] = False
        profile["account_key"] = ledger_key
        daily = dict(profile.get("daily_summary") or {})
        balances = {
            str(day.get("date")): day
            for day in account.get("days") or []
            if day.get("date")
        }
        source_days = list(daily.get("days") or [])
        if not source_days:
            source_days = [
                {
                    "date": day.get("date"),
                    "cumulative_realized": day.get("cumulative_realized", 0.0),
                    "realized_pnl": day.get("realized_pnl", 0.0),
                }
                for day in account.get("days") or []
            ]
        merged_days = []
        for source in source_days:
            balance = balances.get(str(source.get("date"))) or {}
            merged_days.append(
                {
                    **source,
                    "opening_equity": balance.get("opening_equity"),
                    "daily_realized_pnl": balance.get("daily_realized_pnl"),
                    "closing_equity": balance.get("closing_equity"),
                }
            )
        daily.update(
            {
                "available": True,
                "equity_available": True,
                "equity_basis": "account_equity",
                "starting_bankroll": account.get("initial_equity"),
                "bankroll": account.get("initial_equity"),
                "current_equity": account.get("realized_equity"),
                "days": merged_days,
            }
        )
        profile["daily_summary"] = daily

    active_order = {"live": 0, "research-target": 1}
    return sorted(
        rows,
        key=lambda row: (
            0
            if str(row.get("risk_profile")) in active_order
            else 1,
            active_order.get(
                str(row.get("risk_profile")),
                archive_order.get(str(row.get("risk_profile")), 99),
            ),
            str(row.get("risk_profile")),
        ),
    )


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
    live_account_id = str(
        live.get("account_id") or LIVE_STABILITY_ACCOUNT_ID
    )
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
            (live_account_id,),
        ).fetchall()
        boundary_row = conn.execute(
            "SELECT MIN(created_at) FROM paper_account_ledger "
            "WHERE account_id=? AND event_type='EXECUTION_SEMANTICS_TRANSITION' "
            "AND idempotency_key=?",
            (live_account_id, f"execution:{EXECUTION_MODEL_VERSION}"),
        ).fetchone()

    evidence_boundary = (
        boundary_row[0]
        if boundary_row and boundary_row[0]
        else live.get("created_at")
    )
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
        "account_id": live_account_id,
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
        "excludes": [
            "research-roi",
            "archived-performance",
            "unrealized-marks",
        ],
        "disclaimer": "Research objective, not a guaranteed return; risk gates remain binding.",
    }


def _load_or_build_dataset_research(
    *,
    forecaster_root: Path,
    db_path: Path,
    allow_build: bool = True,
) -> dict[str, Any] | None:
    published = _load_json_optional(forecaster_root / "dataset_research.json")
    if published:
        return published
    if not allow_build:
        return {
            "available": False,
            "reason": (
                "dataset research is not published; rebuild is deferred from "
                "the bounded recurring Strategy Lab path"
            ),
        }
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
        {"term": "Dedupe", "note": "Repeated scheduled scans are reduced to one sampled row per target, market, and side."},
        {"term": "Calibration", "note": "How closely stated probabilities match observed win frequencies."},
        {"term": "Brier score", "note": "Squared probability error; lower is better."},
        {"term": "Log loss", "note": "Penalty for assigning low probability to what happened; lower is better."},
        {"term": "Paper trading", "note": "Simulated positions recorded for research. No live money is placed."},
        {"term": "Challenger model", "note": "A research calibration compared against the active execution calibration."},
    ]
