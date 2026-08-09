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
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ._util import _table_exists
from .cities import CITIES, CityConfig, city_for_market_ticker
from .config import DEFAULT_DB_PATH, DEFAULT_FORECASTER_ROOT, normalize_risk_profile_name
from .emos_sources import forecast_source_precedence
from .forecast import ForecastDataError, SfoForecasterAdapter
from .models import ForecastSnapshot
from .settlement_day import settlement_today


def _target_status(target: str, today: date) -> str:
    target_date = date.fromisoformat(target)
    if target_date < today:
        return "past"
    if target_date == today:
        return "settlement_day"
    return "upcoming"


def _city_forecasts(
    conn: sqlite3.Connection,
    city: CityConfig,
    today: date,
    *,
    forecaster_root: Path,
) -> list[dict]:
    if not _table_exists(conn, "forecast_emos_daily_high"):
        return []
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(forecast_emos_daily_high)")
    }
    if "station_id" not in columns:
        return []
    rows = conn.execute(
        """
        SELECT target_date, lead_days, predicted_high_f, sigma_f, n_models,
               model_spread_f, fetched_at, method, source
        FROM forecast_emos_daily_high
        WHERE station_id = ? AND target_date >= ?
        ORDER BY target_date
        """,
        (city.nws_station_id, today.isoformat()),
    ).fetchall()
    preferred: dict[str, tuple[tuple[int, str], tuple]] = {}
    for row in rows:
        target, _lead, _mu, _sigma, _n_models, _spread, fetched_at, _method, source = row
        rank = (forecast_source_precedence(str(source)), str(fetched_at))
        current = preferred.get(str(target))
        if current is None or rank > current[0]:
            preferred[str(target)] = (rank, row)

    out: list[dict] = []
    adapter: SfoForecasterAdapter | None = None
    for target in sorted(preferred):
        _rank, row = preferred[target]
        target, lead, mu, sigma, n_models, spread, fetched_at, method, _source = row
        entry: dict = {
            "target_date": target,
            "target_status": _target_status(target, today),
            "lead_days": lead,
            "predicted_high_f": round(float(mu), 2),
            # The EMOS mean before any intraday update. This is the value
            # ``sigma_f`` is the spread of; ``predicted_high_f`` may have moved
            # off it once the day started being observed.
            "predicted_high_f_pre_intraday": round(float(mu), 2),
            "intraday_update": None,
            "sigma_f": round(float(sigma), 2),
            "n_models": n_models,
            "model_spread_f": spread,
            "fetched_at": fetched_at,
            "method": method,
        }
        if entry["target_status"] == "settlement_day":
            if adapter is None:
                adapter = SfoForecasterAdapter(forecaster_root, city)
            # Feed the unrounded mu, exactly what the trading path blends, so
            # the two artifacts round once and land on the same cent.
            entry = _with_intraday_update(entry, adapter, city, base_high_f=float(mu))
        out.append(entry)
    return out


def _with_intraday_update(
    forecast: dict,
    adapter: SfoForecasterAdapter,
    city: CityConfig,
    *,
    base_high_f: float,
) -> dict:
    """Serve the same intraday-updated high the trading path already serves.

    Once a settlement day starts being observed, the trading path folds the
    high-so-far into the point forecast before it quotes anything
    (``SfoForecasterAdapter.apply_intraday_update``). Publishing the raw EMOS
    mean here instead made this file disagree with trading_signal.json for the
    same city and day by more than the sigma printed next to it. Reuse the
    adapter so one number means one thing in both artifacts, and keep the
    pre-update mean rather than dropping the evidence.
    """

    target = date.fromisoformat(forecast["target_date"])
    try:
        intraday = adapter.intraday_snapshot(target)
    except (ForecastDataError, sqlite3.Error, OSError):
        # A public snapshot must never fail on a missing observation table;
        # the un-updated EMOS mean stays, labelled as such.
        return forecast
    if intraday is None or intraday.observed_high_f is None:
        return forecast
    updated = adapter.apply_intraday_update(
        ForecastSnapshot(
            target_date=target,
            predicted_high_f=base_high_f,
            station_id=city.nws_station_id,
            fetched_at=forecast["fetched_at"],
            method=str(forecast["method"] or "emos"),
        ),
        intraday,
    )
    detail = updated.raw.get("intraday_update") or {}
    return {
        **forecast,
        "predicted_high_f": round(float(updated.predicted_high_f), 2),
        "method": updated.method,
        "intraday_update": {
            "applied": True,
            "observed_high_f": intraday.observed_high_f,
            "latest_temp_f": intraday.latest_temp_f,
            "latest_observed_at": intraday.latest_observed_at,
            "remaining_forecast_high_f": intraday.remaining_forecast_high_f,
            "observation_count": intraday.observation_count,
            "observed_high_source": intraday.observed_high_source,
            "is_complete": intraday.is_complete,
            "weight": detail.get("intraday_weight"),
        },
    }


def _city_settlement(conn: sqlite3.Connection, city: CityConfig) -> dict | None:
    if not _table_exists(conn, "cli_settlements"):
        return None
    columns = {row[1] for row in conn.execute("PRAGMA table_info(cli_settlements)")}
    final_filter = "AND is_final = 1" if "is_final" in columns else ""
    row = conn.execute(
        f"""
        SELECT local_date, max_temperature_f, fetched_at, source
        FROM cli_settlements
        WHERE station_id = ? AND max_temperature_f IS NOT NULL
          {final_filter}
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


def _empty_profile_book() -> dict:
    return {
        "open_positions": 0,
        "open_exposure": 0.0,
        "settled_orders": 0,
        "settled_pnl": 0.0,
    }


def _empty_city_book() -> dict:
    return {
        "live": _empty_profile_book(),
        "research": _empty_profile_book(),
        "decisions_24h": 0,
        "approved_24h": 0,
    }


def _all_city_books(conn: sqlite3.Connection, cutoff_iso: str) -> dict[str, dict]:
    """Aggregate every configured city's activity with two table passes total."""

    books = {city.slug: _empty_city_book() for city in CITIES}
    if _table_exists(conn, "decision_snapshots"):
        decision_rows = conn.execute(
            """
            SELECT market_ticker, COUNT(*), COALESCE(SUM(approved), 0)
            FROM decision_snapshots
            WHERE created_at >= ?
            GROUP BY market_ticker
            """,
            (cutoff_iso,),
        ).fetchall()
        for ticker, count, approved in decision_rows:
            city = city_for_market_ticker(ticker)
            if city is None:
                continue
            books[city.slug]["decisions_24h"] += int(count)
            books[city.slug]["approved_24h"] += int(approved)

    if _table_exists(conn, "paper_orders"):
        order_rows = conn.execute(
            """
            SELECT market_ticker,
                   COALESCE(risk_profile, 'live') AS risk_profile,
                   SUM(CASE
                         WHEN status IN (
                              'PAPER_FILLED', 'PAPER_LIMIT_RESTING',
                              'PAPER_PARTIALLY_FILLED', 'PAPER_PARTIAL_EXPIRED'
                         )
                          AND settled_at IS NULL AND closed_at IS NULL
                         THEN 1 ELSE 0
                       END) AS open_positions,
                   COALESCE(SUM(CASE
                         WHEN status IN (
                              'PAPER_FILLED', 'PAPER_LIMIT_RESTING',
                              'PAPER_PARTIALLY_FILLED', 'PAPER_PARTIAL_EXPIRED'
                         )
                          AND settled_at IS NULL AND closed_at IS NULL
                         THEN contracts * cost_per_contract ELSE 0
                       END), 0) AS open_exposure,
                   SUM(CASE WHEN status = 'PAPER_SETTLED' THEN 1 ELSE 0 END)
                       AS settled_orders,
                   COALESCE(SUM(CASE
                         WHEN status = 'PAPER_SETTLED' THEN realized_pnl ELSE 0
                       END), 0) AS settled_pnl
            FROM paper_orders
            GROUP BY market_ticker, COALESCE(risk_profile, 'live')
            """
        ).fetchall()
        for ticker, raw_profile, open_count, exposure, settled_count, pnl in order_rows:
            city = city_for_market_ticker(ticker)
            if city is None:
                continue
            try:
                profile = normalize_risk_profile_name(str(raw_profile))
            except ValueError:
                continue
            if profile not in ("live", "research"):
                continue
            profile_book = books[city.slug][profile]
            profile_book["open_positions"] += int(open_count)
            profile_book["open_exposure"] += float(exposure)
            profile_book["settled_orders"] += int(settled_count)
            profile_book["settled_pnl"] += float(pnl)
        for city_book in books.values():
            for profile in ("live", "research"):
                city_book[profile]["open_exposure"] = round(
                    float(city_book[profile]["open_exposure"]),
                    2,
                )
                city_book[profile]["settled_pnl"] = round(
                    float(city_book[profile]["settled_pnl"]),
                    2,
                )
    return books


def build_cities_data(
    forecaster_root: Path,
    paper_db: Path,
    *,
    now: datetime | None = None,
) -> dict:
    generated_at = now or datetime.now(timezone.utc)
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    generated_at = generated_at.astimezone(timezone.utc)
    cutoff_iso = (generated_at - timedelta(days=1)).isoformat(timespec="seconds")
    weather_db = Path(forecaster_root) / "weather.db"
    cities_payload: list[dict] = []
    weather_conn = sqlite3.connect(weather_db) if weather_db.exists() else None
    paper_conn = sqlite3.connect(paper_db) if Path(paper_db).exists() else None
    try:
        all_books = _all_city_books(paper_conn, cutoff_iso) if paper_conn is not None else None
        for city in CITIES:
            city_today = settlement_today(generated_at, city)
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
                "settlement_today": city_today.isoformat(),
                "has_full_blend": city.has_full_blend,
                "forecasts": [],
                "latest_settlement": None,
                "books": None,
            }
            if weather_conn is not None:
                entry["forecasts"] = _city_forecasts(
                    weather_conn,
                    city,
                    city_today,
                    forecaster_root=Path(forecaster_root),
                )
                entry["latest_settlement"] = _city_settlement(weather_conn, city)
            if all_books is not None:
                entry["books"] = all_books[city.slug]
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
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "city_count": len(cities_payload),
        "cities_with_live_forecasts": len(fresh),
        "note": (
            "Paper-trading research only. Forecasts are EMOS-calibrated "
            "multi-model NWP Gaussians per settlement station; once a "
            "settlement day is under way its predicted_high_f also carries the "
            "observed high-so-far update the trading path serves, with the "
            "pre-update mean kept as predicted_high_f_pre_intraday. Every "
            "market settles on its own NWS Climatological Report."
        ),
        "cities": cities_payload,
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=1, sort_keys=True) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forecaster-root", default=str(DEFAULT_FORECASTER_ROOT))
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    payload = build_cities_data(Path(args.forecaster_root), Path(args.db_path))
    _atomic_write_json(Path(args.output), payload)
    print(
        f"wrote {args.output}: {payload['cities_with_live_forecasts']}/"
        f"{payload['city_count']} cities with live forecasts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
