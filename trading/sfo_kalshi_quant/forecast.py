from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .config import DEFAULT_FORECASTER_ROOT, SFO_TZ
from .models import ForecastOutcome, ForecastSnapshot, IntradaySnapshot
from .settlement_day import PACIFIC_STANDARD_TZ, settlement_today


class ForecastDataError(RuntimeError):
    pass


def parse_target_date(value: str | date | None) -> date:
    if isinstance(value, date):
        return value
    today = settlement_today()
    if value is None or value == "tomorrow":
        return today + timedelta(days=1)
    if value == "today":
        return today
    if value == "yesterday":
        return today - timedelta(days=1)
    return date.fromisoformat(value)


def parse_target_dates(value: str | date | None) -> list[date]:
    """Parse one or more analysis targets.

    ``both`` is a convenience for the active workflow: today has the live market,
    tomorrow is the research target that may not be listed yet.
    """

    if isinstance(value, date):
        return [value]
    if value in ("both", "today,tomorrow", "tomorrow,today"):
        today = settlement_today()
        return [today, today + timedelta(days=1)]
    if value == "rolling":
        today = settlement_today()
        return [today, today + timedelta(days=1), today + timedelta(days=2)]
    if isinstance(value, str) and "," in value:
        return [parse_target_date(part.strip()) for part in value.split(",") if part.strip()]
    return [parse_target_date(value)]


class SfoForecasterAdapter:
    """Read artifacts from the SFO forecaster project root."""

    def __init__(self, root: Path = DEFAULT_FORECASTER_ROOT) -> None:
        self.root = Path(root)
        self.weather_db = self.root / "weather.db"
        self.ab_test_path = self.root / "ab_test_results.json"
        self.google_cache_path = self.root / "google_weather_cache.json"

    def latest_blend(self, target: date) -> ForecastSnapshot:
        if self.weather_db.exists():
            row = self._latest_blend_row(target)
            if row is not None:
                return row
        if self.google_cache_path.exists():
            cache = json.loads(self.google_cache_path.read_text())
            cache_row = _cache_daily_high(cache, target)
            if cache_row is not None and cache_row.get("highF") is not None:
                return ForecastSnapshot(
                    target_date=target,
                    predicted_high_f=float(cache_row["highF"]),
                    fetched_at=cache_row.get("fetched_at") or cache.get("fetched_at"),
                    lead_hours=_maybe_float(cache_row.get("lead_hours")),
                    method=cache_row.get("method") or cache.get("method", "google_weather_cache"),
                    google_high_f=float(cache_row["highF"]),
                    source_count=1,
                    max_calls_per_day=_maybe_int(cache_row.get("max_calls_per_day") or cache.get("max_calls_per_day")),
                    calls_used_today=_maybe_int(cache_row.get("calls_used_today") or cache.get("calls_used_today")),
                    raw={"source": "google_weather_cache", "cache": cache, "daily_high": cache_row},
                )
        raise ForecastDataError(
            f"No forecast found for {target.isoformat()} in {self.weather_db} or {self.google_cache_path}"
        )

    def intraday_snapshot(self, target: date) -> IntradaySnapshot | None:
        if not self.weather_db.exists():
            return None
        try:
            with sqlite3.connect(self.weather_db) as conn:
                official_row = None
                if _table_exists(conn, "nws_daily_high_ground_truth"):
                    official_row = conn.execute(
                        """
                        SELECT high_f, high_observed_at, observation_count, is_complete, source
                        FROM nws_daily_high_ground_truth
                        WHERE station_id = 'KSFO' AND local_date = ? AND high_f IS NOT NULL
                        """,
                        (target.isoformat(),),
                    ).fetchone()
                high_row = conn.execute(
                    """
                    SELECT MAX(temp_f), COUNT(*)
                    FROM nws_station_observations
                    WHERE station_id = 'KSFO' AND local_date = ? AND temp_f IS NOT NULL
                    """,
                    (target.isoformat(),),
                ).fetchone()
                latest_row = conn.execute(
                    """
                    SELECT observed_at, temp_f
                    FROM nws_station_observations
                    WHERE station_id = 'KSFO' AND local_date = ? AND temp_f IS NOT NULL
                    ORDER BY observed_at DESC
                    LIMIT 1
                    """,
                    (target.isoformat(),),
                ).fetchone()
                fetched_row = conn.execute(
                    """
                    SELECT MAX(fetched_at)
                    FROM forecast_google_hourly
                    WHERE target_date = ?
                    """,
                    (target.isoformat(),),
                ).fetchone()

                latest_observed_at = latest_row[0] if latest_row else None
                latest_temp_f = _maybe_float(latest_row[1]) if latest_row else None
                forecast_fetched_at = fetched_row[0] if fetched_row else None
                remaining_high_f = None
                if forecast_fetched_at:
                    query = """
                        SELECT MAX(temperature_f)
                        FROM forecast_google_hourly
                        WHERE target_date = ? AND fetched_at = ?
                    """
                    params: tuple[object, ...] = (target.isoformat(), forecast_fetched_at)
                    if latest_observed_at:
                        query += " AND forecast_hour_utc >= ?"
                        params = (*params, latest_observed_at)
                    remaining_row = conn.execute(query, params).fetchone()
                    remaining_high_f = _maybe_float(remaining_row[0]) if remaining_row else None
        except sqlite3.Error as exc:
            raise ForecastDataError(f"Could not read intraday data from {self.weather_db}: {exc}") from exc

        observed_high_f = _maybe_float(high_row[0]) if high_row else None
        observation_count = int(high_row[1] or 0) if high_row else 0
        observed_high_source = "nws_station_observations" if observed_high_f is not None else None
        is_complete = False
        if official_row is not None:
            official_high = _maybe_float(official_row[0])
            if official_high is not None:
                observed_high_f = official_high
                latest_observed_at = official_row[1] or latest_observed_at
                observation_count = int(official_row[2] or observation_count)
                is_complete = bool(official_row[3])
                observed_high_source = official_row[4] or "nws_daily_high_ground_truth"
        if observed_high_f is None and remaining_high_f is None:
            return None
        return IntradaySnapshot(
            target_date=target,
            observed_high_f=observed_high_f,
            latest_temp_f=latest_temp_f,
            latest_observed_at=latest_observed_at,
            remaining_forecast_high_f=remaining_high_f,
            forecast_fetched_at=forecast_fetched_at,
            observation_count=observation_count,
            observed_high_source=observed_high_source,
            is_complete=is_complete,
        )

    def apply_intraday_update(
        self,
        forecast: ForecastSnapshot,
        intraday: IntradaySnapshot | None,
    ) -> ForecastSnapshot:
        if intraday is None or intraday.observed_high_f is None:
            return forecast
        candidates = [intraday.observed_high_f]
        if intraday.remaining_forecast_high_f is not None:
            candidates.append(intraday.remaining_forecast_high_f)
        intraday_anchor = max(candidates)
        weight = _intraday_weight(intraday.latest_observed_at)
        adjusted_high = max(
            intraday_anchor,
            weight * intraday_anchor + (1.0 - weight) * forecast.predicted_high_f,
        )
        raw = {
            **forecast.raw,
            "intraday_update": {
                "pre_intraday_predicted_high_f": forecast.predicted_high_f,
                "adjusted_predicted_high_f": round(adjusted_high, 2),
                "observed_high_f": intraday.observed_high_f,
                "latest_temp_f": intraday.latest_temp_f,
                "latest_observed_at": intraday.latest_observed_at,
                "remaining_forecast_high_f": intraday.remaining_forecast_high_f,
                "forecast_fetched_at": intraday.forecast_fetched_at,
                "observed_high_source": intraday.observed_high_source,
                "is_complete": intraday.is_complete,
                "intraday_weight": weight,
            },
        }
        return replace(
            forecast,
            predicted_high_f=round(adjusted_high, 2),
            method=f"{forecast.method} + intraday high-so-far update",
            raw=raw,
        )

    def _latest_blend_row(self, target: date) -> ForecastSnapshot | None:
        query = """
            SELECT
                fetched_at,
                target_date,
                lead_hours,
                predicted_high_f,
                google_high_f,
                nws_high_f,
                open_meteo_high_f,
                history_high_f,
                google_weight,
                nws_weight,
                open_meteo_weight,
                history_weight,
                station_adjustment_f,
                fresh_station_count,
                source_count,
                method,
                max_calls_per_day,
                calls_used_today,
                details_json
            FROM forecast_blend_daily_high
            WHERE target_date = ?
            ORDER BY fetched_at DESC
            LIMIT 1
        """
        try:
            with sqlite3.connect(self.weather_db) as conn:
                row = conn.execute(query, (target.isoformat(),)).fetchone()
        except sqlite3.Error as exc:
            raise ForecastDataError(f"Could not read {self.weather_db}: {exc}") from exc
        if row is None:
            return None
        (
            fetched_at,
            target_date,
            lead_hours,
            predicted_high_f,
            google_high_f,
            nws_high_f,
            open_meteo_high_f,
            history_high_f,
            google_weight,
            nws_weight,
            open_meteo_weight,
            history_weight,
            station_adjustment_f,
            fresh_station_count,
            source_count,
            method,
            max_calls_per_day,
            calls_used_today,
            details_json,
        ) = row
        details = _json_or_empty(details_json)
        sources = details.get("sources") if isinstance(details, dict) else None
        google_source = sources.get("google") if isinstance(sources, dict) else None
        return ForecastSnapshot(
            target_date=date.fromisoformat(target_date),
            predicted_high_f=float(predicted_high_f),
            fetched_at=fetched_at,
            lead_hours=_maybe_float(lead_hours),
            method=method,
            google_high_f=_maybe_float(google_high_f),
            nws_high_f=_maybe_float(nws_high_f),
            open_meteo_high_f=_maybe_float(open_meteo_high_f),
            history_high_f=_maybe_float(history_high_f),
            google_weight=_maybe_float(google_weight),
            nws_weight=_maybe_float(nws_weight),
            open_meteo_weight=_maybe_float(open_meteo_weight),
            history_weight=_maybe_float(history_weight),
            station_adjustment_f=_maybe_float(station_adjustment_f),
            fresh_station_count=_maybe_int(fresh_station_count),
            source_count=int(source_count or 0),
            max_calls_per_day=_maybe_int(max_calls_per_day),
            calls_used_today=_maybe_int(calls_used_today),
            raw={
                "source": "forecast_blend_daily_high",
                "details": details,
                "blend_weighting": details.get("blend_weighting") if isinstance(details, dict) else None,
                "observed_high_decision": details.get("observed_high_decision") if isinstance(details, dict) else None,
                "google_weather_api": details.get("google_weather_api") if isinstance(details, dict) else None,
                "google_components": google_source.get("components") if isinstance(google_source, dict) else None,
                "google_warning": google_source.get("warning") if isinstance(google_source, dict) else None,
            },
        )

    def load_lstm_outcomes(self) -> list[ForecastOutcome]:
        if not self.ab_test_path.exists():
            raise ForecastDataError(f"Missing calibration file: {self.ab_test_path}")
        payload = json.loads(self.ab_test_path.read_text())
        daily = payload["target_daily_high_next_day"]["chart"]["daily"]
        outcomes = [
            ForecastOutcome(
                local_date=date.fromisoformat(row["date"]),
                predicted_high_f=float(row["lstm"]),
                actual_high_f=_integer_settlement_high_f(row["actual"]),
                model_name="lstm",
            )
            for row in daily
        ]
        outcomes.sort(key=lambda row: row.local_date)
        return outcomes

    def load_calibration_outcomes(
        self,
        source: str = "auto",
        *,
        min_clean_blend: int = 30,
    ) -> list[ForecastOutcome]:
        normalized = source.replace("_", "-").lower()
        if normalized == "lstm":
            return self.load_lstm_outcomes()
        if normalized == "clean-blend":
            return self.load_clean_blend_outcomes()
        if normalized != "auto":
            raise ValueError("calibration source must be auto, lstm, or clean-blend")

        try:
            clean = self.load_clean_blend_outcomes()
        except ForecastDataError:
            clean = []
        if len(clean) >= min_clean_blend:
            return clean
        return self.load_lstm_outcomes()

    def load_clean_blend_outcomes(self) -> list[ForecastOutcome]:
        """Load archived point-in-time blend outcomes for calibration/backtests."""

        if not self.weather_db.exists():
            raise ForecastDataError(f"Missing forecast archive: {self.weather_db}")
        query = """
            SELECT
                target_date,
                predicted_high_f,
                actual_high_f,
                fetched_at,
                details_json
            FROM forecast_blend_daily_high
            WHERE actual_high_f IS NOT NULL
              AND abs_error_f IS NOT NULL
            ORDER BY target_date, fetched_at
        """
        try:
            with sqlite3.connect(self.weather_db) as conn:
                if not _table_exists(conn, "forecast_blend_daily_high"):
                    raise ForecastDataError("forecast_blend_daily_high archive table is missing")
                rows = conn.execute(query).fetchall()
        except sqlite3.Error as exc:
            raise ForecastDataError(f"Could not read {self.weather_db}: {exc}") from exc

        latest_by_day = {}
        for target_iso, predicted, actual, fetched_at, details_json in rows:
            if not _is_clean_next_day_blend(target_iso, fetched_at, details_json):
                continue
            current = latest_by_day.get(target_iso)
            if current is None or fetched_at > current[3]:
                latest_by_day[target_iso] = (target_iso, predicted, actual, fetched_at)

        outcomes = [
            ForecastOutcome(
                local_date=date.fromisoformat(target_iso),
                predicted_high_f=float(predicted),
                actual_high_f=_integer_settlement_high_f(actual),
                model_name="clean_blend",
            )
            for target_iso, predicted, actual, _ in latest_by_day.values()
        ]
        outcomes.sort(key=lambda row: row.local_date)
        if not outcomes:
            raise ForecastDataError("No clean next-day blend outcomes are available")
        return outcomes

    def load_ksfo_daily_highs(self) -> dict[date, float]:
        if not self.weather_db.exists():
            return {}
        query = """
            SELECT local_date, high_f
            FROM nws_daily_high_ground_truth
            WHERE high_f IS NOT NULL AND is_complete = 1
        """
        with sqlite3.connect(self.weather_db) as conn:
            if not _table_exists(conn, "nws_daily_high_ground_truth"):
                return {}
            rows = conn.execute(query).fetchall()
        return {date.fromisoformat(local_date): float(high_f) for local_date, high_f in rows}

    def load_emos_mu_sigma(
        self, lead_days: int = 1, *, source: str | None = None
    ) -> dict[date, tuple[float, float]]:
        """target_date -> (mu, sigma) from the forecaster's EMOS archive.

        Reads ``forecast_emos_daily_high`` (written by ``emos_forecast.py``).
        Empty when the artifact has not been built, so a caller that always
        passes the lookup degrades gracefully to the residual-calibrated path.

        ``source`` filters the EMOS source: ``'rolling_origin'`` for leakage-safe
        backtest/rescore reads (every value is strictly out-of-sample),
        ``'live'`` for the served current-run forecast. When None, all sources are
        returned with a deterministic freshest-wins precedence per target_date
        (``ORDER BY fetched_at``) so a two-source date never resolves by row order.
        """

        if not self.weather_db.exists():
            return {}
        query = (
            "SELECT target_date, predicted_high_f, sigma_f "
            "FROM forecast_emos_daily_high WHERE lead_days = ?"
        )
        params: tuple = (lead_days,)
        if source is not None:
            query += " AND source = ?"
            params = (lead_days, source)
        query += " ORDER BY fetched_at"  # later (fresher) rows win in the dict below
        try:
            with sqlite3.connect(self.weather_db) as conn:
                if not _table_exists(conn, "forecast_emos_daily_high"):
                    return {}
                rows = conn.execute(query, params).fetchall()
        except sqlite3.Error as exc:
            raise ForecastDataError(f"Could not read EMOS archive from {self.weather_db}: {exc}") from exc
        return {
            date.fromisoformat(target_iso): (float(mu), float(sigma))
            for target_iso, mu, sigma in rows
        }


def _maybe_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _maybe_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _json_or_empty(value: object) -> dict:
    if not value:
        return {}
    try:
        payload = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_clean_next_day_blend(target_iso: object, fetched_at: object, details_json: object) -> bool:
    if not target_iso or not fetched_at:
        return False
    try:
        target = date.fromisoformat(str(target_iso))
        fetched = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    # Settlement-day (fixed PST) fetch date, to match the forecaster's
    # is_clean_next_day_forecast classification exactly.
    if target != fetched.astimezone(PACIFIC_STANDARD_TZ).date() + timedelta(days=1):
        return False
    details = _json_or_empty(details_json)
    decision = details.get("observed_high_decision") if isinstance(details, dict) else None
    mode = decision.get("mode") if isinstance(decision, dict) else None
    return str(mode).lower() not in {"floor", "lock"}


def _integer_settlement_high_f(value: object) -> float:
    high = float(value)
    if not math.isfinite(high):
        raise ValueError("settlement high must be finite")
    return float(math.floor(high + 0.5))


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _cache_daily_high(cache: dict, target: date) -> dict | None:
    target_iso = target.isoformat()
    for row in cache.get("daily_highs") or []:
        if isinstance(row, dict) and row.get("target_date") == target_iso:
            return row
    if cache.get("target_date") == target_iso:
        return cache
    return None


def _intraday_weight(observed_at: str | None) -> float:
    if not observed_at:
        return 0.55
    try:
        observed_dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        local_hour = observed_dt.astimezone(SFO_TZ).hour
    except ValueError:
        return 0.55
    if local_hour < 10:
        return 0.35
    if local_hour < 12:
        return 0.50
    if local_hour < 15:
        return 0.65
    if local_hour < 18:
        return 0.80
    return 0.90


def rolling_outcomes(outcomes: Iterable[ForecastOutcome], before: date) -> list[ForecastOutcome]:
    return [row for row in outcomes if row.local_date < before]


def has_forecaster_observed_high_adjustment(forecast: ForecastSnapshot) -> bool:
    decision = forecast.raw.get("observed_high_decision") if isinstance(forecast.raw, dict) else None
    mode = decision.get("mode") if isinstance(decision, dict) else None
    return str(mode).lower() in {"floor", "lock"}
