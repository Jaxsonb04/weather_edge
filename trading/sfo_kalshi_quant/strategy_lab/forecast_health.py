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

def _forecast_health_payload(
    forecaster_root: Path,
    *,
    config: StrategyConfig,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_utc = _coerce_utc(now)
    today = settlement_today(current_utc)
    rolling_targets = [
        (today + timedelta(days=offset)).isoformat()
        for offset in range(FORECAST_HEALTH_ROLLING_DAYS)
    ]
    db_path = Path(forecaster_root) / "weather.db"
    warnings: list[dict[str, Any]] = []
    payload: dict[str, Any] = {
        "available": False,
        "db_path_hint": str(db_path),
        "generated_at": current_utc.isoformat(),
        "rolling_targets": rolling_targets,
        "nwp": {"available": False, "targets": []},
        "emos": {
            "available": False,
            "profiles_using_emos": _emos_enabled_profiles(config),
            "live_targets": [],
        },
        "clisfo": {"available": False},
        "nws_ground_truth": {"available": False},
        "warnings": warnings,
    }
    if not db_path.exists():
        warnings.append(
            _health_warning(
                "critical",
                "weather-db-missing",
                "Weather DB missing",
                f"Strategy Lab cannot inspect NWP, EMOS, or CLISFO health at {db_path}.",
                "Verify the AWS forecaster runtime path and refresh service.",
            )
        )
        return payload

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            payload["available"] = True
            payload["nwp"] = _nwp_health(conn, rolling_targets, current_utc, warnings)
            payload["emos"] = _emos_health(
                conn,
                rolling_targets,
                current_utc,
                warnings,
                profiles_using_emos=_emos_enabled_profiles(config),
            )
            payload["clisfo"] = _clisfo_health(conn, current_utc, warnings)
            payload["nws_ground_truth"] = _nws_ground_truth_health(conn, today)
    except sqlite3.Error as exc:
        payload["available"] = False
        warnings.append(
            _health_warning(
                "critical",
                "weather-db-unreadable",
                "Weather DB unreadable",
                f"{type(exc).__name__}: {exc}",
                "Inspect weather.db permissions and SQLite integrity on AWS.",
            )
        )
    return payload


def _nwp_health(
    conn: sqlite3.Connection,
    rolling_targets: list[str],
    current_utc: datetime,
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    if not _sqlite_table_exists(conn, "nwp_model_forecasts"):
        warnings.append(
            _health_warning(
                "warning",
                "nwp-table-missing",
                "NWP archive missing",
                "The nwp_model_forecasts table is not present in weather.db.",
                "Run the forecaster NWP archive refresh and check its logs.",
            )
        )
        return {"available": False, "targets": [], "reason": "nwp_model_forecasts table missing"}

    # Availability calendar (why each rolling target is checked differently):
    # the previous-runs ARCHIVE is refreshed by the nightly maintenance unit
    # with a fetch window ending at today+1, so the only target with reliably
    # complete archive rows around the clock is TODAY at lead 1 -- tomorrow's
    # rows appear only after the nightly run (and cover a handful of stations
    # until the day's model runs publish), and today+2 is never inside the
    # fetch window at all. Demanding lead-1 archive rows for future targets is
    # what produced the daily false "nwp-thin-target" for today+2. Tomorrow and
    # the 2-day-out target are instead checked against the LIVE serve's model
    # coverage (forecast_emos_daily_high.n_models, refreshed every tick from
    # the current-run forecast API) -- the models actually feeding those
    # markets' distributions.
    targets: list[dict[str, Any]] = []
    for offset, target in enumerate(rolling_targets):
        if offset == 0:
            row = conn.execute(
                """
                SELECT target_date,
                       lead_days,
                       COUNT(DISTINCT model) AS model_count,
                       MAX(fetched_at) AS latest_fetched_at,
                       GROUP_CONCAT(DISTINCT source) AS sources
                FROM nwp_model_forecasts
                WHERE target_date = ? AND lead_days = 1
                GROUP BY target_date, lead_days
                """,
                (target,),
            ).fetchone()
            target_health = _nwp_target_health(row, current_utc, target)
            target_health["check"] = "archive_lead1"
            targets.append(target_health)
            if target_health["model_count"] < FORECAST_HEALTH_MIN_NWP_MODELS:
                warnings.append(
                    _health_warning(
                        "warning",
                        "nwp-thin-target",
                        "Thin NWP target",
                        (
                            f"{target} has {target_health['model_count']} model(s), below "
                            f"the {FORECAST_HEALTH_MIN_NWP_MODELS}-model health floor."
                        ),
                        "Check the NWP archive refresh log for source outages.",
                        target_date=target,
                    )
                )
            if target_health.get("latest_age_hours") is not None and (
                target_health["latest_age_hours"]
                > FORECAST_HEALTH_MAX_NWP_AGE.total_seconds() / 3600
            ):
                warnings.append(
                    _health_warning(
                        "warning",
                        "nwp-stale-target",
                        "Stale NWP target",
                        f"{target} NWP data is {target_health['latest_age_hours']:.1f} hours old.",
                        "Check sfo-forecaster-refresh.service and the NWP archive step.",
                        target_date=target,
                    )
                )
            continue
        target_health = _nwp_live_serve_health(conn, target, current_utc)
        targets.append(target_health)
        model_count = target_health["model_count"]
        # A missing live serve for the target is _emos_health's
        # "emos-live-missing" alarm; only a PRESENT-but-thin serve is an NWP
        # coverage problem, so model_count None never warns here.
        if model_count is not None and model_count < FORECAST_HEALTH_MIN_NWP_MODELS:
            warnings.append(
                _health_warning(
                    "warning",
                    "nwp-thin-target",
                    "Thin NWP target",
                    (
                        f"{target} live serve has a station with {model_count} model(s), "
                        f"below the {FORECAST_HEALTH_MIN_NWP_MODELS}-model health floor."
                    ),
                    "Check the live EMOS serve log for current-run model outages.",
                    target_date=target,
                )
            )

    recent = [
        _round_dict(dict(row))
        for row in conn.execute(
            """
            SELECT target_date,
                   lead_days,
                   COUNT(DISTINCT model) AS model_count,
                   MAX(fetched_at) AS latest_fetched_at,
                   GROUP_CONCAT(DISTINCT source) AS sources
            FROM nwp_model_forecasts
            GROUP BY target_date, lead_days
            ORDER BY target_date DESC, lead_days
            LIMIT 21
            """
        ).fetchall()
    ]
    return {
        "available": True,
        "min_healthy_models": FORECAST_HEALTH_MIN_NWP_MODELS,
        "max_stale_hours": FORECAST_HEALTH_MAX_NWP_AGE.total_seconds() / 3600,
        "targets": targets,
        "recent_targets": recent,
    }


def _nwp_target_health(row: sqlite3.Row | None, current_utc: datetime, target: str) -> dict[str, Any]:
    if row is None:
        return {
            "target_date": target,
            "lead_days": 1,
            "model_count": 0,
            "latest_fetched_at": None,
            "latest_age_hours": None,
            "sources": [],
        }
    latest = _parse_timestamp(row["latest_fetched_at"])
    return {
        "target_date": str(row["target_date"]),
        "lead_days": int(row["lead_days"]),
        "model_count": int(row["model_count"] or 0),
        "latest_fetched_at": row["latest_fetched_at"],
        "latest_age_hours": _age_hours(current_utc, latest),
        "sources": _split_csv(row["sources"]),
    }


def _nwp_live_serve_health(
    conn: sqlite3.Connection, target: str, current_utc: datetime
) -> dict[str, Any]:
    """Model coverage for a future rolling target via the live EMOS serve.

    The archive cannot hold rows for targets past today+1 (nightly fetch
    window), so the model count that matters for those markets is n_models on
    the freshest live serve per station: the worst station's coverage is the
    honest number. ``model_count`` is None when no live rows exist yet -- that
    absence is reported by _emos_health, not here.
    """

    empty = {
        "target_date": target,
        "lead_days": None,
        "check": "live_serve_models",
        "model_count": None,
        "latest_fetched_at": None,
        "latest_age_hours": None,
        "sources": [],
    }
    if not _sqlite_table_exists(conn, "forecast_emos_daily_high"):
        return empty
    station_expr = (
        "station_id"
        if "station_id" in _sqlite_columns(conn, "forecast_emos_daily_high")
        else "'KSFO' AS station_id"
    )
    rows = conn.execute(
        f"""
        SELECT {station_expr}, n_models, fetched_at
        FROM forecast_emos_daily_high
        WHERE source = 'live' AND target_date = ?
        ORDER BY fetched_at DESC
        """,
        (target,),
    ).fetchall()
    freshest: dict[str, sqlite3.Row] = {}
    for row in rows:
        freshest.setdefault(str(row["station_id"]), row)
    if not freshest:
        return empty
    latest_fetched = max(str(row["fetched_at"]) for row in freshest.values())
    return {
        "target_date": target,
        "lead_days": None,
        "check": "live_serve_models",
        "model_count": min(int(row["n_models"] or 0) for row in freshest.values()),
        "latest_fetched_at": latest_fetched,
        "latest_age_hours": _age_hours(current_utc, _parse_timestamp(latest_fetched)),
        "sources": ["live_emos_serve"],
    }


def _emos_health(
    conn: sqlite3.Connection,
    rolling_targets: list[str],
    current_utc: datetime,
    warnings: list[dict[str, Any]],
    *,
    profiles_using_emos: list[str],
) -> dict[str, Any]:
    if not _sqlite_table_exists(conn, "forecast_emos_daily_high"):
        warnings.append(
            _health_warning(
                "warning",
                "emos-table-missing",
                "EMOS table missing",
                "The forecast_emos_daily_high table is not present in weather.db.",
                "Run the EMOS archive and live serving step.",
            )
        )
        return {
            "available": False,
            "profiles_using_emos": profiles_using_emos,
            "live_targets": [],
            "reason": "forecast_emos_daily_high table missing",
        }

    # Per-station, per-target: the serve writes one live row per city per
    # rolling target per tick (15 cities x 3 targets at leads 0..2), so a
    # global "latest 12 rows" slice conflated cities and fired a false
    # "emos-live-missing" for today/today+1 on every scan. The freshest row
    # per (station, target) -- at ANY lead, which is how the trader reads it
    # and how the lead-0 same-day serve lands -- is the unit of health.
    station_keyed = "station_id" in _sqlite_columns(conn, "forecast_emos_daily_high")
    expected_stations = (
        [city.nws_station_id for city in CITIES] if station_keyed else ["KSFO"]
    )
    station_expr = "station_id" if station_keyed else "'KSFO' AS station_id"
    placeholders = ",".join("?" for _ in rolling_targets)
    live_rows = conn.execute(
        f"""
        SELECT {station_expr}, target_date, lead_days, predicted_high_f, sigma_f,
               n_models, fetched_at, method, source
        FROM forecast_emos_daily_high
        WHERE source = 'live' AND target_date IN ({placeholders})
        ORDER BY fetched_at DESC
        """,
        tuple(rolling_targets),
    ).fetchall()
    latest_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in live_rows:
        key = (str(row["station_id"]), str(row["target_date"]))
        latest_by_key.setdefault(key, _emos_row(row, current_utc))

    # A settled (station, target) is done: the serve refuses to overwrite a
    # settled day by design, so neither its absence nor its age is a failure.
    settled: set[tuple[str, str]] = set()
    if _sqlite_table_exists(conn, "cli_settlements"):
        final_filter = (
            "AND is_final = 1"
            if "is_final" in _sqlite_columns(conn, "cli_settlements")
            else ""
        )
        settled = {
            (str(row["station_id"]), str(row["local_date"]))
            for row in conn.execute(
                f"""
                SELECT station_id, local_date FROM cli_settlements
                WHERE local_date IN ({placeholders}) AND max_temperature_f IS NOT NULL
                  {final_filter}
                """,
                tuple(rolling_targets),
            ).fetchall()
        }

    live_targets = [
        latest_by_key[(station, target)]
        for target in rolling_targets
        for station in expected_stations
        if (station, target) in latest_by_key
    ]
    max_age_hours = FORECAST_HEALTH_MAX_EMOS_AGE.total_seconds() / 3600
    for station in expected_stations:
        open_targets = [
            target for target in rolling_targets if (station, target) not in settled
        ]
        missing = [
            target for target in open_targets if (station, target) not in latest_by_key
        ]
        if missing and profiles_using_emos:
            warnings.append(
                _health_warning(
                    "warning",
                    "emos-live-missing",
                    "Live EMOS target missing",
                    (
                        f"{station} has no live EMOS distribution for "
                        f"{', '.join(missing)}; EMOS-enabled profiles degrade to "
                        f"residual calibration there."
                    ),
                    "Run emos_forecast.py --serve-rolling --cities all and inspect its output.",
                    target_date=missing[0],
                    station=station,
                )
            )
            continue
        stale = [
            (target, latest_by_key[(station, target)]["latest_age_hours"])
            for target in open_targets
            if (station, target) in latest_by_key
            and latest_by_key[(station, target)]["latest_age_hours"] is not None
            and latest_by_key[(station, target)]["latest_age_hours"] > max_age_hours
        ]
        if stale:
            worst_target, worst_age = max(stale, key=lambda item: item[1])
            extra = f" ({len(stale)} targets stale)" if len(stale) > 1 else ""
            warnings.append(
                _health_warning(
                    "warning",
                    "emos-live-stale",
                    "Live EMOS target stale",
                    f"{station} live EMOS for {worst_target} is "
                    f"{worst_age:.1f} hours old{extra}.",
                    "Check the forecaster refresh timer and EMOS serve-rolling step.",
                    target_date=worst_target,
                    station=station,
                )
            )

    archive = conn.execute(
        """
        SELECT COUNT(*) AS rows,
               MAX(target_date) AS latest_target_date,
               MAX(fetched_at) AS latest_fetched_at
        FROM forecast_emos_daily_high
        WHERE source != 'live'
        """
    ).fetchone()
    return {
        "available": True,
        "profiles_using_emos": profiles_using_emos,
        "max_stale_hours": FORECAST_HEALTH_MAX_EMOS_AGE.total_seconds() / 3600,
        "stations_checked": expected_stations,
        "live_targets": live_targets,
        "recent_live_targets": live_targets,
        "rolling_archive": {
            "rows": int(archive["rows"] or 0),
            "latest_target_date": archive["latest_target_date"],
            "latest_fetched_at": archive["latest_fetched_at"],
        },
    }


def _emos_row(row: sqlite3.Row, current_utc: datetime) -> dict[str, Any]:
    fetched = _parse_timestamp(row["fetched_at"])
    return {
        "station_id": str(row["station_id"]),
        "target_date": str(row["target_date"]),
        "lead_days": int(row["lead_days"]),
        "mu_f": _round(row["predicted_high_f"], 2),
        "sigma_f": _round(row["sigma_f"], 2),
        "n_models": int(row["n_models"] or 0),
        "fetched_at": row["fetched_at"],
        "latest_age_hours": _age_hours(current_utc, fetched),
        "method": row["method"],
        "source": row["source"],
    }


def _clisfo_health(
    conn: sqlite3.Connection,
    current_utc: datetime,
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Per-station CLI settlement freshness (the settlement instrument).

    The legacy single-city ``clisfo_settlements`` table was dropped in the
    15-city rearchitecture; truth lives in the station-keyed
    ``cli_settlements``. Checking the legacy name fired "table missing" on
    every scan while real per-station staleness went undetected. Each
    station's lag is measured against ITS OWN settlement day (fixed standard
    time), because an eastern station's climate day rolls hours before SFO's.
    """

    if not _sqlite_table_exists(conn, "cli_settlements"):
        warnings.append(
            _health_warning(
                "warning",
                "clisfo-table-missing",
                "CLI settlements missing",
                "The cli_settlements table is not present in weather.db.",
                "Run the CLI settlement refresh and inspect source access.",
            )
        )
        return {"available": False, "reason": "cli_settlements table missing"}
    final_filter = (
        "AND is_final = 1"
        if "is_final" in _sqlite_columns(conn, "cli_settlements")
        else ""
    )
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS rows,
               MAX(local_date) AS latest_date,
               MAX(fetched_at) AS latest_fetched_at
        FROM cli_settlements
        WHERE max_temperature_f IS NOT NULL
          {final_filter}
        """
    ).fetchone()
    total_rows = int(row["rows"] or 0)
    if total_rows == 0:
        warnings.append(
            _health_warning(
                "critical",
                "clisfo-empty",
                "CLI truth empty",
                "No CLI settlement rows with max_temperature_f are available.",
                "Check CLI fetch logs before trusting calibration or settlement.",
            )
        )
        return {
            "available": True,
            "rows": 0,
            "latest_date": None,
            "latest_fetched_at": None,
            "lag_days": None,
            "max_lag_days": FORECAST_HEALTH_MAX_CLISFO_LAG_DAYS,
            "stations": [],
        }
    latest_by_station = {
        str(station): latest
        for station, latest in conn.execute(
            f"""
            SELECT station_id, MAX(local_date)
            FROM cli_settlements
            WHERE max_temperature_f IS NOT NULL
              {final_filter}
            GROUP BY station_id
            """
        ).fetchall()
    }
    stations: list[dict[str, Any]] = []
    worst_lag: int | None = None
    for city in CITIES:
        station = city.nws_station_id
        station_today = settlement_today(current_utc, city)
        latest = latest_by_station.get(station)
        latest_date = _date_from_string(latest)
        lag_days = (station_today - latest_date).days if latest_date is not None else None
        stations.append(
            {"station_id": station, "latest_date": latest, "lag_days": lag_days}
        )
        if lag_days is None:
            warnings.append(
                _health_warning(
                    "warning",
                    "clisfo-stale",
                    "CLI truth missing for station",
                    f"{station} has no settled CLI rows in cli_settlements.",
                    "Check the CLI settlement fetch for that station.",
                    station=station,
                )
            )
        elif lag_days > FORECAST_HEALTH_MAX_CLISFO_LAG_DAYS:
            warnings.append(
                _health_warning(
                    "warning",
                    "clisfo-stale",
                    "CLI truth stale",
                    (
                        f"{station} latest CLI truth ({latest}) is {lag_days} "
                        f"settlement day(s) behind."
                    ),
                    "Run paper-auto-settle or inspect the CLI/NWS source fetch.",
                    station=station,
                )
            )
        if lag_days is not None:
            worst_lag = lag_days if worst_lag is None else max(worst_lag, lag_days)
    return {
        "available": True,
        "rows": total_rows,
        "latest_date": row["latest_date"],
        "latest_fetched_at": row["latest_fetched_at"],
        # Worst station lag: the honest headline number for a 15-city truth feed.
        "lag_days": worst_lag,
        "max_lag_days": FORECAST_HEALTH_MAX_CLISFO_LAG_DAYS,
        "stations": stations,
    }


def _nws_ground_truth_health(conn: sqlite3.Connection, today: object) -> dict[str, Any]:
    if not _sqlite_table_exists(conn, "nws_daily_high_ground_truth"):
        return {"available": False, "reason": "nws_daily_high_ground_truth table missing"}
    columns = _sqlite_columns(conn, "nws_daily_high_ground_truth")
    observed_expr = "MAX(high_observed_at)" if "high_observed_at" in columns else "NULL"
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS rows,
               MAX(local_date) AS latest_date,
               {observed_expr} AS latest_observed_at
        FROM nws_daily_high_ground_truth
        WHERE high_f IS NOT NULL
        """
    ).fetchone()
    latest_date = _date_from_string(row["latest_date"])
    lag_days = (today - latest_date).days if latest_date is not None else None
    return {
        "available": True,
        "rows": int(row["rows"] or 0),
        "latest_date": row["latest_date"],
        "latest_observed_at": row["latest_observed_at"],
        "lag_days": lag_days,
    }


def _emos_enabled_profiles(config: StrategyConfig) -> list[str]:
    profiles: list[str] = []
    if config.emos_distribution_enabled:
        profiles.append(PRIMARY_PROFILE)
    for profile in sorted(EXPERIMENTAL_PROFILES):
        if strategy_config_for_profile(profile).emos_distribution_enabled:
            profiles.append(profile)
    return profiles


def _health_warning(
    level: str,
    code: str,
    title: str,
    detail: str,
    action: str,
    *,
    target_date: str | None = None,
    station: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "level": level,
        "code": code,
        "title": title,
        "detail": detail,
        "action": action,
    }
    if target_date is not None:
        row["target_date"] = target_date
    if station is not None:
        row["station_id"] = station
    return row


def _sqlite_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _coerce_utc(value: datetime | None) -> datetime:
    current_utc = value or datetime.now(UTC)
    if current_utc.tzinfo is None:
        return current_utc.replace(tzinfo=UTC)
    return current_utc.astimezone(UTC)


def _age_hours(current_utc: datetime, timestamp: datetime | None) -> float | None:
    if timestamp is None:
        return None
    return _round(max(0.0, (current_utc - timestamp).total_seconds() / 3600.0), 2)


def _split_csv(value: object) -> list[str]:
    if value is None:
        return []
    return [part for part in str(value).split(",") if part]

