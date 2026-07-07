"""City registry: every Kalshi daily-high market WeatherEdge forecasts and trades.

One frozen record per city ties together the four identities that must never
drift apart, because settlement correctness depends on all of them agreeing:

* the Kalshi series ticker (which market we trade),
* the NWS settlement station (which thermometer decides the outcome),
* the NWS CLI product (site + issuedby) that publishes that station's official
  daily maximum (the settlement source quoted in the market rules), and
* the station's fixed *standard-time* offset, because the NWS climate day --
  and therefore the settlement window -- runs midnight-to-midnight local
  standard time year round, not civil (DST) time.

Station identities were verified against the live market rules text, the
series ``settlement_sources`` URL, and the actual CLI product header on
2026-07-06. The traps are real: Houston settles on Hobby (KHOU) not
Intercontinental, Dallas on DFW not Love Field, Chicago on Midway not O'Hare,
and New York on Central Park (KNYC) not any airport.

This file is deliberately duplicated as ``forecaster/cities.py`` and
``trading/sfo_kalshi_quant/cities.py`` (the two packages do not import each
other, same as ``settlement_calendar``); ``test_cities_parity.py`` keeps the
copies identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, timezone


@dataclass(frozen=True)
class CityConfig:
    slug: str
    name: str
    series_ticker: str
    # ICAO id of the settlement station -- the station whose NWS Climatological
    # Report (Daily) maximum resolves the market. Also the station used for
    # live observations and forecast verification.
    nws_station_id: str
    # forecast.weather.gov CLI product coordinates: the issuing WFO (``site``)
    # and the product's ``issuedby`` code.
    cli_site: str
    cli_issuedby: str
    latitude: float
    longitude: float
    civil_tz_name: str
    # Fixed standard-time UTC offset (negative west). The NWS climate day is
    # midnight-to-midnight LOCAL STANDARD TIME year round; using civil (DST)
    # time here would misalign the settlement window by an hour all summer.
    standard_utc_offset_hours: int
    # True only for SFO: the legacy Google/NWS/Open-Meteo point-blend stack
    # (plus LSTM residual calibration and marine-layer features) exists for it.
    # Every other city runs the station-agnostic NWP -> EMOS -> CLI path.
    has_full_blend: bool = False
    # True only where per-cohort calibration evidence exists to justify the
    # profile-level blocked_forecast_cohorts gate (SFO's warm/hot history).
    # Absolute-F cohorts are climate-relative: a HOT block that is a sane guard
    # in San Francisco would simply turn Phoenix off.
    apply_cohort_blocks: bool = False

    @property
    def settlement_tz_name(self) -> str:
        """IANA fixed-offset zone for the climate day (POSIX sign inverted)."""

        return f"Etc/GMT+{-self.standard_utc_offset_hours}"

    def fixed_standard_timezone(self) -> timezone:
        return timezone(timedelta(hours=self.standard_utc_offset_hours))

    @property
    def cli_product_url(self) -> str:
        return (
            "https://forecast.weather.gov/product.php"
            f"?site={self.cli_site}&product=CLI&issuedby={self.cli_issuedby}&format=txt"
        )


# Ordered by 24h volume on 2026-07-06 (Miami ~1.34M contracts ... Denver ~33K).
# All 15 settle on a CLI daily maximum, list 6 bins at 2F spacing, and close at
# local midnight.
CITIES: tuple[CityConfig, ...] = (
    CityConfig(
        slug="mia", name="Miami", series_ticker="KXHIGHMIA",
        nws_station_id="KMIA", cli_site="MFL", cli_issuedby="MIA",
        latitude=25.7906, longitude=-80.3164,
        civil_tz_name="America/New_York", standard_utc_offset_hours=-5,
    ),
    CityConfig(
        slug="lax", name="Los Angeles", series_ticker="KXHIGHLAX",
        nws_station_id="KLAX", cli_site="LOX", cli_issuedby="LAX",
        latitude=33.9381, longitude=-118.3889,
        civil_tz_name="America/Los_Angeles", standard_utc_offset_hours=-8,
    ),
    CityConfig(
        slug="chi", name="Chicago", series_ticker="KXHIGHCHI",
        nws_station_id="KMDW", cli_site="LOT", cli_issuedby="MDW",
        latitude=41.7842, longitude=-87.7553,
        civil_tz_name="America/Chicago", standard_utc_offset_hours=-6,
    ),
    CityConfig(
        slug="atl", name="Atlanta", series_ticker="KXHIGHTATL",
        nws_station_id="KATL", cli_site="FFC", cli_issuedby="ATL",
        latitude=33.6403, longitude=-84.4269,
        civil_tz_name="America/New_York", standard_utc_offset_hours=-5,
    ),
    CityConfig(
        slug="nyc", name="New York", series_ticker="KXHIGHNY",
        nws_station_id="KNYC", cli_site="OKX", cli_issuedby="NYC",
        latitude=40.7833, longitude=-73.9667,
        civil_tz_name="America/New_York", standard_utc_offset_hours=-5,
    ),
    CityConfig(
        slug="dal", name="Dallas", series_ticker="KXHIGHTDAL",
        nws_station_id="KDFW", cli_site="FWD", cli_issuedby="DFW",
        latitude=32.8974, longitude=-97.0220,
        civil_tz_name="America/Chicago", standard_utc_offset_hours=-6,
    ),
    CityConfig(
        slug="sea", name="Seattle", series_ticker="KXHIGHTSEA",
        nws_station_id="KSEA", cli_site="SEW", cli_issuedby="SEA",
        latitude=47.4447, longitude=-122.3136,
        civil_tz_name="America/Los_Angeles", standard_utc_offset_hours=-8,
    ),
    CityConfig(
        slug="phl", name="Philadelphia", series_ticker="KXHIGHPHIL",
        nws_station_id="KPHL", cli_site="PHI", cli_issuedby="PHL",
        latitude=39.8733, longitude=-75.2268,
        civil_tz_name="America/New_York", standard_utc_offset_hours=-5,
    ),
    CityConfig(
        slug="phx", name="Phoenix", series_ticker="KXHIGHTPHX",
        nws_station_id="KPHX", cli_site="PSR", cli_issuedby="PHX",
        latitude=33.4278, longitude=-112.0035,
        # Arizona observes no DST: civil time IS standard time year round.
        civil_tz_name="America/Phoenix", standard_utc_offset_hours=-7,
    ),
    CityConfig(
        slug="aus", name="Austin", series_ticker="KXHIGHAUS",
        nws_station_id="KAUS", cli_site="EWX", cli_issuedby="AUS",
        latitude=30.1830, longitude=-97.6799,
        civil_tz_name="America/Chicago", standard_utc_offset_hours=-6,
    ),
    CityConfig(
        slug="sfo", name="San Francisco", series_ticker="KXHIGHTSFO",
        nws_station_id="KSFO", cli_site="MTR", cli_issuedby="SFO",
        # Kept at the exact coordinates the NWP archive has always fetched
        # (nwp_archive.py used 37.62/-122.38 from day one). Changing them would
        # move the Open-Meteo grid cell and silently invalidate every learned
        # per-model EMOS bias.
        latitude=37.62, longitude=-122.38,
        civil_tz_name="America/Los_Angeles", standard_utc_offset_hours=-8,
        has_full_blend=True, apply_cohort_blocks=True,
    ),
    CityConfig(
        slug="hou", name="Houston", series_ticker="KXHIGHTHOU",
        # Hobby, NOT Bush Intercontinental: the settlement URL is issuedby=HOU
        # and the live CLI header reads "HOUSTON/HOBBY AIRPORT CLIMATE SUMMARY".
        nws_station_id="KHOU", cli_site="HGX", cli_issuedby="HOU",
        latitude=29.6375, longitude=-95.2825,
        civil_tz_name="America/Chicago", standard_utc_offset_hours=-6,
    ),
    CityConfig(
        slug="okc", name="Oklahoma City", series_ticker="KXHIGHTOKC",
        nws_station_id="KOKC", cli_site="OUN", cli_issuedby="OKC",
        latitude=35.3886, longitude=-97.6003,
        civil_tz_name="America/Chicago", standard_utc_offset_hours=-6,
    ),
    CityConfig(
        slug="bos", name="Boston", series_ticker="KXHIGHTBOS",
        nws_station_id="KBOS", cli_site="BOX", cli_issuedby="BOS",
        latitude=42.3606, longitude=-71.0106,
        civil_tz_name="America/New_York", standard_utc_offset_hours=-5,
    ),
    CityConfig(
        slug="den", name="Denver", series_ticker="KXHIGHDEN",
        # Denver International, ~25 km NE of downtown and regularly several
        # degrees different from it.
        nws_station_id="KDEN", cli_site="BOU", cli_issuedby="DEN",
        latitude=39.8466, longitude=-104.6562,
        civil_tz_name="America/Denver", standard_utc_offset_hours=-7,
    ),
)

DEFAULT_CITY_SLUG = "sfo"

CITY_BY_SLUG: dict[str, CityConfig] = {city.slug: city for city in CITIES}
CITY_BY_STATION: dict[str, CityConfig] = {city.nws_station_id: city for city in CITIES}
CITY_BY_SERIES: dict[str, CityConfig] = {city.series_ticker: city for city in CITIES}


def get_city(slug: str) -> CityConfig:
    normalized = (slug or "").strip().lower()
    if normalized in CITY_BY_SLUG:
        return CITY_BY_SLUG[normalized]
    raise KeyError(
        f"unknown city {slug!r}; expected one of {', '.join(sorted(CITY_BY_SLUG))}"
    )


def city_for_station(station_id: str) -> CityConfig:
    normalized = (station_id or "").strip().upper()
    if normalized in CITY_BY_STATION:
        return CITY_BY_STATION[normalized]
    raise KeyError(f"no city configured for station {station_id!r}")


def city_for_series(series_ticker: str) -> CityConfig:
    normalized = (series_ticker or "").strip().upper()
    if normalized in CITY_BY_SERIES:
        return CITY_BY_SERIES[normalized]
    raise KeyError(f"no city configured for series {series_ticker!r}")


def city_for_market_ticker(ticker: str) -> CityConfig | None:
    """Resolve a market/event ticker (``KXHIGHNY-26JUL06-B79.5``) to its city.

    Longest-prefix match so ``KXHIGHTSFO`` can never be shadowed by a shorter
    hypothetical ``KXHIGHTS``. Returns None for unknown series rather than
    raising: historical journals may hold retired tickers.
    """

    normalized = (ticker or "").strip().upper()
    best: CityConfig | None = None
    for city in CITIES:
        prefix = city.series_ticker + "-"
        if normalized == city.series_ticker or normalized.startswith(prefix):
            if best is None or len(city.series_ticker) > len(best.series_ticker):
                best = city
    return best


def parse_city_slugs(raw: str | None) -> tuple[CityConfig, ...]:
    """Parse a CLI/env city list: 'all', a comma list of slugs, or empty=all."""

    value = (raw or "all").strip().lower()
    if value in ("", "all", "*"):
        return CITIES
    return tuple(get_city(part.strip()) for part in value.split(",") if part.strip())
