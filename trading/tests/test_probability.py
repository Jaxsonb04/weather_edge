import math
from dataclasses import replace
from datetime import date, timedelta

from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.config import StrategyConfig, intraday_timezone_for_city
from sfo_kalshi_quant.models import ForecastOutcome, IntradaySnapshot
from sfo_kalshi_quant.probability import (
    SOURCE_SPREAD_DEBIAS_SCALE,
    ResidualCalibrator,
    _market_implied_probabilities,
    _market_prior_reliability,
    _intraday_probability_model,
    _local_decimal_hour,
    _model_weight,
    _normalize_weather_probabilities,
    raw_equivalent_source_spread_f,
)
from sfo_kalshi_quant.standard_bins import fallback_bins, standard_sfo_bins


def _outcomes():
    start = date(2025, 1, 1)
    rows = []
    for idx in range(220):
        pred = 66.0 + (idx % 10) * 0.7
        residual = [-3, -2, -1, 0, 1, 2, 3, 4, -1, 1][idx % 10]
        rows.append(
            ForecastOutcome(
                local_date=start + timedelta(days=idx),
                predicted_high_f=pred,
                actual_high_f=pred + residual,
            )
        )
    return rows


def test_bucket_probabilities_sum_to_one():
    config = StrategyConfig(min_conditional_samples=20)
    calibrator = ResidualCalibrator(_outcomes(), config)
    probabilities = calibrator.bucket_probabilities(standard_sfo_bins(), 69.0)
    total = sum(row.probability for row in probabilities.values())
    assert abs(total - 1.0) < 1e-9
    assert all(0.0 <= row.lower_confidence <= row.probability <= 1.0 for row in probabilities.values())
    assert all(
        row.upper_confidence is not None
        and row.probability <= row.upper_confidence <= 1.0
        for row in probabilities.values()
    )


def test_global_fallback_confidence_band_rests_on_global_support():
    # Replaces test_missing_conditional_analogue_cannot_narrow_confidence_band
    # (2026-08-16), retired 2026-08-30 with the cap it pinned: pricing the
    # global-fallback estimate as a min_conditional_samples-sized sample drove
    # production edge_lcb deductions to 0.16-0.22 at mid-range p and cut paper
    # entries ~10x while public tape volume was unchanged. The fallback
    # probability IS the global estimate, so its sampling error must rest on
    # the global window's support, not a synthetic small n.
    start = date(2025, 1, 1)
    residuals = (-2.0, -1.0, 0.0, 1.0, 2.0)
    outcomes = [
        ForecastOutcome(
            local_date=start + timedelta(days=index),
            predicted_high_f=70.0 if index < 50 else 90.0,
            actual_high_f=(70.0 if index < 50 else 90.0) + residuals[index % 5],
        )
        for index in range(400)
    ]
    calibrator = ResidualCalibrator(
        outcomes,
        StrategyConfig(
            min_conditional_samples=50,
            shrinkage_samples=0,
            empirical_weight=0.0,
        ),
    )

    with_analogue = list(
        calibrator.bucket_probabilities(fallback_bins("NEAR", 70.0), 70.0).values()
    )[2]
    without_analogue = list(
        calibrator.bucket_probabilities(fallback_bins("FAR", 150.0), 150.0).values()
    )[2]

    assert abs(with_analogue.probability - without_analogue.probability) < 1e-12
    fallback_width = without_analogue.probability - without_analogue.lower_confidence
    p = without_analogue.probability
    # 400 global outcomes support a band far tighter than the retired
    # min_conditional_samples cap would allow; guard against the cap coming
    # back by bounding the width below the n=min_conditional_samples value.
    capped_width = 1.96 * math.sqrt(p * (1.0 - p) / 50.0)
    honest_width = 1.96 * math.sqrt(p * (1.0 - p) / 400.0)
    assert fallback_width < capped_width * 0.75
    assert fallback_width >= honest_width * 0.99
    # A real conditional window still prices its own (smaller) support: the
    # 50-sample analogue band must stay at least as wide as the 400-sample
    # global fallback band.
    assert (
        with_analogue.probability - with_analogue.lower_confidence
    ) >= fallback_width


def test_observed_high_so_far_rules_out_lower_today_bins():
    config = StrategyConfig(min_conditional_samples=20)
    calibrator = ResidualCalibrator(_outcomes(), config)
    markets = [
        replace(
            market,
            status="active",
            yes_bid=0.01,
            yes_ask=0.02,
            yes_bid_size=10.0,
            yes_ask_size=10.0,
        )
        for market in standard_sfo_bins()
    ]
    probabilities = calibrator.bucket_probabilities(
        markets,
        68.0,
        observed_high_f=67.0,
    )
    low = next(row for row in probabilities.values() if row.label == "65° or below")
    # A NONFINAL raw observation is not exact settlement truth (audit MD-01):
    # a bin 1.5F below the running maximum is effectively excluded but keeps
    # dust mass for the raw-to-official reporting error.
    assert low.probability < 0.005
    assert low.model_probability < 0.005
    assert low.observed_high_is_final is False
    assert abs(sum(row.probability for row in probabilities.values()) - 1.0) < 1e-9


def test_observed_high_above_half_degree_boundary_damps_current_bin_without_certainty():
    """Raw 69.9F sits above the 68-69 bin's 69.5 boundary, but the official
    integer report can still land at 69 (audit MD-01, order 188: raw 87.8F
    settled 87F). The current bin must be strongly damped, never zeroed."""

    config = StrategyConfig(min_conditional_samples=20)
    calibrator = ResidualCalibrator(_outcomes(), config)
    probabilities = calibrator.bucket_probabilities(
        standard_sfo_bins(),
        69.9,
        observed_high_f=69.9,
    )
    current = next(row for row in probabilities.values() if row.label == "68° to 69°")
    next_bin = next(row for row in probabilities.values() if row.label == "70° to 71°")
    assert 0.0 < current.probability < next_bin.probability
    assert current.model_probability is not None and current.model_probability > 0.0
    assert next_bin.probability > 0.0


def test_intraday_near_boundary_before_peak_shifts_probability_to_next_bin():
    config = StrategyConfig(min_conditional_samples=20)
    calibrator = ResidualCalibrator(_outcomes(), config)
    intraday = IntradaySnapshot(
        target_date=date(2026, 6, 5),
        observed_high_f=69.3,
        latest_temp_f=69.3,
        latest_observed_at="2026-06-05T20:00:00+00:00",
        remaining_forecast_high_f=70.0,
        forecast_fetched_at="2026-06-05T19:45:00+00:00",
    )
    probabilities = calibrator.bucket_probabilities(
        standard_sfo_bins(),
        69.3,
        observed_high_f=69.3,
        intraday=intraday,
    )
    current = next(row for row in probabilities.values() if row.label == "68° to 69°")
    next_bin = next(row for row in probabilities.values() if row.label == "70° to 71°")
    assert current.intraday_probability is not None
    assert current.remaining_heat_risk is not None
    assert current.remaining_heat_risk > 0.50
    assert next_bin.probability > current.probability


def test_intraday_model_uses_city_fixed_standard_time_for_diurnal_state():
    """18Z is 13:00 EST in NYC, independent of summer DST."""

    config = StrategyConfig(min_conditional_samples=20)
    intraday = IntradaySnapshot(
        target_date=date(2026, 7, 10),
        observed_high_f=68.0,
        latest_temp_f=68.0,
        latest_observed_at="2026-07-10T18:00:00+00:00",
        remaining_forecast_high_f=68.0,
        forecast_fetched_at="2026-07-10T17:45:00+00:00",
    )
    nyc_tz = intraday_timezone_for_city(get_city("nyc"))
    sfo_tz = intraday_timezone_for_city(get_city("sfo"))

    nyc = _intraday_probability_model(
        standard_sfo_bins(), 68.0, intraday, config=config, standard_timezone=nyc_tz
    )
    sfo = _intraday_probability_model(
        standard_sfo_bins(), 68.0, intraday, config=config, standard_timezone=sfo_tz
    )

    assert _local_decimal_hour(intraday.latest_observed_at, nyc_tz) == 13.0
    assert _local_decimal_hour(intraday.latest_observed_at, sfo_tz) == 11.0
    assert _local_decimal_hour(
        intraday.latest_observed_at, get_city("den").fixed_standard_timezone()
    ) == 11.0
    assert nyc is not None and sfo is not None
    assert nyc.sigma_f == 0.9
    assert sfo.sigma_f == 1.1
    assert nyc.blend_weight == 0.55
    assert sfo.blend_weight == 0.4
    assert nyc.remaining_heat_risk is not None
    assert sfo.remaining_heat_risk is not None
    assert nyc.remaining_heat_risk < sfo.remaining_heat_risk


def test_market_prior_uses_yes_and_no_book_bounds():
    base = standard_sfo_bins()
    markets = [
        replace(
            base[0],
            status="active",
            yes_bid=0.04,
            yes_ask=0.06,
            no_bid=0.94,
            no_ask=0.96,
        ),
        replace(
            base[1],
            status="active",
            yes_bid=0.14,
            yes_ask=0.16,
            no_bid=0.84,
            no_ask=0.86,
        ),
    ]

    probabilities = _market_implied_probabilities(markets)

    assert round(probabilities[markets[0].ticker], 2) == 0.25
    assert round(probabilities[markets[1].ticker], 2) == 0.75


def test_market_prior_weight_is_reliability_aware():
    base = standard_sfo_bins()[0]
    config = StrategyConfig()
    tight_deep = replace(
        base,
        status="active",
        yes_bid=0.49,
        yes_ask=0.51,
        no_bid=0.49,
        no_ask=0.51,
        yes_bid_size=100.0,
        yes_ask_size=100.0,
    )
    wide_thin = replace(
        base,
        status="active",
        yes_bid=0.20,
        yes_ask=0.35,
        no_bid=0.65,
        no_ask=0.80,
        yes_bid_size=1.0,
        yes_ask_size=1.0,
    )

    assert _market_prior_reliability(tight_deep, config) > _market_prior_reliability(wide_thin, config)
    assert _model_weight(0.0, market=tight_deep, config=config) < _model_weight(
        0.0,
        market=wide_thin,
        config=config,
    )


def test_predawn_intraday_does_not_crush_high_bracket_tails():
    """At 2:36am the overnight observed high says ~nothing about the afternoon
    peak; the 2026-06-10 book bet against >=79F at p=0.008 and the day settled
    at 79F. Pre-dawn the intraday gaussian must stay wide and lightly weighted."""

    config = StrategyConfig(min_conditional_samples=20)
    calibrator = ResidualCalibrator(_outcomes(), config)
    markets = [
        replace(
            market,
            status="active",
            yes_bid=0.10,
            yes_ask=0.12,
            yes_bid_size=20.0,
            yes_ask_size=20.0,
        )
        for market in standard_sfo_bins()
    ]
    intraday = IntradaySnapshot(
        target_date=date(2026, 6, 10),
        observed_high_f=55.0,
        latest_temp_f=54.6,
        latest_observed_at="2026-06-10T09:36:00+00:00",  # 2:36am PDT
        remaining_forecast_high_f=68.0,
        forecast_fetched_at="2026-06-10T09:30:00+00:00",
        observation_count=20,
        observed_high_source="nws_station_observations",
        is_complete=False,
    )
    probabilities = calibrator.bucket_probabilities(
        markets,
        70.2,
        source_spread_f=9.6,
        intraday=intraday,
    )
    top = next(row for row in probabilities.values() if row.label == "74° or above")
    assert top.intraday_probability is not None
    assert top.intraday_probability > 0.05
    # The blended weather probability must not collapse to near-zero either.
    assert top.probability > 0.05


def test_normalize_weather_probabilities_zero_mass_returns_uniform():
    # When the intraday blend zeroes every bucket, the old code returned the
    # un-normalized (sum==0) list, silently zeroing every bucket's edge. The fix
    # falls back to a uniform prior so the vector still sums to 1.
    markets = standard_sfo_bins()[:3]
    rows = [(market, 0.0, 0.0, 0.0, None) for market in markets]
    out = _normalize_weather_probabilities(rows)
    probs = [p for _, p, _, _, _ in out]
    assert abs(sum(probs) - 1.0) < 1e-9
    assert all(abs(p - 1.0 / len(markets)) < 1e-12 for p in probs)


def test_normalize_weather_probabilities_empty_returns_empty():
    assert _normalize_weather_probabilities([]) == []


def test_normalize_weather_probabilities_preserves_positive_mass():
    markets = standard_sfo_bins()[:2]
    rows = [(markets[0], 1.0, 0.0, 0.0, None), (markets[1], 3.0, 0.0, 0.0, None)]
    out = _normalize_weather_probabilities(rows)
    probs = [p for _, p, _, _, _ in out]
    assert abs(probs[0] - 0.25) < 1e-9
    assert abs(probs[1] - 0.75) < 1e-9


# --------------------------------------------------------------------------- #
# FC-1: source_spread_f changed units (raw range -> debiased range). Every
# control in this module keyed to that statistic was tuned on the raw scale, so
# each one must read a raw-EQUIVALENT value. These tests pin the knee, not just
# the helper -- a silent revert would otherwise pass the whole suite.
# --------------------------------------------------------------------------- #

def test_raw_equivalent_source_spread_round_trips_the_measured_scale():
    for raw in (3.0, 6.0, 10.0, 11.75, 13.667):
        debiased = raw * SOURCE_SPREAD_DEBIAS_SCALE
        assert abs(raw_equivalent_source_spread_f(debiased) - raw) < 1e-9
    # The map must SHRINK raw -> debiased, i.e. expand on the way back.
    assert 0.0 < SOURCE_SPREAD_DEBIAS_SCALE < 1.0
    assert raw_equivalent_source_spread_f(5.0) > 5.0


def test_market_weight_shift_knee_is_in_debiased_units():
    """The 3.0 F knee is a RAW-scale constant; in debiased units it sits at
    3.0 * scale. Feeding the debiased statistic straight in would move weight
    away from the Kalshi price -- the worse forecaster of the two."""

    market = replace(
        standard_sfo_bins()[0],
        status="active",
        yes_bid=0.49,
        yes_ask=0.51,
        no_bid=0.49,
        no_ask=0.51,
        yes_bid_size=100.0,
        yes_ask_size=100.0,
    )
    config = StrategyConfig()
    assert config.source_spread_market_weight_per_f > 0.0

    knee = 3.0 * SOURCE_SPREAD_DEBIAS_SCALE
    at_knee = _model_weight(knee, market=market, config=config)
    below_knee = _model_weight(knee - 0.5, market=market, config=config)
    # Nothing happens at or below the (debiased) knee.
    assert abs(at_knee - below_knee) < 1e-12
    # Above it, model weight falls -- and by the RAW-scale slope, so a debiased
    # spread one raw-degree past the knee shifts exactly one slope-step.
    one_raw_degree_past = _model_weight(
        4.0 * SOURCE_SPREAD_DEBIAS_SCALE, market=market, config=config
    )
    reliability = _market_prior_reliability(market, config)
    expected_shift = config.source_spread_market_weight_per_f * reliability
    assert abs((at_knee - one_raw_degree_past) - expected_shift) < 1e-9


def test_model_risk_penalty_ramp_is_in_debiased_units():
    """The LCB deduction ramps from a raw 3.0 F at 0.0075/raw-degree, capped at
    0.08. Live runs min_edge_lcb = 0.00, so a units drift here silently loosens
    the gate the live book trades against."""

    config = StrategyConfig(min_conditional_samples=20)
    calibrator = ResidualCalibrator(_outcomes(), config)
    markets = standard_sfo_bins()

    def band(spread: float) -> float:
        rows = calibrator.bucket_probabilities(markets, 69.0, source_spread_f=spread)
        row = max(rows.values(), key=lambda r: r.probability)
        return row.probability - row.lower_confidence

    knee = 3.0 * SOURCE_SPREAD_DEBIAS_SCALE
    # At the debiased knee the penalty is still zero (and so is the widening).
    assert abs(band(knee) - band(0.0)) < 1e-9
    # Between a raw 11.75 (where the sigma widening saturates) and a raw 13.667
    # (where the penalty saturates) the ONLY moving part is this penalty, so the
    # slope is readable exactly: one raw degree costs 0.0075 of extra deduction.
    assert (
        abs(
            (
                band(13.0 * SOURCE_SPREAD_DEBIAS_SCALE)
                - band(12.0 * SOURCE_SPREAD_DEBIAS_SCALE)
            )
            - 0.0075
        )
        < 1e-9
    )
    # The 0.08 cap still lands at a raw 13.667, not at a debiased 13.667.
    saturated = band(13.667 * SOURCE_SPREAD_DEBIAS_SCALE)
    assert saturated > band(13.0 * SOURCE_SPREAD_DEBIAS_SCALE)
    assert abs(band(20.0 * SOURCE_SPREAD_DEBIAS_SCALE) - saturated) < 1e-9


def test_source_spread_sigma_inflation_knee_is_in_debiased_units():
    """The <=1.35x sigma widening is the control the live profile's
    max_source_spread_f comment leans on to size uncertain days down. It binds
    only where the EMOS Gaussian does not overwrite sigma -- i.e. SFO."""

    config = StrategyConfig(min_conditional_samples=20)
    calibrator = ResidualCalibrator(_outcomes(), config)
    markets = standard_sfo_bins()

    def peak(spread: float) -> float:
        rows = calibrator.bucket_probabilities(markets, 69.0, source_spread_f=spread)
        return max(row.normal_probability for row in rows.values())

    knee = 3.0 * SOURCE_SPREAD_DEBIAS_SCALE
    assert abs(peak(knee) - peak(0.0)) < 1e-9  # no widening at or below the knee
    assert peak(6.0 * SOURCE_SPREAD_DEBIAS_SCALE) < peak(knee)  # wider sigma, flatter peak
    # The cap is reached at a raw 11.75 and nothing past it widens further.
    capped = peak(11.75 * SOURCE_SPREAD_DEBIAS_SCALE)
    assert abs(peak(30.0 * SOURCE_SPREAD_DEBIAS_SCALE) - capped) < 1e-9
