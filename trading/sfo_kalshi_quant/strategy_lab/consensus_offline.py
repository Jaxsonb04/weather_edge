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
    from ..consensus import build_market_consensus
    from ..report import consensus_to_dict

    consensus = build_market_consensus(markets)
    probabilities = {
        ticker: _ModelProbabilityRef(value) for ticker, value in model_probabilities.items()
    }
    payload = consensus_to_dict(consensus, predicted_high_f, probabilities)
    if payload.get("available"):
        payload = {**payload, "target_date": target_date}
    return payload
