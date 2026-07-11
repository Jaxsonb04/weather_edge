"""Leakage-free multi-model NWP forecast archive for the SFO daily high.

Phase 0 of the forecast-accuracy upgrade. The live trade engine's "model
probability" rides on a heuristic point-blend of a few public forecasts; this
module builds the missing foundation: a clean archive of what the major weather
models *actually predicted* for each SFO settlement day, so a trained
probabilistic post-processor (EMOS/analogs/DRN in later phases) has something to
learn from and the scoreboard has something to score.

Why the Previous-Runs API (and not the plain Historical-Forecast daily max):

* The Historical-Forecast API returns, for a past date, the *best* (shortest
  lead, analysis-like) value -- which leaks future information into a backtest.
  Empirically the analysis max and the true day-ahead forecast differ
  (e.g. 62.5F vs 63.2F for the same SFO day), so scoring against the analysis
  would flatter any model that consumed it.
* The Previous-Runs API exposes ``temperature_2m_previous_dayN`` -- for each
  valid hour, the value from the model run issued N days earlier. We reconstruct
  the daily max over the fixed-PST settlement window from those hours, giving a
  genuine ~24/48/72h-lead forecast known at trade time. That is the only honest
  input for an out-of-sample gate.

Design choices mirror ``forecast_backtest.py``: pure standard library (no numpy)
so the project test runner stays dependency-light, fixed-PST (``Etc/GMT+8``)
alignment so the daily max covers the same window Kalshi settles on, and
fail-soft per-model fetching so one model's gap never aborts the whole archive.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from cities import CITIES, CityConfig, get_city, parse_city_slugs

DEFAULT_CITY = get_city("sfo")

OPEN_METEO_PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"

# Fixed PST (POSIX sign is inverted: Etc/GMT+8 == UTC-8). SFO legacy constant;
# per-city code uses CityConfig.settlement_tz_name (each station's climate day
# runs midnight-to-midnight in ITS OWN standard time).
SETTLEMENT_TZ = "Etc/GMT+8"

# KSFO coordinates -- kept as the canonical SFO grid cell (moving it would
# invalidate every learned per-model EMOS bias). Per-city code reads
# CityConfig.latitude/longitude.
KSFO_LATITUDE = DEFAULT_CITY.latitude
KSFO_LONGITUDE = DEFAULT_CITY.longitude

# Models verified to return an SFO daily max via Open-Meteo on 2026-06-25.
# Ordered by expected value: the US MOS gold-standard (NBM) and ECMWF first, then
# GFS/ICON/GEM globals, the AI models, and the remaining globals. EMOS/QRF in
# Phase 1 learns each model's bias and weight, so a noisy member is harmless --
# it just gets down-weighted -- but a *missing* strong model is unrecoverable.
#
# gfs_graphcast025 (Google GraphCast) was removed 2026-07-10: the Open-Meteo
# previous-runs API publishes it with a ~7-week lag, so it had contributed no
# archive row since 2026-05-21 (a permanently absent member at fetch time).
# Its historical archive rows are kept; EMOS reweights over present members,
# so past fits are unaffected and future fits simply never see it.
NWP_MODELS: tuple[str, ...] = (
    "ncep_nbm_conus",       # NOAA National Blend of Models (statistical MOS blend)
    "ecmwf_ifs025",         # ECMWF IFS 0.25
    "gfs_seamless",         # NOAA GFS
    "icon_seamless",        # DWD ICON
    "gem_global",           # Environment Canada GEM
    "ecmwf_aifs025_single", # ECMWF AIFS (AI model)
    "jma_seamless",         # JMA
    "meteofrance_seamless", # Meteo-France ARPEGE/AROME
)

# Lead horizons to archive. Lead 1 is the day-ahead forecast the trade engine
# acts on; 2 and 3 give lead-stratified skill and let later phases see how fast
# each model's edge decays.
LEAD_DAYS: tuple[int, ...] = (1, 2, 3)

# Below this many models returning data, the --daily refresh warns of a partial
# collapse (a healthy run is 8/8 now that GraphCast is delisted; the floor
# leaves margin for a couple of transient model outages).
MIN_DAILY_MODELS = 6

DEFAULT_SOURCE = "openmeteo_previous_runs"
DB_PATH = Path(__file__).resolve().parent / "weather.db"

# Open-Meteo caps the date span per request; chunk long backfills well under it.
_MAX_CHUNK_DAYS = 300
_HTTP_TIMEOUT = 45.0


class NwpArchiveError(RuntimeError):
    pass


def _ssl_context() -> ssl.SSLContext:
    """Prefer certifi's CA bundle when present, else the system default.

    Production cron boxes verify fine with the system store; some local Python
    builds (Homebrew/pyenv on macOS) ship without one, so certifi is the
    belt-and-suspenders path. We never disable verification.
    """

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - certifi optional; fall back to system store
        return ssl.create_default_context()


_SSL_CONTEXT = _ssl_context()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _settlement_today(city: CityConfig = DEFAULT_CITY) -> date:
    """Today's settlement date in the city's fixed standard time (no DST).

    The archive is aligned to each station's own climate-day window, so
    cron/verify windows derive 'today' the same way -- UTC ``.date()`` would
    drift a day ahead overnight and silently desync from the settlement
    calendar.
    """

    return (
        datetime.now(timezone.utc)
        + timedelta(hours=city.standard_utc_offset_hours)
    ).date()


def _http_get_json(url: str, timeout: float = _HTTP_TIMEOUT) -> dict:
    try:
        with urlopen(url, timeout=timeout, context=_SSL_CONTEXT) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        body = ""
        try:
            body = json.loads(exc.read()).get("reason", "")
        except Exception:  # noqa: BLE001 - error body is best-effort context only
            pass
        raise NwpArchiveError(f"HTTP {exc.code} for {url}: {body}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise NwpArchiveError(f"request failed for {url}: {exc}") from exc


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nwp_model_forecasts (
            station_id TEXT NOT NULL DEFAULT 'KSFO',
            target_date TEXT NOT NULL,
            model TEXT NOT NULL,
            lead_days INTEGER NOT NULL,
            predicted_high_f REAL,
            fetched_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'openmeteo_previous_runs',
            PRIMARY KEY (station_id, target_date, model, lead_days, source)
        )
        """
    )
    _migrate_station_key(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nwp_station_target "
        "ON nwp_model_forecasts(station_id, target_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nwp_model_lead ON nwp_model_forecasts(model, lead_days)"
    )


def _migrate_station_key(conn: sqlite3.Connection) -> None:
    """Rebuild a pre-multi-city table (no station_id) in place, once.

    SQLite cannot extend a PRIMARY KEY with ALTER, so the legacy table is
    renamed, its rows re-inserted as KSFO (the only station it ever held), and
    the shell dropped. Idempotent: a station-keyed table passes straight through.
    """

    columns = {row[1] for row in conn.execute("PRAGMA table_info(nwp_model_forecasts)")}
    if "station_id" in columns:
        return
    conn.execute("ALTER TABLE nwp_model_forecasts RENAME TO nwp_model_forecasts_legacy")
    conn.execute(
        """
        CREATE TABLE nwp_model_forecasts (
            station_id TEXT NOT NULL DEFAULT 'KSFO',
            target_date TEXT NOT NULL,
            model TEXT NOT NULL,
            lead_days INTEGER NOT NULL,
            predicted_high_f REAL,
            fetched_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'openmeteo_previous_runs',
            PRIMARY KEY (station_id, target_date, model, lead_days, source)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO nwp_model_forecasts
            (station_id, target_date, model, lead_days, predicted_high_f, fetched_at, source)
        SELECT 'KSFO', target_date, model, lead_days, predicted_high_f, fetched_at, source
        FROM nwp_model_forecasts_legacy
        """
    )
    conn.execute("DROP TABLE nwp_model_forecasts_legacy")
    conn.commit()


def upsert_forecasts(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Idempotent upsert. A re-fetch overwrites the same (date, model, lead,
    source) row rather than duplicating, so backfills are safely re-runnable."""

    if not rows:
        return 0
    conn.executemany(
        """
        INSERT OR REPLACE INTO nwp_model_forecasts
            (station_id, target_date, model, lead_days, predicted_high_f, fetched_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return len(rows)


# --------------------------------------------------------------------------- #
# Fetch + reconstruct
# --------------------------------------------------------------------------- #

def previous_day_variable(lead_days: int) -> str:
    return f"temperature_2m_previous_day{lead_days}"


def reconstruct_daily_max(payload: dict, lead_days: int) -> dict[str, float]:
    """Daily max per local date from the leakage-free previous-day hourly series.

    We read *only* ``temperature_2m_previous_day{N}`` -- never the plain
    ``temperature_2m`` (which is the freshest/analysis run and would leak). Hours
    are grouped by the fixed-PST local date in the API's ``time`` strings, so the
    max covers the Kalshi settlement window.
    """

    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get(previous_day_variable(lead_days)) or []
    by_date: dict[str, float] = {}
    for stamp, value in zip(times, temps):
        if value is None:
            continue
        local_date = str(stamp)[:10]
        current = by_date.get(local_date)
        if current is None or value > current:
            by_date[local_date] = float(value)
    return by_date


def fetch_model_range(
    model: str,
    start: str,
    end: str,
    lead_days: int,
    *,
    city: CityConfig = DEFAULT_CITY,
    timeout: float = _HTTP_TIMEOUT,
) -> dict[str, float]:
    """Day-ahead (lead N) daily max per date for one model over [start, end]."""

    params = urlencode(
        {
            "latitude": f"{city.latitude:.4f}",
            "longitude": f"{city.longitude:.4f}",
            "hourly": previous_day_variable(lead_days),
            "temperature_unit": "fahrenheit",
            "timezone": city.settlement_tz_name,
            "start_date": start,
            "end_date": end,
            "models": model,
        }
    )
    payload = _http_get_json(f"{OPEN_METEO_PREVIOUS_RUNS_URL}?{params}", timeout)
    return reconstruct_daily_max(payload, lead_days)


def _date_chunks(start: str, end: str, span_days: int = _MAX_CHUNK_DAYS):
    cursor = date.fromisoformat(start)
    last = date.fromisoformat(end)
    if cursor > last:
        raise NwpArchiveError(f"start {start} is after end {end}")
    while cursor <= last:
        chunk_end = min(cursor + timedelta(days=span_days - 1), last)
        yield cursor.isoformat(), chunk_end.isoformat()
        cursor = chunk_end + timedelta(days=1)


def archive_range(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    city: CityConfig = DEFAULT_CITY,
    models: tuple[str, ...] = NWP_MODELS,
    leads: tuple[int, ...] = LEAD_DAYS,
    fetched_at: str | None = None,
    source: str = DEFAULT_SOURCE,
    verbose: bool = False,
) -> dict[str, int]:
    """Fetch + persist every (model, lead) over a date range.

    Returns a coverage summary keyed ``"model@leadN"`` -> rows written. One
    request per (model, lead) per <=300-day chunk, so cost scales with models
    and leads, not days. A failing model/chunk is logged and skipped -- never
    silently dropped -- so partial coverage is visible, not invisible.
    """

    ensure_schema(conn)
    stamp = fetched_at or _utcnow_iso()
    summary: dict[str, int] = {}
    for model in models:
        for lead in leads:
            written = 0
            for chunk_start, chunk_end in _date_chunks(start, end):
                try:
                    by_date = fetch_model_range(model, chunk_start, chunk_end, lead, city=city)
                except NwpArchiveError as exc:
                    if verbose:
                        print(f"  ! {model}@lead{lead} {chunk_start}..{chunk_end}: {exc}")
                    continue
                rows = [
                    (city.nws_station_id, target_date, model, lead, value, stamp, source)
                    for target_date, value in by_date.items()
                ]
                written += upsert_forecasts(conn, rows)
            # Commit per (model, lead) so a long backfill is resumable: an
            # interruption keeps every series persisted so far, and a re-run
            # resumes via the idempotent upsert rather than restarting from zero.
            conn.commit()
            summary[f"{model}@lead{lead}"] = written
            if verbose:
                print(f"  {model}@lead{lead}: {written} days")
    conn.commit()
    return summary


def coverage_report(conn: sqlite3.Connection) -> list[tuple]:
    """(model, lead_days, days, min_date, max_date) per archived series."""

    ensure_schema(conn)
    return conn.execute(
        """
        SELECT station_id, model, lead_days, COUNT(*) AS days,
               MIN(target_date) AS min_date, MAX(target_date) AS max_date
        FROM nwp_model_forecasts
        WHERE predicted_high_f IS NOT NULL
        GROUP BY station_id, model, lead_days
        ORDER BY station_id, lead_days, model
        """
    ).fetchall()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _connect(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path)


def _print_coverage(conn: sqlite3.Connection) -> None:
    """Coverage with interior-gap visibility: density % and missing-day count over
    each series' own [min,max] span, so a holey range cannot read as fully covered."""

    rows = coverage_report(conn)
    if not rows:
        print("  (archive empty)")
        return
    print(f"  {'station':8s} {'model':22s} {'lead':>4s} {'days':>6s} {'dens%':>6s} {'gaps':>5s}  range")
    for station, model, lead, days, lo, hi in rows:
        span = (date.fromisoformat(hi) - date.fromisoformat(lo)).days + 1
        density = 100.0 * days / span if span else 0.0
        gaps = max(0, span - days)
        flag = "  <-- interior holes" if gaps else ""
        print(
            f"  {station:8s} {model:22s} {lead:>4d} {days:>6d} {density:>6.1f} "
            f"{gaps:>5d}  {lo} -> {hi}{flag}"
        )


def _cmd_verify(conn: sqlite3.Connection, city: CityConfig) -> int:
    """Connectivity + schema smoke test: archive the last week at lead 1."""

    today = _settlement_today(city)
    start = (today - timedelta(days=8)).isoformat()
    end = (today - timedelta(days=1)).isoformat()
    print(f"verify[{city.slug}]: fetching {start}..{end} (lead 1) for {len(NWP_MODELS)} models")
    summary = archive_range(conn, start, end, city=city, leads=(1,), verbose=True)
    present = [name for name, n in summary.items() if n > 0]
    print(f"\nmodels returning data: {len(present)}/{len(NWP_MODELS)}")
    print("\narchive coverage:")
    _print_coverage(conn)
    if not present:
        print("\nFAIL: no model returned data -- check network/SSL or API params")
        return 1
    print("\nOK: archive table populated and reachable")
    return 0


def _cmd_backfill(conn: sqlite3.Connection, cities: tuple[CityConfig, ...], start: str, end: str) -> int:
    total = 0
    for city in cities:
        print(f"backfill[{city.slug}]: {start}..{end}  models={len(NWP_MODELS)} leads={LEAD_DAYS}")
        summary = archive_range(conn, start, end, city=city, verbose=True)
        total += sum(summary.values())
    print(f"\nwrote {total} forecast rows")
    print("\narchive coverage:")
    _print_coverage(conn)
    return 0 if total else 1


def _cmd_daily(conn: sqlite3.Connection, cities: tuple[CityConfig, ...], days: int) -> int:
    total = 0
    worst_models_present = len(NWP_MODELS)
    for city in cities:
        today = _settlement_today(city)
        start = (today - timedelta(days=days)).isoformat()
        end = (today + timedelta(days=1)).isoformat()
        summary = archive_range(
            conn, start, end, city=city, leads=(1, 2), verbose=False
        )
        city_total = sum(summary.values())
        total += city_total
        models_present = {name.split("@")[0] for name, n in summary.items() if n > 0}
        worst_models_present = min(worst_models_present, len(models_present))
        print(
            f"daily[{city.slug}]: {city_total} rows ({start}..{end}); "
            f"{len(models_present)}/{len(NWP_MODELS)} models returned data"
        )
    # Fail loud on a total miss so a cron wrapper's exit-code monitor catches a
    # stale archive instead of reading silence as success.
    if total == 0:
        print("FAIL: no rows fetched -- archive going stale (network/SSL/API change?)")
        return 1
    if worst_models_present < MIN_DAILY_MODELS:
        print(
            f"WARN: a city had only {worst_models_present} models return data "
            f"(floor {MIN_DAILY_MODELS}) -- partial collapse, investigate"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH), help="weather.db path")
    parser.add_argument("--verify", action="store_true", help="connectivity + schema smoke test")
    parser.add_argument("--backfill", action="store_true", help="archive a historical range")
    parser.add_argument("--daily", action="store_true", help="archive recent days (cron mode)")
    parser.add_argument("--start", help="backfill start date YYYY-MM-DD")
    parser.add_argument("--end", help="backfill end date YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=5, help="--daily lookback window")
    parser.add_argument("--cities", default="all", help="'all' or comma slugs (e.g. sfo,nyc)")
    args = parser.parse_args(argv)

    cities = parse_city_slugs(args.cities)
    with _connect(args.db) as conn:
        if args.verify:
            return _cmd_verify(conn, cities[0])
        if args.backfill:
            if not (args.start and args.end):
                parser.error("--backfill requires --start and --end")
            return _cmd_backfill(conn, cities, args.start, args.end)
        if args.daily:
            return _cmd_daily(conn, cities, args.days)
        parser.error("choose one of --verify, --backfill, or --daily")


if __name__ == "__main__":
    raise SystemExit(main())
