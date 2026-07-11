"""EMOS-distribution calibrator path: inert by default, EMOS Gaussian when on."""

from datetime import date, timedelta

import sfo_kalshi_quant.probability as probability_module
from sfo_kalshi_quant.config import StrategyConfig, strategy_config_for_profile
from sfo_kalshi_quant.models import ForecastOutcome
from sfo_kalshi_quant.probability import ResidualCalibrator, interval_probability_normal
from sfo_kalshi_quant.standard_bins import standard_sfo_bins


def _outcomes():
    start = date(2025, 1, 1)
    rows = []
    for idx in range(220):
        pred = 66.0 + (idx % 10) * 0.7
        residual = [-3, -2, -1, 0, 1, 2, 3, 4, -1, 1][idx % 10]
        rows.append(ForecastOutcome(local_date=start + timedelta(days=idx), predicted_high_f=pred, actual_high_f=pred + residual))
    return rows


def _calibrator(**overrides):
    return ResidualCalibrator(_outcomes(), StrategyConfig(min_conditional_samples=20, **overrides))


def test_emos_distribution_inert_when_flag_disabled():
    # Default config: supplying an EMOS (mu, sigma) must change NOTHING.
    ladder = standard_sfo_bins()
    cal = _calibrator()
    base = cal.bucket_probabilities(ladder, 69.0)
    with_emos = cal.bucket_probabilities(ladder, 69.0, emos_mu_sigma=(75.0, 4.0))
    for ticker in base:
        assert abs(base[ticker].probability - with_emos[ticker].probability) < 1e-12
        assert abs(base[ticker].normal_probability - with_emos[ticker].normal_probability) < 1e-12


def test_emos_enabled_but_no_forecast_is_identity():
    # Enabling the flag without an EMOS forecast for the day degrades gracefully.
    ladder = standard_sfo_bins()
    on = _calibrator(emos_distribution_enabled=True).bucket_probabilities(ladder, 69.0)
    off = _calibrator().bucket_probabilities(ladder, 69.0)
    for ticker in on:
        assert abs(on[ticker].probability - off[ticker].probability) < 1e-12


def test_emos_distribution_drives_gaussian_when_enabled():
    ladder = standard_sfo_bins()
    mu, sigma = 75.0, 4.0
    probs = _calibrator(emos_distribution_enabled=True).bucket_probabilities(
        ladder, 69.0, emos_mu_sigma=(mu, sigma)
    )
    for market in ladder:
        lo, hi = market.continuous_interval()
        # The normal component is exactly the EMOS Gaussian integrated per bin.
        assert abs(probs[market.ticker].normal_probability - interval_probability_normal(mu, sigma, lo, hi)) < 1e-9
        # The empirical component collapses onto the EMOS normal component (pins
        # the p_emp = p_norm supersession; a regression dropping it survives the
        # normal_probability check alone but fails here).
        assert abs(probs[market.ticker].empirical_probability - probs[market.ticker].normal_probability) < 1e-12
    # Normalized, and clearly different from the residual path centered at 69F.
    assert abs(sum(row.probability for row in probs.values()) - 1.0) < 1e-9
    base = _calibrator().bucket_probabilities(ladder, 69.0)
    assert any(abs(probs[ticker].probability - base[ticker].probability) > 0.01 for ticker in probs)


def test_emos_disabled_on_live_profile_is_bit_identical():
    # Pin the safety contract on the RESOLVED profiles that actually ship, not a
    # hand-built default: live must leave the flag off (-> bit-identical even if an
    # EMOS forecast is supplied), research must turn it on.
    assert strategy_config_for_profile("live").emos_distribution_enabled is False
    assert strategy_config_for_profile("research").emos_distribution_enabled is True

    ladder = standard_sfo_bins()
    cal = ResidualCalibrator(_outcomes(), strategy_config_for_profile("live"))
    base = cal.bucket_probabilities(ladder, 69.0)
    with_emos = cal.bucket_probabilities(ladder, 69.0, emos_mu_sigma=(75.0, 4.0))
    for ticker in base:
        assert abs(base[ticker].probability - with_emos[ticker].probability) < 1e-12
        assert abs(base[ticker].lower_confidence - with_emos[ticker].lower_confidence) < 1e-12
        assert abs(base[ticker].empirical_probability - with_emos[ticker].empirical_probability) < 1e-12


def test_emos_lcb_window_follows_the_emos_center():
    # The edge_lcb band's sample size must reflect support where the EMOS
    # distribution lives, not the blend point: a far-from-history EMOS mean finds
    # little conditional support (falls back to global), so effective_n differs
    # from an in-history EMOS mean. A regression that conditioned on
    # predicted_high_f instead would make effective_n invariant to mu_emos.
    ladder = standard_sfo_bins()
    cal = _calibrator(emos_distribution_enabled=True)
    in_range = next(iter(cal.bucket_probabilities(ladder, 69.0, emos_mu_sigma=(69.0, 3.0)).values()))
    far_off = next(iter(cal.bucket_probabilities(ladder, 69.0, emos_mu_sigma=(120.0, 3.0)).values()))
    assert abs(in_range.effective_n - far_off.effective_n) > 1e-6


def test_intraday_model_centers_on_emos_mean_when_active():
    # Pin the intraday-centering fix: when EMOS is active the intraday model must
    # be centered on mu_emos, not the stale blend point, so the afternoon intraday
    # blend cannot drag the EMOS distribution back toward predicted_high_f.
    captured = {}
    original = probability_module._intraday_probability_model

    def spy(markets, center, intraday, *, config, standard_timezone):
        captured["center"] = center
        return original(
            markets,
            center,
            intraday,
            config=config,
            standard_timezone=standard_timezone,
        )

    probability_module._intraday_probability_model = spy
    try:
        ladder = standard_sfo_bins()
        _calibrator(emos_distribution_enabled=True).bucket_probabilities(ladder, 69.0, emos_mu_sigma=(78.0, 3.0))
        assert abs(captured["center"] - 78.0) < 1e-9  # centered on mu_emos
        _calibrator().bucket_probabilities(ladder, 69.0, emos_mu_sigma=(78.0, 3.0))
        assert abs(captured["center"] - 69.0) < 1e-9  # flag off -> blend point, emos ignored
    finally:
        probability_module._intraday_probability_model = original
