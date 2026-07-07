"""Multi-city invariants: per-city config, clocks, EMOS adapter, scoped caps."""

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.config import config_for_city, strategy_config_for_profile
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.forecast import SfoForecasterAdapter
from sfo_kalshi_quant.models import TradeDecision
from sfo_kalshi_quant.settlement_day import settlement_today
from sfo_kalshi_quant.standard_bins import fallback_bins


def _decision(ticker: str, contracts: float = 10.0) -> TradeDecision:
    return TradeDecision(
        ticker=ticker,
        label="test",
        action="BUY_YES",
        approved=True,
        probability=0.30,
        probability_lcb=0.20,
        yes_bid=0.02,
        yes_ask=0.03,
        spread=0.01,
        fee_per_contract=0.01,
        cost_per_contract=0.04,
        edge=0.26,
        edge_lcb=0.16,
        kelly_fraction=0.01,
        recommended_contracts=contracts,
        expected_profit=2.6,
        reasons=[],
        strike_type="between",
        floor_strike=66.0,
        cap_strike=67.0,
    )


def test_config_for_city_forces_emos_and_scopes_cohort_blocks():
    live = strategy_config_for_profile("live")
    assert live.blocked_forecast_cohorts  # SFO evidence gate present on live

    phoenix = config_for_city(live, get_city("phx"))
    assert phoenix.emos_distribution_enabled  # only calibrated distribution
    assert phoenix.blocked_forecast_cohorts == ()  # SFO evidence must not gate PHX

    sfo = config_for_city(live, get_city("sfo"))
    assert sfo == live  # identity for the blend city


def test_settlement_today_uses_each_citys_standard_clock():
    # 06:00 UTC: 01:00 EST (already Jul 6 in NYC) but 22:00 PST (still Jul 5 at SFO).
    instant = datetime(2026, 7, 6, 6, 0, tzinfo=timezone.utc)
    assert settlement_today(instant, get_city("nyc")) == date(2026, 7, 6)
    assert settlement_today(instant, get_city("sfo")) == date(2026, 7, 5)
    # Phoenix never observes DST; its clock is MST year round.
    assert settlement_today(instant, get_city("phx")) == date(2026, 7, 5)


def test_fallback_bins_recenter_on_the_citys_forecast():
    ladder = fallback_bins("KXHIGHTPHX-26JUL08-PAPER", 108.3)
    tickers = [m.ticker for m in ladder]
    assert tickers[0].endswith("-T104")
    assert tickers[-1].endswith("-T111")
    assert len(ladder) == 6
    # Legacy SFO ladder is preserved bit-for-bit via the compat wrapper.
    from sfo_kalshi_quant.standard_bins import standard_sfo_bins

    legacy = [m.ticker for m in standard_sfo_bins("E")]
    assert legacy == ["E-T66", "E-B66.5", "E-B68.5", "E-B70.5", "E-B72.5", "E-T73"]


def _seed_emos(root: Path) -> None:
    with sqlite3.connect(root / "weather.db") as conn:
        conn.execute(
            """
            CREATE TABLE forecast_emos_daily_high (
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
        # Scored history for calibration + one live row for the target.
        for i in range(40):
            day = f"2026-05-{i % 28 + 1:02d}"
            conn.execute(
                "INSERT OR REPLACE INTO forecast_emos_daily_high VALUES "
                "('KNYC', ?, 1, ?, 2.0, 8, 3.0, 't', 'emos_wmean', 'rolling_origin', ?)",
                (day, 80.0 + (i % 5), 80 + (i % 5)),
            )
        conn.execute(
            "INSERT INTO forecast_emos_daily_high VALUES "
            "('KNYC', '2026-07-08', 1, 85.4, 2.1, 8, 4.2, "
            "'2026-07-07T12:00:00+00:00', 'emos_wmean', 'live', NULL)"
        )


def test_emos_only_city_reads_snapshot_and_outcomes_from_archive():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_emos(root)
        adapter = SfoForecasterAdapter(root, city=get_city("nyc"))

        snapshot = adapter.latest_blend(date(2026, 7, 8))
        assert snapshot.predicted_high_f == 85.4
        assert snapshot.station_id == "KNYC"
        assert snapshot.source_count == 8
        assert snapshot.source_spread_f == 4.2  # cross-model spread override
        assert "emos" in snapshot.method

        outcomes = adapter.load_calibration_outcomes("lstm")
        assert len(outcomes) >= 28  # routed to the EMOS archive, not LSTM
        assert all(o.station_id == "KNYC" for o in outcomes)

        emos = adapter.load_emos_mu_sigma(lead_days=None)
        assert emos[date(2026, 7, 8)] == (85.4, 2.1)


def test_emos_only_city_with_no_forecast_raises_not_guesses():
    from sfo_kalshi_quant.forecast import ForecastDataError

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _seed_emos(root)
        adapter = SfoForecasterAdapter(root, city=get_city("chi"))  # no KMDW rows
        try:
            adapter.latest_blend(date(2026, 7, 8))
        except ForecastDataError:
            pass
        else:
            raise AssertionError("expected ForecastDataError for a city with no EMOS row")


def test_exposure_and_settlement_are_scoped_per_series():
    with TemporaryDirectory() as tmp:
        store = PaperStore(Path(tmp) / "paper.db")
        store.record_paper_order("2026-07-08", _decision("KXHIGHNY-26JUL08-B84.5"))
        store.record_paper_order("2026-07-08", _decision("KXHIGHTSFO-26JUL08-B66.5"))

        total = store.paper_spend_for_target("2026-07-08")
        nyc_only = store.paper_spend_for_target(
            "2026-07-08", series_ticker="KXHIGHNY"
        )
        assert 0 < nyc_only < total

        # NYC's settlement high must not resolve SFO's bins on the same date.
        settled = store.settle_paper_orders(
            "2026-07-08", 85.0, series_ticker="KXHIGHNY"
        )
        assert settled == 1
        rows = {r["market_ticker"]: r for r in store.paper_orders(10)}
        assert rows["KXHIGHNY-26JUL08-B84.5"]["status"] == "PAPER_SETTLED"
        assert rows["KXHIGHTSFO-26JUL08-B66.5"]["status"] == "PAPER_FILLED"

        assert store.open_paper_target_dates(series_ticker="KXHIGHTSFO") == [
            "2026-07-08"
        ]
        assert store.open_paper_target_dates(series_ticker="KXHIGHNY") == []
