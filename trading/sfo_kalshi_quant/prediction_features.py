from __future__ import annotations

import math
from typing import Any, Sequence

from .config import temperature_cohort
from .consensus import MarketConsensus
from .models import ForecastSnapshot, IntradaySnapshot, MarketBin
from .probability import interval_probability_normal


def build_prediction_feature_snapshot(
    forecast: ForecastSnapshot | None,
    *,
    market_consensus: MarketConsensus | None = None,
    intraday: IntradaySnapshot | None = None,
) -> dict[str, object]:
    """Compact feature context recorded next to every trading decision.

    This is not a model by itself. It is the audit trail needed to learn which
    regimes actually improve or hurt growth: lead time, source disagreement,
    market-implied forecast delta, station adjustment, and marine-layer proxies.
    """

    if forecast is None:
        return {}
    payload: dict[str, object] = {
        "forecast_regime": temperature_cohort(forecast.predicted_high_f),
        "predicted_high_f": _round(forecast.predicted_high_f),
        "lead_hours": _round(forecast.lead_hours),
        "source_spread_f": _round(forecast.source_spread_f),
        "source_count": forecast.source_count,
        "google_high_f": _round(forecast.google_high_f),
        "nws_high_f": _round(forecast.nws_high_f),
        "open_meteo_high_f": _round(forecast.open_meteo_high_f),
        "history_high_f": _round(forecast.history_high_f),
        "station_adjustment_f": _round(forecast.station_adjustment_f),
        "fresh_station_count": forecast.fresh_station_count,
    }
    if market_consensus is not None and market_consensus.available:
        payload.update(
            {
                "market_implied_high_f": _round(market_consensus.implied_high_f),
                "market_implied_high_delta_f": _round(
                    market_consensus.gap_to_forecast_f(forecast.predicted_high_f)
                ),
                "market_implied_stdev_f": _round(market_consensus.implied_stdev_f),
                "market_modal_probability": _round(market_consensus.modal_probability),
                "market_overround": _round(market_consensus.overround),
                "market_liquid_bin_count": market_consensus.liquid_bin_count,
            }
        )
    if intraday is not None:
        payload.update(
            {
                "observed_high_f": _round(intraday.observed_high_f),
                "latest_temp_f": _round(intraday.latest_temp_f),
                "remaining_forecast_high_f": _round(intraday.remaining_forecast_high_f),
                "observed_high_gap_f": _round(
                    forecast.predicted_high_f - intraday.observed_high_f
                    if intraday.observed_high_f is not None
                    else None
                ),
                "latest_temp_gap_f": _round(
                    forecast.predicted_high_f - intraday.latest_temp_f
                    if intraday.latest_temp_f is not None
                    else None
                ),
                "intraday_observation_count": intraday.observation_count,
                "intraday_is_complete": intraday.is_complete,
            }
        )
    for name in (
        "marine_layer_index",
        "offshore_flow",
        "offshore_flow_strength",
        "offshore_flow_strength_lag_24h",
        "ocean_temp_f",
        "sea_surface_temp_f",
        "dewpoint_depression",
        "cloud_cover_pct",
    ):
        value = _nested_number(forecast.raw, name)
        if value is not None:
            payload[name] = _round(value)
    return {key: value for key, value in payload.items() if value is not None}


def build_google_challenger_bracket_probabilities(
    mu: float | None,
    sigma: float,
    markets: Sequence[MarketBin],
) -> dict[str, float] | None:
    """Research-only bracket probabilities for the paired Google challenger.

    Pure derived-probability computation: callers pass an already-computed
    (mu, sigma) pair -- e.g. the fixed research challenger's baseline or
    challenger output -- as plain floats, never a raw Google value. Returns
    ``None`` when ``mu`` is ``None`` (the challenger's
    ``external_runtime_corroboration_block`` action, or no markets to price)
    so callers never persist an empty or meaningless probability payload.

    Never called from ``build_prediction_feature_snapshot`` or any live
    decision-recording path -- used only to build the immutable paired
    evidence Task 7 persists in ``google_challenger_snapshots``.
    """

    if mu is None or not markets:
        return None
    probabilities: dict[str, float] = {}
    for market in markets:
        lo, hi = market.continuous_interval()
        probabilities[market.ticker] = interval_probability_normal(mu, sigma, lo, hi)
    return probabilities


def _round(value: float | int | None) -> float | int | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    rounded = round(number, 4)
    if rounded.is_integer():
        return int(rounded) if isinstance(value, int) else float(rounded)
    return rounded


def _nested_number(raw: Any, key: str) -> float | None:
    if isinstance(raw, dict):
        if key in raw:
            return _number_or_none(raw.get(key))
        for value in raw.values():
            found = _nested_number(value, key)
            if found is not None:
                return found
    elif isinstance(raw, list):
        for value in raw:
            found = _nested_number(value, key)
            if found is not None:
                return found
    return None


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
