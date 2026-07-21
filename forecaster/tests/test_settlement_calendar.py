"""Multi-city fixed-standard settlement-day bucketing tests.

Task 4 is the first caller that buckets hourly Google Weather readings by an
arbitrary city's fixed standard-time offset (never its civil/DST timezone,
never a Pacific default). These tests pin down
``settlement_calendar.local_standard_date`` across every configured city and
across a real DST transition so a future regression cannot silently
reintroduce a civil-time bucketing bug into the Google runtime fetch path.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cities import CITIES, get_city
from settlement_calendar import local_standard_date


@pytest.mark.parametrize("city", CITIES, ids=lambda city: city.slug)
def test_every_city_buckets_by_its_own_fixed_standard_offset(city):
    # Independently derive the expected local date via raw UTC-offset
    # arithmetic (not by re-calling astimezone with the same tz object), so
    # this genuinely cross-checks local_standard_date's behavior.
    instant = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)
    expected = (instant + timedelta(hours=city.standard_utc_offset_hours)).date()

    assert local_standard_date(instant, city.fixed_standard_timezone()) == expected


def test_nyc_fixed_standard_bucketing_ignores_the_spring_dst_skip():
    """2026-03-08 07:00 UTC is when America/New_York civil time jumps 2am -> 3am.

    The fixed -5 offset must show a smooth 00:00..04:00 progression with no
    skip; a civil-zone bucketing would show a gap (there is no "2am" that
    day) and would disagree with the fixed offset about the calendar hour.
    """

    nyc = get_city("nyc")
    fixed_tz = nyc.fixed_standard_timezone()
    transition = datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)

    fixed_moments = [
        (transition + timedelta(hours=offset)).astimezone(fixed_tz)
        for offset in (-2, -1, 0, 1, 2)
    ]

    assert [moment.hour for moment in fixed_moments] == [0, 1, 2, 3, 4]
    assert {moment.date() for moment in fixed_moments} == {date(2026, 3, 8)}
    assert all(moment.utcoffset() == timedelta(hours=-5) for moment in fixed_moments)

    civil_tz = ZoneInfo(nyc.civil_tz_name)
    civil_hours = [
        (transition + timedelta(hours=offset)).astimezone(civil_tz).hour
        for offset in (-2, -1, 0, 1, 2)
    ]
    assert civil_hours != [0, 1, 2, 3, 4], "civil time should skip 2am on the transition"


def test_fixed_standard_settlement_day_can_differ_from_the_civil_day():
    """During DST, a fixed-standard local date can fall a day behind civil time.

    2026-06-15T04:30:00Z is 2026-06-14 23:30 on the fixed -5 offset but
    2026-06-15 00:30 on civil America/New_York (EDT, -4). Bucketing this
    instant with the civil timezone would silently attribute it to the wrong
    settlement day.
    """

    nyc = get_city("nyc")
    instant = datetime(2026, 6, 15, 4, 30, tzinfo=timezone.utc)

    fixed_date = local_standard_date(instant, nyc.fixed_standard_timezone())
    civil_date = instant.astimezone(ZoneInfo(nyc.civil_tz_name)).date()

    assert fixed_date == date(2026, 6, 14)
    assert civil_date == date(2026, 6, 15)
    assert fixed_date != civil_date


def test_phoenix_has_no_dst_so_fixed_and_civil_agree_year_round():
    """Arizona observes no DST: civil IS standard time, a useful control case."""

    phx = get_city("phx")
    summer = datetime(2026, 7, 15, 6, 0, tzinfo=timezone.utc)
    winter = datetime(2026, 1, 15, 6, 0, tzinfo=timezone.utc)

    for instant in (summer, winter):
        fixed_date = local_standard_date(instant, phx.fixed_standard_timezone())
        civil_date = instant.astimezone(ZoneInfo(phx.civil_tz_name)).date()
        assert fixed_date == civil_date
