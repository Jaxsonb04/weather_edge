"""Build cities_data.json: the public multi-city snapshot the SPA renders.

One entry per configured city: the live EMOS forecast for each open target,
the latest CLI settlement, and the paper books' activity in that city's
market. Everything here is already public by design (paper research), and the
freshness fields let the page be truthful about data status instead of
implying liveness it does not have.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .cities import CITIES, CityConfig
from .config import DEFAULT_DB_PATH, DEFAULT_FORECASTER_ROOT
from .settlement_day import settlement_today


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _city_forecasts(conn: sqlite3.Connection, city: CityConfig) -> list[dict]:
    if not _table_exists(conn, "forecast_emos_daily_high"):
        return []
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(forecast_emos_daily_high)")
    }
    if "station_id" not in columns:
        return []
    today = settlement_today(None, city).isoformat()
    rows = conn.execute(
        """
        SELECT target_date, lead_days, predicted_high_f, sigma_f, n_models,
               model_spread_f, fetched_at, method
        FROM forecast_emos_daily_high
        WHERE station_id = ? AND target_date >= ?
        ORDER BY target_date, fetched_at DESC
        """,
        (city.nws_station_id, today),
    ).fetchall()
    seen: set[str] = set()
    out: list[dict] = []
    for target, lead, mu, sigma, n_models, spread, fetched_at, method in rows:
        if target in seen:
            continue  # freshest row per target wins (ordered DESC)
        seen.add(target)
        out.append(
            {
                "target_date": target,
                "lead_days": lead,
                "predicted_high_f": round(float(mu), 2),
                "sigma_f": round(float(sigma), 2),
                "n_models": n_models,
                "model_spread_f": spread,
                "fetched_at": fetched_at,
                "method": method,
            }
        )
    return out


def _city_settlement(conn: sqlite3.Connection, city: CityConfig) -> dict | None:
    if not _table_exists(conn, "cli_settlements"):
        return None
    row = conn.execute(
        """
        SELECT local_date, max_temperature_f, fetched_at, source
        FROM cli_settlements
        WHERE station_id = ? AND max_temperature_f IS NOT NULL
        ORDER BY local_date DESC LIMIT 1
        """,
        (city.nws_station_id,),
    ).fetchone()
    if row is None:
        return None
    local_date, high, fetched_at, source = row
    return {
        "local_date": local_date,
        "high_f": high,
        "fetched_at": fetched_at,
        "source": source,
    }


def _city_books(conn: sqlite3.Connection, city: CityConfig) -> dict:
    prefix = f"{city.series_ticker}-%"
    books: dict = {}
    for profile in ("live", "research"):
        open_row = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(contracts * cost_per_contract), 0)
            FROM paper_orders
            WHERE market_ticker LIKE ?
              AND COALESCE(risk_profile, 'live') = ?
              AND status IN ('PAPER_FILLED', 'PAPER_LIMIT_RESTING')
              AND settled_at IS NULL AND closed_at IS NULL
            """,
            (prefix, profile),
        ).fetchone()
        settled_row = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0)
            FROM paper_orders
            WHERE market_ticker LIKE ?
              AND COALESCE(risk_profile, 'live') = ?
              AND status = 'PAPER_SETTLED'
            """,
            (prefix, profile),
        ).fetchone()
        decisions_24h = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(approved), 0)
            FROM decision_snapshots
            WHERE market_ticker LIKE ?
              AND created_at > datetime('now', '-1 day')
            """,
            (prefix,),
        ).fetchone()
        books[profile] = {
            "open_positions": open_row[0],
            "open_exposure": round(float(open_row[1]), 2),
            "settled_orders": settled_row[0],
            "settled_pnl": round(float(settled_row[1]), 2),
        }
        books["decisions_24h"] = decisions_24h[0]
        books["approved_24h"] = decisions_24h[1]
    return books


def build_cities_data(
    forecaster_root: Path, paper_db: Path
) -> dict:
    weather_db = Path(forecaster_root) / "weather.db"
    cities_payload: list[dict] = []
    weather_conn = sqlite3.connect(weather_db) if weather_db.exists() else None
    paper_conn = sqlite3.connect(paper_db) if Path(paper_db).exists() else None
    try:
        for city in CITIES:
            entry: dict = {
                "slug": city.slug,
                "name": city.name,
                "series_ticker": city.series_ticker,
                "station_id": city.nws_station_id,
                "settlement_source": (
                    f"NWS Climatological Report (CLI {city.cli_issuedby}, "
                    f"WFO {city.cli_site})"
                ),
                "civil_tz": city.civil_tz_name,
                "has_full_blend": city.has_full_blend,
                "forecasts": [],
                "latest_settlement": None,
                "books": None,
            }
            if weather_conn is not None:
                entry["forecasts"] = _city_forecasts(weather_conn, city)
                entry["latest_settlement"] = _city_settlement(weather_conn, city)
            if paper_conn is not None:
                entry["books"] = _city_books(paper_conn, city)
            cities_payload.append(entry)
    finally:
        if weather_conn is not None:
            weather_conn.close()
        if paper_conn is not None:
            paper_conn.close()

    fresh = [
        c
        for c in cities_payload
        if any(f.get("fetched_at") for f in c["forecasts"])
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "city_count": len(cities_payload),
        "cities_with_live_forecasts": len(fresh),
        "note": (
            "Paper-trading research only. Forecasts are EMOS-calibrated "
            "multi-model NWP Gaussians per settlement station; every market "
            "settles on its own NWS Climatological Report."
        ),
        "cities": cities_payload,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forecaster-root", default=str(DEFAULT_FORECASTER_ROOT))
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    payload = build_cities_data(Path(args.forecaster_root), Path(args.db_path))
    Path(args.output).write_text(json.dumps(payload, indent=1, sort_keys=True))
    print(
        f"wrote {args.output}: {payload['cities_with_live_forecasts']}/"
        f"{payload['city_count']} cities with live forecasts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
