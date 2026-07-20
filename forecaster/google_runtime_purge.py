#!/usr/bin/env python3
"""Standalone periodic purge of expired Google Weather runtime content.

Every producer/consumer of ``GoogleRuntimeStore`` already runs
``purge_expired`` as part of its own cycle (spec section 7.2, and see
``google_multicity_refresh.refresh_all_cities``'s startup purge). This script
exists as an independent safety net for the case that concerns Task 8 item 1:
if Google Weather is unavailable, the API key is missing, or the monthly/
daily budget is exhausted for an extended stretch, no refresh cycle runs at
all, and expired rows would otherwise sit in ``/run/weatheredge`` until the
next successful fetch -- silently violating the "expired data is unavailable
at the query boundary" invariant only in spirit (reads already filter
``expires_at > now``, so a stale row is never SERVED), but not in the
"physically deleted at its expiry" sense the plan calls for.

systemd invokes this on a short, fixed cadence
(weatheredge-google-runtime-purge.timer) rather than sleeping until the next
computed expiry: every operational unit in this deploy is a periodic
oneshot+timer pair, and a long-running "sleep until next_expiry() then loop"
daemon would be the only exception. The chosen interval is well under the
shortest enforced TTL (one hour, for hourly/current rows -- see
weather_cache_config.GOOGLE_HOURLY_TTL/GOOGLE_CURRENT_TTL), so staleness is
bounded tightly even though this is polling rather than exact scheduling.

NOTE on reboots: /run is tmpfs. On every reboot, systemd-tmpfiles recreates
/run/weatheredge empty (see weatheredge-tmpfiles.conf) -- the runtime
database, and every scope-keyed generation watermark in it, is gone. That is
expected: all TTLs are bounded (<= 30 days) and every reader performs its own
startup purge before trusting the store, but it means this purge script may
find nothing to do immediately after a reboot, and any in-flight
"corroboration age" research signal resets to zero at that point too.
"""

from __future__ import annotations

from datetime import datetime, timezone

from google_weather_store import GoogleRuntimeStore
from weather_cache_config import GOOGLE_RUNTIME_DB_PATH, GOOGLE_RUNTIME_PRODUCTION


def main() -> int:
    store = GoogleRuntimeStore(GOOGLE_RUNTIME_DB_PATH, production=GOOGLE_RUNTIME_PRODUCTION)
    purged = store.purge_expired(now=datetime.now(timezone.utc))
    print(f"purged {purged} expired Google runtime row(s) from {GOOGLE_RUNTIME_DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
