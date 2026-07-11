#!/usr/bin/env python3
"""SQLite archive schema, migrations, settlement refresh, and scoring."""

import json
import os
import sqlite3
from datetime import datetime, timezone

import city_truth
from clisfo import fetch_recent_cli_reports
from forecast_scoring import is_clean_next_day_forecast
from google_api import (
    condition_text,
    hour_local_datetime,
    local_midnight_utc,
    parse_google_timestamp,
    precip_probability,
    temp_to_f,
)
from settlement_calendar import integer_settlement_high_f, local_standard_date
from weather_cache_config import DB_PATH, SFO_WEATHER_STATION_ID

def table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn, table_name: str) -> dict:
    if not table_exists(conn, table_name):
        return {}
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1]: row for row in rows}


def daily_archive_columns(conn):
    if not table_exists(conn, "forecast_google_daily_high"):
        return {}
    rows = conn.execute("PRAGMA table_info(forecast_google_daily_high)").fetchall()
    return {row[1]: row for row in rows}


def create_daily_archive_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS forecast_google_daily_high (
            fetched_at TEXT NOT NULL,
            target_date TEXT NOT NULL,
            lead_hours REAL,
            source TEXT NOT NULL,
            method TEXT NOT NULL,
            predicted_high_f REAL NOT NULL,
            peak_hour_local TEXT,
            hours_used INTEGER,
            forecast_start_local TEXT,
            forecast_end_local TEXT,
            condition TEXT,
            precipitation_probability_pct REAL,
            time_zone TEXT,
            max_calls_per_day INTEGER,
            calls_used_today INTEGER,
            actual_high_f REAL,
            abs_error_f REAL,
            scored_at TEXT,
            truth_source TEXT,
            PRIMARY KEY (fetched_at, target_date)
        )
        """
    )


def create_blend_archive_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS forecast_blend_daily_high (
            fetched_at TEXT NOT NULL,
            target_date TEXT NOT NULL,
            lead_hours REAL,
            method TEXT NOT NULL,
            predicted_high_f REAL NOT NULL,
            google_high_f REAL,
            nws_high_f REAL,
            open_meteo_high_f REAL,
            history_high_f REAL,
            google_weight REAL,
            nws_weight REAL,
            open_meteo_weight REAL,
            history_weight REAL,
            station_adjustment_f REAL,
            fresh_station_count INTEGER,
            source_count INTEGER,
            time_zone TEXT,
            max_calls_per_day INTEGER,
            calls_used_today INTEGER,
            details_json TEXT,
            actual_high_f REAL,
            abs_error_f REAL,
            scored_at TEXT,
            truth_source TEXT,
            PRIMARY KEY (fetched_at, target_date)
        )
        """
    )


def migrate_daily_archive(conn):
    columns = daily_archive_columns(conn)
    if not columns:
        create_daily_archive_table(conn)
        return

    pk_cols = [row[1] for row in sorted(columns.values(), key=lambda row: row[5]) if row[5]]
    if pk_cols == ["fetched_at", "target_date"] and "lead_hours" in columns:
        return

    old_col_names = set(columns)
    conn.execute("ALTER TABLE forecast_google_daily_high RENAME TO forecast_google_daily_high_old")
    create_daily_archive_table(conn)
    copied_cols = [
        "fetched_at",
        "target_date",
        "source",
        "method",
        "predicted_high_f",
        "peak_hour_local",
        "hours_used",
        "forecast_start_local",
        "forecast_end_local",
        "condition",
        "precipitation_probability_pct",
        "time_zone",
        "max_calls_per_day",
        "calls_used_today",
        "actual_high_f",
        "abs_error_f",
        "scored_at",
    ]
    selected = ", ".join(col for col in copied_cols if col in old_col_names)
    inserted = ", ".join(col for col in copied_cols if col in old_col_names)
    if selected:
        conn.execute(
            f"""
            INSERT OR IGNORE INTO forecast_google_daily_high ({inserted})
            SELECT {selected}
            FROM forecast_google_daily_high_old
            """
        )
    conn.execute("DROP TABLE forecast_google_daily_high_old")


def hourly_archive_columns(conn):
    if not table_exists(conn, "forecast_google_hourly"):
        return {}
    rows = conn.execute("PRAGMA table_info(forecast_google_hourly)").fetchall()
    return {row[1]: row for row in rows}


def init_archive(conn):
    migrate_daily_archive(conn)
    create_blend_archive_table(conn)
    city_truth.ensure_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS forecast_google_hourly (
            fetched_at TEXT NOT NULL,
            target_date TEXT NOT NULL,
            forecast_hour_utc TEXT NOT NULL,
            forecast_hour_local TEXT NOT NULL,
            lead_hours REAL,
            temperature_f REAL,
            condition TEXT,
            precipitation_probability_pct REAL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (fetched_at, forecast_hour_utc)
        )
        """
    )
    hourly_columns = hourly_archive_columns(conn)
    if "lead_hours" not in hourly_columns:
        conn.execute("ALTER TABLE forecast_google_hourly ADD COLUMN lead_hours REAL")
    # Track which ground-truth source set each score so a late-arriving CLISFO
    # settlement can re-score rows first scored against the NWS fallback.
    for archive_table in ("forecast_blend_daily_high", "forecast_google_daily_high"):
        if "truth_source" not in table_columns(conn, archive_table):
            conn.execute(f"ALTER TABLE {archive_table} ADD COLUMN truth_source TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_google_daily_target
        ON forecast_google_daily_high(target_date)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_google_hourly_target
        ON forecast_google_hourly(target_date)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_blend_daily_target
        ON forecast_blend_daily_high(target_date)
        """
    )


def archive_summary(conn, summary):
    if not summary.get("available") or not summary.get("fetched_at"):
        return 0

    summaries = summary.get("daily_highs") or [summary]
    count = 0
    for row in summaries:
        if not row.get("available") or not row.get("target_date") or row.get("highF") is None:
            continue
        conn.execute(
            """
            INSERT INTO forecast_google_daily_high (
                fetched_at,
                target_date,
                lead_hours,
                source,
                method,
                predicted_high_f,
                peak_hour_local,
                hours_used,
                forecast_start_local,
                forecast_end_local,
                condition,
                precipitation_probability_pct,
                time_zone,
                max_calls_per_day,
                calls_used_today
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fetched_at, target_date) DO UPDATE SET
                lead_hours = excluded.lead_hours,
                source = excluded.source,
                method = excluded.method,
                predicted_high_f = excluded.predicted_high_f,
                peak_hour_local = excluded.peak_hour_local,
                hours_used = excluded.hours_used,
                forecast_start_local = excluded.forecast_start_local,
                forecast_end_local = excluded.forecast_end_local,
                condition = excluded.condition,
                precipitation_probability_pct = excluded.precipitation_probability_pct,
                time_zone = excluded.time_zone,
                max_calls_per_day = excluded.max_calls_per_day,
                calls_used_today = excluded.calls_used_today
            """,
            (
                row.get("fetched_at", summary["fetched_at"]),
                row["target_date"],
                row.get("lead_hours"),
                row.get("source", summary.get("source", "Google Weather API")),
                row.get("method", summary.get("method", "cached Google forecast high")),
                row["highF"],
                row.get("peak_hour_local"),
                row.get("hours_used"),
                row.get("forecast_start_local"),
                row.get("forecast_end_local"),
                row.get("condition"),
                row.get("precipitation_probability_pct"),
                row.get("time_zone", summary.get("time_zone")),
                row.get("max_calls_per_day", summary.get("max_calls_per_day")),
                row.get("calls_used_today", summary.get("calls_used_today")),
            ),
        )
        count += 1
    return count


def archive_blend_summary(conn, blend):
    if not blend:
        return 0
    if isinstance(blend, list):
        return sum(archive_blend_summary(conn, row) for row in blend if row)

    conn.execute(
        """
        INSERT INTO forecast_blend_daily_high (
            fetched_at,
            target_date,
            lead_hours,
            method,
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
            time_zone,
            max_calls_per_day,
            calls_used_today,
            details_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fetched_at, target_date) DO UPDATE SET
            lead_hours = excluded.lead_hours,
            method = excluded.method,
            predicted_high_f = excluded.predicted_high_f,
            google_high_f = excluded.google_high_f,
            nws_high_f = excluded.nws_high_f,
            open_meteo_high_f = excluded.open_meteo_high_f,
            history_high_f = excluded.history_high_f,
            google_weight = excluded.google_weight,
            nws_weight = excluded.nws_weight,
            open_meteo_weight = excluded.open_meteo_weight,
            history_weight = excluded.history_weight,
            station_adjustment_f = excluded.station_adjustment_f,
            fresh_station_count = excluded.fresh_station_count,
            source_count = excluded.source_count,
            time_zone = excluded.time_zone,
            max_calls_per_day = excluded.max_calls_per_day,
            calls_used_today = excluded.calls_used_today,
            details_json = excluded.details_json
        """,
        (
            blend["fetched_at"],
            blend["target_date"],
            blend.get("lead_hours"),
            blend["method"],
            blend["predicted_high_f"],
            blend.get("google_high_f"),
            blend.get("nws_high_f"),
            blend.get("open_meteo_high_f"),
            blend.get("history_high_f"),
            blend.get("google_weight"),
            blend.get("nws_weight"),
            blend.get("open_meteo_weight"),
            blend.get("history_weight"),
            blend.get("station_adjustment_f"),
            blend.get("fresh_station_count"),
            blend.get("source_count"),
            blend.get("time_zone"),
            blend.get("max_calls_per_day"),
            blend.get("calls_used_today"),
            json.dumps(blend.get("details", {}), separators=(",", ":")),
        ),
    )
    return 1


def archive_hourly_rows(conn, payload, summary):
    fetched_at = summary.get("fetched_at")
    fetched_time = parse_google_timestamp(fetched_at)
    if not fetched_at or not fetched_time:
        return 0

    rows = []
    for hour in payload.get("forecastHours") or []:
        local_time = hour_local_datetime(hour)
        temp_f = temp_to_f(hour.get("temperature") or {})
        if not local_time or temp_f is None:
            continue
        hour_utc = local_time.astimezone(timezone.utc).isoformat()
        lead_hours = round((local_time.astimezone(timezone.utc) - fetched_time).total_seconds() / 3600, 2)
        rows.append(
            (
                fetched_at,
                # Settlement-day bucketing (fixed PST), matching
                # daily_high_summaries; civil dates would file the last PST
                # hour of each day under the next target during DST.
                local_standard_date(local_time).isoformat(),
                hour_utc,
                local_time.strftime("%Y-%m-%d %H:%M %Z"),
                lead_hours,
                temp_f,
                condition_text(hour),
                precip_probability(hour),
                json.dumps(hour, separators=(",", ":")),
            )
        )

    conn.executemany(
        """
        INSERT OR IGNORE INTO forecast_google_hourly (
            fetched_at,
            target_date,
            forecast_hour_utc,
            forecast_hour_local,
            lead_hours,
            temperature_f,
            condition,
            precipitation_probability_pct,
            raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


def update_scores_for_table(conn, table_name):
    has_truth_source = "truth_source" in table_columns(conn, table_name)
    if has_truth_source:
        # Re-select rows that are unscored OR were scored against a non-CLISFO
        # fallback, so a late-arriving CLISFO settlement can correct them.
        rows = conn.execute(
            f"""
            SELECT fetched_at, target_date, predicted_high_f, actual_high_f, truth_source
            FROM {table_name}
            WHERE actual_high_f IS NULL
               OR truth_source IS NULL
               OR truth_source != 'clisfo'
            """
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT fetched_at, target_date, predicted_high_f, actual_high_f, NULL AS truth_source
            FROM {table_name}
            WHERE actual_high_f IS NULL
            """
        ).fetchall()
    scored = 0

    for fetched_at, target_iso, predicted_high_f, existing_actual, existing_source in rows:
        actual, source = actual_high_with_source(conn, target_iso)
        if actual is None:
            continue
        settlement_actual = integer_settlement_high_f(actual)
        if settlement_actual is None:
            continue

        is_unscored = existing_actual is None
        is_clisfo_upgrade = source == "clisfo" and existing_source != "clisfo"
        # Legacy rows predate the truth_source column. Stamp them as "legacy"
        # WITHOUT rewriting the stored value -- only a first-time score or a
        # CLISFO upgrade may change actual_high_f/abs_error_f, so a label
        # backfill can never silently corrupt historical error records.
        needs_stamp = has_truth_source and existing_source is None and not is_unscored

        if is_unscored or is_clisfo_upgrade:
            abs_error = round(abs(float(predicted_high_f) - settlement_actual), 2)
            scored_at = datetime.now(timezone.utc).isoformat()
            if has_truth_source:
                conn.execute(
                    f"""
                    UPDATE {table_name}
                    SET actual_high_f = ?,
                        abs_error_f = ?,
                        scored_at = ?,
                        truth_source = ?
                    WHERE fetched_at = ?
                      AND target_date = ?
                    """,
                    (settlement_actual, abs_error, scored_at, source, fetched_at, target_iso),
                )
            else:
                conn.execute(
                    f"""
                    UPDATE {table_name}
                    SET actual_high_f = ?,
                        abs_error_f = ?,
                        scored_at = ?
                    WHERE fetched_at = ?
                      AND target_date = ?
                    """,
                    (settlement_actual, abs_error, scored_at, fetched_at, target_iso),
                )
            scored += 1
        elif needs_stamp:
            conn.execute(
                f"UPDATE {table_name} SET truth_source = 'legacy' "
                "WHERE fetched_at = ? AND target_date = ?",
                (fetched_at, target_iso),
            )
            scored += 1

    return scored


def refresh_clisfo_settlements(conn):
    """Fetch recent CLISFO settlement highs and upsert them.

    Network failures must not break scoring, so this swallows fetch errors and
    leaves the table as-is. Disable with SFO_DISABLE_CLISFO=1.
    """

    if os.getenv("SFO_DISABLE_CLISFO", "0").strip().lower() in {"1", "true", "yes", "on"}:
        return 0
    try:
        reports = fetch_recent_cli_reports("MTR", "SFO")
    except Exception:
        return 0
    city_truth.ensure_schema(conn)
    observed_at = city_truth._utcnow()
    now_iso = observed_at.isoformat()
    sfo_city = next(city for city in city_truth.CITIES if city.nws_station_id == "KSFO")
    stored = 0
    for report_date, report in reports.items():
        city_truth.upsert_settlement(
            conn,
            "KSFO",
            report_date.isoformat(),
            int(report.max_temperature_f),
            fetched_at=now_iso,
            is_final=(
                not report.is_preliminary
                and city_truth.settlement_is_final(sfo_city, report_date, observed_at)
            ),
        )
        stored += 1
    return stored


def clisfo_high_for(conn, target_iso):
    city_truth.ensure_schema(conn)
    return city_truth.cli_high_for(conn, "KSFO", target_iso)


def update_scores(conn):
    refresh_clisfo_settlements(conn)
    scored = update_scores_for_table(conn, "forecast_google_daily_high")
    scored += update_scores_for_table(conn, "forecast_blend_daily_high")
    return scored


def nws_daily_complete_high(conn, target_iso: str) -> float | None:
    if not table_exists(conn, "nws_daily_high_ground_truth"):
        return None

    row = conn.execute(
        """
        SELECT high_f
        FROM nws_daily_high_ground_truth
        WHERE station_id = 'KSFO'
          AND local_date = ?
          AND is_complete = 1
          AND high_f IS NOT NULL
        """,
        (target_iso,),
    ).fetchone()
    return integer_settlement_high_f(row[0]) if row else None


def actual_high_from_ground_truth(conn, target_iso):
    # Prefer the CLISFO Daily Climate Report MAXIMUM -- the same settlement
    # truth the trader resolves on -- so forecast skill and the learned source
    # weights are scored against the real Kalshi outcome, not the max of hourly
    # observations (which can differ by ~1F and flip an integer-bin membership).
    finality_authoritative = "is_final" in table_columns(conn, "cli_settlements")
    clisfo_high = clisfo_high_for(conn, target_iso)
    if clisfo_high is not None:
        return clisfo_high
    if finality_authoritative:
        return None
    return nws_daily_complete_high(conn, target_iso)


def actual_high_with_source(conn, target_iso: str) -> tuple[float | None, str | None]:
    """Settlement high plus the truth source it came from.

    Returns ``(value, source)`` where source is ``"clisfo"`` (the real Kalshi
    settlement), ``"nws_daily"`` (completed NWS daily report), or
    ``"nws_hourly_fallback"`` (max of archived hourly observations). The source
    lets a late-arriving CLISFO settlement re-score rows first scored against a
    fallback -- the fallback can diverge from CLISFO by ~1F and flip a bin.
    """

    finality_authoritative = "is_final" in table_columns(conn, "cli_settlements")
    clisfo_high = clisfo_high_for(conn, target_iso)
    if clisfo_high is not None:
        return clisfo_high, "clisfo"
    if finality_authoritative:
        return None, None
    nws_daily = nws_daily_complete_high(conn, target_iso)
    if nws_daily is not None:
        return nws_daily, "nws_daily"
    history = actual_high_from_history(conn, target_iso)
    if history is not None:
        return history, "nws_hourly_fallback"
    return None, None


def actual_high_from_history(conn, target_iso):
    if not table_exists(conn, "weather"):
        return None

    start_utc, end_utc = local_midnight_utc(target_iso)
    return conn.execute(
        """
        SELECT MAX(temp_f)
        FROM weather
        WHERE station_id = ?
          AND timestamp >= ?
          AND timestamp < ?
        """,
        (SFO_WEATHER_STATION_ID, start_utc, end_utc),
    ).fetchone()[0]


def archive_forecast(summary, payload=None, blend=None):
    with sqlite3.connect(DB_PATH) as conn:
        init_archive(conn)
        daily_rows = archive_summary(conn, summary)
        blend_rows = archive_blend_summary(conn, blend)
        hourly_rows = 0
        if payload:
            hourly_rows = archive_hourly_rows(conn, payload, summary)
        scored = update_scores(conn)
        conn.commit()
    return {
        "daily_rows": daily_rows,
        "blend_rows": blend_rows,
        "hourly_rows": hourly_rows,
        "scored": scored,
    }


def score_archive():
    with sqlite3.connect(DB_PATH) as conn:
        init_archive(conn)
        scored = update_scores(conn)
        conn.commit()
    return {"daily_rows": 0, "blend_rows": 0, "hourly_rows": 0, "scored": scored}


def latest_scored_blend_rows():
    if not DB_PATH.exists():
        return []
    try:
        with sqlite3.connect(DB_PATH) as conn:
            if not table_exists(conn, "forecast_blend_daily_high"):
                return []
            has_clisfo = table_exists(conn, "cli_settlements") or table_exists(
                conn, "clisfo_settlements"
            )
            finality_authoritative = (
                table_exists(conn, "cli_settlements")
                and "is_final" in table_columns(conn, "cli_settlements")
            )
            blend_columns = table_columns(conn, "forecast_blend_daily_high")
            has_truth_source = "truth_source" in blend_columns
            conn.row_factory = sqlite3.Row

            stored_source = "b.truth_source" if has_truth_source else "NULL"
            station_adjustment_expr = (
                "b.station_adjustment_f" if "station_adjustment_f" in blend_columns else "NULL"
            )
            if has_clisfo:
                # Prefer the CLISFO settlement directly so learned weights track
                # the real Kalshi outcome even when a row's stored actual_high_f
                # predates the late CLISFO arrival.
                actual_expr = (
                    "c.max_temperature_f"
                    if finality_authoritative
                    else "COALESCE(c.max_temperature_f, b.actual_high_f)"
                )
                effective_source_expr = (
                    f"CASE WHEN c.max_temperature_f IS NOT NULL THEN 'clisfo' "
                    f"ELSE {stored_source} END"
                )
                if table_exists(conn, "cli_settlements"):
                    final_join = (
                        "AND c.is_final = 1 "
                        if "is_final" in table_columns(conn, "cli_settlements")
                        else ""
                    )
                    join_clause = (
                        "LEFT JOIN cli_settlements c "
                        "ON c.local_date = b.target_date AND c.station_id = 'KSFO' "
                        f"AND c.max_temperature_f IS NOT NULL {final_join}"
                    )
                else:
                    join_clause = (
                        "LEFT JOIN clisfo_settlements c "
                        "ON c.local_date = b.target_date AND c.max_temperature_f IS NOT NULL"
                    )
            else:
                actual_expr = "b.actual_high_f"
                effective_source_expr = stored_source
                join_clause = ""

            truth_filter = (
                "c.max_temperature_f IS NOT NULL"
                if finality_authoritative
                else "b.actual_high_f IS NOT NULL"
            )

            rows = conn.execute(
                f"""
                SELECT b.target_date,
                       {actual_expr} AS actual_high_f,
                       b.predicted_high_f,
                       b.google_high_f,
                       b.nws_high_f,
                       b.open_meteo_high_f,
                       b.history_high_f,
                       b.fetched_at,
                       b.details_json,
                       {station_adjustment_expr} AS station_adjustment_f,
                       {effective_source_expr} AS effective_truth_source
                FROM forecast_blend_daily_high b
                {join_clause}
                WHERE {truth_filter}
                  AND b.abs_error_f IS NOT NULL
                ORDER BY b.target_date, b.fetched_at
                """
            ).fetchall()
            eligible = [
                row
                for row in rows
                if is_clean_next_day_forecast(
                    row["target_date"],
                    row["fetched_at"],
                    row["details_json"],
                )
            ]
            latest_by_day = {}
            for row in eligible:
                current = latest_by_day.get(row["target_date"])
                if current is None or row["fetched_at"] > current["fetched_at"]:
                    latest_by_day[row["target_date"]] = row
            return list(latest_by_day.values())
    except sqlite3.Error:
        return []
