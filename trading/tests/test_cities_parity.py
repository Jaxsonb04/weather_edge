"""The two cities.py copies (forecaster + trading) must stay byte-identical.

The packages deliberately do not import each other (same convention as
settlement_calendar), so the registry is duplicated; this test is the lock
that keeps the copies from drifting.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_cities_files_are_identical():
    forecaster_copy = (ROOT / "forecaster" / "cities.py").read_text()
    trading_copy = (ROOT / "trading" / "sfo_kalshi_quant" / "cities.py").read_text()
    assert forecaster_copy == trading_copy


def test_registry_shape_and_settlement_identities():
    import sys

    sys.path.insert(0, str(ROOT / "forecaster"))
    try:
        import cities
    finally:
        sys.path.pop(0)

    assert len(cities.CITIES) == 15
    slugs = [c.slug for c in cities.CITIES]
    assert len(set(slugs)) == 15
    assert cities.DEFAULT_CITY_SLUG == "sfo"

    sfo = cities.get_city("sfo")
    assert sfo.series_ticker == "KXHIGHTSFO"
    assert sfo.nws_station_id == "KSFO"
    # The NWP archive grid cell must never move for SFO.
    assert (sfo.latitude, sfo.longitude) == (37.62, -122.38)
    assert sfo.has_full_blend and sfo.apply_cohort_blocks
    assert sfo.settlement_tz_name == "Etc/GMT+8"
    assert [c for c in cities.CITIES if c.has_full_blend] == [sfo]

    # Settlement traps verified against live CLI product headers on 2026-07-06.
    assert cities.get_city("hou").nws_station_id == "KHOU"  # Hobby, not KIAH
    assert cities.get_city("dal").nws_station_id == "KDFW"  # DFW, not Love
    assert cities.get_city("chi").nws_station_id == "KMDW"  # Midway, not O'Hare
    assert cities.get_city("nyc").nws_station_id == "KNYC"  # Central Park
    assert cities.get_city("phx").civil_tz_name == "America/Phoenix"  # no DST

    # Ticker resolution must survive the KXHIGH/KXHIGHT prefix mix.
    assert cities.city_for_market_ticker("KXHIGHTSFO-26JUL08-B67.5").slug == "sfo"
    assert cities.city_for_market_ticker("KXHIGHNY-26JUL08-T52").slug == "nyc"
    assert cities.city_for_market_ticker("KXUNKNOWN-26JUL08") is None

    assert len(cities.parse_city_slugs("all")) == 15
    assert [c.slug for c in cities.parse_city_slugs("sfo,nyc")] == ["sfo", "nyc"]
