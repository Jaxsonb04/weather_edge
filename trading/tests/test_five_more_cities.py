"""The five daily-HIGH series added 2026-09-13: LV, MIN, SATX, NOLA, DC.

Registry identities were verified live on 2026-09-13 against the Kalshi
public API (the ``CLIxxx`` settlement token in each series' ``rules_primary``),
the NWS CLI product header for each issuing office, and the api.weather.gov
station record (ASOS coordinates). The scan-level test locks the fail-closed
behaviour the expansion depends on: until the post-deploy EMOS backfill has
produced scored rows for a station, the scanner must skip that city rather
than trade it on an uncalibrated forecast.
"""

from __future__ import annotations

import io
import sqlite3
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from sfo_kalshi_quant._cli.scan import ScanCommandDependencies, cmd_portfolio_scan
from sfo_kalshi_quant.account import REGION_BY_SERIES
from sfo_kalshi_quant.cities import CITIES, city_for_market_ticker, get_city
from sfo_kalshi_quant.config import config_for_city, strategy_config_for_profile
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.forecast import SfoForecasterAdapter
from sfo_kalshi_quant.probability import ResidualCalibrator

# slug -> (series, station, cli_site, cli_issuedby, standard offset, civil tz)
NEW_CITIES = {
    "lv": ("KXHIGHTLV", "KLAS", "VEF", "LAS", -8, "America/Los_Angeles"),
    "min": ("KXHIGHTMIN", "KMSP", "MPX", "MSP", -6, "America/Chicago"),
    "satx": ("KXHIGHTSATX", "KSAT", "EWX", "SAT", -6, "America/Chicago"),
    "nola": ("KXHIGHTNOLA", "KMSY", "LIX", "MSY", -6, "America/Chicago"),
    "dc": ("KXHIGHTDC", "KDCA", "LWX", "DCA", -5, "America/New_York"),
}


@pytest.mark.parametrize("slug", sorted(NEW_CITIES))
def test_new_city_settlement_identities(slug: str) -> None:
    series, station, site, issuedby, offset, civil_tz = NEW_CITIES[slug]
    city = get_city(slug)

    assert city.series_ticker == series
    assert city.nws_station_id == station
    assert (city.cli_site, city.cli_issuedby) == (site, issuedby)
    assert city.standard_utc_offset_hours == offset
    assert city.civil_tz_name == civil_tz
    assert city.settlement_tz_name == f"Etc/GMT+{-offset}"
    assert f"site={site}&product=CLI&issuedby={issuedby}" in city.cli_product_url
    # Every new city runs the station-agnostic NWP -> EMOS -> CLI path.
    assert not city.has_full_blend
    assert not city.apply_cohort_blocks


def test_new_series_resolve_from_market_tickers_without_prefix_collisions() -> None:
    # KXHIGHTDC- vs KXHIGHTDAL- and KXHIGHTSATX- vs KXHIGHTSEA- share prefixes
    # up to the city letters; the "-" terminated match must keep them apart.
    assert city_for_market_ticker("KXHIGHTDC-26SEP13-T91").slug == "dc"
    assert city_for_market_ticker("KXHIGHTDAL-26SEP13-B80.5").slug == "dal"
    assert city_for_market_ticker("KXHIGHTSATX-26SEP13-T98").slug == "satx"
    assert city_for_market_ticker("KXHIGHTSEA-26SEP13-B70.5").slug == "sea"
    assert city_for_market_ticker("KXHIGHTLV-26SEP13-T98").slug == "lv"
    assert city_for_market_ticker("KXHIGHTMIN-26SEP13-T72").slug == "min"
    assert city_for_market_ticker("KXHIGHTNOLA-26SEP13-T95").slug == "nola"
    assert city_for_market_ticker("KXHIGHTD-26SEP13-T91") is None


def test_every_series_has_a_region_and_texas_holds_four_cities() -> None:
    # The region map bounds same-day correlated exposure (REGION_DAY_PCT) and
    # feeds research climate-region pooling; an unmapped series would fall
    # into the shared "unknown" bucket and silently share a cap with nothing.
    assert set(REGION_BY_SERIES) == {city.series_ticker for city in CITIES}
    assert REGION_BY_SERIES["KXHIGHTLV"] == "southwest"
    assert REGION_BY_SERIES["KXHIGHTMIN"] == "midwest"
    assert REGION_BY_SERIES["KXHIGHTSATX"] == "texas"
    assert REGION_BY_SERIES["KXHIGHTNOLA"] == "southeast"
    assert REGION_BY_SERIES["KXHIGHTDC"] == "northeast"
    by_region = Counter(REGION_BY_SERIES.values())
    assert by_region["texas"] == 4  # DAL, AUS, HOU, SATX under one region-day cap
    assert "unknown" not in by_region


# ---------------------------------------------------------------------------
# Fail-closed: a registry row with no scored EMOS history is skipped, not traded.
# ---------------------------------------------------------------------------


def _seed_scored_emos(root: Path, station_id: str, days: int = 40) -> None:
    with sqlite3.connect(root / "weather.db") as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS forecast_emos_daily_high (
                station_id TEXT NOT NULL DEFAULT 'KSFO',
                target_date TEXT NOT NULL,
                lead_days INTEGER NOT NULL,
                predicted_high_f REAL NOT NULL,
                sigma_f REAL NOT NULL,
                n_models INTEGER,
                model_spread_f REAL,
                fetched_at TEXT NOT NULL,
                method TEXT NOT NULL DEFAULT 'emos_ngr',
                source TEXT NOT NULL DEFAULT 'rolling_origin',
                actual_high_f REAL,
                PRIMARY KEY (station_id, target_date, lead_days, source)
            )
            """
        )
        first = date(2026, 5, 1)
        for i in range(days):
            day = (first + timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO forecast_emos_daily_high VALUES "
                "(?, ?, 1, ?, 2.0, 8, 3.0, 't', 'emos_wmean', 'rolling_origin', ?)",
                (station_id, day, 80.0 + (i % 5), 80 + (i % 5) + (1 if i % 3 == 0 else 0)),
            )


def _dependencies(cities, target: date, scanned: list[str]) -> ScanCommandDependencies:
    def never(*_args, **_kwargs):  # pragma: no cover - guards the wrong command path
        raise AssertionError("unexpected non-portfolio target call")

    def portfolio_target(_args, _target, _adapter, _calibrator, _config, _store, _color, *, city, **_kw):
        scanned.append(city.slug)

    return ScanCommandDependencies(
        cities_for_args=lambda _args: tuple(cities),
        config_for_args=lambda _args: strategy_config_for_profile("research"),
        resolve_targets=lambda _args, _color, _client, _city: ([target], {}),
        client_factory=lambda: object(),
        store_factory=PaperStore,
        city_config_factory=config_for_city,
        adapter_factory=SfoForecasterAdapter,
        calibrator_factory=ResidualCalibrator,
        sizing_model_factory=lambda _config, _store: None,
        analyze_target=never,
        tail_basket_target=never,
        arbitrage_target=never,
        portfolio_target=portfolio_target,
        city_lookup=get_city,
    )


def _run_scan(root: Path, cities) -> tuple[int, list[str], str]:
    scanned: list[str] = []
    args = SimpleNamespace(
        no_color=True,
        db_path=root / "paper.db",
        forecaster_root=root,
        calibration_source="auto",
    )
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cmd_portfolio_scan(
            args, dependencies=_dependencies(cities, date(2026, 9, 14), scanned)
        )
    return code, scanned, err.getvalue()


def test_portfolio_scan_skips_a_city_with_no_emos_rows_and_scans_the_calibrated_one() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_scored_emos(root, "KNYC")  # calibrated; KLAS has no rows at all

        code, scanned, stderr = _run_scan(root, (get_city("nyc"), get_city("lv")))

    assert scanned == ["nyc"]
    assert "[lv] skipped: calibration unavailable" in stderr
    assert "At least 30 forecast outcomes" in stderr
    assert code == 0  # one city produced a scannable target


def test_portfolio_scan_over_only_uncalibrated_cities_trades_nothing_and_exits_nonzero() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_scored_emos(root, "KNYC")  # table exists, but none of the scanned stations
        cities = tuple(get_city(slug) for slug in sorted(NEW_CITIES))

        code, scanned, stderr = _run_scan(root, cities)

    assert scanned == []
    for slug in NEW_CITIES:
        assert f"[{slug}] skipped: calibration unavailable" in stderr
    assert code == 1


def test_portfolio_scan_refuses_a_thin_emos_history_below_the_calibration_floor() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_scored_emos(root, "KLAS", days=29)  # one short of ResidualCalibrator's floor

        code, scanned, stderr = _run_scan(root, (get_city("lv"),))

    assert scanned == []
    assert "[lv] skipped: calibration unavailable" in stderr
    assert code == 1
