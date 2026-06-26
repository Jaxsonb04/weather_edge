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
    # Climatological-prior Brier on the same cohort days (the baseline a useful
    # forecaster must beat) and the Brier Skill Score 1 - model/clim. A flat
    # absolute-Brier bar is unachievable on the interior 2F bins by ANY calibrated
    # model and is only met by catch-all-dominated cohorts, so the readiness gate
    # judges SKILL (model beats climatology -> skill > 0), not absolute Brier.
    climatology_brier_score: float
    brier_skill: float


@dataclass(frozen=True)
class CalibrationBacktestResult:
    n: int
    brier_score: float
    log_loss: float
    top_bin_accuracy: float
    avg_winning_probability: float
    avg_entropy: float
    climatology_brier_score: float
    brier_skill: float
    calibration_buckets: tuple[CalibrationBucket, ...]
    cohorts: tuple[CalibrationCohort, ...]


def run_walk_forward_calibration_backtest(
    outcomes: list[ForecastOutcome],
    *,
    config: StrategyConfig | None = None,
    min_train: int = 180,
    markets: list[MarketBin] | None = None,
    emos_lookup: dict | None = None,
) -> CalibrationBacktestResult:
    """Walk-forward probability backtest using historical forecast outcomes.

    This tests the model's probability calibration independent of Kalshi prices.
    Market-PnL backtests require archived entry-time quotes, which the collector
    stores going forward.

    ``emos_lookup`` (target_date -> (mu, sigma)) feeds the trained EMOS Gaussian
    into the calibrator; combined with ``config.emos_distribution_enabled`` it
    scores the EMOS-distribution path head-to-head against the residual-calibrated
    path on the same days. The EMOS (mu, sigma) values are already out-of-sample
    (rolling-origin), so the comparison stays leakage-safe.
    """

    cfg = config or StrategyConfig()
    ladder = markets or standard_sfo_bins("KXHIGHTSFO-BACKTEST")
    scored = []
    calibration_samples: list[tuple[float, float]] = []
    for idx in range(min_train, len(outcomes)):
        train = outcomes[:idx]
        test = outcomes[idx]
        calibrator = ResidualCalibrator(train, cfg)
        emos_mu_sigma = emos_lookup.get(test.local_date) if emos_lookup else None
        probs = calibrator.bucket_probabilities(
            ladder, test.predicted_high_f, emos_mu_sigma=emos_mu_sigma
        )
        # Climatological prior: each bin's marginal YES-frequency over the
        # training window, independent of the forecast. It is the no-skill
        # baseline a useful forecaster must beat; per-bin Brier vs this prior
        # gives a Brier Skill Score that does not penalize irreducible multi-bin
        # spread the way a flat absolute-Brier threshold does.
        clim_prior = _climatological_prior(ladder, train)
        brier = 0.0
        clim_brier = 0.0
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
            clim_brier += (clim_prior[market.ticker] - outcome) ** 2
            entropy -= probability * math.log(max(probability, 1e-12))
            if outcome:
                winning_probability = probability
                winning_ticker = market.ticker
        scored.append(
            {
                "brier": brier,
                "clim_brier": clim_brier,
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
    model_brier = sum(row["brier"] for row in scored) / n
    clim_brier_overall = sum(row["clim_brier"] for row in scored) / n
    return CalibrationBacktestResult(
        n=n,
        brier_score=model_brier,
        log_loss=sum(row["log_loss"] for row in scored) / n,
        top_bin_accuracy=sum(row["top_hit"] for row in scored) / n,
        avg_winning_probability=sum(row["winning_probability"] for row in scored) / n,
        avg_entropy=sum(row["entropy"] for row in scored) / n,
        climatology_brier_score=clim_brier_overall,
        brier_skill=_brier_skill(model_brier, clim_brier_overall),
        calibration_buckets=_calibration_buckets(calibration_samples),
        cohorts=_calibration_cohorts(scored),
    )


def _climatological_prior(
    ladder: list[MarketBin], train: list[ForecastOutcome]
) -> dict[str, float]:
    """Each bin's marginal YES-frequency over the training window (no-skill prior)."""

    n = len(train)
    if n == 0:
        return {market.ticker: 0.0 for market in ladder}
    settled = [math.floor(o.actual_high_f + 0.5) for o in train]
    return {
        market.ticker: sum(1 for high in settled if market.resolves_yes(high)) / n
        for market in ladder
    }


def _brier_skill(model_brier: float, clim_brier: float) -> float:
    """Brier Skill Score: 1 - model/clim. > 0 means the model beats climatology."""

    if clim_brier <= 0.0:
        return 0.0
    return 1.0 - (model_brier / clim_brier)


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
        cohort_brier = sum(row["brier"] for row in rows) / count
        cohort_clim_brier = sum(row["clim_brier"] for row in rows) / count
        cohorts.append(
            CalibrationCohort(
                name=name,
                count=count,
                brier_score=cohort_brier,
                log_loss=sum(row["log_loss"] for row in rows) / count,
                top_bin_accuracy=sum(row["top_hit"] for row in rows) / count,
                avg_winning_probability=sum(row["winning_probability"] for row in rows) / count,
                climatology_brier_score=cohort_clim_brier,
                brier_skill=_brier_skill(cohort_brier, cohort_clim_brier),
            )
        )
    return tuple(cohorts)
