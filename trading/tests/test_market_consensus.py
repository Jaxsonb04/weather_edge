"""Kalshi market-implied consensus extraction: de-vig the bin ladder into a
coherent forecast (implied high, distribution, percentiles, confidence)."""

import math
from dataclasses import replace

import pytest

from datetime import datetime, timezone

from sfo_kalshi_quant.consensus import apply_market_headline, build_market_consensus, ladder_mean
from sfo_kalshi_quant.models import MarketBin
from sfo_kalshi_quant.probability import (
    market_implied_probabilities,
    market_implied_yes_value,
)


def _bin(label, strike_type, floor, cap, yes_bid, yes_ask, *, size=150, two_sided=True, last=None):
    no_bid = round(1.0 - yes_ask, 2) if two_sided else 0.0
    no_ask = round(1.0 - yes_bid, 2) if two_sided else 1.0
    return MarketBin.from_kalshi(
        {
            "ticker": f"KXHIGHTSFO-26JUN20-{label}",
            "event_ticker": "KXHIGHTSFO-26JUN20",
            "title": "",
            "yes_sub_title": label,
            "strike_type": strike_type,
            "floor_strike": floor,
            "cap_strike": cap,
            "yes_bid_dollars": yes_bid,
            "yes_ask_dollars": yes_ask,
            "no_bid_dollars": no_bid,
            "no_ask_dollars": no_ask,
            "yes_bid_size_fp": size,
            "yes_ask_size_fp": size,
            "last_price_dollars": last,
            "status": "active",
        }
    )


def _peaked_ladder():
    """A tight 2F-wide ladder peaking on the 67-68 bin."""

    return [
        _bin("T63", "less", 63, 63, 0.01, 0.03),
        _bin("B64", "between", 63, 64, 0.04, 0.06),
        _bin("B66", "between", 65, 66, 0.14, 0.16),
        _bin("B68", "between", 67, 68, 0.40, 0.43),
        _bin("B70", "between", 69, 70, 0.20, 0.23),
        _bin("B72", "between", 71, 72, 0.06, 0.08),
        _bin("G73", "greater", 73, 73, 0.01, 0.03),
    ]


def test_empty_ladder_is_unavailable():
    consensus = build_market_consensus([])
    assert consensus.available is False
    assert consensus.implied_high_f is None
    assert consensus.median_f is None
    # gap helper degrades gracefully when there is nothing to compare against.
    assert consensus.gap_to_forecast_f(70.0) is None


def test_no_book_ladder_is_unavailable():
    # Every bin one-sided with no real quote -> no implied distribution.
    flat = [_bin("B68", "between", 67, 68, 0.0, 1.0, two_sided=False)]
    consensus = build_market_consensus(flat)
    assert consensus.available is False


def test_peaked_ladder_distribution_is_coherent():
    consensus = build_market_consensus(_peaked_ladder())
    assert consensus.available is True

    # Mass de-vigs to a proper distribution.
    total = sum(b.implied_probability for b in consensus.bins)
    assert abs(total - 1.0) < 1e-9

    # The implied high lands in the body, just under the 67-68 modal bin.
    assert 67.0 <= consensus.implied_high_f <= 68.5
    assert consensus.modal_bin_label == "B68"
    assert consensus.modal_probability > 0.30

    # A tight ladder reads as a confident (small) implied spread.
    assert 0.0 < consensus.implied_stdev_f < 4.0

    # Percentiles are monotone and bracket the median.
    assert (
        consensus.p10_f
        < consensus.p25_f
        < consensus.median_f
        < consensus.p75_f
        < consensus.p90_f
    )
    assert abs(consensus.median_f - consensus.implied_high_f) < 2.0


def test_gap_to_forecast_is_signed_model_minus_market():
    consensus = build_market_consensus(_peaked_ladder())
    warm_gap = consensus.gap_to_forecast_f(consensus.implied_high_f + 3.0)
    cool_gap = consensus.gap_to_forecast_f(consensus.implied_high_f - 3.0)
    assert warm_gap > 0  # our model warmer than the market
    assert cool_gap < 0  # our model cooler than the market
    assert abs(warm_gap - 3.0) < 1e-6


def test_single_bin_is_a_degenerate_point():
    consensus = build_market_consensus([_bin("B68", "between", 67, 68, 0.40, 0.43)])
    assert consensus.available is True
    # One bin -> all mass there: zero spread, median at the bin center.
    assert consensus.implied_stdev_f == 0.0
    assert math.isclose(consensus.median_f, consensus.implied_high_f, abs_tol=0.6)
    assert consensus.modal_probability == 1.0


def test_open_tails_do_not_blow_up_the_mean():
    # A ladder with heavy open-tail bins must still produce a finite, sane mean
    # (tails are placed one bin-width inside the boundary, not at +/- infinity).
    ladder = [
        _bin("T60", "less", 60, 60, 0.30, 0.33),
        _bin("B62", "between", 61, 62, 0.30, 0.33),
        _bin("G63", "greater", 63, 63, 0.30, 0.33),
    ]
    consensus = build_market_consensus(ladder)
    assert consensus.available is True
    assert math.isfinite(consensus.implied_high_f)
    assert 55.0 < consensus.implied_high_f < 67.0


def test_two_sided_liquidity_is_counted():
    ladder = [
        _bin("B66", "between", 65, 66, 0.30, 0.33),  # two-sided
        _bin("B68", "between", 67, 68, 0.0, 1.0, two_sided=False),  # no book
    ]
    consensus = build_market_consensus(ladder)
    assert consensus.liquid_bin_count == 1


def test_forecast_tracks_last_trade_over_mid():
    # Kalshi volume-weights its headline forecast toward the last trade. Two bins
    # with identical bid/ask: mid-only weights them equally, but last trades that
    # favor the hotter bin must pull the reconstructed forecast hotter.
    mid_only = build_market_consensus(
        [
            _bin("B66", "between", 65, 66, 0.30, 0.34),
            _bin("B70", "between", 69, 70, 0.30, 0.34),
        ]
    )
    last_based = build_market_consensus(
        [
            _bin("B66", "between", 65, 66, 0.30, 0.34, last=0.10),
            _bin("B70", "between", 69, 70, 0.30, 0.34, last=0.80),
        ]
    )
    assert last_based.implied_high_f > mid_only.implied_high_f + 1.0
    # No last trade -> falls back to the bid/ask mid (so other tests are stable).
    no_trade = build_market_consensus([_bin("B68", "between", 67, 68, 0.40, 0.43)])
    assert no_trade.available is True


def test_public_devig_aliases_match_private_path():
    ladder = _peaked_ladder()
    probabilities = market_implied_probabilities(ladder)
    assert abs(sum(probabilities.values()) - 1.0) < 1e-9
    # Single-bin alias returns a value in (0, 1) for a real two-sided quote.
    value = market_implied_yes_value(ladder[3])
    assert value is not None and 0.0 < value < 1.0


def _three_bracket_ladder(tail_lo=(0.19, 0.21), tail_hi=(0.19, 0.21), mid=(0.59, 0.61)):
    return [
        _bin("T63", "less", 63, 63, *tail_lo),
        _bin("B64", "between", 63, 64, *mid),
        _bin("G73", "greater", 73, 73, *tail_hi),
    ]


def test_ladder_mean_normalises_mids_and_centres_tails_three_degrees_out():
    mean, sd = ladder_mean(_three_bracket_ladder())
    # Centres: 62.5 - 3, (62.5 + 64.5) / 2, 73.5 + 3 with weights .2 / .6 / .2.
    assert mean == pytest.approx(65.3, abs=0.05)
    assert sd == pytest.approx(math.sqrt(33.76 - 1 / 3), abs=0.05)


def test_ladder_mean_sd_is_floored_at_half_a_degree():
    ladder = _three_bracket_ladder(tail_lo=(0.0, 0.002), tail_hi=(0.0, 0.002), mid=(0.99, 0.995))
    assert ladder_mean(ladder)[1] == pytest.approx(0.5)


def test_ladder_mean_needs_both_active_tails():
    full = _three_bracket_ladder()
    assert ladder_mean(full[:2]) is None  # no top tail
    assert ladder_mean(full[1:]) is None  # no bottom tail
    inactive_tail = replace(full[2], status="finalized")
    assert ladder_mean([*full[:2], inactive_tail]) is None
    assert ladder_mean([]) is None


def test_headline_is_market_with_model_kept_and_falls_back_without_ladder():
    forecast = {"predicted_high_f": 70.0, "sigma_f": 2.0}
    ladder = ladder_mean(_three_bracket_ladder())
    market = apply_market_headline(forecast, ladder)
    assert market["forecast_source"] == "market"
    assert market["predicted_high_f"] == round(ladder[0], 2) == market["market_implied_high_f"]
    assert market["sigma_f"] == round(ladder[1], 2)
    assert (market["model_high_f"], market["model_sigma_f"]) == (70.0, 2.0)
    model = apply_market_headline(forecast, None)
    assert model["forecast_source"] == "model"
    assert model["predicted_high_f"] == 70.0
    assert model["market_implied_high_f"] is None


def test_headline_floors_at_observed_high_only_from_a_fresh_observation():
    now = datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc)
    ladder = (65.0, 2.0)
    fresh = {"observed_high_f": 70.0, "latest_observed_at": "2026-07-09T19:30:00+00:00"}
    stale = {"observed_high_f": 70.0, "latest_observed_at": "2026-07-09T17:00:00+00:00"}
    floored = apply_market_headline({"predicted_high_f": 68.0}, ladder, intraday=fresh, now=now)
    assert floored["predicted_high_f"] == 69.5
    assert floored["market_implied_high_f"] == 65.0  # the raw market mean stays visible
    assert apply_market_headline({"predicted_high_f": 68.0}, ladder, intraday=stale, now=now)[
        "predicted_high_f"
    ] == 65.0
