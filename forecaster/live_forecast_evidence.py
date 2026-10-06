"""Immutable evidence for the forecast actually served, separate from replay.

The current Open-Meteo endpoint returns named daily maxima. It does not expose
the model initialization or constituent-hour completeness in this response.
Those fields remain NULL; retrieval is evidence of availability, never an
inferred initialization time. No historical forecasts are backdated here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from cities import CityConfig
from settlement_calendar import utc_window_for_local_standard_date


class LiveModelForecast(dict):
    """Compatibility mapping carrying the actual response retrieval clock."""

    def __init__(self, values, *, retrieved_at: str):
        super().__init__(values)
        self.retrieved_at = retrieved_at


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS forecast_emos_live_vintages (
            snapshot_id TEXT PRIMARY KEY,
            station_id TEXT NOT NULL,
            target_date TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            retrieved_at TEXT,
            provider_initialized_at TEXT,
            source TEXT NOT NULL,
            window_start_utc TEXT NOT NULL,
            window_end_utc TEXT NOT NULL,
            standard_utc_offset_hours INTEGER NOT NULL,
            lead_days INTEGER NOT NULL,
            fit_lead_days INTEGER NOT NULL,
            legacy_lead_days INTEGER NOT NULL,
            predicted_high_f REAL NOT NULL,
            sigma_f REAL NOT NULL,
            method TEXT NOT NULL,
            training_days INTEGER NOT NULL,
            training_truth_end TEXT NOT NULL,
            training_digest_sha256 TEXT NOT NULL,
            fit_params_json TEXT NOT NULL,
            serve_policy_json TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nwp_live_forecast_members (
            snapshot_id TEXT NOT NULL REFERENCES forecast_emos_live_vintages(snapshot_id),
            model TEXT NOT NULL,
            predicted_high_f REAL NOT NULL,
            used_in_fit INTEGER NOT NULL,
            complete_hour_count INTEGER,
            aggregation_basis TEXT NOT NULL,
            PRIMARY KEY (snapshot_id, model)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_live_vintages_station_target_clock "
                 "ON forecast_emos_live_vintages(station_id, target_date, recorded_at)")
    for table in ("forecast_emos_live_vintages", "nwp_live_forecast_members"):
        for operation in ("UPDATE", "DELETE"):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_reject_{operation.lower()} "
                f"BEFORE {operation} ON {table} BEGIN "
                "SELECT RAISE(ABORT, 'live forecast evidence is append-only'); END"
            )
        identity = "snapshot_id = NEW.snapshot_id"
        if table == "nwp_live_forecast_members":
            identity += " AND model = NEW.model"
        # REPLACE deletes without firing DELETE triggers unless SQLite's
        # recursive_triggers option is enabled. Reject replacement at INSERT
        # as well, independently of that connection-level setting.
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_reject_replacement "
            f"BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {identity}) "
            "BEGIN SELECT RAISE(ABORT, 'live forecast evidence is append-only'); END"
        )


def _iso_utc(value: str) -> str:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("live evidence clocks require a timezone")
    return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")


def record_live_forecast(
    conn: sqlite3.Connection, *, city: CityConfig, target_date: date,
    recorded_at: str, inputs: dict[str, float], used_models: set[str],
    mu: float, sigma: float, fit_lead_days: int, legacy_lead_days: int,
    method: str, training: list[tuple[str, dict[str, float], float]],
    fit_params: dict, serve_policy: dict,
) -> str:
    """Append one served distribution and its named inputs in caller's transaction.

    Content-addressed identity deduplicates an identical repeat while retaining
    different inputs/outputs even if their recorded clock happens to collide.
    Plain injected mappings carry no verified retrieval clock and stay NULL.
    """
    ensure_schema(conn)
    if not training or not math.isfinite(mu) or not math.isfinite(sigma) or sigma <= 0:
        raise ValueError("live evidence requires finite forecasts and training lineage")
    clock = _iso_utc(recorded_at)
    retrieved = getattr(inputs, "retrieved_at", None)
    retrieved = _iso_utc(retrieved) if retrieved else None
    if retrieved and retrieved > clock:
        raise ValueError("retrieval cannot follow evidence recording")
    start, end = utc_window_for_local_standard_date(target_date, city.fixed_standard_timezone())
    observed_day = datetime.fromisoformat(clock).astimezone(city.fixed_standard_timezone()).date()
    members = [
        {"model": name, "predicted_high_f": float(value), "used_in_fit": int(name in used_models),
         "complete_hour_count": None, "aggregation_basis": "provider_daily_max" if retrieved else "unverified_input"}
        for name, value in sorted(inputs.items())
        if type(value) in (int, float) and math.isfinite(value)
    ]
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    training_digest = hashlib.sha256(canonical(training).encode()).hexdigest()
    payload = {
        "station_id": city.nws_station_id, "target_date": target_date.isoformat(),
        "recorded_at": clock, "retrieved_at": retrieved, "provider_initialized_at": None,
        "source": "openmeteo_current_forecast" if retrieved else "unverified_input",
        "window_start_utc": start.isoformat(), "window_end_utc": end.isoformat(),
        "standard_utc_offset_hours": city.standard_utc_offset_hours,
        "lead_days": (target_date - observed_day).days,
        "fit_lead_days": fit_lead_days, "legacy_lead_days": legacy_lead_days,
        "predicted_high_f": mu, "sigma_f": sigma, "method": method,
        "training_days": len(training), "training_truth_end": max(row[0] for row in training),
        "training_digest_sha256": training_digest, "fit_params_json": canonical(fit_params),
        "serve_policy_json": canonical(serve_policy),
    }
    snapshot_id = hashlib.sha256(canonical({**payload, "members": members}).encode()).hexdigest()
    payload = {"snapshot_id": snapshot_id, **payload}
    conn.execute(
        f"INSERT INTO forecast_emos_live_vintages ({', '.join(payload)}) "
        f"SELECT {', '.join('?' for _ in payload)} "
        "WHERE NOT EXISTS(SELECT 1 FROM forecast_emos_live_vintages WHERE snapshot_id=?)",
        (*payload.values(), snapshot_id),
    )
    conn.executemany("INSERT INTO nwp_live_forecast_members "
                     "SELECT ?, ?, ?, ?, ?, ? WHERE NOT EXISTS(SELECT 1 FROM nwp_live_forecast_members "
                     "WHERE snapshot_id=? AND model=?)", [
        (snapshot_id, row["model"], row["predicted_high_f"], row["used_in_fit"],
         row["complete_hour_count"], row["aggregation_basis"], snapshot_id, row["model"])
        for row in members
    ])
    return snapshot_id


def export_live_forecasts(conn: sqlite3.Connection, *, before: str, limit: int = 1000) -> list[dict]:
    """Bounded read-only export of vintages observed by a cutoff, newest first."""
    if not 1 <= limit <= 10000:
        raise ValueError("limit must be 1..10000")
    cutoff = _iso_utc(before)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='forecast_emos_live_vintages'").fetchone() is None:
        return []
    cursor = conn.execute("SELECT * FROM forecast_emos_live_vintages WHERE recorded_at <= ? "
                          "ORDER BY recorded_at DESC, snapshot_id LIMIT ?", (cutoff, limit))
    columns = [item[0] for item in cursor.description]
    out = []
    for values in cursor.fetchall():
        row = dict(zip(columns, values))
        row["members"] = [dict(zip(
            ("model", "predicted_high_f", "used_in_fit", "complete_hour_count", "aggregation_basis"), values
        )) for values in conn.execute(
            "SELECT model, predicted_high_f, used_in_fit, complete_hour_count, aggregation_basis "
            "FROM nwp_live_forecast_members WHERE snapshot_id=? ORDER BY model", (row["snapshot_id"],)
        )]
        out.append(row)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--before", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args(argv)
    uri = Path(args.db).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        for row in export_live_forecasts(conn, before=args.before, limit=args.limit):
            print(json.dumps(row, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
