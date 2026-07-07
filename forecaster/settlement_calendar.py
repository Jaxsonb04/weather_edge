from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math


PACIFIC_STANDARD_TZ = timezone(timedelta(hours=-8), "PST")


def standard_timezone(standard_utc_offset_hours: int) -> timezone:
    """Fixed standard-time zone for a station (the NWS climate day ignores DST)."""

    return timezone(timedelta(hours=standard_utc_offset_hours))


def local_standard_date(
    timestamp: datetime, tz: timezone = PACIFIC_STANDARD_TZ
) -> date:
    """Return the NWS/Kalshi report date for an observation timestamp.

    Defaults to Pacific standard time (the original SFO behavior); pass a
    station's fixed standard zone for other cities.
    """

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(tz).date()


def today_local_standard(
    now: datetime | None = None, tz: timezone = PACIFIC_STANDARD_TZ
) -> date:
    now = now or datetime.now(timezone.utc)
    return local_standard_date(now, tz)


def utc_window_for_local_standard_date(
    local_date: str | date, tz: timezone = PACIFIC_STANDARD_TZ
) -> tuple[datetime, datetime]:
    if isinstance(local_date, str):
        day = date.fromisoformat(local_date)
    else:
        day = local_date
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def integer_settlement_high_f(value: object) -> float | None:
    if value is None:
        return None
    high = float(value)
    if not math.isfinite(high):
        return None
    return float(math.floor(high + 0.5))
