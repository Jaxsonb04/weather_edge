"""Prospective, walk-forward forecast challengers that never affect live weights."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from statistics import fmean
from typing import Iterable


@dataclass(frozen=True)
class ForecastCase:
    station_id: str
    target_date: date
    lead_days: int
    mu: float
    sigma: float
    actual: float


@dataclass(frozen=True)
class IntradayCase:
    station_id: str
    target_date: date
    season: int
    hour_bucket: int
    observed_high_f: float
    baseline_mu: float
    baseline_sigma: float
    actual: float


def evaluate_matched_lead_emos(cases: Iterable[ForecastCase]) -> dict[str, object]:
    """Exact station/lead trailing calibration evaluated strictly forward."""

    history: dict[tuple[str, int], list[ForecastCase]] = defaultdict(list)
    scored: list[tuple[str, float, float, float, float]] = []
    for case in sorted(cases, key=lambda row: (row.target_date, row.station_id, row.lead_days)):
        prior = history[(case.station_id, case.lead_days)]
        errors = [row.mu - row.actual for row in prior[-60:]]
        n = len(errors)
        bias = (n / (n + 10.0)) * fmean(errors) if errors else 0.0
        z_sq = [
            ((row.actual - (row.mu - bias)) / max(0.1, row.sigma)) ** 2
            for row in prior[-60:]
        ]
        raw_scale = math.sqrt(fmean(z_sq)) if z_sq else 1.0
        scale = min(1.5, max(0.75, 1.0 + (n / (n + 10.0)) * (raw_scale - 1.0)))
        candidate_mu = case.mu - bias
        candidate_sigma = max(0.1, case.sigma * scale)
        scored.append(
            (
                case.target_date.isoformat(),
                _crps(case.mu, case.sigma, case.actual),
                _crps(candidate_mu, candidate_sigma, case.actual),
                _coverage(case.mu, case.sigma, case.actual),
                _coverage(candidate_mu, candidate_sigma, case.actual),
            )
        )
        prior.append(case)
    return _summary(
        "matched_lead_emos",
        scored,
        note="exact station/lead trailing bias and dispersion; shadow only",
    )


def evaluate_partial_pooled_intraday(
    cases: Iterable[IntradayCase],
) -> dict[str, object]:
    """City/season/hour residual adjustment with hierarchical shrinkage."""

    history: list[IntradayCase] = []
    scored: list[tuple[str, float, float, float, float]] = []
    for case in sorted(
        cases,
        key=lambda row: (row.target_date, row.station_id, row.hour_bucket),
    ):
        prior = [row for row in history if row.target_date < case.target_date]
        hour = [
            row.actual - row.baseline_mu
            for row in prior
            if row.hour_bucket == case.hour_bucket
        ]
        season = [
            row.actual - row.baseline_mu
            for row in prior
            if row.hour_bucket == case.hour_bucket and row.season == case.season
        ]
        station = [
            row.actual - row.baseline_mu
            for row in prior
            if row.hour_bucket == case.hour_bucket
            and row.season == case.season
            and row.station_id == case.station_id
        ]
        hour_estimate = _shrunken_mean(hour, 20.0, 0.0)
        season_estimate = _shrunken_mean(season, 12.0, hour_estimate)
        adjustment = _shrunken_mean(station, 8.0, season_estimate)
        candidate_mu = max(case.observed_high_f, case.baseline_mu + adjustment)
        scored.append(
            (
                case.target_date.isoformat(),
                _crps(case.baseline_mu, case.baseline_sigma, case.actual),
                _crps(candidate_mu, case.baseline_sigma, case.actual),
                _coverage(case.baseline_mu, case.baseline_sigma, case.actual),
                _coverage(candidate_mu, case.baseline_sigma, case.actual),
            )
        )
        history.append(case)
    return _summary(
        "partial_pooled_intraday",
        scored,
        note="city/season/hour residual hierarchy; shadow only",
    )


def _shrunken_mean(values: list[float], k: float, parent: float) -> float:
    if not values:
        return parent
    n = len(values)
    weight = n / (n + k)
    return weight * fmean(values) + (1.0 - weight) * parent


def _crps(mu: float, sigma: float, actual: float) -> float:
    sigma = max(0.1, float(sigma))
    z = (actual - mu) / sigma
    cdf = 0.5 * math.erfc(-z / math.sqrt(2.0))
    density = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    return sigma * (
        z * (2.0 * cdf - 1.0)
        + 2.0 * density
        - 1.0 / math.sqrt(math.pi)
    )


def _coverage(mu: float, sigma: float, actual: float) -> float:
    return float(abs(actual - mu) <= 1.28155157 * max(0.1, sigma))


def _summary(
    key: str,
    scored: list[tuple[str, float, float, float, float]],
    *,
    note: str,
) -> dict[str, object]:
    if not scored:
        return {
            "key": key,
            "available": False,
            "active": False,
            "promotion_eligible": False,
            "cases": 0,
            "block_reasons": ["no matched walk-forward cases"],
            "note": note,
        }
    deltas = [(day, candidate - baseline) for day, baseline, candidate, _, _ in scored]
    ci = _day_clustered_ci(deltas)
    baseline_crps = fmean(row[1] for row in scored)
    candidate_crps = fmean(row[2] for row in scored)
    baseline_coverage = fmean(row[3] for row in scored)
    candidate_coverage = fmean(row[4] for row in scored)
    independent_days = len({row[0] for row in scored})
    reasons: list[str] = []
    if independent_days < 30:
        reasons.append(f"{independent_days}/30 independent matched days")
    if ci is None or ci[1] >= 0:
        reasons.append("paired day-clustered 95% CRPS improvement is not below zero")
    if abs(candidate_coverage - 0.80) > 0.10:
        reasons.append("candidate 80% interval coverage gap exceeds 0.10")
    statistical_gates_passed = not reasons
    reasons.append("paired after-fee trading replay is not recorded")
    return {
        "key": key,
        "available": True,
        "active": False,
        "promotion_eligible": False,
        "statistical_gates_passed": statistical_gates_passed,
        "cases": len(scored),
        "independent_days": independent_days,
        "baseline_crps": round(baseline_crps, 6),
        "candidate_crps": round(candidate_crps, 6),
        "paired_crps_delta": round(candidate_crps - baseline_crps, 6),
        "paired_crps_delta_ci95": (
            [round(ci[0], 6), round(ci[1], 6)] if ci is not None else None
        ),
        "baseline_80_coverage": round(baseline_coverage, 6),
        "candidate_80_coverage": round(candidate_coverage, 6),
        "block_reasons": reasons,
        "note": note,
    }


def _day_clustered_ci(
    deltas: list[tuple[str, float]],
    *,
    samples: int = 1000,
) -> tuple[float, float] | None:
    by_day: dict[str, list[float]] = defaultdict(list)
    for day, delta in deltas:
        by_day[day].append(delta)
    days = sorted(by_day)
    if len(days) < 2:
        return None
    rng = random.Random(0)
    draws: list[float] = []
    for _ in range(samples):
        selected = [rng.choice(days) for _ in days]
        draws.append(fmean(delta for day in selected for delta in by_day[day]))
    draws.sort()
    return draws[int(0.025 * (samples - 1))], draws[int(0.975 * (samples - 1))]
