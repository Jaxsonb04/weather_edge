#!/usr/bin/env python3
"""One-shot cutover helper: seed the permanent Google usage ledger with the
legacy JSON usage file's current-month event count (T8-4, Task 6 review).

The pre-Task-6 SFO-only refresh tracked its own monthly count in
``.google_weather_usage.json`` -- an entirely separate accounting mechanism
from the permanent ``GoogleUsageLedger`` this project's Task 2 introduced.
The systemd cutover (Task 8) stops invoking any code path that updates that
legacy file, so its count freezes at whatever it last recorded for the
Pacific month the cutover happens in -- but the permanent ledger starts that
same month at zero. Run this once, right after the systemd cutover, so
combined real Google Weather usage for the cutover month cannot exceed the
account's actual 8,000-event monthly cap.

Deliberately reads the legacy file's OWN recorded ``month``/``monthly_events``
fields directly rather than going through ``google_api.load_usage``, which
resets ``monthly_events`` to zero on any month rollover relative to the
current wall clock -- exactly the normalization this script must NOT apply,
since the legacy file may already be stale (frozen) by the time an operator
runs this.

Safe to run twice: ``GoogleUsageLedger.seed_legacy_month_carryover`` is
idempotent for a fixed ``(billing_month, event_count)`` pair, and only ever
tops up (never removes) rows if a later run reports a larger event_count.

Usage (from the forecaster venv, on the box):

    .venv/bin/python seed_google_usage_ledger.py
"""

from __future__ import annotations

from google_api import read_json
from google_weather_store import GoogleUsageLedger
from weather_cache_config import DB_PATH, USAGE_PATH


def main() -> int:
    legacy = read_json(USAGE_PATH, {})
    month = str(legacy.get("month") or "").strip()
    if not month:
        print(f"no legacy usage recorded at {USAGE_PATH}; nothing to seed")
        return 0
    event_count = int(legacy.get("monthly_events", 0) or 0)

    ledger = GoogleUsageLedger(DB_PATH)
    inserted = ledger.seed_legacy_month_carryover(
        billing_month=month, event_count=event_count
    )
    print(
        f"seeded {inserted} new legacy-carryover row(s) for {month} "
        f"(legacy file recorded {event_count} total for that month; the "
        "permanent ledger now reflects that count toward its monthly cap)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
