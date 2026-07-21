from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from sfo_kalshi_quant.consensus import MarketConsensus
from sfo_kalshi_quant.db import PaperStore
from sfo_kalshi_quant.models import ForecastSnapshot, IntradaySnapshot, TradeDecision
from sfo_kalshi_quant.prediction_features import build_prediction_feature_snapshot


def _decision() -> TradeDecision:
    return TradeDecision(
        ticker="KXHIGHTSFO-TEST-B70.5",
        label="70 to 71",
        action="BUY_NO",
        side="NO",
        approved=True,
        probability=0.90,
        probability_lcb=0.84,
        yes_bid=0.10,
        yes_ask=0.12,
        entry_bid=0.86,
        entry_ask=0.88,
        entry_bid_size=25,
        entry_ask_size=25,
        spread=0.02,
        fee_per_contract=0.01,
        cost_per_contract=0.89,
        edge=0.01,
        edge_lcb=0.0,
        kelly_fraction=0.02,
        recommended_contracts=5,
        expected_profit=0.05,
        reasons=[],
        strike_type="between",
        floor_strike=70,
        cap_strike=71,
    )


def _forecast() -> ForecastSnapshot:
    return ForecastSnapshot(
        target_date=date(2026, 6, 20),
        predicted_high_f=70.0,
        fetched_at="2026-06-19T12:00:00+00:00",
        lead_hours=18.0,
        method="weatheredge-blend",
        google_high_f=71.0,
        nws_high_f=69.0,
        open_meteo_high_f=70.0,
        history_high_f=67.0,
        station_adjustment_f=-0.5,
        fresh_station_count=4,
        source_count=4,
        raw={
            "marine_layer_index": 0.72,
            "offshore_flow_strength": -4.5,
            "ocean_temp_f": 54.2,
        },
    )


def _consensus() -> MarketConsensus:
    return MarketConsensus(
        available=True,
        implied_high_f=68.0,
        modal_bin_ticker="KXHIGHTSFO-TEST-B68.5",
        modal_bin_label="68 to 69",
        modal_probability=0.34,
        implied_stdev_f=2.1,
        p10_f=65.0,
        p25_f=67.0,
        median_f=68.0,
        p75_f=70.0,
        p90_f=72.0,
        overround=0.03,
        liquid_bin_count=6,
        bins=(),
    )


def test_prediction_feature_snapshot_captures_model_market_station_and_marine_context() -> None:
    intraday = IntradaySnapshot(
        target_date=date(2026, 6, 20),
        observed_high_f=66.0,
        latest_temp_f=64.0,
        latest_observed_at="2026-06-20T18:00:00+00:00",
        remaining_forecast_high_f=69.0,
        forecast_fetched_at="2026-06-20T17:00:00+00:00",
    )

    payload = build_prediction_feature_snapshot(
        _forecast(),
        market_consensus=_consensus(),
        intraday=intraday,
    )

    assert payload["forecast_regime"] == "warm_70_79f"
    assert payload["lead_hours"] == 18.0
    assert payload["source_spread_f"] == 4.0
    assert payload["market_implied_high_delta_f"] == 2.0
    assert payload["station_adjustment_f"] == -0.5
    assert payload["marine_layer_index"] == 0.72
    assert payload["offshore_flow_strength"] == -4.5
    assert payload["ocean_temp_f"] == 54.2
    assert payload["observed_high_gap_f"] == 4.0


def test_decision_snapshots_persist_prediction_feature_context() -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "paper.db"
        store = PaperStore(db_path)
        store.record_decisions(
            "2026-06-20",
            [_decision()],
            forecast=_forecast(),
            intraday=None,
            market_consensus=_consensus(),
            risk_profile="live",
            bankroll=1000.0,
        )
        with store.connect() as conn:
            row = conn.execute(
                "SELECT d.prediction_features_json, c.prediction_features_json "
                "FROM decision_snapshots d JOIN scan_context_snapshots c "
                "ON c.id=d.scan_context_id LIMIT 1"
            ).fetchone()

    assert row[0] is None
    payload = json.loads(row[1])
    assert payload["market_implied_high_delta_f"] == 2.0
    assert payload["source_spread_f"] == 4.0
    assert payload["fresh_station_count"] == 4


# ---------------------------------------------------------------------------
# Task 7: research-only bracket probabilities for the paired Google
# challenger. Pure derived-probability computation from an already-computed
# (mu, sigma) pair -- never called from build_prediction_feature_snapshot or
# any live decision-recording path.
# ---------------------------------------------------------------------------

from sfo_kalshi_quant.models import MarketBin
from sfo_kalshi_quant.prediction_features import (
    build_google_challenger_bracket_probabilities,
)


def _bin(ticker: str, strike_type: str, floor: float | None, cap: float | None) -> MarketBin:
    return MarketBin(
        ticker=ticker,
        event_ticker="KXHIGHTSFO-TEST",
        title="",
        yes_sub_title="",
        strike_type=strike_type,
        floor_strike=floor,
        cap_strike=cap,
        yes_bid=0.0,
        yes_ask=1.0,
        no_bid=0.0,
        no_ask=1.0,
        yes_bid_size=0.0,
        yes_ask_size=0.0,
        status="active",
    )


def test_bracket_probabilities_returns_none_when_the_challenger_has_no_mean():
    markets = [_bin("KXHIGHTSFO-TEST-B70.5", "between", 70, 71)]

    result = build_google_challenger_bracket_probabilities(None, 3.0, markets)

    assert result is None


def test_bracket_probabilities_are_keyed_by_market_ticker_and_sum_near_one():
    # Contiguous brackets (each continuous_interval() abuts the next, no gap
    # or overlap) so their probabilities partition the real line and sum
    # to ~1.0: less-than-70 -> (-inf, 69.5); 70..72 -> (69.5, 72.5);
    # greater-than-72 -> (72.5, inf).
    markets = [
        _bin("KXHIGHTSFO-TEST-T70", "less", None, 70),
        _bin("KXHIGHTSFO-TEST-B70-72", "between", 70, 72),
        _bin("KXHIGHTSFO-TEST-T72", "greater", 72, None),
    ]

    result = build_google_challenger_bracket_probabilities(70.0, 2.0, markets)

    assert result is not None
    assert set(result) == {market.ticker for market in markets}
    assert all(0.0 <= probability <= 1.0 for probability in result.values())
    assert sum(result.values()) == pytest.approx(1.0, abs=1e-6)


def test_prediction_feature_snapshot_never_carries_google_challenger_fields():
    """Isolation: the live decision-recording feature snapshot must never
    gain challenger/probability fields -- those belong only to the separate,
    research-only google_challenger_snapshots evidence table."""

    payload = build_prediction_feature_snapshot(_forecast())

    forbidden = {
        key
        for key in payload
        if "challenger" in key.lower() or "bracket_probabilit" in key.lower()
    }
    assert not forbidden
