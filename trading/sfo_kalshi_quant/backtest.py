from __future__ import annotations

import math
from dataclasses import dataclass

from .config import StrategyConfig
from .models import ForecastOutcome, MarketBin
from .probability import ResidualCalibrator
from .standard_bins import standard_sfo_bins


@dataclass(frozen=True)
class CalibrationBucket:
    lower: float
    upper: float
    count: int
    avg_probability: float
    observed_frequency: float
    brier_score: float


@dataclass(frozen=True)
class CalibrationCohort:
    name: str
    count: int
    brier_score: float
    log_loss: float
    top_bin_accuracy: float
    avg_winning_probability: float


@dataclass(frozen=True)
class CalibrationBacktestResult:
    n: int
    brier_score: float
    log_loss: float
    top_bin_accuracy: float
    avg_winning_probability: float
    avg_entropy: float
    calibration_buckets: tuple[CalibrationBucket, ...]
    cohorts: tuple[CalibrationCohort, ...]


def run_walk_forward_calibration_backtest(
    outcomes: list[ForecastOutcome],
    *,
    config: StrategyConfig | None = None,
    min_train: int = 180,
    markets: list[MarketBin] | None = None,
) -> CalibrationBacktestResult:
    """Walk-forward probability backtest using historical forecast outcomes.

    This tests the model's probability calibration independent of Kalshi prices.
    Market-PnL backtests require archived entry-time quotes, which the collector
    stores going forward.
    """

    cfg = config or StrategyConfig()
    ladder = markets or standard_sfo_bins("KXHIGHTSFO-BACKTEST")
    scored = []
    calibration_samples: list[tuple[float, float]] = []
    for idx in range(min_train, len(outcomes)):
        train = outcomes[:idx]
        test = outcomes[idx]
        calibrator = ResidualCalibrator(train, cfg)
        probs = calibrator.bucket_probabilities(ladder, test.predicted_high_f)
        brier = 0.0
        winning_probability = 0.0
        top_ticker = max(probs.values(), key=lambda row: row.probability).ticker
        winning_ticker = None
        entropy = 0.0
        for market in ladder:
            probability = probs[market.ticker].probability
            # Half-up to the integer settlement value, matching the NWS/Kalshi
            # convention used by the forecast adapters (never banker's rounding).
            outcome = 1.0 if market.resolves_yes(math.floor(test.actual_high_f + 0.5)) else 0.0
            calibration_samples.append((probability, outcome))
            brier += (probability - outcome) ** 2
            entropy -= probability * math.log(max(probability, 1e-12))
            if outcome:
                winning_probability = probability
                winning_ticker = market.ticker
        scored.append(
            {
                "brier": brier,
                "log_loss": -math.log(max(winning_probability, 1e-12)),
                "top_hit": 1.0 if top_ticker == winning_ticker else 0.0,
                "winning_probability": winning_probability,
                "entropy": entropy,
                "actual_high_f": test.actual_high_f,
            }
        )
    if not scored:
        raise ValueError("Not enough outcomes for backtest")
    n = len(scored)
    return CalibrationBacktestResult(
        n=n,
        brier_score=sum(row["brier"] for row in scored) / n,
        log_loss=sum(row["log_loss"] for row in scored) / n,
        top_bin_accuracy=sum(row["top_hit"] for row in scored) / n,
        avg_winning_probability=sum(row["winning_probability"] for row in scored) / n,
        avg_entropy=sum(row["entropy"] for row in scored) / n,
        calibration_buckets=_calibration_buckets(calibration_samples),
        cohorts=_calibration_cohorts(scored),
    )


def _calibration_buckets(samples: list[tuple[float, float]]) -> tuple[CalibrationBucket, ...]:
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(10)]
    for probability, outcome in samples:
        bucket_idx = min(9, max(0, int(probability * 10.0)))
        buckets[bucket_idx].append((probability, outcome))

    rows = []
    for idx, bucket in enumerate(buckets):
        lower = idx / 10.0
        upper = (idx + 1) / 10.0
        if not bucket:
            rows.append(CalibrationBucket(lower, upper, 0, 0.0, 0.0, 0.0))
            continue
        count = len(bucket)
        avg_probability = sum(probability for probability, _ in bucket) / count
        observed_frequency = sum(outcome for _, outcome in bucket) / count
        brier_score = sum((probability - outcome) ** 2 for probability, outcome in bucket) / count
        rows.append(
            CalibrationBucket(
                lower=lower,
                upper=upper,
                count=count,
                avg_probability=avg_probability,
                observed_frequency=observed_frequency,
                brier_score=brier_score,
            )
        )
    return tuple(rows)


def _calibration_cohorts(scored: list[dict[str, float]]) -> tuple[CalibrationCohort, ...]:
    definitions = (
        ("cold_below_60f", lambda row: row["actual_high_f"] < 60.0),
        ("normal_60_69f", lambda row: 60.0 <= row["actual_high_f"] < 70.0),
        ("warm_70_79f", lambda row: 70.0 <= row["actual_high_f"] < 80.0),
        ("hot_80f_plus", lambda row: row["actual_high_f"] >= 80.0),
    )
    cohorts = []
    for name, predicate in definitions:
        rows = [row for row in scored if predicate(row)]
        if not rows:
            continue
        count = len(rows)
        cohorts.append(
            CalibrationCohort(
                name=name,
                count=count,
                brier_score=sum(row["brier"] for row in rows) / count,
                log_loss=sum(row["log_loss"] for row in rows) / count,
                top_bin_accuracy=sum(row["top_hit"] for row in rows) / count,
                avg_winning_probability=sum(row["winning_probability"] for row in rows) / count,
            )
        )
    return tuple(cohorts)
