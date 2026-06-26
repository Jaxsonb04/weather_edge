"""One-time historical backfill of the multi-model NWP forecast archive.

Thin wrapper over ``nwp_archive.archive_range`` mirroring
``backfill_clisfo_from_ghcn.py``: additive and reversible (rows are tagged
``source`` and upserted by primary key, so a re-run overwrites in place and never
duplicates), with a ``--dry-run`` that prints the plan before any network call.

The default window starts at GFS's Open-Meteo archive origin (2021-03-24);
models with shorter histories simply contribute fewer rows -- the per-model
coverage summary makes any gap visible rather than silent.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone

from nwp_archive import (
    DB_PATH,
    LEAD_DAYS,
    NWP_MODELS,
    _print_coverage,
    archive_range,
)

DEFAULT_START = "2021-03-24"  # GFS Open-Meteo archive origin


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--start", default=DEFAULT_START, help="YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, fetch nothing")
    args = parser.parse_args(argv)

    end = args.end or (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    requests = len(NWP_MODELS) * len(LEAD_DAYS)
    print(f"NWP backfill plan: {args.start} -> {end}")
    print(f"  models={len(NWP_MODELS)}  leads={LEAD_DAYS}  (~{requests} API requests per 300-day chunk)")
    if args.dry_run:
        print("  dry-run: no network calls made")
        return 0

    with sqlite3.connect(args.db) as conn:
        summary = archive_range(conn, args.start, end, verbose=True)
        print(f"\nwrote {sum(summary.values())} forecast rows")
        _print_coverage(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
