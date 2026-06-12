from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .backtest import CalibrationBacktestResult, run_walk_forward_calibration_backtest
from .config import StrategyConfig
from .models import ForecastOutcome


DEFAULT_STACK_MIN_TRAIN = 120
DEFAULT_CALIBRATION_MIN_TRAIN = 120
DEFAULT_RIDGE_ALPHA = 10.0
DEFAULT_RIDGE_ALPHA_GRID = (0.1, 0.3, 1.0, 3.0, 10.0, 30.0, 100.0)
SYNTHETIC_FEATURES = (
    "lstm_high_f",
    "xgb_high_f",
    "model_spread_f",
    "season_sin",
    "season_cos",
    "yesterday_high_f",
)


@dataclass(frozen=True)
class SyntheticBlendRow:
    local_date: date
    actual_high_f: float
    settlement_high_f: float
    lstm_high_f: float
    xgb_high_f: float
    yesterday_high_f: float

    @property
    def model_spread_f(self) -> float:
        return self.xgb_high_f - self.lstm_high_f

    @property
    def season_sin(self) -> float:
        day_of_year = self.local_date.timetuple().tm_yday
        return math.sin(2.0 * math.pi * day_of_year / 366.0)

    @property
    def season_cos(self) -> float:
        day_of_year = self.local_date.timetuple().tm_yday
        return math.cos(2.0 * math.pi * day_of_year / 366.0)

    def feature_vector(self) -> list[float]:
        return [float(getattr(self, name)) for name in SYNTHETIC_FEATURES]


@dataclass(frozen=True)
class RidgeModel:
    means: tuple[float, ...]
    scales: tuple[float, ...]
    coefficients: tuple[float, ...]

    def predict(self, row: SyntheticBlendRow) -> float:
        features = row.feature_vector()
        value = self.coefficients[0]
        for idx, raw_feature in enumerate(features):
            scaled = (raw_feature - self.means[idx]) / self.scales[idx]
            value += self.coefficients[idx + 1] * scaled
        return value


def build_synthetic_blend_calibration(
    ab_test_path: Path,
    *,
    config: StrategyConfig | None = None,
    stack_min_train: int = DEFAULT_STACK_MIN_TRAIN,
    calibration_min_train: int = DEFAULT_CALIBRATION_MIN_TRAIN,
    ridge_alpha: float = DEFAULT_RIDGE_ALPHA,
    ridge_alpha_grid: tuple[float, ...] | list[float] | None = DEFAULT_RIDGE_ALPHA_GRID,
) -> dict[str, Any]:
    """Build a walk-forward synthetic blend calibration experiment.

    The experiment trains only on rows before each target date. It is synthetic
    because the project does not yet have enough archived point-in-time blend
    forecasts; instead, it learns from the historical LSTM/XGBoost comparison
    plus simple features available before the forecast target resolves.
    """

    rows = load_ab_test_daily_rows(ab_test_path)
    if len(rows) <= stack_min_train + calibration_min_train:
        raise ValueError(
            "not enough rows for synthetic blend calibration: "
            f"need more than {stack_min_train + calibration_min_train}, got {len(rows)}"
        )

    model_outcomes = build_walk_forward_outcomes(
        rows,
        stack_min_train=stack_min_train,
        ridge_alpha=ridge_alpha,
    )
    models = {
        name: _model_payload(outcomes, config=config, calibration_min_train=calibration_min_train)
        for name, outcomes in model_outcomes.items()
    }
    alpha_sweep = _ridge_alpha_sweep(
        rows,
        config=config,
        stack_min_train=stack_min_train,
        calibration_min_train=calibration_min_train,
        ridge_alphas=ridge_alpha_grid,
    )
    prediction_rows = _prediction_rows(model_outcomes)
    best_point = min(models, key=lambda name: models[name]["point"]["mae_f"])
    best_probability = min(models, key=lambda name: models[name]["calibration"]["brier_score"])
    best_alpha_by_brier = alpha_sweep[0]["ridge_alpha"] if alpha_sweep else ridge_alpha
    best_alpha_by_mae = (
        min(alpha_sweep, key=lambda row: row["point"]["mae_f"])["ridge_alpha"]
        if alpha_sweep
        else ridge_alpha
    )
    ridge = models["ridge_synthetic_blend"]
    lstm = models["lstm_same_window"]
    return {
        "schema_version": 1,
        "source": str(ab_test_path),
        "mode": "synthetic_historical_blend_calibration",
        "status": "research_only",
        "configuration": {
            "stack_min_train": stack_min_train,
            "calibration_min_train": calibration_min_train,
            "ridge_alpha": ridge_alpha,
            "ridge_alpha_grid": [row["ridge_alpha"] for row in alpha_sweep],
            "features": list(SYNTHETIC_FEATURES),
            "target": "next-day SFO high temperature",
            "kalshi_settlement": "actual highs are rounded before bin calibration",
        },
        "summary": {
            "input_rows": len(rows),
            "synthetic_rows": len(next(iter(model_outcomes.values()))),
            "first_synthetic_date": next(iter(model_outcomes.values()))[0].local_date.isoformat(),
            "last_synthetic_date": next(iter(model_outcomes.values()))[-1].local_date.isoformat(),
            "best_point_model": best_point,
            "best_probability_model": best_probability,
            "best_ridge_alpha_by_brier": best_alpha_by_brier,
            "best_ridge_alpha_by_mae": best_alpha_by_mae,
            "ridge_vs_lstm": {
                "mae_delta_f": round(
                    ridge["point"]["mae_f"] - lstm["point"]["mae_f"],
                    4,
                ),
                "brier_delta": round(
                    ridge["calibration"]["brier_score"] - lstm["calibration"]["brier_score"],
                    4,
                ),
                "log_loss_delta": round(
                    ridge["calibration"]["log_loss"] - lstm["calibration"]["log_loss"],
                    4,
                ),
            },
        },
        "models": models,
        "ridge_alpha_sweep": alpha_sweep,
        "recent_predictions": prediction_rows[-10:],
        "warnings": [
            "This is not yet the live landing-page blend history; it is a synthetic point-in-time proxy built from the historical LSTM/XGBoost comparison.",
            "Use this to research calibration before switching live trading probabilities.",
        ],
    }


def write_synthetic_blend_calibration(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def load_ab_test_daily_rows(ab_test_path: Path) -> list[SyntheticBlendRow]:
    payload = json.loads(Path(ab_test_path).read_text())
    try:
        daily = payload["target_daily_high_next_day"]["chart"]["daily"]
    except KeyError as exc:
        raise ValueError("ab test payload is missing target_daily_high_next_day chart rows") from exc

    raw_rows = sorted(daily, key=lambda row: row["date"])
    rows: list[SyntheticBlendRow] = []
    previous_actual: float | None = None
    for raw in raw_rows:
        actual = float(raw["actual"])
        lstm = float(raw["lstm"])
        row = SyntheticBlendRow(
            local_date=date.fromisoformat(raw["date"]),
            actual_high_f=actual,
            settlement_high_f=float(math.floor(actual + 0.5)),
            lstm_high_f=lstm,
            xgb_high_f=float(raw["xgb"]),
            yesterday_high_f=previous_actual if previous_actual is not None else lstm,
        )
        rows.append(row)
        previous_actual = actual
    return rows


def build_walk_forward_outcomes(
    rows: list[SyntheticBlendRow],
    *,
    stack_min_train: int = DEFAULT_STACK_MIN_TRAIN,
    ridge_alpha: float = DEFAULT_RIDGE_ALPHA,
) -> dict[str, list[ForecastOutcome]]:
    if stack_min_train < 30:
        raise ValueError("stack_min_train must be at least 30")
    if len(rows) <= stack_min_train:
        raise ValueError("not enough rows for requested stack_min_train")

    ridge_outcomes: list[ForecastOutcome] = []
    lstm_outcomes: list[ForecastOutcome] = []
    xgb_outcomes: list[ForecastOutcome] = []
    for idx in range(stack_min_train, len(rows)):
        train = rows[:idx]
        test = rows[idx]
        ridge = fit_ridge_model(train, ridge_alpha=ridge_alpha)
        ridge_outcomes.append(
            ForecastOutcome(
                local_date=test.local_date,
                predicted_high_f=ridge.predict(test),
                actual_high_f=test.settlement_high_f,
                model_name="ridge_synthetic_blend",
            )
        )
        lstm_outcomes.append(
            ForecastOutcome(
                local_date=test.local_date,
                predicted_high_f=test.lstm_high_f,
                actual_high_f=test.settlement_high_f,
                model_name="lstm_same_window",
            )
        )
        xgb_outcomes.append(
            ForecastOutcome(
                local_date=test.local_date,
                predicted_high_f=test.xgb_high_f,
                actual_high_f=test.settlement_high_f,
                model_name="xgb_same_window",
            )
        )
    return {
        "ridge_synthetic_blend": ridge_outcomes,
        "lstm_same_window": lstm_outcomes,
        "xgb_same_window": xgb_outcomes,
    }


def fit_ridge_model(rows: list[SyntheticBlendRow], *, ridge_alpha: float) -> RidgeModel:
    if not rows:
        raise ValueError("at least one row is required")
    feature_count = len(SYNTHETIC_FEATURES)
    columns = [row.feature_vector() for row in rows]
    means = tuple(sum(row[idx] for row in columns) / len(columns) for idx in range(feature_count))
    scales = []
    for idx, mean in enumerate(means):
        variance = sum((row[idx] - mean) ** 2 for row in columns) / len(columns)
        scales.append(math.sqrt(variance) or 1.0)

    size = feature_count + 1
    xtx = [[0.0 for _ in range(size)] for _ in range(size)]
    xty = [0.0 for _ in range(size)]
    for row, raw_features in zip(rows, columns, strict=True):
        features = [1.0] + [
            (raw_features[idx] - means[idx]) / scales[idx]
            for idx in range(feature_count)
        ]
        target = row.actual_high_f
        for i in range(size):
            xty[i] += features[i] * target
            for j in range(size):
                xtx[i][j] += features[i] * features[j]
    for idx in range(1, size):
        xtx[idx][idx] += ridge_alpha

    return RidgeModel(
        means=means,
        scales=tuple(scales),
        coefficients=tuple(_solve_linear_system(xtx, xty)),
    )


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


def _model_payload(
    outcomes: list[ForecastOutcome],
    *,
    config: StrategyConfig | None,
    calibration_min_train: int,
) -> dict[str, Any]:
    calibration = run_walk_forward_calibration_backtest(
        outcomes,
        config=config,
        min_train=calibration_min_train,
    )
    return {
        "point": _point_metrics(outcomes),
        "calibration": _calibration_payload(calibration),
    }


def _ridge_alpha_sweep(
    rows: list[SyntheticBlendRow],
    *,
    config: StrategyConfig | None,
    stack_min_train: int,
    calibration_min_train: int,
    ridge_alphas: tuple[float, ...] | list[float] | None,
) -> list[dict[str, Any]]:
    if not ridge_alphas:
        return []
    seen: set[float] = set()
    sweep_rows = []
    for alpha in ridge_alphas:
        alpha = float(alpha)
        if alpha <= 0.0 or alpha in seen:
            continue
        seen.add(alpha)
        outcomes = build_walk_forward_outcomes(
            rows,
            stack_min_train=stack_min_train,
            ridge_alpha=alpha,
        )["ridge_synthetic_blend"]
        payload = _model_payload(
            outcomes,
            config=config,
            calibration_min_train=calibration_min_train,
        )
        sweep_rows.append(
            {
                "ridge_alpha": alpha,
                "point": payload["point"],
                "calibration": payload["calibration"],
            }
        )
    sweep_rows.sort(
        key=lambda row: (
            row["calibration"]["brier_score"],
            row["calibration"]["log_loss"],
            row["point"]["mae_f"],
            row["ridge_alpha"],
        )
    )
    return sweep_rows


def _point_metrics(outcomes: list[ForecastOutcome]) -> dict[str, Any]:
    errors = [row.predicted_high_f - row.actual_high_f for row in outcomes]
    absolute_errors = [abs(error) for error in errors]
    squared_errors = [error**2 for error in errors]
    return {
        "n": len(outcomes),
        "mae_f": round(sum(absolute_errors) / len(absolute_errors), 4),
        "rmse_f": round(math.sqrt(sum(squared_errors) / len(squared_errors)), 4),
        "bias_f": round(sum(errors) / len(errors), 4),
    }


def _calibration_payload(result: CalibrationBacktestResult) -> dict[str, Any]:
    return {
        "n": result.n,
        "brier_score": round(result.brier_score, 4),
        "log_loss": round(result.log_loss, 4),
        "top_bin_accuracy": round(result.top_bin_accuracy, 4),
        "avg_winning_probability": round(result.avg_winning_probability, 4),
        "avg_entropy": round(result.avg_entropy, 4),
        "buckets": [
            {
                "range": f"{bucket.lower:.1f}-{bucket.upper:.1f}",
                "count": bucket.count,
                "avg_probability": round(bucket.avg_probability, 4),
                "observed_frequency": round(bucket.observed_frequency, 4),
                "brier_score": round(bucket.brier_score, 4),
            }
            for bucket in result.calibration_buckets
        ],
        "cohorts": [
            {
                "name": cohort.name,
                "count": cohort.count,
                "brier_score": round(cohort.brier_score, 4),
                "log_loss": round(cohort.log_loss, 4),
                "top_bin_accuracy": round(cohort.top_bin_accuracy, 4),
                "avg_winning_probability": round(cohort.avg_winning_probability, 4),
            }
            for cohort in result.cohorts
        ],
    }


def _prediction_rows(model_outcomes: dict[str, list[ForecastOutcome]]) -> list[dict[str, Any]]:
    ridge_rows = model_outcomes["ridge_synthetic_blend"]
    lstm_rows = model_outcomes["lstm_same_window"]
    xgb_rows = model_outcomes["xgb_same_window"]
    rows = []
    for ridge, lstm, xgb in zip(ridge_rows, lstm_rows, xgb_rows, strict=True):
        rows.append(
            {
                "date": ridge.local_date.isoformat(),
                "actual_high_f": ridge.actual_high_f,
                "ridge_synthetic_blend_f": round(ridge.predicted_high_f, 2),
                "lstm_f": round(lstm.predicted_high_f, 2),
                "xgb_f": round(xgb.predicted_high_f, 2),
                "ridge_abs_error_f": round(abs(ridge.predicted_high_f - ridge.actual_high_f), 2),
                "lstm_abs_error_f": round(abs(lstm.predicted_high_f - lstm.actual_high_f), 2),
            }
        )
    return rows
