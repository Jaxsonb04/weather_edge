"""Single source of truth for the Kalshi/NWS settlement day in trading code.

The NWS daily climate report (and therefore Kalshi settlement) buckets a day
by the station's local *standard* time year-round, so trading must define
"today/tomorrow/rolling" with fixed UTC-8, not civil America/Los_Angeles
dates. During DST the two disagree between 23:00 and 24:00 PST (00:00-01:00
PDT); civil dates would target, gate, freshness-check, and auto-settle the
wrong Kalshi day in that window. This mirrors
forecaster/settlement_calendar.py, which the trading package cannot import.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .cities import CityConfig
from .config import SFO_TZ

PACIFIC_STANDARD_TZ = timezone(timedelta(hours=-8), "PST")

# IANA spelling of fixed UTC-8 (POSIX sign convention), for APIs that take a
# timezone name and aggregate daily values over it (Open-Meteo, IEM).
IANA_FIXED_PST = "Etc/GMT+8"


def _station_standard_tz(city: CityConfig | None) -> timezone:
    if city is None:
        return PACIFIC_STANDARD_TZ
    return city.fixed_standard_timezone()


def settlement_clock(now: datetime | None = None, city: CityConfig | None = None) -> datetime:
    """Return the current moment on a station's fixed-standard settlement clock.

    Defaults to the fixed-PST SFO clock. Naive inputs are assumed to be civil
    wall-clock time at the station, the historical convention for the ``now``
    test hooks.
    """

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        civil = ZoneInfo(city.civil_tz_name) if city is not None else SFO_TZ
        moment = moment.replace(tzinfo=civil)
    return moment.astimezone(_station_standard_tz(city))


def settlement_today(now: datetime | None = None, city: CityConfig | None = None) -> date:
    """Return the settlement day currently being measured at the station."""

    return settlement_clock(now, city).date()
