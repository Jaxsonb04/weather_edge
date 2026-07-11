"""edge_scan internals: maker post pricing, bin probability, sample-size math."""

import math
import sqlite3
from pathlib import Path

from sfo_kalshi_quant.edge_scan import (
    _bin_yes_probability,
    _load_live_emos,
    _maker_post_price,
    required_sample_size,
    summarize,
)
from sfo_kalshi_quant.models import MarketBin


def _bin(strike_type="between", floor=84, cap=85):
    payload = {
        "ticker": "T-B84.5",
        "event_ticker": "T",
        "title": "84-85",
        "yes_sub_title": "84 to 85",
        "strike_type": strike_type,
        "yes_bid_dollars": "0.7000",
        "yes_ask_dollars": "0.7400",
        "no_bid_dollars": "0.2600",
        "no_ask_dollars": "0.3000",
        "yes_bid_size_fp": "50",
        "yes_ask_size_fp": "50",
        "status": "active",
    }
    if floor is not None:
        payload["floor_strike"] = floor
    if cap is not None:
        payload["cap_strike"] = cap
    return MarketBin.from_kalshi(payload)


def test_maker_post_improves_bid_inside_wide_spread_and_joins_tight_spread():
    assert _maker_post_price(0.70, 0.74) == 0.71  # improve by one tick
    assert _maker_post_price(0.73, 0.74) == 0.73  # join the bid at one tick
    assert _maker_post_price(0.0, 0.74) is None  # empty bid: nothing to join
    assert _maker_post_price(0.99, 1.0) is None  # untradeable ask


def test_bin_probability_integrates_the_gaussian_over_the_bin():
    market = _bin()
    # mu centered on the bin: substantial probability; far mu: none. The bin
    # covers [84, 86) in continuous space (84-85 integer settles).
    p_center = _bin_yes_probability(market, 85.0, 1.5)
    p_far = _bin_yes_probability(market, 60.0, 1.5)
    assert 0.3 < p_center < 0.8
    assert p_far < 1e-6

    tail = _bin("greater", floor=87, cap=None)
    p_tail = _bin_yes_probability(tail, 92.0, 2.0)
    assert p_tail > 0.9


def test_required_sample_size_matches_the_variance_math():
    assert required_sample_size(0.026, 0.33) == math.ceil((1.96 * 0.33 / 0.026) ** 2)
    assert required_sample_size(0.0, 0.33) == -1


def test_summarize_counts_and_percentiles():
    results = [
        {
            "city": "nyc",
            "events": 2,
            "error": None,
            "candidates": [
                {
                    "target_date": "2026-07-08",
                    "spread": 0.02,
                    "maker_vs_taker_saving": 0.015,
                    "model_edge_after_maker_fee": 0.03,
                },
                {
                    "target_date": "2026-07-08",
                    "spread": 0.04,
                    "maker_vs_taker_saving": 0.025,
                    "model_edge_after_maker_fee": -0.01,
                },
            ],
        },
        {"city": "chi", "events": 0, "error": "URLError: x", "candidates": []},
    ]
    summary = summarize(results)
    assert summary["favorite_band_candidates"] == 2
    assert summary["candidates_positive_model_edge"] == 1
    assert summary["cities_with_errors"] == ["chi"]
    assert summary["distinct_city_days"] == 1
    assert summary["model_edge_mean"] == 0.01
    assert summary["sample_size_note"]["n_at_literature_priors"] == 619


def test_offline_edge_scan_prefers_live_then_v2_then_v1(tmp_path: Path):
    db = tmp_path / "weather.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE forecast_emos_daily_high ("
            "station_id TEXT, target_date TEXT, predicted_high_f REAL, "
            "sigma_f REAL, fetched_at TEXT, source TEXT)"
        )
        conn.executemany(
            "INSERT INTO forecast_emos_daily_high VALUES ('KSFO', ?, ?, 2, ?, ?)",
            [
                ("2026-07-09", 68, "2026-07-09T09:00:00+00:00", "live"),
                ("2026-07-09", 88, "2026-07-09T12:00:00+00:00", "rolling_origin_v2"),
                ("2026-07-10", 69, "2026-07-09T09:00:00+00:00", "rolling_origin_v2"),
                ("2026-07-10", 99, "2026-07-09T12:00:00+00:00", "rolling_origin"),
                ("2026-07-11", 70, "2026-07-09T12:00:00+00:00", "rolling_origin"),
            ],
        )

    assert _load_live_emos(db, "KSFO") == {
        "2026-07-09": (68.0, 2.0),
        "2026-07-10": (69.0, 2.0),
        "2026-07-11": (70.0, 2.0),
    }
