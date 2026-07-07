"""Station-keyed CLI settlement truth for every configured city.

The ``cli_settlements`` table is the single settlement ground-truth store:
one row per (station, climate day) holding the MAXIMUM from that station's NWS
Climatological Report (Daily) -- the exact number the Kalshi market resolves
on. Two writers keep it current:

* ``refresh_live`` scans the last ~10 versions of each city's live CLI product
  on forecast.weather.gov (newest first, so a corrected final report shadows
  the preliminary evening one), and
* ``backfill_iem`` pulls the historical CLI archive from the Iowa
  Environmental Mesonet (``/json/cli.py``), which republishes the same NWS CLI
  products -- so backfilled truth and live truth are the same instrument.

The table replaces the SFO-only ``clisfo_settlements``; ``ensure_schema``
migrates legacy rows (as station KSFO) exactly once and drops the old table so
there is one source of truth, not two drifting ones.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from cities import CITIES, CityConfig, parse_city_slugs
from clisfo import fetch_recent_cli_settlements
from settlement_calendar import integer_settlement_high_f

DB_PATH = Path(__file__).resolve().parent / "weather.db"
IEM_CLI_URL = "https://mesonet.agron.iastate.edu/json/cli.py?station={station}&year={year}"
_USER_AGENT = "weatheredge-forecaster/0.2 (student research project)"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cli_settlements (
            station_id TEXT NOT NULL,
            local_date TEXT NOT NULL,
            max_temperature_f INTEGER,
            fetched_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'nws_cli',
            PRIMARY KEY (station_id, local_date)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cli_settlements_date ON cli_settlements(local_date)"
    )
    # One-time migration: fold the SFO-only legacy table in, then drop it so
    # scoring can never read a stale fork of the truth.
    legacy = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='clisfo_settlements'"
    ).fetchone()
    if legacy is not None:
        conn.execute(
            """
            INSERT OR IGNORE INTO cli_settlements
                (station_id, local_date, max_temperature_f, fetched_at, source)
            SELECT 'KSFO', local_date, max_temperature_f, fetched_at, 'nws_cli'
            FROM clisfo_settlements
            """
        )
        conn.execute("DROP TABLE clisfo_settlements")
        conn.commit()


def upsert_settlement(
    conn: sqlite3.Connection,
    station_id: str,
    local_date: str,
    max_temperature_f: int,
    *,
    source: str = "nws_cli",
    fetched_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO cli_settlements
            (station_id, local_date, max_temperature_f, fetched_at, source)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(station_id, local_date) DO UPDATE SET
            max_temperature_f = excluded.max_temperature_f,
            fetched_at = excluded.fetched_at,
            source = excluded.source
        """,
        (
            station_id,
            local_date,
            int(max_temperature_f),
            fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source,
        ),
    )


def refresh_live(
    conn: sqlite3.Connection,
    cities: tuple[CityConfig, ...] = CITIES,
    *,
    versions: int = 6,
    timeout: int = 20,
) -> dict[str, int]:
    """Fetch recent live CLI reports for each city and upsert. Fail-soft per
    city: one WFO outage must not stall truth for the other fourteen."""

    ensure_schema(conn)
    stored: dict[str, int] = {}
    for city in cities:
        try:
            settlements = fetch_recent_cli_settlements(
                city.cli_site, city.cli_issuedby, versions=versions, timeout=timeout
            )
        except Exception:  # noqa: BLE001 - network truth refresh is best-effort
            stored[city.nws_station_id] = 0
            continue
        for report_date, high in settlements.items():
            upsert_settlement(conn, city.nws_station_id, report_date.isoformat(), high)
        stored[city.nws_station_id] = len(settlements)
    conn.commit()
    return stored


def _iem_rows(station_id: str, year: int, *, timeout: int = 45) -> list[dict]:
    url = IEM_CLI_URL.format(station=station_id, year=year)
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload.get("results")
    return rows if isinstance(rows, list) else []


def backfill_iem(
    conn: sqlite3.Connection,
    city: CityConfig,
    *,
    start_year: int,
    end_year: int | None = None,
) -> int:
    """Backfill historical CLI maxima for one city from the IEM archive.

    IEM may hold several CLI versions per day; later list entries are later
    products, so the last write per day wins -- matching the live rule that the
    corrected final report shadows the preliminary one.
    """

    ensure_schema(conn)
    end = end_year or date.today().year
    written = 0
    for year in range(start_year, end + 1):
        try:
            rows = _iem_rows(city.nws_station_id, year)
        except Exception as exc:  # noqa: BLE001 - report and continue per year
            print(f"  ! {city.nws_station_id} {year}: {exc}")
            continue
        for row in rows:
            high = row.get("high")
            valid = row.get("valid")
            if high is None or not valid:
                continue
            try:
                high_int = int(high)
            except (TypeError, ValueError):
                continue
            upsert_settlement(
                conn, city.nws_station_id, str(valid)[:10], high_int, source="iem_cli"
            )
            written += 1
    conn.commit()
    return written


def load_cli_truth(conn: sqlite3.Connection, station_id: str) -> dict[str, float]:
    """local_date -> integer settlement high (F) for one station."""

    ensure_schema(conn)
    truth: dict[str, float] = {}
    for local_date, max_t in conn.execute(
        "SELECT local_date, max_temperature_f FROM cli_settlements "
        "WHERE station_id = ? AND max_temperature_f IS NOT NULL",
        (station_id,),
    ):
        value = integer_settlement_high_f(max_t)
        if value is not None:
            truth[local_date] = float(value)
    return truth


def cli_high_for(conn: sqlite3.Connection, station_id: str, local_date: str) -> float | None:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT max_temperature_f FROM cli_settlements "
        "WHERE station_id = ? AND local_date = ? AND max_temperature_f IS NOT NULL",
        (station_id, local_date),
    ).fetchone()
    return integer_settlement_high_f(row[0]) if row else None


def coverage(conn: sqlite3.Connection) -> list[tuple]:
    ensure_schema(conn)
    return conn.execute(
        """
        SELECT station_id, COUNT(*), MIN(local_date), MAX(local_date)
        FROM cli_settlements WHERE max_temperature_f IS NOT NULL
        GROUP BY station_id ORDER BY station_id
        """
    ).fetchall()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--cities", default="all", help="'all' or comma slugs (e.g. sfo,nyc)")
    parser.add_argument("--refresh", action="store_true", help="fetch recent live CLI reports")
    parser.add_argument("--backfill-iem", action="store_true", help="backfill from the IEM CLI archive")
    parser.add_argument("--start-year", type=int, default=date.today().year - 2)
    parser.add_argument("--coverage", action="store_true")
    args = parser.parse_args(argv)
    if not (args.refresh or args.backfill_iem or args.coverage):
        parser.error("nothing to do; pass --refresh, --backfill-iem, or --coverage")

    cities = parse_city_slugs(args.cities)
    with sqlite3.connect(args.db) as conn:
        if args.backfill_iem:
            for city in cities:
                written = backfill_iem(conn, city, start_year=args.start_year)
                print(f"{city.slug}: backfilled {written} CLI days from IEM")
        if args.refresh:
            stored = refresh_live(conn, cities)
            summary = ", ".join(f"{station}={n}" for station, n in stored.items())
            print(f"live CLI refresh: {summary}")
        if args.coverage or args.backfill_iem:
            print("cli_settlements coverage:")
            for station, days, lo, hi in coverage(conn):
                print(f"  {station}: {days} days {lo} -> {hi}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
