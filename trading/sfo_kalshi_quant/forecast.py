from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Iterable

from ._util import _table_exists
from .cities import CityConfig, get_city
from .config import DEFAULT_FORECASTER_ROOT, SFO_TZ, intraday_timezone_for_city
from .emos_sources import (
    ROLLING_ORIGIN_SOURCES,
    forecast_source_precedence,
    preferred_rolling_origin_source,
)
from .models import ForecastOutcome, ForecastSnapshot, IntradaySnapshot
from .settlement_day import PACIFIC_STANDARD_TZ, settlement_today
from .settlement_truth import (
    integer_settlement_high_f as _integer_settlement_high_f,
    load_cli_settlement_truth,
)


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
    """Read one city's forecast artifacts from the forecaster project root.

    SFO reads its full blend stack (blend rows, Google cache, LSTM
    calibration). Every other city reads the station-agnostic EMOS pipeline:
    the live EMOS Gaussian is the point forecast, the scored rolling-origin
    EMOS archive is the calibration record, and the cross-model NWP spread
    stands in for blend source disagreement.
    """

    def __init__(
        self, root: Path = DEFAULT_FORECASTER_ROOT, city: CityConfig | None = None
    ) -> None:
        self.root = Path(root)
        self.city = city or get_city("sfo")
        self.station_id = self.city.nws_station_id
        self.weather_db = self.root / "weather.db"
        self.ab_test_path = self.root / "ab_test_results.json"

    def latest_blend(self, target: date) -> ForecastSnapshot:
        if not self.city.has_full_blend:
            snapshot = self._latest_emos_snapshot(target)
            if snapshot is not None:
                return snapshot
            raise ForecastDataError(
                f"No live EMOS forecast for {self.city.slug} {target.isoformat()} "
                f"in {self.weather_db}"
            )
        if self.weather_db.exists():
            row = self._latest_blend_row(target)
            if row is not None:
                return row
        # T7-1: the legacy raw-JSON google_weather_cache.json fallback that
        # used to live here was removed (2026-07-17 plan, Task 7 binding
        # condition T7-1). No consumer may read Google content from that
        # file any more -- raw Google fields belong ONLY in the TTL-enforced
        # runtime store (spec section 7.2). A missing weather.db or a
        # weather.db with no matching forecast_blend_daily_high row is now
        # an explicit, hard failure instead of a silent degrade to legacy
        # cache content: callers must treat this as a real outage, not a
        # softer "no live data yet" state.
        raise ForecastDataError(
            f"No forecast found for {target.isoformat()} in {self.weather_db} "
            "(the legacy google_weather_cache.json fallback was removed; "
            "see docs/superpowers/plans/2026-07-17-multicity-google-runtime-weather.md Task 7, T7-1)"
        )

    def latest_live_forecast(
        self,
        target: date,
        *,
        max_age_hours: float,
        now: datetime | None = None,
    ) -> ForecastSnapshot:
        """Return the preferred fresh forecast without weakening freshness gates.

        The Google runtime-store migration stopped regenerating the legacy SFO
        ``forecast_blend_daily_high`` rows while keeping the permanent EMOS
        baseline current. Preserve the legacy SFO blend exactly while it is
        fresh. If it is stale, missing, or unreadable, use a fresh live EMOS row
        for the same station and target. If neither source is fresh, return the
        best primary candidate so the caller's existing freshness gate still
        fails closed with its normal diagnostic.

        Non-SFO cities already use EMOS through ``latest_blend`` and are left
        unchanged. ``latest_blend`` itself also remains unchanged for historical
        calibration and the byte-stable Google challenger shadow comparison.
        """

        if max_age_hours < 0:
            raise ValueError("max_age_hours must be non-negative")
        if not self.city.has_full_blend:
            return self.latest_blend(target)

        primary: ForecastSnapshot | None = None
        primary_error: ForecastDataError | None = None
        if self.weather_db.exists():
            try:
                primary = self._latest_blend_row(target)
            except ForecastDataError as exc:
                primary_error = exc

        primary_age = primary.age_hours(now) if primary is not None else None
        if primary is not None and primary_age is not None and primary_age <= max_age_hours:
            return primary

        fallback: ForecastSnapshot | None = None
        fallback_error: ForecastDataError | None = None
        try:
            fallback = self._latest_emos_snapshot(target)
        except ForecastDataError as exc:
            fallback_error = exc
        fallback_age = fallback.age_hours(now) if fallback is not None else None
        if fallback is not None and fallback_age is not None and fallback_age <= max_age_hours:
            if primary is None:
                reason = (
                    "legacy_sfo_blend_unreadable"
                    if primary_error is not None
                    else "legacy_sfo_blend_missing"
                )
            elif primary_age is None:
                reason = "legacy_sfo_blend_timestamp_invalid"
            else:
                reason = "legacy_sfo_blend_stale"
            return replace(
                fallback,
                method=f"{fallback.method} [SFO operational fallback]",
                raw={
                    **fallback.raw,
                    "operational_fallback": {
                        "reason": reason,
                        "legacy_blend_fetched_at": (
                            primary.fetched_at if primary is not None else None
                        ),
                        "max_age_hours": float(max_age_hours),
                    },
                },
            )

        if primary is not None:
            return primary
        if fallback is not None:
            return fallback
        if primary_error is not None:
            raise primary_error
        if fallback_error is not None:
            raise fallback_error
        return self.latest_blend(target)

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
                        WHERE station_id = ? AND local_date = ? AND high_f IS NOT NULL
                        """,
                        (self.station_id, target.isoformat()),
                    ).fetchone()
                high_row = conn.execute(
                    """
                    SELECT MAX(temp_f), COUNT(*)
                    FROM nws_station_observations
                    WHERE station_id = ? AND local_date = ? AND temp_f IS NOT NULL
                    """,
                    (self.station_id, target.isoformat()),
                ).fetchone()
                latest_row = conn.execute(
                    """
                    SELECT observed_at, temp_f
                    FROM nws_station_observations
                    WHERE station_id = ? AND local_date = ? AND temp_f IS NOT NULL
                    ORDER BY observed_at DESC
                    LIMIT 1
                    """,
                    (self.station_id, target.isoformat()),
                ).fetchone()
                # The hourly remaining-heat forecast is a Google artifact and
                # exists only for the blend city.
                fetched_row = None
                if self.city.has_full_blend:
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
        weight = _intraday_weight(
            intraday.latest_observed_at,
            intraday_timezone_for_city(self.city),
        )
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

    _GOOGLE_CHALLENGER_BASELINE_COLUMNS = (
        "station_id", "target_date", "issued_at", "policy_version",
        "baseline_mu", "baseline_sigma", "challenger_mu", "challenger_sigma", "action",
    )

    def latest_google_challenger_baseline(self, target: date) -> dict | None:
        """Read one durable paired baseline/Google-challenger evidence row.

        Task 7: a plain SQL read of the forecaster-owned, non-Google-raw
        ``google_challenger_research_baseline`` table -- mirrors
        ``_latest_blend_row``/``_latest_emos_snapshot``'s existing pattern of
        reading forecaster's weather.db directly rather than importing
        forecaster's Python modules (the two projects deliberately do not
        import each other; see ``cities.py``). Returns ``None`` when
        weather.db, the table, or a matching row does not exist yet -- this
        is research-only shadow evidence and is never required for the live
        forecast path.
        """

        if not self.weather_db.exists():
            return None
        columns_sql = ", ".join(self._GOOGLE_CHALLENGER_BASELINE_COLUMNS)
        query = f"""
            SELECT {columns_sql}
            FROM google_challenger_research_baseline
            WHERE station_id = ? AND target_date = ?
            ORDER BY issued_at DESC
            LIMIT 1
        """
        try:
            with sqlite3.connect(self.weather_db) as conn:
                if not _table_exists(conn, "google_challenger_research_baseline"):
                    return None
                row = conn.execute(query, (self.station_id, target.isoformat())).fetchone()
        except sqlite3.Error as exc:
            raise ForecastDataError(f"Could not read {self.weather_db}: {exc}") from exc
        if row is None:
            return None
        return dict(zip(self._GOOGLE_CHALLENGER_BASELINE_COLUMNS, row))

    def _latest_emos_snapshot(self, target: date) -> ForecastSnapshot | None:
        """Build a ForecastSnapshot from the freshest live EMOS row for a city.

        The EMOS Gaussian is the city's forecast: mu is the point, sigma rides
        along in ``raw`` for the distribution override, and the cross-model NWP
        spread fills the source-disagreement gate. Freshest row wins across
        leads (the live serve writes each rolling target at its true lead).
        """

        if not self.weather_db.exists():
            return None
        query = """
            SELECT predicted_high_f, sigma_f, n_models, model_spread_f,
                   fetched_at, method, lead_days, source
            FROM forecast_emos_daily_high
            WHERE station_id = ? AND target_date = ?
        """
        try:
            with sqlite3.connect(self.weather_db) as conn:
                if not _table_exists(conn, "forecast_emos_daily_high"):
                    return None
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(forecast_emos_daily_high)")
                }
                if "station_id" not in columns:
                    return None
                rows = conn.execute(query, (self.station_id, target.isoformat())).fetchall()
        except sqlite3.Error as exc:
            raise ForecastDataError(f"Could not read {self.weather_db}: {exc}") from exc
        if not rows:
            return None
        row = max(
            rows,
            key=lambda candidate: (
                forecast_source_precedence(str(candidate[7])),
                str(candidate[4]),
            ),
        )
        mu, sigma, n_models, model_spread, fetched_at, method, lead_days, _source = row
        return ForecastSnapshot(
            target_date=target,
            predicted_high_f=float(mu),
            station_id=self.station_id,
            fetched_at=fetched_at,
            lead_hours=float(lead_days) * 24.0 if lead_days is not None else None,
            method=f"{method or 'emos'} (live NWP ensemble)",
            source_count=int(n_models or 0),
            source_spread_override_f=(
                float(model_spread) if model_spread is not None else None
            ),
            raw={
                "source": "forecast_emos_daily_high",
                "emos": {
                    "mu": float(mu),
                    "sigma": float(sigma),
                    "n_models": n_models,
                    "model_spread_f": model_spread,
                    "lead_days": lead_days,
                },
            },
        )

    def latest_emos_snapshot(self, target: date) -> ForecastSnapshot | None:
        """Public live-EMOS read for fail-closed monitor refreshes."""

        return self._latest_emos_snapshot(target)

    def load_emos_outcomes(self) -> list[ForecastOutcome]:
        """Scored rolling-origin EMOS outcomes for this station.

        Each row is the out-of-sample EMOS prediction joined with the CLI
        settlement -- the same leakage-free record the postproc backtests
        score, reused as the residual-calibration history for cities that have
        no blend/LSTM archive.
        """

        if not self.weather_db.exists():
            raise ForecastDataError(f"Missing forecast archive: {self.weather_db}")
        try:
            with sqlite3.connect(self.weather_db) as conn:
                if not _table_exists(conn, "forecast_emos_daily_high"):
                    return []
                settlement_columns = (
                    {row[1] for row in conn.execute("PRAGMA table_info(cli_settlements)")}
                    if _table_exists(conn, "cli_settlements")
                    else set()
                )
                archive_source = preferred_rolling_origin_source(
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT source FROM forecast_emos_daily_high "
                        "WHERE station_id = ? AND lead_days = 1",
                        (self.station_id,),
                    )
                )
                if archive_source is None:
                    return []
                if "is_final" in settlement_columns:
                    query = """
                        SELECT f.target_date, f.predicted_high_f, c.max_temperature_f
                        FROM forecast_emos_daily_high AS f
                        JOIN cli_settlements AS c
                          ON c.station_id = f.station_id
                         AND c.local_date = f.target_date
                         AND c.is_final = 1
                         AND c.max_temperature_f IS NOT NULL
                        WHERE f.station_id = ? AND f.source = ?
                          AND f.lead_days = 1
                        ORDER BY f.target_date
                    """
                else:
                    query = """
                        SELECT target_date, predicted_high_f, actual_high_f
                        FROM forecast_emos_daily_high
                        WHERE station_id = ? AND source = ?
                          AND lead_days = 1 AND actual_high_f IS NOT NULL
                        ORDER BY target_date
                    """
                rows = conn.execute(
                    query, (self.station_id, archive_source)
                ).fetchall()
        except sqlite3.Error as exc:
            raise ForecastDataError(f"Could not read {self.weather_db}: {exc}") from exc
        return [
            ForecastOutcome(
                local_date=date.fromisoformat(target_date),
                predicted_high_f=float(mu),
                actual_high_f=_integer_settlement_high_f(actual),
                model_name="emos_wmean",
                station_id=self.station_id,
            )
            for target_date, mu, actual in rows
        ]

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
        if not self.city.has_full_blend:
            # LSTM/blend archives are SFO-only artifacts; every other city's
            # calibration record is its scored rolling-origin EMOS history.
            return self.load_emos_outcomes()
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
        try:
            with sqlite3.connect(self.weather_db) as conn:
                if not _table_exists(conn, "forecast_blend_daily_high"):
                    raise ForecastDataError("forecast_blend_daily_high archive table is missing")
                settlement_columns = (
                    {row[1] for row in conn.execute("PRAGMA table_info(cli_settlements)")}
                    if _table_exists(conn, "cli_settlements")
                    else set()
                )
                if "is_final" in settlement_columns:
                    query = """
                        SELECT b.target_date, b.predicted_high_f, c.max_temperature_f,
                               b.fetched_at, b.details_json
                        FROM forecast_blend_daily_high AS b
                        JOIN cli_settlements AS c
                          ON c.station_id = 'KSFO'
                         AND c.local_date = b.target_date
                         AND c.is_final = 1
                         AND c.max_temperature_f IS NOT NULL
                        WHERE b.actual_high_f IS NOT NULL
                          AND b.abs_error_f IS NOT NULL
                        ORDER BY b.target_date, b.fetched_at
                    """
                else:
                    query = """
                        SELECT target_date, predicted_high_f, actual_high_f,
                               fetched_at, details_json
                        FROM forecast_blend_daily_high
                        WHERE actual_high_f IS NOT NULL
                          AND abs_error_f IS NOT NULL
                        ORDER BY target_date, fetched_at
                    """
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

    def load_cli_settlement_highs(self) -> dict[date, float]:
        """Archived CLI settlement maxima for this station (weather.db truth).

        This is the same instrument Kalshi settles on (live CLI + IEM archive),
        unlike the observation-derived daily high below, which runs a few
        degrees low of the CLI on some days and must not settle orders. Legacy
        schemas without explicit finality fail closed.
        """

        if not self.weather_db.exists():
            return {}
        with sqlite3.connect(self.weather_db) as conn:
            if not _table_exists(conn, "cli_settlements"):
                return {}
            columns = {row[1] for row in conn.execute("PRAGMA table_info(cli_settlements)")}
            if "is_final" not in columns:
                return {}
            rows = conn.execute(
                "SELECT local_date, max_temperature_f FROM cli_settlements "
                "WHERE station_id = ? AND max_temperature_f IS NOT NULL "
                "AND is_final = 1",
                (self.station_id,),
            ).fetchall()
        return {
            date.fromisoformat(local_date): float(high)
            for local_date, high in rows
        }

    def load_cli_settlement_truth(self) -> dict[tuple[str, str], float]:
        """All city outcomes keyed by (series ticker, target date)."""

        if not self.weather_db.exists():
            return {}
        with sqlite3.connect(self.weather_db) as conn:
            if not _table_exists(conn, "cli_settlements"):
                return {}
            return load_cli_settlement_truth(conn)

    def load_ksfo_daily_highs(self) -> dict[date, float]:
        """Deprecated SFO-only view of authoritative CLI settlements."""

        truth = self.load_cli_settlement_truth()
        return {
            date.fromisoformat(target_date): high
            for (series, target_date), high in truth.items()
            if series == "KXHIGHTSFO"
        }

    def load_emos_mu_sigma(
        self, lead_days: int | None = 1, *, source: str | None = None
    ) -> dict[date, tuple[float, float]]:
        """target_date -> (mu, sigma) from the forecaster's EMOS archive.

        Reads ``forecast_emos_daily_high`` (written by ``emos_forecast.py``).
        Empty when the artifact has not been built, so a caller that always
        passes the lookup degrades gracefully to the residual-calibrated path.

        ``lead_days`` filters to one forecast horizon; ``lead_days=None`` reads
        every lead, keyed by target_date. The live serve path stores each rolling
        target at its TRUE lead (next-day at lead 1, the 2-day-out market at lead
        2), so the live trader must pass ``lead_days=None`` to see both -- a fixed
        ``lead_days=1`` would silently miss the 2-day-out market entirely.

        An explicit ``source`` is an exact read (including historical v1).
        With ``source=None``, each lead uses v2 rolling-origin rows exclusively
        when that version exists for the queried station/lead, otherwise v1;
        the versions are never mixed. Other source families retain their prior
        behavior, and ``live`` rows deterministically overwrite rebuild rows per
        target regardless of timestamps.
        """

        if not self.weather_db.exists():
            return {}
        query = (
            "SELECT target_date, predicted_high_f, sigma_f, lead_days, source "
            "FROM forecast_emos_daily_high"
        )
        clauses: list[str] = []
        params: list[object] = []
        try:
            with sqlite3.connect(self.weather_db) as conn:
                station_keyed = _table_exists(conn, "forecast_emos_daily_high") and (
                    "station_id"
                    in {
                        row[1]
                        for row in conn.execute(
                            "PRAGMA table_info(forecast_emos_daily_high)"
                        )
                    }
                )
        except sqlite3.Error:
            return {}
        if station_keyed:
            clauses.append("station_id = ?")
            params.append(self.station_id)
        elif self.station_id != "KSFO":
            return {}
        if lead_days is not None:
            clauses.append("lead_days = ?")
            params.append(lead_days)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        # Apply rebuild rows first; live serving rows must overwrite them even
        # when a later maintenance rebuild carries a newer fetched_at stamp.
        query += " ORDER BY CASE WHEN source = 'live' THEN 1 ELSE 0 END, fetched_at"
        try:
            with sqlite3.connect(self.weather_db) as conn:
                if not _table_exists(conn, "forecast_emos_daily_high"):
                    return {}
                rows = conn.execute(query, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            raise ForecastDataError(f"Could not read EMOS archive from {self.weather_db}: {exc}") from exc
        if source is None:
            rolling_by_lead: dict[int, set[str]] = {}
            for _target_iso, _mu, _sigma, row_lead, row_source in rows:
                if row_source in ROLLING_ORIGIN_SOURCES:
                    rolling_by_lead.setdefault(int(row_lead), set()).add(str(row_source))
            preferred_by_lead = {
                row_lead: preferred_rolling_origin_source(available)
                for row_lead, available in rolling_by_lead.items()
            }
            rows = [
                row
                for row in rows
                if row[4] not in ROLLING_ORIGIN_SOURCES
                or row[4] == preferred_by_lead.get(int(row[3]))
            ]
        return {
            date.fromisoformat(target_iso): (float(mu), float(sigma))
            for target_iso, mu, sigma, _row_lead, _row_source in rows
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


def _intraday_weight(
    observed_at: str | None,
    standard_timezone: tzinfo = SFO_TZ,
) -> float:
    if not observed_at:
        return 0.55
    try:
        observed_dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        local_hour = observed_dt.astimezone(standard_timezone).hour
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
