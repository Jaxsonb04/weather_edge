"""Leakage-resistant probabilistic forecast scorecards and promotion gates."""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any

from .cities import CITIES, CITY_BY_STATION


_SQRT_2PI = math.sqrt(2.0 * math.pi)
_INTERVAL_Z = {"50": 0.67448975, "80": 1.28155157, "90": 1.64485363}


def _cdf(value: float) -> float:
    return 0.5 * math.erfc(-value / math.sqrt(2.0))


def _crps(mu: float, sigma: float, actual: float) -> float:
    sigma = max(float(sigma), 0.1)
    z = (actual - mu) / sigma
    phi = math.exp(-0.5 * z * z) / _SQRT_2PI
    return sigma * (z * (2.0 * _cdf(z) - 1.0) + 2.0 * phi - 1.0 / math.sqrt(math.pi))


def _threshold_brier(mu: float, sigma: float, actual: float) -> float:
    """Mean Brier score over integer temperature CDF thresholds (-50..140F)."""

    sigma = max(float(sigma), 0.1)
    scores = []
    for threshold in range(-50, 141):
        probability = _cdf((threshold + 0.5 - mu) / sigma)
        observed = 1.0 if actual <= threshold else 0.0
        scores.append((probability - observed) ** 2)
    return fmean(scores)


def _log_score(mu: float, sigma: float, actual: float) -> float:
    sigma = max(float(sigma), 0.1)
    z = (actual - mu) / sigma
    return math.log(sigma * _SQRT_2PI) + 0.5 * z * z


def build_forecast_scorecards(weather_db: Path | str) -> dict[str, Any]:
    """Score archived distributions against station-keyed CLI settlement truth.

    Embedded ``actual_high_f`` values are deliberately ignored.  Only an inner
    join on ``(station_id, target_date)`` to ``cli_settlements`` can create a
    scored case.
    """

    path = Path(weather_db)
    if not path.exists():
        return _unavailable("weather database not found")
    try:
        with sqlite3.connect(path) as conn:
            if not _table_exists(conn, "forecast_emos_daily_high"):
                return _unavailable("forecast_emos_daily_high table missing")
            if not _table_exists(conn, "cli_settlements"):
                return _unavailable("cli_settlements table missing")
            forecast_columns = _columns(conn, "forecast_emos_daily_high")
            truth_columns = _columns(conn, "cli_settlements")
            if "station_id" not in forecast_columns or "station_id" not in truth_columns:
                return _unavailable("station-keyed forecast and settlement tables required")
            rows = conn.execute(
                """
                SELECT f.station_id, f.target_date, f.lead_days,
                       f.predicted_high_f, f.sigma_f, f.method, f.source,
                       s.max_temperature_f
                FROM forecast_emos_daily_high AS f
                JOIN cli_settlements AS s
                  ON s.station_id = f.station_id
                 AND s.local_date = f.target_date
                WHERE f.source != 'live'
                  AND f.predicted_high_f IS NOT NULL
                  AND f.sigma_f IS NOT NULL
                ORDER BY f.station_id, f.lead_days, f.method, f.source, f.target_date
                """
            ).fetchall()
    except sqlite3.Error as exc:
        return _unavailable(f"{type(exc).__name__}: {exc}")

    if not rows:
        return _unavailable("no archived forecasts matched authoritative settlements")

    groups: dict[tuple[str, int, str, str], list[tuple[Any, ...]]] = defaultdict(list)
    for row in rows:
        groups[(str(row[0]), int(row[2]), str(row[5]), str(row[6]))].append(row)

    scorecards = [_score_group(key, cases) for key, cases in sorted(groups.items())]
    return {
        "available": True,
        "truth_source": "cli_settlements joined by (station_id, target_date)",
        "matched_cases": len(rows),
        "cities_scored": len({row["station_id"] for row in scorecards}),
        "scorecards": scorecards,
        "challenger_gates": _challenger_gates(scorecards),
        "promotion_policy": {
            "crps": "paired improvement confidence interval must be entirely below zero",
            "city_regression_limit": 0.02,
            "interval_coverage_error_max": 0.05,
            "activation": "qualifying city/lead combinations only unless every city passes",
        },
    }


def _score_group(key: tuple[str, int, str, str], rows: list[tuple[Any, ...]]) -> dict[str, Any]:
    station, lead, method, source = key
    errors: list[float] = []
    crps: list[float] = []
    brier: list[float] = []
    log_scores: list[float] = []
    pits: list[float] = []
    coverage = {key: 0 for key in _INTERVAL_Z}
    for row in rows:
        mu, sigma, actual = float(row[3]), max(float(row[4]), 0.1), float(row[7])
        errors.append(abs(mu - actual))
        crps.append(_crps(mu, sigma, actual))
        brier.append(_threshold_brier(mu, sigma, actual))
        log_scores.append(_log_score(mu, sigma, actual))
        pits.append(_cdf((actual - mu) / sigma))
        for label, z_value in _INTERVAL_Z.items():
            coverage[label] += int(mu - z_value * sigma <= actual <= mu + z_value * sigma)
    count = len(rows)
    city = CITY_BY_STATION.get(station)
    return {
        "city": city.name if city else station,
        "station_id": station,
        "lead_days": lead,
        "method": method,
        "source": source,
        "cases": count,
        "first_target_date": str(rows[0][1]),
        "last_target_date": str(rows[-1][1]),
        "mae_f": round(fmean(errors), 4),
        "crps": round(fmean(crps), 4),
        "ranked_probability_score": round(fmean(brier) * 191.0, 4),
        "threshold_mean_brier": round(fmean(brier), 6),
        "log_score": round(fmean(log_scores), 4),
        "pit_mean": round(fmean(pits), 4),
        "interval_coverage": {label: round(value / count, 4) for label, value in coverage.items()},
        "coverage_error": {
            label: round(value / count - int(label) / 100.0, 4)
            for label, value in coverage.items()
        },
    }


def _challenger_gates(scorecards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    city_count = len(CITIES)
    pooled_cases = sum(int(row["cases"]) for row in scorecards)
    by_method: dict[str, dict[str, int]] = defaultdict(dict)
    for row in scorecards:
        station = str(row["station_id"])
        method = str(row["method"]).lower()
        by_method[method][station] = max(by_method[method].get(station, 0), int(row["cases"]))

    return [
        {
            "key": "current_ols_weighted_baseline",
            "label": "Current OLS-weighted baseline",
            "status": "active_reference",
            "promotion_eligible": False,
            "block_reasons": ["reference arm; not a challenger"],
        },
        _gate(
            "minimum_crps_emos", "Minimum-CRPS EMOS", by_method, 90,
            tags=("min_crps_nested",), city_count=city_count,
            extra="nested 30/60/90-day window selection evidence is not persisted",
        ),
        _gate(
            "time_series_emos", "Time-series EMOS", by_method, 180,
            tags=("time_series_emos", "ts_emos"), city_count=city_count,
        ),
        _gate(
            "analog_ensemble", "Analog ensemble", by_method, 365,
            tags=("analog_ensemble", "anen"), city_count=city_count,
        ),
        _gate(
            "pooled_distributional", "Pooled station-aware distributional model", by_method, 365,
            tags=("pooled_distributional", "station_embedding"), city_count=city_count,
            required_pooled=5000, pooled_cases=pooled_cases,
        ),
    ]


def _gate(
    key: str,
    label: str,
    by_method: dict[str, dict[str, int]],
    required: int,
    *,
    tags: tuple[str, ...],
    city_count: int,
    extra: str | None = None,
    required_pooled: int | None = None,
    pooled_cases: int = 0,
) -> dict[str, Any]:
    matches = {
        station: cases
        for method, stations in by_method.items()
        if any(tag in method for tag in tags)
        for station, cases in stations.items()
    }
    qualified = sorted(station for station, cases in matches.items() if cases >= required)
    reasons: list[str] = []
    if len(qualified) < city_count:
        reasons.append(f"{len(qualified)}/{city_count} cities have at least {required} matched cases")
    if required_pooled is not None and pooled_cases < required_pooled:
        reasons.append(f"{pooled_cases}/{required_pooled} pooled station-days available")
    if extra and not matches:
        reasons.append(extra)
    reasons.append("paired CRPS confidence interval and city-regression checks not yet recorded")
    return {
        "key": key,
        "label": label,
        "status": "collect_only" if reasons else "eligible_for_shadow_review",
        "promotion_eligible": False,
        "required_cases_per_city": required,
        "required_pooled_station_days": required_pooled,
        "qualified_stations": qualified,
        "block_reasons": reasons,
    }


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "matched_cases": 0,
        "cities_scored": 0,
        "scorecards": [],
        "challenger_gates": _challenger_gates([]),
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
