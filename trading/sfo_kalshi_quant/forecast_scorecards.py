"""Leakage-resistant probabilistic forecast scorecards and promotion gates."""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

from ._util import _table_exists
from .cities import CITIES, CITY_BY_STATION
from .emos_sources import ROLLING_ORIGIN_V1_SOURCE, ROLLING_ORIGIN_V2_SOURCE
from .forecast_challengers import (
    ForecastCase,
    IntradayCase,
    evaluate_matched_lead_emos,
    evaluate_partial_pooled_intraday,
)
from .research_operate import EvaluationRun


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
    v2_scopes: set[tuple[str, int, str]] = set()
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
            v2_scopes = {
                (str(station), int(lead), str(method))
                for station, lead, method in conn.execute(
                    "SELECT DISTINCT station_id, lead_days, method "
                    "FROM forecast_emos_daily_high WHERE source = ?",
                    (ROLLING_ORIGIN_V2_SOURCE,),
                )
            }
            final_filter = "AND s.is_final = 1" if "is_final" in truth_columns else ""
            rows = conn.execute(
                f"""
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
                  {final_filter}
                ORDER BY f.station_id, f.lead_days, f.method, f.source, f.target_date
                """
            ).fetchall()
    except sqlite3.Error as exc:
        return _unavailable(f"{type(exc).__name__}: {exc}")

    rows = [
        row
        for row in rows
        if not (
            row[6] == ROLLING_ORIGIN_V1_SOURCE
            and (str(row[0]), int(row[2]), str(row[5])) in v2_scopes
        )
    ]
    if not rows:
        return _unavailable("no archived forecasts matched authoritative settlements")

    groups: dict[tuple[str, int, str, str], list[tuple[Any, ...]]] = defaultdict(list)
    for row in rows:
        groups[(str(row[0]), int(row[2]), str(row[5]), str(row[6]))].append(row)

    scorecards = [_score_group(key, cases) for key, cases in sorted(groups.items())]
    forecast_cases = [
        ForecastCase(
            station_id=str(row[0]),
            target_date=date.fromisoformat(str(row[1])),
            lead_days=int(row[2]),
            mu=float(row[3]),
            sigma=float(row[4]),
            actual=float(row[7]),
        )
        for row in rows
    ]
    shadow_challengers = {
        "matched_lead_emos": evaluate_matched_lead_emos(forecast_cases),
        "partial_pooled_intraday": evaluate_partial_pooled_intraday(
            _load_intraday_cases(path, rows)
        ),
    }
    return {
        "available": True,
        "truth_source": "cli_settlements joined by (station_id, target_date)",
        "matched_cases": len(rows),
        "cities_scored": len({row["station_id"] for row in scorecards}),
        "scorecards": scorecards,
        "challenger_gates": _challenger_gates(scorecards),
        "shadow_challengers": shadow_challengers,
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
        "shadow_challengers": {
            "matched_lead_emos": evaluate_matched_lead_emos([]),
            "partial_pooled_intraday": evaluate_partial_pooled_intraday([]),
        },
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _load_intraday_cases(
    path: Path,
    forecast_rows: list[tuple[Any, ...]],
) -> list[IntradayCase]:
    """One leakage-safe running-high case per station/day/two-hour bucket."""

    baseline = {
        (str(row[0]), str(row[1])): (float(row[3]), float(row[4]))
        for row in forecast_rows
        if int(row[2]) == 0
    }
    if not baseline:
        return []
    try:
        with sqlite3.connect(path) as conn:
            if not _table_exists(conn, "nws_station_observations"):
                return []
            truth_columns = _columns(conn, "cli_settlements")
            final_filter = "AND s.is_final = 1" if "is_final" in truth_columns else ""
            observations = conn.execute(
                f"""
                SELECT o.station_id, o.local_date, o.observed_at, o.temp_f,
                       s.max_temperature_f
                FROM nws_station_observations AS o
                JOIN cli_settlements AS s
                  ON s.station_id=o.station_id AND s.local_date=o.local_date
                WHERE o.temp_f IS NOT NULL AND s.max_temperature_f IS NOT NULL
                  {final_filter}
                ORDER BY o.station_id, o.local_date, o.observed_at
                """
            ).fetchall()
    except sqlite3.Error:
        return []

    running_high: dict[tuple[str, str], float] = {}
    latest: dict[tuple[str, str, int], IntradayCase] = {}
    for station, target, observed_at, temp_f, actual in observations:
        station_key = str(station)
        target_key = str(target)
        base = baseline.get((station_key, target_key))
        city = CITY_BY_STATION.get(station_key)
        if base is None or city is None:
            continue
        try:
            stamp = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            local_hour = stamp.astimezone(city.fixed_standard_timezone()).hour
            target_date = date.fromisoformat(target_key)
        except ValueError:
            continue
        day_key = (station_key, target_key)
        observed_high = max(running_high.get(day_key, float("-inf")), float(temp_f))
        running_high[day_key] = observed_high
        bucket = local_hour // 2
        latest[(station_key, target_key, bucket)] = IntradayCase(
            station_id=station_key,
            target_date=target_date,
            season=(target_date.month - 1) // 3,
            hour_bucket=bucket,
            observed_high_f=observed_high,
            baseline_mu=max(observed_high, base[0]),
            baseline_sigma=max(0.1, base[1]),
            actual=float(actual),
        )
    return list(latest.values())


# ---------------------------------------------------------------------------
# Task 7: publish the chronological research evidence loop.
#
# Plan Task 7 Step 3, verbatim: "Publish fold coverage, replay completeness,
# paired statistics, adjusted promotion gates, candidate version, and
# immutable experiment identity. Clearly label $50/day as a hard research
# KPI and show observed hit rate/shortfall." ``build_research_evaluation_report``
# is the one publication surface for that requirement: a PURE formatting
# function over ``research_operate.EvaluationRun`` (Task 7's own end-to-end
# orchestration result) -- it never touches the database, the clock, or
# config, and never mutates its input. The CLI layer
# (``_cli/research.py``'s ``cmd_research_evaluate``/
# ``cmd_research_propose_target``) is the only caller today.
#
# "$50/day is a hard research KPI, not a guarantee" (spec Sec 1/6): the
# label below is deliberately verbose about this, and the payload reports
# the OBSERVED hit rate/shortfall -- never a claim that the target was
# achieved or guaranteed when it was not.
# ---------------------------------------------------------------------------

RESEARCH_TARGET_KPI_LABEL = (
    "$50/day is a hard research KPI for research-target -- it is OBSERVED, "
    "never guaranteed. A missed day is recorded as a miss, not papered over."
)


def _research_kpi_payload(kpis: Any) -> dict[str, Any]:
    return {
        "observed_days": kpis.observed_days,
        "zero_activity_days": kpis.zero_activity_days,
        "realized_pnl_total": kpis.realized_pnl_total,
        "mean_daily_pnl": kpis.mean_daily_pnl,
        "median_daily_pnl": kpis.median_daily_pnl,
        "stdev_daily_pnl": kpis.stdev_daily_pnl,
        "positive_day_rate": kpis.positive_day_rate,
        "target_hit_rate": kpis.target_hit_rate,
        "after_fee_roi": kpis.after_fee_roi,
        "log_growth_per_day": kpis.log_growth_per_day,
        "maximum_drawdown_pct": kpis.maximum_drawdown_pct,
        "turnover_ratio": kpis.turnover_ratio,
        "fills": kpis.fills,
        "contracts": kpis.contracts,
        "dollars_at_risk": kpis.dollars_at_risk,
    }


def _research_kpi_delta_payload(delta: Any) -> dict[str, Any]:
    return {
        "mean_daily_pnl": delta.mean_daily_pnl,
        "positive_day_rate": delta.positive_day_rate,
        "target_hit_rate": delta.target_hit_rate,
        "after_fee_roi": delta.after_fee_roi,
        "log_growth_per_day": delta.log_growth_per_day,
        "maximum_drawdown_dollars": delta.maximum_drawdown_dollars,
    }


def _research_capacity_payload(capacity: Any) -> dict[str, Any] | None:
    if capacity is None:
        return None
    return {
        "policy_version": capacity.policy_version,
        "max_daily_utilization_pct": capacity.max_daily_utilization_pct,
        "window_total_utilization_pct": capacity.window_total_utilization_pct,
    }


def build_research_evaluation_report(run: EvaluationRun) -> dict[str, Any]:
    """Publish one Task 7 ``research-evaluate``/``research-propose-target``
    run: fold coverage, replay completeness, paired statistics, adjusted
    promotion gates, candidate version, and immutable experiment identity
    (plan Task 7 Step 3). Pure formatting over ``run``'s own fields.
    """

    declaration = run.declaration
    decision = run.decision
    report = run.report
    challenger_kpis = report.challenger_kpis

    observed_days = challenger_kpis.observed_days
    hit_rate = challenger_kpis.target_hit_rate
    mean_daily_pnl = challenger_kpis.mean_daily_pnl
    shortfall = (
        max(0.0, report.target_pnl - mean_daily_pnl) if mean_daily_pnl is not None else None
    )

    return {
        "experiment_identity": {
            "experiment_id": declaration.experiment_id,
            "hypothesis_family": declaration.hypothesis_family,
            "candidate_key": declaration.candidate_key,
            "candidate_version": declaration.candidate_version,
            "evidence_role": declaration.evidence_role,
            "predicted_edge_scope": declaration.predicted_edge_scope,
        },
        "fold_coverage": {
            "folds": len(run.walk_forward.folds),
            "unavailable_folds": len(run.walk_forward.unavailable),
            "skipped_historical_rows": len(run.walk_forward.skips),
        },
        "replay_completeness": {
            "paired_case_count": decision.paired_case_count,
            "coverage_exclusion_count": decision.coverage_exclusion_count,
            "fold_unavailable_count": decision.fold_unavailable_count,
            "crps_score_coverage_folds": decision.crps_score_coverage_folds,
            "brier_score_coverage_folds": decision.brier_score_coverage_folds,
            "calibration_pit_coverage_count": decision.calibration_pit_coverage_count,
        },
        "google_evidence_join": {
            "matched_row_count": run.google_join_matched_row_count,
            "vintage_mismatch_count": len(run.google_join_skips),
        },
        "paired_statistics": {
            "baseline_kpis": _research_kpi_payload(report.baseline_kpis),
            "challenger_kpis": _research_kpi_payload(challenger_kpis),
            "kpi_delta": _research_kpi_delta_payload(report.kpi_delta),
            "target_capacity": _research_capacity_payload(report.challenger_capacity.get("target")),
            "motion_capacity": _research_capacity_payload(report.challenger_capacity.get("motion")),
            "execution_model_versions": list(report.execution_model_versions),
            "side_scopes": list(report.side_scopes),
            "fill_scopes": list(report.fill_scopes),
        },
        "promotion_gate": {
            "eligible_for_target_paper": decision.eligible_for_target_paper,
            "effect_classification": decision.effect_classification,
            "instrument_scope_statement": decision.instrument_scope_statement,
            "block_reasons": list(decision.block_reasons),
            "independent_confirmatory_days": decision.independent_confirmatory_days,
            "distinct_calendar_target_days": decision.distinct_calendar_target_days,
            "holm_p_value": decision.holm_p_value,
            "holm_adjusted_significant": decision.holm_adjusted_significant,
            "prior_family_attempts": len(run.prior_family_attempts),
            # Always False: research_promotion.evaluate_promotion never
            # sets it, and this report never overrides it.
            "live_activation_allowed": decision.live_activation_allowed,
        },
        "daily_target_kpi": {
            "label": RESEARCH_TARGET_KPI_LABEL,
            "target_pnl": report.target_pnl,
            "observed_days": observed_days,
            "observed_hit_rate": hit_rate,
            "observed_hit_days": (
                round(hit_rate * observed_days) if hit_rate is not None else None
            ),
            "observed_mean_daily_pnl": mean_daily_pnl,
            "observed_shortfall_vs_target": shortfall,
        },
        "persistence": {
            "persisted_fold_count": run.persisted_fold_count,
            "skipped_fold_persist_reasons": list(run.skipped_fold_persist_reasons),
        },
    }
