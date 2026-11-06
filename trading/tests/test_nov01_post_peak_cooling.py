"""Audit F2 (finding 1b, 2026-11-01): after the daily peak the same-day model
kept the forecast high as the expected final high with a 0.30-0.45F spread.

Boston, Nov 1: the station maximum was 57.0F at 15:53 EST and the temperature
was 48F and falling at 21:10, but the live EMOS lead-0 mean of 59.0F centred
the intraday model on 59 and the monitor heartbeat priced the never-reached
59-60 bin at 0.856. That read vetoed the stop on a NO in the reached 57-58 bin
until the catastrophic floor. Every probability here is built the way the
heartbeat builds it: the research profile, resolved for the city, with the
EMOS distribution forced on.

The cap engages only once the newest reading is at least 2F below the maximum,
at or after 14:00 fixed-standard time (or a later same-day entry cutoff). Two
whole-degree-C readings one step apart differ by 1.8F with no true change, and
exits.decide_exit vetoes a NO stop on the model probability alone (audit F3),
so a smaller gate let rounding noise at the peak flip that stop.
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
    POST_PEAK_MIN_DROP_F,
    ResidualCalibrator,
    _intraday_probability_model,
    _post_peak_earliest_standard_hour,
)
from sfo_kalshi_quant.standard_bins import standard_sfo_bins

BOS = get_city("bos")
EST = BOS.fixed_standard_timezone()
BOS_TZ = intraday_timezone_for_city(BOS)
SFO = get_city("sfo")
SFO_CIVIL_TZ = intraday_timezone_for_city(SFO)
PDT = ZoneInfo("America/Los_Angeles")
TARGET = date(2026, 11, 1)
OBSERVED_MAX_F = 57.0
EMOS_MU, EMOS_SIGMA = 59.0, 1.6
REACHED = "KXHIGHTBOS-26NOV01-B57.5"
FORECAST_BIN = "KXHIGHTBOS-26NOV01-B59.5"
CUTOFF_ENV = "PAPER_SAME_DAY_ENTRY_CUTOFF_HOUR"


@pytest.fixture(autouse=True)
def _default_same_day_entry_cutoff(monkeypatch):
    # The gate's hour follows PAPER_SAME_DAY_ENTRY_CUTOFF_HOUR; pin the default.
    monkeypatch.delenv(CUTOFF_ENV, raising=False)


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
    remaining_forecast_high_f: float | None = None,
) -> IntradaySnapshot:
    return IntradaySnapshot(
        target_date=target,
        observed_high_f=observed_high_f,
        latest_temp_f=latest_temp_f,
        latest_observed_at=latest_at.astimezone(timezone.utc).isoformat(),
        remaining_forecast_high_f=remaining_forecast_high_f,
        forecast_fetched_at=None,
        observation_count=22,
        observed_high_source="nws_daily_high_ground_truth",
    )


def _bos_model(intraday: IntradaySnapshot, predicted_high_f: float = EMOS_MU):
    model = _intraday_probability_model(
        MARKETS, predicted_high_f, intraday, config=_config(), standard_timezone=BOS_TZ
    )
    assert model is not None
    return model


def _model_probabilities(intraday: IntradaySnapshot, *, emos_mu: float = EMOS_MU) -> dict[str, float]:
    probabilities = _calibrator().bucket_probabilities(
        MARKETS,
        emos_mu,
        observed_high_f=intraday.observed_high_f,
        intraday=intraday,
        emos_mu_sigma=(emos_mu, EMOS_SIGMA),
        standard_timezone=BOS_TZ,
    )
    return {ticker: row.model_probability for ticker, row in probabilities.items()}


def _no_exit(model_no_probability: float, *, entry_cost: float, net_exit: float):
    return decide_exit(
        side="NO",
        entry_cost=entry_cost,
        net_exit=net_exit,
        stop_loss_net=entry_cost * 0.65,
        model_side_probability=model_no_probability,
        model_veto_buffer=0.08,
        model_veto_max_loss_roi=0.60,
        legacy_take_profit_net=entry_cost * 1.40,
        stop_loss_pct=35.0,
        settlement_first_no_min_cost=0.73,
    )


def test_cooling_evening_converges_on_the_observed_high_not_the_emos_mean():
    intraday = _snapshot(datetime(2026, 11, 1, 21, 10, tzinfo=EST), 48.0)

    model = _bos_model(intraday)
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

    signal = _no_exit(no_reached, entry_cost=0.80, net_exit=0.44)

    assert signal.action == "STOP_LOSS"
    assert not signal.catastrophic


def test_cooling_is_recognised_while_the_snapshot_carries_the_peak_time():
    """Until audit F1 lands, ``latest_observed_at`` holds the time of the
    maximum (15:53), not the newest reading, while ``latest_temp_f`` is the
    newest reading. The cap must still engage, using the peak hour's own
    remaining heat and spread."""

    intraday = _snapshot(datetime(2026, 11, 1, 15, 53, tzinfo=EST), 48.0)

    model = _bos_model(intraday)
    assert OBSERVED_MAX_F <= model.mean_final_high_f <= OBSERVED_MAX_F + 0.8 + 1e-9
    assert model.sigma_f >= NONFINAL_OBSERVED_HIGH_SIGMA_F

    probabilities = _model_probabilities(intraday)
    assert probabilities[REACHED] == max(probabilities.values())
    assert probabilities[REACHED] > probabilities[FORECAST_BIN]


@pytest.mark.parametrize(
    ("label", "latest_at", "latest_temp_f", "observed_high_f", "table_sigma_f"),
    [
        # Before the 14:00 fixed-standard same-day cutoff a dip can recover,
        # and pre-cutoff scans price research entries on this model.
        ("13:30 EST, 3F below the max", datetime(2026, 11, 1, 13, 30, tzinfo=EST), 54.0, 57.0, 0.90),
        # The newest reading is the maximum: the day may still be warming.
        ("16:30 EST, at the max", datetime(2026, 11, 1, 16, 30, tzinfo=EST), 57.0, 57.0, 0.45),
        ("16:30 EST, 0.8F below the max", datetime(2026, 11, 1, 16, 30, tzinfo=EST), 56.2, 57.0, 0.45),
        # One whole-degree-C step: a 13C reading (55.4F) under a 14C maximum
        # (57.2F) is 1.8F lower even while the temperature has not moved.
        ("21:10 EST, 13C under a 14C max", datetime(2026, 11, 1, 21, 10, tzinfo=EST), 55.4, 57.2, 0.30),
        # A precise maximum against a 13C reading.
        ("16:30 EST, 13C under a precise max", datetime(2026, 11, 1, 16, 30, tzinfo=EST), 55.4, 57.0, 0.45),
        # Just under the 2F gate.
        ("16:30 EST, 1.8F below the max", datetime(2026, 11, 1, 16, 30, tzinfo=EST), 55.2, 57.0, 0.45),
        ("14:05 EST, 1.1F below the max", datetime(2026, 11, 1, 14, 5, tzinfo=EST), 55.9, 57.0, 0.70),
    ],
)
def test_forecast_centre_is_unchanged_before_the_cutoff_or_without_cooling(
    label, latest_at, latest_temp_f, observed_high_f, table_sigma_f
):
    intraday = _snapshot(latest_at, latest_temp_f, observed_high_f=observed_high_f)

    model = _bos_model(intraday)

    assert model.mean_final_high_f == EMOS_MU, label
    assert model.sigma_f == table_sigma_f, label


def test_adjacent_whole_degree_c_readings_at_the_peak_do_not_flip_the_stop():
    """Review of the 1F gate: at 14:30 EST with the maximum at 14C (57.2F), a
    newest reading of 13C (55.4F) opened the gate and moved P(59-60) from 0.692
    to 0.285, so a NO on 59-60 (cost 0.70, net 0.38) went from STOP_LOSS to
    HOLD_MODEL_VETO, and readings alternating between the two flipped it back
    and forth with no change in the true temperature."""

    assert POST_PEAK_MIN_DROP_F > 57.2 - 55.4
    at = datetime(2026, 11, 1, 14, 30, tzinfo=EST)
    at_peak = _snapshot(at, 57.2, observed_high_f=57.2)
    one_step_down = _snapshot(at, 55.4, observed_high_f=57.2)

    assert _bos_model(one_step_down).mean_final_high_f == EMOS_MU
    probabilities_at_peak = _model_probabilities(at_peak)
    probabilities_one_step_down = _model_probabilities(one_step_down)
    assert probabilities_one_step_down == probabilities_at_peak
    assert probabilities_at_peak[FORECAST_BIN] == pytest.approx(0.692, abs=1e-3)
    for probabilities in (probabilities_at_peak, probabilities_one_step_down):
        signal = _no_exit(1.0 - probabilities[FORECAST_BIN], entry_cost=0.70, net_exit=0.38)
        assert signal.action == "STOP_LOSS"


def test_a_sub_2f_dip_after_the_cutoff_keeps_the_stop_on_a_no_above_the_max():
    """Review of the 1F gate: at 14:05 EST with the maximum at 57.0F, a single
    55.9F reading lifted P(NO on 59-60) from 0.227 to 0.729 and turned the stop
    on that NO (cost 0.70, net 0.38) into HOLD_MODEL_VETO, because the veto
    reads the model probability alone (audit F3). A 1.1F dip no longer opens
    the gate. A real 2F drop still does; see the module notes on F3."""

    intraday = _snapshot(datetime(2026, 11, 1, 14, 5, tzinfo=EST), 55.9)

    no_above_max = 1.0 - _model_probabilities(intraday, emos_mu=59.5)[FORECAST_BIN]

    assert no_above_max == pytest.approx(0.227, abs=1e-3)
    assert _no_exit(no_above_max, entry_cost=0.70, net_exit=0.38).action == "STOP_LOSS"


def test_late_afternoon_cap_uses_the_16_to_18_hour_remaining_heat():
    """From 16:00 to 18:00 standard the climatological remaining heat is 0.35F,
    and the 0.6F floor replaces the table's 0.45F spread. A reading exactly 2F
    below the maximum opens the gate."""

    for latest_at, latest_temp_f in (
        (datetime(2026, 11, 1, 17, 0, tzinfo=EST), 54.0),
        (datetime(2026, 11, 1, 16, 30, tzinfo=EST), OBSERVED_MAX_F - 2.0),
    ):
        model = _bos_model(_snapshot(latest_at, latest_temp_f))

        assert model.mean_final_high_f == pytest.approx(OBSERVED_MAX_F + 0.35)
        assert model.sigma_f == NONFINAL_OBSERVED_HIGH_SIGMA_F

    probabilities = _model_probabilities(_snapshot(datetime(2026, 11, 1, 17, 0, tzinfo=EST), 54.0))
    assert probabilities[REACHED] == max(probabilities.values())


def test_spread_floor_can_add_the_boundary_boost_when_the_forecast_is_below_the_max():
    """With the forecast (57.0F) below a 57.8F maximum the cap changes nothing,
    but after 18:00 the 0.6F floor replaces the 0.30F table spread. The maximum
    sits 0.7F under the 57-58 bin's continuous top edge (58.5F), so remaining
    heat risk rises from 0.020 to 0.243 and the 0.20 branch adds the +0.15
    boost. Before the fix this read gave 57-58 0.894 and 59-60 0.096; without
    the boost it would give 59-60 0.170. Pinned so a change is deliberate."""

    intraday = _snapshot(
        datetime(2026, 11, 1, 21, 10, tzinfo=EST), 55.0, observed_high_f=57.8
    )

    model = _bos_model(intraday, predicted_high_f=57.0)

    assert model.mean_final_high_f == 57.8
    assert model.sigma_f == NONFINAL_OBSERVED_HIGH_SIGMA_F
    assert model.remaining_heat_risk == pytest.approx(0.2433, abs=5e-4)
    assert model.blend_weight == pytest.approx(0.80)

    probabilities = _model_probabilities(intraday, emos_mu=57.0)
    assert probabilities[REACHED] == pytest.approx(0.8433, abs=5e-4)
    assert probabilities[FORECAST_BIN] == pytest.approx(0.1506, abs=5e-4)
    assert sum(probabilities.values()) == pytest.approx(1.0)


def test_sfo_civil_clock_summer_afternoon_waits_for_the_fixed_standard_cutoff():
    """SFO's intraday tables read civil Pacific time, but the same-day entry
    cutoff is 14:00 PST. At 14:30 PDT (13:30 PST) the scanner still prices
    same-day research entries, so the cap must not engage yet; at 15:30 PDT
    (14:30 PST) it does."""

    july = date(2026, 7, 10)
    config = _config(SFO)

    def model_at(hour: int, minute: int):
        intraday = _snapshot(
            datetime(2026, 7, 10, hour, minute, tzinfo=PDT),
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


def test_sfo_remaining_forecast_high_is_capped_past_the_peak():
    """SFO is the only city with a remaining-heat forecast, and it can lift the
    mean above the forecast high. At 15:30 PDT (14:30 PST) with the maximum at
    70F, the newest reading at 67F and a remaining forecast of 72F, the mean is
    capped at 70.8F (the 14-16h remaining heat), not left at 72F. Before the
    cutoff, or with a dip under 2F, the remaining forecast still sets it."""

    july = date(2026, 7, 10)
    config = _config(SFO)

    def mean_at(hour: int, minute: int, latest_temp_f: float) -> float:
        intraday = _snapshot(
            datetime(2026, 7, 10, hour, minute, tzinfo=PDT),
            latest_temp_f,
            observed_high_f=70.0,
            target=july,
            remaining_forecast_high_f=72.0,
        )
        model = _intraday_probability_model(
            standard_sfo_bins(), 71.0, intraday, config=config, standard_timezone=SFO_CIVIL_TZ
        )
        assert model is not None
        return model.mean_final_high_f

    assert mean_at(14, 30, 67.0) == 72.0
    assert mean_at(15, 30, 68.5) == 72.0
    assert mean_at(15, 30, 67.0) == pytest.approx(70.8)


@pytest.mark.parametrize(
    ("cutoff", "closed_at", "open_at"),
    [
        # A later cutoff moves the gate with it, so same-day research entries
        # admitted between 14:00 and that cutoff keep the uncapped model.
        ("16", (15, 50), (16, 0)),
        # An earlier cutoff keeps the 14:00 floor.
        ("12", (13, 50), (14, 0)),
        # A bad value falls back to 14, as in the scanner and the monitor.
        ("2pm", (13, 50), (14, 0)),
    ],
)
def test_post_peak_gate_never_opens_before_the_same_day_entry_cutoff(
    monkeypatch, cutoff, closed_at, open_at
):
    monkeypatch.setenv(CUTOFF_ENV, cutoff)

    def mean_at(hour: int, minute: int) -> float:
        return _bos_model(
            _snapshot(datetime(2026, 11, 1, hour, minute, tzinfo=EST), 54.0)
        ).mean_final_high_f

    assert mean_at(*closed_at) == EMOS_MU
    assert mean_at(*open_at) < EMOS_MU


@pytest.mark.parametrize("raw", [None, "14", "16", "9", "23", "30", "-3", "2pm"])
def test_gate_hour_reads_the_cutoff_the_way_the_scanner_and_monitor_do(monkeypatch, raw):
    from sfo_kalshi_quant import monitor
    from sfo_kalshi_quant._cli import scan

    if raw is not None:
        monkeypatch.setenv(CUTOFF_ENV, raw)

    assert scan._same_day_entry_cutoff_hour() == monitor._same_day_entry_cutoff_hour()
    assert _post_peak_earliest_standard_hour() == max(
        14.0, float(scan._same_day_entry_cutoff_hour())
    )


def test_post_peak_cap_never_lifts_a_forecast_already_at_or_below_the_max():
    """The cap only lowers the mean: the removed 0.35 x climatology mean lift
    must not come back. With the forecast below the maximum the mean stays the
    observed maximum, and only the spread widens to the observation error."""

    intraday = _snapshot(datetime(2026, 11, 1, 21, 10, tzinfo=EST), 48.0)

    model = _bos_model(intraday, predicted_high_f=56.0)

    assert model.mean_final_high_f == OBSERVED_MAX_F
    assert model.sigma_f == NONFINAL_OBSERVED_HIGH_SIGMA_F
