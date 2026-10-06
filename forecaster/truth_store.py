"""Small SQLite readers shared by live EMOS serving and research backtests."""

from __future__ import annotations

import sqlite3
import math

import city_truth
from nwp_archive import DEFAULT_SOURCE as NWP_DEFAULT_SOURCE


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
    conn: sqlite3.Connection, lead_days: int, station_id: str = "KSFO",
    *, source: str | None = None,
) -> dict[str, dict[str, float]]:
    """Return daily highs from exactly one archive source for a station/lead.

    Prefer the canonical previous-runs archive. A single alternative remains
    readable for legacy/research fixtures, but ambiguous alternatives require an
    explicit source. Never merge retrospective and live-input sources by row
    order; source is part of the archive's primary key for a reason.
    """

    out: dict[str, dict[str, float]] = {}
    if not _table_exists(conn, "nwp_model_forecasts"):
        return out
    columns = {row[1] for row in conn.execute("PRAGMA table_info(nwp_model_forecasts)")}
    if "station_id" not in columns and station_id != "KSFO":
        return out
    filters = ["lead_days = ?", "predicted_high_f IS NOT NULL"]
    params: list[object] = [lead_days]
    if "station_id" in columns:
        filters.append("station_id = ?")
        params.append(station_id)
    if "source" in columns:
        sources = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT source FROM nwp_model_forecasts WHERE "
                + " AND ".join(filters), params,
            )
        }
        selected = source
        if selected is None:
            if NWP_DEFAULT_SOURCE in sources:
                selected = NWP_DEFAULT_SOURCE
            elif len(sources) == 1:
                selected = next(iter(sources))
            elif sources:
                raise ValueError("ambiguous NWP archive sources; select one explicitly")
            else:
                return out
        filters.append("source = ?")
        params.append(selected)
    elif source is not None:
        return out
    cursor = conn.execute(
        "SELECT target_date, model, predicted_high_f FROM nwp_model_forecasts WHERE "
        + " AND ".join(filters), params,
    )
    for target_date, model, value in cursor:
        number = float(value)
        if math.isfinite(number):
            out.setdefault(target_date, {})[model] = number
    return out
