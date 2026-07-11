"""Compatibility facade for the Strategy Lab artifact builder.

Implementation lives in :mod:`sfo_kalshi_quant.strategy_lab`; imports from
this historical module remain stable for operational scripts and callers.
The build wrapper temporarily forwards historically monkeypatchable globals to
their owning domain modules and restores them after every call.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from ._util import (
    _date_from_string,
    _db_table_exists,
    _env_float,
    _load_json_optional,
    _null_metric,
    _round,
    _round_dict,
    _table_exists as _sqlite_table_exists,
    _to_float,
)
from .backtest import run_walk_forward_calibration_backtest
from .cities import CITIES
from .config import DEFAULT_DB_PATH, DEFAULT_FORECASTER_ROOT, SFO_TZ, StrategyConfig
from .dataset_research import DEFAULT_MIN_AFTER_COST_TRADES, DEFAULT_MIN_MATCHED_ROWS
from .exits import (
    DEFAULT_NO_STOP_LOSS_PCT,
    DEFAULT_NO_TAKE_PROFIT_PCT,
    DEFAULT_RESEARCH_NO_SETTLEMENT_FIRST_MIN_COST,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
    DEFAULT_YES_STOP_LOSS_PCT,
    DEFAULT_YES_TAKE_PROFIT_PCT,
)
from .settlement_truth import is_pre_resolution_decision as _is_strategy_pre_resolution
from .strategy_lab import (
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
from .strategy_lab import (
    build,
    calibration,
    consensus_offline,
    dataset_summary,
    forecast_health,
    paper_card,
    profiles,
    readiness,
    status_alerts,
)
from .strategy_lab.build import (
    build_strategy_research as _build_strategy_research,
    _daily_summary_payload,
    write_strategy_research,
    _accounting_payload,
    _load_or_build_dataset_research,
    _research_notes,
)
from .strategy_lab.profiles import (
    _profile_views,
    _profile_names,
    _profile_view,
    _profile_daily_summary,
    _profile_paper_payload,
    _profile_signal_quality,
    _profile_gate_behavior,
    _profile_learnings,
    _profile_recommendations,
    _profile_status,
    _profile_row,
    _default_profile,
    _profile_key,
    _profile_sort_key,
    _profile_label,
    _probability_market_points,
    _edge_by_market_bucket,
    _quality_distribution,
)
from .strategy_lab.calibration import (
    _calibration_payload,
    _comparison_summary,
    _prediction_replay_payload,
    _config_rescore_payload,
    _research_shadow_payload,
    _signal_backtest_payload,
    _is_live_candidate,
    _signal_quality_payload,
    _candidate_rows_by_profile,
    _decision_lead_mode_counts,
    _lead_mode_counts,
    _empty_lead_mode_counts,
    _forecast_lead_mode,
    _latest_decision_rows,
    _decision_row,
    _decisions_from_trading_signal,
    _cohort_rows,
    _empty_cohorts,
    _empty_cohort,
    _cohort_label,
)
from .strategy_lab.readiness import (
    _real_money_readiness_payload,
    _live_frequency_tuning_payload,
)
from .strategy_lab.paper_card import (
    _paper_payload,
    _paper_diagnostics,
    _empty_paper_diagnostics,
    _paper_group_diagnostics,
    _paper_group_summary,
    _worst_paper_segments,
    _paper_exit_reason,
    _paper_order_won,
    _paper_order_decided,
    _profiles_with_scanners,
    _profile_summary_row,
    _row_risk_profile,
    _latest_monitor_marks,
    _latest_position_marks,
    _fresh_model_side_probability,
    _position_mark_for,
    _side_from_row,
    _paper_monitor_config,
    _monitor_thresholds_for_side,
    _settlement_first_no_min_cost_for_row,
    _position_mark_status,
    _paper_row,
    _paper_action_row,
    _paper_open_action_row,
    _paper_limit_action_row,
    _paper_monitor_snapshot_row,
    _duplicate_group_row,
)
from .strategy_lab.forecast_health import (
    _forecast_health_payload,
    _nwp_health,
    _nwp_target_health,
    _nwp_live_serve_health,
    _emos_health,
    _emos_row,
    _clisfo_health,
    _nws_ground_truth_health,
    _emos_enabled_profiles,
    _health_warning,
    _sqlite_columns,
    _coerce_utc,
    _age_hours,
    _split_csv,
)
from .strategy_lab.dataset_summary import (
    _dataset_research_summary,
    _dataset_candidate_row,
    _dataset_candidate_count,
    _dataset_accuracy_candidate_count,
    _dataset_stack_summary,
    _dataset_research_headline,
    _dataset_blocking_gates,
    _dataset_action_items,
    _dataset_max_matched_rows,
    _dataset_candidate_next_use,
    _dataset_promotion_rule,
)
from .strategy_lab.status_alerts import (
    _status_payload,
    _decision_reason,
    _why_trade_good,
    _paper_status,
    _strategy_alerts,
    _forecast_health_alerts,
    _alert,
    _alert_level,
    _entry_block_reason,
    _status_target_date,
    _target_from_signal,
    _is_probability_only_ticker,
    _age_label,
)
from .strategy_lab.consensus_offline import (
    _ModelProbabilityRef,
    _latest_consensus_inputs,
    _market_consensus_payload,
)

_BUILD_COMPAT_DEPENDENCIES = {
    "datetime": (build, calibration, forecast_health, paper_card, status_alerts),
    "run_walk_forward_calibration_backtest": (calibration,),
    "ACTIVE_CALIBRATION_SOURCE": (status_alerts,),
    "CHALLENGER_CALIBRATION_SOURCE": (build, status_alerts),
    "MIN_CLEAN_WINNER_SAMPLE": (calibration,),
    "DEFAULT_MODEL_VETO_MAX_LOSS_PCT": (paper_card,),
    "DEFAULT_MODEL_VETO_BUFFER": (paper_card,),
    "PRIMARY_PROFILE": (calibration, forecast_health, profiles),
    "EXPERIMENTAL_PROFILES": (forecast_health, profiles),
    "FORECAST_HEALTH_ROLLING_DAYS": (forecast_health,),
    "FORECAST_HEALTH_MIN_NWP_MODELS": (forecast_health,),
    "FORECAST_HEALTH_MAX_NWP_AGE": (forecast_health,),
    "FORECAST_HEALTH_MAX_EMOS_AGE": (forecast_health,),
    "FORECAST_HEALTH_MAX_CLISFO_LAG_DAYS": (forecast_health,),
    "FORECAST_LEAD_MODE_LABELS": (calibration,),
    "_calibration_payload": (build,),
    "_comparison_summary": (build,),
    "_config_rescore_payload": (build,),
    "_prediction_replay_payload": (build,),
    "_research_shadow_payload": (build,),
    "_signal_backtest_payload": (build,),
    "_signal_quality_payload": (build,),
    "_dataset_research_summary": (build,),
    "_forecast_health_payload": (build,),
    "_paper_payload": (build,),
    "_default_profile": (build,),
    "_profile_views": (build,),
    "_live_frequency_tuning_payload": (build,),
    "_real_money_readiness_payload": (build,),
    "_status_payload": (build,),
}
_BUILD_COMPAT_LOCK = RLock()


@contextmanager
def _forward_build_compatibility() -> Iterator[None]:
    with _BUILD_COMPAT_LOCK:
        originals: list[tuple[object, str, object]] = []
        try:
            for name, owners in _BUILD_COMPAT_DEPENDENCIES.items():
                value = globals()[name]
                for owner in owners:
                    originals.append((owner, name, getattr(owner, name)))
                    setattr(owner, name, value)
            yield
        finally:
            for owner, name, value in reversed(originals):
                setattr(owner, name, value)


def build_strategy_research(
    *,
    forecaster_root: Path = DEFAULT_FORECASTER_ROOT,
    db_path: Path = DEFAULT_DB_PATH,
    config: StrategyConfig | None = None,
    calibration_min_train: int = 180,
) -> dict[str, Any]:
    """Build through the compatibility surface used by historical callers."""

    with _forward_build_compatibility():
        return _build_strategy_research(
            forecaster_root=forecaster_root,
            db_path=db_path,
            config=config,
            calibration_min_train=calibration_min_train,
        )
