from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .forecast import ForecastDataError, SfoForecasterAdapter
from .models import ForecastOutcome


DEFAULT_MIN_MATCHED_ROWS = 30
DEFAULT_MIN_MAE_IMPROVEMENT_F = 0.25
DEFAULT_HOLDOUT_FRACTION = 0.25
DEFAULT_MIN_AFTER_COST_TRADES = 30
DEFAULT_MAX_STACK_FEATURES = 8
DEFAULT_STACK_RIDGE_ALPHA = 10.0
EXPECTED_DATASET_SOURCES = (
    "iem-asos",
    "open-meteo-previous-runs",
    "open-meteo-historical-forecast",
    "lamp",
    "gfs-mos",
    "nbm",
    "hrrr",
    "kalshi-history",
)
DATASET_SOURCE_STALE_HOURS = 30.0


@dataclass(frozen=True)
class _FeatureCandidate:
    source: str
    model: str
    variable: str
    lead_hours: float | None
    rows: tuple[tuple[date, float], ...]

    @property
    def key(self) -> str:
        lead = "none" if self.lead_hours is None else f"{self.lead_hours:g}h"
        return f"{self.source}/{self.model}/{self.variable}/{lead}"


@dataclass(frozen=True)
class _StackTrainingRow:
    local_date: date
    actual_high_f: float
    baseline_high_f: float
    features: tuple[float, ...]


def build_dataset_research(
    *,
    db_path: Path,
    forecaster_root: Path,
    min_matched_rows: int = DEFAULT_MIN_MATCHED_ROWS,
    min_mae_improvement_f: float = DEFAULT_MIN_MAE_IMPROVEMENT_F,
    holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION,
    min_after_cost_trades: int = DEFAULT_MIN_AFTER_COST_TRADES,
) -> dict[str, Any]:
    """Evaluate whether collected external datasets are ready for model/trade use.

    This report is intentionally conservative. It can flag a dataset forecast
    feature as an accuracy candidate, but it keeps the overall status in
    collect-only mode until a separate after-cost trading test exists.
    """

    db_path = Path(db_path)
    generated_at = datetime.now(UTC).isoformat()
    try:
        baseline_outcomes = SfoForecasterAdapter(forecaster_root).load_lstm_outcomes()
    except (ForecastDataError, FileNotFoundError, KeyError, ValueError) as exc:
        return {
            "schema_version": 1,
            "generated_at": generated_at,
            "status": "collect_only",
            "available": False,
            "reason": f"baseline outcomes unavailable: {exc}",
            "summary": {
                "headline": "Baseline LSTM outcomes are unavailable, so dataset promotion cannot be scored.",
                "accuracy_candidate_count": 0,
                "candidate_count": 0,
                "combined_stack_candidate": False,
                "market_trade_rows": 0,
                "blocking_gates": ["baseline gate: LSTM outcome history unavailable"],
                "action_items": ["Restore or regenerate the forecaster outcome artifact before dataset scoring."],
            },
            "dataset_coverage": _dataset_coverage(db_path),
            "source_health": _dataset_source_health(db_path),
            "accuracy_gate": {"available": False, "candidates": []},
            "dataset_stack": {"available": False, "decision": "collect_only"},
            "probabilistic_benchmarks": _probabilistic_benchmarks([]),
            "profitability_gate": _profitability_gate({}, min_after_cost_trades=min_after_cost_trades),
        }

    baseline_by_date = {row.local_date: row for row in baseline_outcomes}
    candidates = _load_forecast_feature_candidates(db_path)
    accuracy_rows = [
        _candidate_payload(
            candidate,
            baseline_by_date=baseline_by_date,
            min_matched_rows=min_matched_rows,
            min_mae_improvement_f=min_mae_improvement_f,
            holdout_fraction=holdout_fraction,
        )
        for candidate in candidates
    ]
    accuracy_rows.sort(
        key=lambda row: (
            row["decision"] != "accuracy_candidate",
            row["holdout"].get("mae_delta_vs_baseline_f", math.inf),
            row["dataset_key"],
        )
    )
    market_counts = _market_history_counts(db_path)
    dataset_stack = _dataset_stack_payload(
        candidates,
        baseline_by_date=baseline_by_date,
        min_matched_rows=min_matched_rows,
        min_mae_improvement_f=min_mae_improvement_f,
        holdout_fraction=holdout_fraction,
    )
    profitability_gate = _profitability_gate(
        market_counts,
        min_after_cost_trades=min_after_cost_trades,
    )
    summary = _research_summary(
        accuracy_rows=accuracy_rows,
        dataset_stack=dataset_stack,
        profitability_gate=profitability_gate,
        min_matched_rows=min_matched_rows,
    )
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "status": "collect_only",
        "available": True,
        "summary": summary,
        "baseline": {
            "source": "lstm",
            "outcome_count": len(baseline_outcomes),
            "settlement": "rounded SFO high temperature",
        },
        "dataset_coverage": _dataset_coverage(db_path),
        "source_health": _dataset_source_health(db_path),
        "accuracy_gate": {
            "available": True,
            "minimum_matched_rows": min_matched_rows,
            "minimum_holdout_mae_improvement_f": min_mae_improvement_f,
            "holdout_fraction": holdout_fraction,
            "candidate_count": len(accuracy_rows),
            "accuracy_candidate_count": sum(1 for row in accuracy_rows if row["decision"] == "accuracy_candidate"),
            "candidates": accuracy_rows,
        },
        "dataset_stack": dataset_stack,
        "probabilistic_benchmarks": _probabilistic_benchmarks(candidates),
        "profitability_gate": profitability_gate,
        "promotion_rule": (
            "Collect broadly, but do not give a new source live model weight or "
            "loosen paper-trading gates until it improves held-out forecast error "
            "and then survives an after-cost market backtest with enough trades."
        ),
    }


def write_dataset_research(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_forecast_feature_candidates(db_path: Path) -> list[_FeatureCandidate]:
    if not Path(db_path).exists():
        return []
    query = """
        SELECT source, model, variable, lead_hours, target_date, value, issued_at
        FROM dataset_forecast_features
        WHERE value IS NOT NULL
          AND station_id = 'KSFO'
          AND target_date IS NOT NULL
          AND variable LIKE '%temperature_2m_max%'
        ORDER BY source, model, variable, lead_hours, target_date, issued_at
    """
    try:
        with sqlite3.connect(db_path) as conn:
            if not _table_exists(conn, "dataset_forecast_features"):
                return []
            rows = conn.execute(query).fetchall()
    except sqlite3.Error:
        return []

    latest_by_key_day: dict[tuple[str, str, str, float | None, str], tuple[str, float]] = {}
    for source, model, variable, lead_hours, target_iso, value, issued_at in rows:
        key = (str(source), str(model), str(variable), _maybe_float(lead_hours), str(target_iso))
        current = latest_by_key_day.get(key)
        if current is None or str(issued_at) > current[0]:
            latest_by_key_day[key] = (str(issued_at), float(value))

    grouped: dict[tuple[str, str, str, float | None], list[tuple[date, float]]] = defaultdict(list)
    for (source, model, variable, lead_hours, target_iso), (_, value) in latest_by_key_day.items():
        grouped[(source, model, variable, lead_hours)].append((date.fromisoformat(target_iso), value))

    return [
        _FeatureCandidate(source, model, variable, lead_hours, tuple(sorted(values)))
        for (source, model, variable, lead_hours), values in grouped.items()
    ]


def _candidate_payload(
    candidate: _FeatureCandidate,
    *,
    baseline_by_date: dict[date, ForecastOutcome],
    min_matched_rows: int,
    min_mae_improvement_f: float,
    holdout_fraction: float,
) -> dict[str, Any]:
    matched = [
        (target, value, baseline_by_date[target])
        for target, value in candidate.rows
        if target in baseline_by_date
    ]
    n = len(matched)
    all_metrics = _metrics(matched)
    holdout = _holdout_metrics(matched, holdout_fraction=holdout_fraction)
    if n < min_matched_rows:
        decision = "collect_only"
        reason = f"needs at least {min_matched_rows} matched settlement rows; has {n}"
    elif holdout["mae_delta_vs_baseline_f"] <= -min_mae_improvement_f:
        decision = "accuracy_candidate"
        reason = "beats baseline on held-out matched dates"
    else:
        decision = "collect_only"
        reason = "does not beat baseline by the required held-out MAE margin"
    next_use = _candidate_next_use(decision, reason)
    return {
        "dataset_key": candidate.key,
        "source": candidate.source,
        "model": candidate.model,
        "variable": candidate.variable,
        "lead_hours": candidate.lead_hours,
        "matched_rows": n,
        "decision": decision,
        "reason": reason,
        "next_use": next_use,
        "all_matched": all_metrics,
        "holdout": holdout,
    }


def _candidate_next_use(decision: str, reason: str) -> str:
    if decision == "accuracy_candidate":
        return (
            "Eligible for challenger-model research; keep live trading weight at zero "
            "until the after-cost market gate passes."
        )
    return f"Keep collecting; {reason}"


def _dataset_stack_payload(
    candidates: list[_FeatureCandidate],
    *,
    baseline_by_date: dict[date, ForecastOutcome],
    min_matched_rows: int,
    min_mae_improvement_f: float,
    holdout_fraction: float,
) -> dict[str, Any]:
    eligible = []
    for candidate in candidates:
        values = {
            target: value
            for target, value in candidate.rows
            if target in baseline_by_date
        }
        if len(values) >= min_matched_rows:
            eligible.append((candidate.key, values))
    eligible.sort(key=lambda item: (-len(item[1]), item[0]))
    selected = eligible[:DEFAULT_MAX_STACK_FEATURES]
    if not selected:
        return {
            "available": False,
            "decision": "collect_only",
            "reason": (
                "No dataset forecast feature has enough matched settlement rows "
                f"for a combined stack yet; need at least {min_matched_rows}."
            ),
            "minimum_matched_rows": min_matched_rows,
            "feature_keys": [],
        }

    feature_names = ["baseline_lstm"]
    for key, _ in selected:
        feature_names.append(key)
        feature_names.append(f"missing:{key}")

    dates = sorted({target for _, values in selected for target in values})
    rows = []
    for target in dates:
        baseline = baseline_by_date.get(target)
        if baseline is None:
            continue
        features = [baseline.predicted_high_f]
        present_count = 0
        for _, values in selected:
            value = values.get(target)
            if value is None:
                features.extend([baseline.predicted_high_f, 1.0])
            else:
                present_count += 1
                features.extend([value, 0.0])
        if present_count == 0:
            continue
        rows.append(
            _StackTrainingRow(
                local_date=target,
                actual_high_f=baseline.actual_high_f,
                baseline_high_f=baseline.predicted_high_f,
                features=tuple(features),
            )
        )

    min_train = max(30, min_matched_rows)
    if len(rows) <= min_train:
        return {
            "available": False,
            "decision": "collect_only",
            "reason": (
                "Matched dataset rows exist, but not enough dated rows remain "
                f"for walk-forward stack scoring after a {min_train}-row training window."
            ),
            "minimum_train_rows": min_train,
            "matched_rows": len(rows),
            "feature_keys": [key for key, _ in selected],
        }

    scored = _walk_forward_stack_predictions(
        rows,
        min_train=min_train,
        ridge_alpha=DEFAULT_STACK_RIDGE_ALPHA,
    )
    all_metrics = _stack_metrics(scored)
    holdout = _stack_holdout_metrics(scored, holdout_fraction=holdout_fraction)
    if holdout["mae_delta_vs_baseline_f"] <= -min_mae_improvement_f:
        decision = "research_candidate"
        reason = "combined dataset stack beats baseline on held-out matched dates"
    else:
        decision = "collect_only"
        reason = "combined dataset stack does not beat baseline by the held-out MAE margin"
    return {
        "available": True,
        "decision": decision,
        "reason": reason,
        "model": "walk_forward_ridge_stack",
        "ridge_alpha": DEFAULT_STACK_RIDGE_ALPHA,
        "minimum_train_rows": min_train,
        "matched_rows": len(rows),
        "scored_rows": len(scored),
        "feature_count": len(selected),
        "feature_keys": [key for key, _ in selected],
        "feature_names": feature_names,
        "all_matched": all_metrics,
        "holdout": holdout,
        "next_use": (
            "Use as a challenger blend input only; live trade gates still need after-cost proof."
            if decision == "research_candidate"
            else "Keep collecting broader feature coverage before changing model weights."
        ),
    }


def _walk_forward_stack_predictions(
    rows: list[_StackTrainingRow],
    *,
    min_train: int,
    ridge_alpha: float,
) -> list[dict[str, Any]]:
    scored = []
    for idx in range(min_train, len(rows)):
        train = rows[:idx]
        test = rows[idx]
        model = _fit_ridge(
            [row.features for row in train],
            [row.actual_high_f for row in train],
            ridge_alpha=ridge_alpha,
        )
        prediction = _predict_ridge(model, test.features)
        scored.append(
            {
                "date": test.local_date,
                "stack_high_f": prediction,
                "baseline_high_f": test.baseline_high_f,
                "actual_high_f": test.actual_high_f,
            }
        )
    return scored


def _fit_ridge(
    rows: list[tuple[float, ...]],
    targets: list[float],
    *,
    ridge_alpha: float,
) -> dict[str, tuple[float, ...]]:
    feature_count = len(rows[0])
    means = tuple(sum(row[idx] for row in rows) / len(rows) for idx in range(feature_count))
    scales = []
    for idx, mean in enumerate(means):
        variance = sum((row[idx] - mean) ** 2 for row in rows) / len(rows)
        scales.append(math.sqrt(variance) or 1.0)

    size = feature_count + 1
    xtx = [[0.0 for _ in range(size)] for _ in range(size)]
    xty = [0.0 for _ in range(size)]
    for raw_features, target in zip(rows, targets, strict=True):
        features = [1.0] + [
            (raw_features[idx] - means[idx]) / scales[idx]
            for idx in range(feature_count)
        ]
        for i in range(size):
            xty[i] += features[i] * target
            for j in range(size):
                xtx[i][j] += features[i] * features[j]
    for idx in range(1, size):
        xtx[idx][idx] += ridge_alpha
    return {
        "means": means,
        "scales": tuple(scales),
        "coefficients": tuple(_solve_linear_system(xtx, xty)),
    }


def _predict_ridge(model: dict[str, tuple[float, ...]], features: tuple[float, ...]) -> float:
    means = model["means"]
    scales = model["scales"]
    coefficients = model["coefficients"]
    value = coefficients[0]
    for idx, raw_feature in enumerate(features):
        value += coefficients[idx + 1] * ((raw_feature - means[idx]) / scales[idx])
    return value


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [list(matrix[idx]) + [vector[idx]] for idx in range(size)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-12:
            augmented[col][col] += 1e-9
            pivot = col
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        divisor = augmented[col][col]
        for cell in range(col, size + 1):
            augmented[col][cell] /= divisor
        for row in range(size):
            if row == col:
                continue
            factor = augmented[row][col]
            for cell in range(col, size + 1):
                augmented[row][cell] -= factor * augmented[col][cell]
    return [augmented[row][size] for row in range(size)]


def _stack_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "n": 0,
            "stack_mae_f": None,
            "baseline_mae_f": None,
            "mae_delta_vs_baseline_f": None,
            "stack_bias_f": None,
            "baseline_bias_f": None,
        }
    stack_errors = [row["stack_high_f"] - row["actual_high_f"] for row in rows]
    baseline_errors = [row["baseline_high_f"] - row["actual_high_f"] for row in rows]
    return {
        "n": len(rows),
        "stack_mae_f": _round(_mae(stack_errors)),
        "baseline_mae_f": _round(_mae(baseline_errors)),
        "mae_delta_vs_baseline_f": _round(_mae(stack_errors) - _mae(baseline_errors)),
        "stack_bias_f": _round(sum(stack_errors) / len(stack_errors)),
        "baseline_bias_f": _round(sum(baseline_errors) / len(baseline_errors)),
    }


def _stack_holdout_metrics(
    rows: list[dict[str, Any]],
    *,
    holdout_fraction: float,
) -> dict[str, Any]:
    if not rows:
        return _stack_metrics([])
    holdout_fraction = min(0.9, max(0.05, holdout_fraction))
    holdout_n = max(1, int(math.ceil(len(rows) * holdout_fraction)))
    return _stack_metrics(rows[-holdout_n:])


def _metrics(rows: list[tuple[date, float, ForecastOutcome]]) -> dict[str, Any]:
    if not rows:
        return {
            "n": 0,
            "dataset_mae_f": None,
            "baseline_mae_f": None,
            "mae_delta_vs_baseline_f": None,
            "dataset_bias_f": None,
            "baseline_bias_f": None,
        }
    dataset_errors = [value - outcome.actual_high_f for _, value, outcome in rows]
    baseline_errors = [outcome.predicted_high_f - outcome.actual_high_f for _, _, outcome in rows]
    return {
        "n": len(rows),
        "dataset_mae_f": _round(_mae(dataset_errors)),
        "baseline_mae_f": _round(_mae(baseline_errors)),
        "mae_delta_vs_baseline_f": _round(_mae(dataset_errors) - _mae(baseline_errors)),
        "dataset_bias_f": _round(sum(dataset_errors) / len(dataset_errors)),
        "baseline_bias_f": _round(sum(baseline_errors) / len(baseline_errors)),
    }


def _holdout_metrics(
    rows: list[tuple[date, float, ForecastOutcome]],
    *,
    holdout_fraction: float,
) -> dict[str, Any]:
    if not rows:
        return _metrics([])
    holdout_fraction = min(0.9, max(0.05, holdout_fraction))
    holdout_n = max(1, int(math.ceil(len(rows) * holdout_fraction)))
    return _metrics(rows[-holdout_n:])


def _profitability_gate(
    market_counts: dict[str, int],
    *,
    min_after_cost_trades: int,
) -> dict[str, Any]:
    trades = int(market_counts.get("trades", 0))
    decision = "collect_only"
    reason = (
        "No dataset source gets live trading weight until it has an after-cost "
        f"market backtest with at least {min_after_cost_trades} matched trades."
    )
    if trades < min_after_cost_trades:
        reason += f" Current collected trade rows: {trades}."
    return {
        "decision": decision,
        "minimum_after_cost_trades": min_after_cost_trades,
        "market_history": market_counts,
        "reason": reason,
    }


def _research_summary(
    *,
    accuracy_rows: list[dict[str, Any]],
    dataset_stack: dict[str, Any],
    profitability_gate: dict[str, Any],
    min_matched_rows: int,
) -> dict[str, Any]:
    candidate_count = len(accuracy_rows)
    accuracy_candidates = [
        row for row in accuracy_rows if row.get("decision") == "accuracy_candidate"
    ]
    stack_candidate = dataset_stack.get("decision") == "research_candidate"
    market_history = profitability_gate.get("market_history") or {}
    trades = int(market_history.get("trades", 0))
    min_trades = int(profitability_gate.get("minimum_after_cost_trades") or 0)

    if candidate_count == 0:
        headline = "No external forecast features are matched to settled SFO highs yet."
    elif accuracy_candidates or stack_candidate:
        headline = "One or more dataset views show forecast lift, but trade weighting is still gated."
    else:
        headline = "Collected dataset features have not beaten the active LSTM baseline on holdout yet."

    action_items = []
    if candidate_count == 0:
        action_items.append(
            "Run the tier1 dataset backfill for completed target dates so Open-Meteo and market history can match settled highs."
        )
    if accuracy_candidates:
        action_items.append(
            "Promote the best accuracy candidates into challenger-blend research, not live execution."
        )
    if stack_candidate:
        action_items.append(
            "Review the combined stack as a challenger source because it beat baseline on held-out rows."
        )
    if trades < min_trades:
        action_items.append(
            f"Backfill or collect Kalshi trades/candles until at least {min_trades} after-cost rows exist; current trade rows: {trades}."
        )
    if not action_items:
        action_items.append(
            "Keep the current collection plan; the next promotion decision depends on larger held-out and after-cost samples."
        )

    blocking_gates = []
    if candidate_count == 0:
        blocking_gates.append(f"accuracy gate: needs at least {min_matched_rows} matched rows per source")
    elif not accuracy_candidates and not stack_candidate:
        blocking_gates.append("accuracy gate: no held-out MAE lift versus baseline")
    if trades < min_trades:
        blocking_gates.append("profitability gate: insufficient after-cost market rows")

    return {
        "headline": headline,
        "accuracy_candidate_count": len(accuracy_candidates),
        "candidate_count": candidate_count,
        "combined_stack_candidate": stack_candidate,
        "market_trade_rows": trades,
        "blocking_gates": blocking_gates,
        "action_items": action_items,
    }


def _probabilistic_benchmarks(candidates: list[_FeatureCandidate]) -> dict[str, Any]:
    nbm_candidates = [
        candidate
        for candidate in candidates
        if candidate.model == "ncep_nbm_conus" or candidate.source == "noaa-nbm"
    ]
    return {
        "nbm": {
            "available": bool(nbm_candidates),
            "role": "calibrated_probabilistic_benchmark",
            "source": "NOAA National Blend of Models / Open-Meteo ncep_nbm_conus",
            "models": ["ncep_nbm_conus"],
            "candidate_keys": [candidate.key for candidate in nbm_candidates],
            "desired_fields": [
                "percentile_temperature_fields",
                "exceedance_probability_fields",
            ],
            "scoring": [
                "brier_skill",
                "ranked_probability_score",
            ],
            "next_use": (
                "Use NBM as a distribution benchmark before giving it live trade "
                "weight; compare by lead time and cohort against the active model."
            ),
        }
    }


def _dataset_coverage(db_path: Path) -> dict[str, Any]:
    tables = {
        "dataset_runs": 0,
        "dataset_station_observations": 0,
        "dataset_forecast_features": 0,
        "dataset_kalshi_markets": 0,
        "dataset_kalshi_candles": 0,
        "dataset_kalshi_trades": 0,
        "dataset_kalshi_orderbook_events": 0,
    }
    if not Path(db_path).exists():
        return {"available": False, "tables": tables, "forecast_feature_sources": []}
    try:
        with sqlite3.connect(db_path) as conn:
            for table in list(tables):
                tables[table] = _table_count(conn, table)
            if not _table_exists(conn, "dataset_forecast_features"):
                return {"available": True, "tables": tables, "forecast_feature_sources": []}
            source_rows = conn.execute(
                """
                SELECT source,
                       model,
                       station_id,
                       variable,
                       COUNT(*) AS rows,
                       COUNT(DISTINCT target_date) AS target_dates,
                       MIN(target_date) AS first_target_date,
                       MAX(target_date) AS last_target_date
                FROM dataset_forecast_features
                GROUP BY source, model, station_id, variable
                ORDER BY target_dates DESC, rows DESC, source, model, station_id, variable
                LIMIT 12
                """
            ).fetchall()
    except sqlite3.Error:
        return {"available": False, "tables": tables, "forecast_feature_sources": []}

    return {
        "available": True,
        "tables": tables,
        "market_history": {
            "markets": tables["dataset_kalshi_markets"],
            "candles": tables["dataset_kalshi_candles"],
            "trades": tables["dataset_kalshi_trades"],
            "minimum_after_cost_trades": DEFAULT_MIN_AFTER_COST_TRADES,
            "trade_rows_ready": tables["dataset_kalshi_trades"] >= DEFAULT_MIN_AFTER_COST_TRADES,
        },
        "forecast_feature_sources": [
            {
                "source": row[0],
                "model": row[1],
                "station_id": row[2],
                "variable": row[3],
                "rows": int(row[4] or 0),
                "target_dates": int(row[5] or 0),
                "first_target_date": row[6],
                "last_target_date": row[7],
            }
            for row in source_rows
        ],
    }


def _dataset_source_health(db_path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    else:
        current = current.astimezone(UTC)
    empty = {
        "available": False,
        "expected_sources": list(EXPECTED_DATASET_SOURCES),
        "stale_after_hours": DATASET_SOURCE_STALE_HOURS,
        "latest_runs": [],
        "warnings": [],
    }
    if not Path(db_path).exists():
        return {**empty, "warnings": [_dataset_warning("dataset-db-missing", "Dataset DB missing")]}
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            if not _table_exists(conn, "dataset_runs"):
                return {
                    **empty,
                    "warnings": [_dataset_warning("dataset-runs-missing", "dataset_runs table missing")],
                }
            rows = conn.execute(
                """
                SELECT r.source, r.status, r.rows_written, r.started_at, r.completed_at, r.message
                FROM dataset_runs r
                JOIN (
                    SELECT source, MAX(id) AS max_id
                    FROM dataset_runs
                    GROUP BY source
                ) latest ON latest.source = r.source AND latest.max_id = r.id
                ORDER BY r.source
                """
            ).fetchall()
    except sqlite3.Error as exc:
        return {**empty, "warnings": [_dataset_warning("dataset-runs-unreadable", f"{type(exc).__name__}: {exc}")]}

    latest: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, str]] = []
    for row in rows:
        completed = _parse_timestamp(row["completed_at"]) or _parse_timestamp(row["started_at"])
        age_hours = _age_hours(current, completed)
        source = str(row["source"])
        status = str(row["status"])
        latest[source] = {
            "source": source,
            "status": status,
            "rows_written": int(row["rows_written"] or 0),
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "age_hours": _round(age_hours),
            "message": row["message"],
        }
        if status != "success":
            warnings.append(_dataset_warning(f"{source}-latest-{status}", f"{source} latest run status is {status}"))
        elif age_hours is not None and age_hours > DATASET_SOURCE_STALE_HOURS:
            warnings.append(_dataset_warning(f"{source}-stale", f"{source} latest run is {age_hours:.1f} hours old"))

    for source in EXPECTED_DATASET_SOURCES:
        if source not in latest:
            warnings.append(_dataset_warning(f"{source}-missing", f"{source} has no recorded dataset run"))

    return {
        "available": True,
        "expected_sources": list(EXPECTED_DATASET_SOURCES),
        "stale_after_hours": DATASET_SOURCE_STALE_HOURS,
        "latest_runs": [latest[source] for source in sorted(latest)],
        "warnings": warnings,
    }


def _market_history_counts(db_path: Path) -> dict[str, int]:
    if not Path(db_path).exists():
        return {"markets": 0, "candles": 0, "trades": 0}
    with sqlite3.connect(db_path) as conn:
        return {
            "markets": _table_count(conn, "dataset_kalshi_markets"),
            "candles": _table_count(conn, "dataset_kalshi_candles"),
            "trades": _table_count(conn, "dataset_kalshi_trades"),
        }


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_hours(now: datetime, timestamp: datetime | None) -> float | None:
    if timestamp is None:
        return None
    return max(0.0, (now - timestamp).total_seconds() / 3600.0)


def _dataset_warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _mae(errors: list[float]) -> float:
    return sum(abs(error) for error in errors) / len(errors)


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 4)
