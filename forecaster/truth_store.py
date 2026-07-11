"""Small SQLite readers shared by live EMOS serving and research backtests."""

from __future__ import annotations

import sqlite3

import city_truth


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def load_clisfo_truth(
    conn: sqlite3.Connection, station_id: str = "KSFO"
) -> dict[str, float]:
    """Load confirmed CLI settlement truth for one station.

    The historical function name remains the compatibility API even though the
    underlying table is station-keyed for all cities.
    """

    city_truth.ensure_schema(conn)
    return city_truth.load_cli_truth(conn, station_id)


def load_nwp_forecasts(
    conn: sqlite3.Connection, lead_days: int, station_id: str = "KSFO"
) -> dict[str, dict[str, float]]:
    """Return ``target_date -> {model: daily high}`` for one station/lead."""

    out: dict[str, dict[str, float]] = {}
    if not _table_exists(conn, "nwp_model_forecasts"):
        return out
    columns = {row[1] for row in conn.execute("PRAGMA table_info(nwp_model_forecasts)")}
    if "station_id" not in columns:
        if station_id != "KSFO":
            return out
        cursor = conn.execute(
            "SELECT target_date, model, predicted_high_f FROM nwp_model_forecasts "
            "WHERE lead_days = ? AND predicted_high_f IS NOT NULL",
            (lead_days,),
        )
    else:
        cursor = conn.execute(
            "SELECT target_date, model, predicted_high_f FROM nwp_model_forecasts "
            "WHERE lead_days = ? AND station_id = ? AND predicted_high_f IS NOT NULL",
            (lead_days, station_id),
        )
    for target_date, model, value in cursor:
        out.setdefault(target_date, {})[model] = float(value)
    return out
