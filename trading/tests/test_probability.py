from dataclasses import replace
from datetime import date, timedelta

from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.config import StrategyConfig, intraday_timezone_for_city
from sfo_kalshi_quant.models import ForecastOutcome, IntradaySnapshot
from sfo_kalshi_quant.probability import (
    ResidualCalibrator,
    _market_implied_probabilities,
    _market_prior_reliability,
    _intraday_probability_model,
    _local_decimal_hour,
    _model_weight,
    _normalize_weather_probabilities,
)
from sfo_kalshi_quant.standard_bins import standard_sfo_bins


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
    assert low.probability == 0.0
    assert low.model_probability == 0.0
    assert abs(sum(row.probability for row in probabilities.values()) - 1.0) < 1e-9


def test_observed_high_above_half_degree_boundary_rules_out_current_integer_bin():
    config = StrategyConfig(min_conditional_samples=20)
    calibrator = ResidualCalibrator(_outcomes(), config)
    probabilities = calibrator.bucket_probabilities(
        standard_sfo_bins(),
        69.9,
        observed_high_f=69.9,
    )
    current = next(row for row in probabilities.values() if row.label == "68° to 69°")
    next_bin = next(row for row in probabilities.values() if row.label == "70° to 71°")
    assert current.probability == 0.0
    assert current.model_probability == 0.0
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
