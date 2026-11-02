"""Audit F2 (finding 1b, 2026-11-01): after the daily peak the same-day model
kept the forecast high as the expected final high with a 0.30-0.45F spread.

Boston, Nov 1: the station maximum was 57.0F at 15:53 EST and the temperature
was 48F and falling at 21:10, but the live EMOS lead-0 mean of 59.0F centred
the intraday model on 59 and the monitor heartbeat priced the never-reached
59-60 bin at 0.856. That read vetoed the stop on a NO in the reached 57-58 bin
until the catastrophic floor. Every probability here is built the way the
heartbeat builds it: the research profile, resolved for the city, with the
EMOS distribution forced on.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.config import (
    config_for_city,
    intraday_timezone_for_city,
    strategy_config_for_profile,
)
from sfo_kalshi_quant.exits import decide_exit
from sfo_kalshi_quant.models import ForecastOutcome, IntradaySnapshot, MarketBin
from sfo_kalshi_quant.probability import (
    NONFINAL_OBSERVED_HIGH_SIGMA_F,
    ResidualCalibrator,
    _intraday_probability_model,
)
from sfo_kalshi_quant.standard_bins import standard_sfo_bins

BOS = get_city("bos")
EST = BOS.fixed_standard_timezone()
BOS_TZ = intraday_timezone_for_city(BOS)
SFO = get_city("sfo")
SFO_CIVIL_TZ = intraday_timezone_for_city(SFO)
TARGET = date(2026, 11, 1)
OBSERVED_MAX_F = 57.0
EMOS_MU, EMOS_SIGMA = 59.0, 1.6
REACHED = "KXHIGHTBOS-26NOV01-B57.5"
FORECAST_BIN = "KXHIGHTBOS-26NOV01-B59.5"


def _bin(suffix: str, strike_type: str, floor: float | None, cap: float | None) -> MarketBin:
    return MarketBin(
        ticker=f"KXHIGHTBOS-26NOV01-{suffix}",
        event_ticker="KXHIGHTBOS-26NOV01",
        title="",
        yes_sub_title=suffix,
        strike_type=strike_type,
        floor_strike=floor,
        cap_strike=cap,
        yes_bid=0.30,
        yes_ask=0.34,
        no_bid=0.66,
        no_ask=0.70,
        yes_bid_size=10.0,
        yes_ask_size=10.0,
        status="active",
    )


MARKETS = [
    _bin("T55", "less", None, 55.0),
    _bin("B55.5", "between", 55.0, 56.0),
    _bin("B57.5", "between", 57.0, 58.0),
    _bin("B59.5", "between", 59.0, 60.0),
    _bin("B61.5", "between", 61.0, 62.0),
    _bin("T62", "greater", 62.0, None),
]


def _config(city=BOS):
    return replace(
        config_for_city(strategy_config_for_profile("research"), city),
        emos_distribution_enabled=True,
    )


def _calibrator() -> ResidualCalibrator:
    outcomes = [
        ForecastOutcome(
            local_date=date(2026, 6, 1) + timedelta(days=idx),
            predicted_high_f=60.0,
            actual_high_f=60.0 + ((idx % 7) - 3) * 0.8,
            model_name="emos_wmean",
            station_id="KBOS",
        )
        for idx in range(60)
    ]
    return ResidualCalibrator(outcomes, _config())


def _snapshot(
    latest_at: datetime,
    latest_temp_f: float,
    *,
    observed_high_f: float = OBSERVED_MAX_F,
    target: date = TARGET,
) -> IntradaySnapshot:
    return IntradaySnapshot(
        target_date=target,
        observed_high_f=observed_high_f,
        latest_temp_f=latest_temp_f,
        latest_observed_at=latest_at.astimezone(timezone.utc).isoformat(),
        remaining_forecast_high_f=None,
        forecast_fetched_at=None,
        observation_count=22,
        observed_high_source="nws_daily_high_ground_truth",
    )


def _model_probabilities(intraday: IntradaySnapshot) -> dict[str, float]:
    probabilities = _calibrator().bucket_probabilities(
        MARKETS,
        EMOS_MU,
        observed_high_f=intraday.observed_high_f,
        intraday=intraday,
        emos_mu_sigma=(EMOS_MU, EMOS_SIGMA),
        standard_timezone=BOS_TZ,
    )
    return {ticker: row.model_probability for ticker, row in probabilities.items()}


def test_cooling_evening_converges_on_the_observed_high_not_the_emos_mean():
    intraday = _snapshot(datetime(2026, 11, 1, 21, 10, tzinfo=EST), 48.0)

    model = _intraday_probability_model(
        MARKETS, EMOS_MU, intraday, config=_config(), standard_timezone=BOS_TZ
    )
    assert model is not None
    # The expected further rise is capped at the climatological remaining heat
    # after 18:00 (0.15F), not the 2F gap up to the stale EMOS mean.
    assert OBSERVED_MAX_F <= model.mean_final_high_f <= OBSERVED_MAX_F + 0.15 + 1e-9
    # Centred on a raw nonfinal maximum, the spread covers the module's own
    # raw-to-official error instead of the 0.30F late-day table value.
    assert model.sigma_f >= NONFINAL_OBSERVED_HIGH_SIGMA_F
    # The +0.15 boundary boost no longer fires on a gap that only the
    # forecast created, so the stale forecast's lean is not amplified.
    assert model.blend_weight == pytest.approx(_config().intraday_probability_weight)

    probabilities = _model_probabilities(intraday)
    reached = probabilities[REACHED]
    assert reached == max(probabilities.values())
    assert reached > 0.5
    assert probabilities[FORECAST_BIN] < 0.25
    assert sum(probabilities.values()) == pytest.approx(1.0)


def test_reached_bin_no_stop_is_not_vetoed_by_the_stale_forecast():
    """A NO on 57-58 (the bin the day actually reached), cost 0.80, bid now
    worth 0.44 net. Before the fix the 21:23 read gave P(NO)=0.895, so the stop
    was held as HOLD_MODEL_VETO toward the 60% floor."""

    intraday = _snapshot(datetime(2026, 11, 1, 21, 10, tzinfo=EST), 48.0)
    no_reached = 1.0 - _model_probabilities(intraday)[REACHED]

    signal = decide_exit(
        side="NO",
        entry_cost=0.80,
        net_exit=0.44,
        stop_loss_net=0.80 * 0.65,
        model_side_probability=no_reached,
        model_veto_buffer=0.08,
        model_veto_max_loss_roi=0.60,
        legacy_take_profit_net=0.80 * 1.40,
        stop_loss_pct=35.0,
        settlement_first_no_min_cost=0.73,
    )

    assert signal.action == "STOP_LOSS"
    assert not signal.catastrophic


def test_cooling_is_recognised_while_the_snapshot_carries_the_peak_time():
    """Until audit F1 lands, ``latest_observed_at`` holds the time of the
    maximum (15:53), not the newest reading, while ``latest_temp_f`` is the
    newest reading. The cap must still engage, using the peak hour's own
    remaining heat and spread."""

    intraday = _snapshot(datetime(2026, 11, 1, 15, 53, tzinfo=EST), 48.0)

    model = _intraday_probability_model(
        MARKETS, EMOS_MU, intraday, config=_config(), standard_timezone=BOS_TZ
    )
    assert model is not None
    assert OBSERVED_MAX_F <= model.mean_final_high_f <= OBSERVED_MAX_F + 0.8 + 1e-9
    assert model.sigma_f >= NONFINAL_OBSERVED_HIGH_SIGMA_F

    probabilities = _model_probabilities(intraday)
    assert probabilities[REACHED] == max(probabilities.values())
    assert probabilities[REACHED] > probabilities[FORECAST_BIN]


@pytest.mark.parametrize(
    ("label", "latest_at", "latest_temp_f", "table_sigma_f"),
    [
        # Before the 14:00 fixed-standard same-day cutoff a dip can recover,
        # and pre-cutoff scans price research entries on this model.
        ("13:30 EST, 3F below the max", datetime(2026, 11, 1, 13, 30, tzinfo=EST), 54.0, 0.90),
        # The newest reading is the maximum: the day may still be warming.
        ("16:30 EST, at the max", datetime(2026, 11, 1, 16, 30, tzinfo=EST), 57.0, 0.45),
        # 0.8F is inside the 0.9F rounding of a whole-degree-C reading.
        ("16:30 EST, 0.8F below the max", datetime(2026, 11, 1, 16, 30, tzinfo=EST), 56.2, 0.45),
    ],
)
def test_forecast_centre_is_unchanged_before_the_cutoff_or_without_cooling(
    label, latest_at, latest_temp_f, table_sigma_f
):
    intraday = _snapshot(latest_at, latest_temp_f)

    model = _intraday_probability_model(
        MARKETS, EMOS_MU, intraday, config=_config(), standard_timezone=BOS_TZ
    )

    assert model is not None, label
    assert model.mean_final_high_f == EMOS_MU, label
    assert model.sigma_f == table_sigma_f, label


def test_sfo_civil_clock_summer_afternoon_waits_for_the_fixed_standard_cutoff():
    """SFO's intraday tables read civil Pacific time, but the same-day entry
    cutoff is 14:00 PST. At 14:30 PDT (13:30 PST) the scanner still prices
    same-day research entries, so the cap must not engage yet; at 15:30 PDT
    (14:30 PST) it does."""

    pdt = ZoneInfo("America/Los_Angeles")
    july = date(2026, 7, 10)
    config = _config(SFO)

    def model_at(hour: int, minute: int):
        intraday = _snapshot(
            datetime(2026, 7, 10, hour, minute, tzinfo=pdt),
            67.0,
            observed_high_f=70.0,
            target=july,
        )
        return _intraday_probability_model(
            standard_sfo_bins(), 73.0, intraday, config=config, standard_timezone=SFO_CIVIL_TZ
        )

    before_cutoff = model_at(14, 30)
    after_cutoff = model_at(15, 30)

    assert before_cutoff is not None and after_cutoff is not None
    assert before_cutoff.mean_final_high_f == 73.0
    assert 70.0 <= after_cutoff.mean_final_high_f <= 70.0 + 0.8 + 1e-9


def test_post_peak_cap_never_lifts_a_forecast_already_at_or_below_the_max():
    """The cap only lowers the mean: the removed 0.35 x climatology mean lift
    must not come back. With the forecast below the maximum the mean stays the
    observed maximum, and only the spread widens to the observation error."""

    intraday = _snapshot(datetime(2026, 11, 1, 21, 10, tzinfo=EST), 48.0)

    model = _intraday_probability_model(
        MARKETS, 56.0, intraday, config=_config(), standard_timezone=BOS_TZ
    )

    assert model is not None
    assert model.mean_final_high_f == OBSERVED_MAX_F
    assert model.sigma_f == NONFINAL_OBSERVED_HIGH_SIGMA_F
