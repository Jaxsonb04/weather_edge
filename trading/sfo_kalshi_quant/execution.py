from __future__ import annotations

import math
from dataclasses import dataclass, replace

from .config import StrategyConfig
from .fees import quadratic_fee_average_per_contract
from .models import TradeDecision


@dataclass(frozen=True)
class BuyLimitQuote:
    price: float
    fee_per_contract: float
    cost_per_contract: float
    edge: float
    edge_lcb: float
    would_cross: bool
    contracts: float


def initial_queue_ahead(
    limit_price: float,
    visible_bid: float | None,
    displayed_bid_size: float | None,
) -> float:
    """Return known queue ahead when posting a buy limit.

    Improving the visible bid creates a new best price with no displayed queue
    known ahead of it. At the visible bid, its displayed size is ahead. A limit
    below the visible bid conservatively retains that size as known liquidity
    at a better price. Missing bid evidence preserves the displayed-depth
    estimate rather than inventing priority.
    """

    depth = max(0.0, float(displayed_bid_size or 0.0))
    if visible_bid is None:
        return depth
    if _round_price(float(limit_price)) > _round_price(float(visible_bid)):
        return 0.0
    return depth


def buy_limit_for_decision(
    decision: TradeDecision,
    config: StrategyConfig,
) -> BuyLimitQuote | None:
    """Return the highest conservative buy limit that preserves LCB edge.

    The rule is a reservation-price calculation: never pay more than the
    probability lower confidence bound can support after fees and the configured
    edge buffer. When the spread is wider than one tick, prefer one tick of price
    improvement over immediately crossing the visible ask.
    """

    if not decision.approved or decision.recommended_contracts <= 0:
        return None
    visible_ask = float(decision.ask)
    if visible_ask <= 0.0 or visible_ask >= 1.0:
        return None
    tick = float(config.limit_price_tick)
    if tick <= 0:
        raise ValueError("limit price tick must be greater than zero")

    visible_bid = max(0.0, float(decision.bid))
    inside_price = _floor_to_tick(visible_bid + tick, tick)
    crosses = inside_price >= visible_ask - 1e-12
    price = _floor_to_tick(visible_ask if crosses else inside_price, tick)
    fee = quadratic_fee_average_per_contract(
        price,
        decision.recommended_contracts,
        maker=not crosses,
        fee_multiplier=config.fee_multiplier,
        taker_rate=config.taker_fee_rate,
        maker_rate=config.maker_fee_rate,
        series_ticker=decision.ticker,
    )
    cost = price + fee
    edge = decision.probability - cost
    edge_lcb = decision.probability_lcb - cost
    if edge_lcb + 1e-12 < config.limit_price_edge_lcb_buffer:
        return None
    return BuyLimitQuote(
        price=_round_price(price),
        fee_per_contract=fee,
        cost_per_contract=cost,
        edge=edge,
        edge_lcb=edge_lcb,
        would_cross=crosses,
        contracts=float(decision.recommended_contracts),
    )


def target_research_quote(
    decision: TradeDecision,
    config: StrategyConfig,
) -> BuyLimitQuote | None:
    """Canonical target-sleeve quote with a zero LCB-edge floor.

    Prefer a one-tick improving maker quote when the spread permits it.  When
    that price would cross, take only whole contracts at the visible ask,
    downsized to displayed depth before fees are recomputed.  Unlike the legacy
    generic limit policy, the target research floor is exactly non-negative
    after-fee LCB edge, not the 2-point buffer.
    """

    if not decision.approved or decision.recommended_contracts <= 0:
        return None
    try:
        contracts = float(decision.recommended_contracts)
        visible_ask = float(decision.ask)
        visible_bid = max(0.0, float(decision.bid))
        tick = float(config.limit_price_tick)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not math.isfinite(contracts)
        or not math.isfinite(visible_ask)
        or not math.isfinite(visible_bid)
        or not math.isfinite(tick)
        or not 0.0 < visible_ask < 1.0
        or contracts <= 0
        or tick <= 0
    ):
        return None
    point_probability = (
        float(decision.model_probability)
        if config.edge_gate_uses_model_probability
        and decision.model_probability is not None
        else float(decision.probability)
    )
    inside_price = _floor_to_tick(visible_bid + tick, tick)
    crosses = inside_price >= visible_ask - 1e-12
    if crosses:
        try:
            ask_size = float(decision.ask_size)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(ask_size):
            return None
        contracts = float(math.floor(min(contracts, ask_size)))
        if contracts < 1.0:
            return None
        price = _floor_to_tick(visible_ask, tick)
    else:
        price = inside_price
    fee = quadratic_fee_average_per_contract(
        price,
        contracts,
        maker=not crosses,
        fee_multiplier=config.fee_multiplier,
        taker_rate=config.taker_fee_rate,
        maker_rate=config.maker_fee_rate,
        series_ticker=decision.ticker,
    )
    cost = price + fee
    edge = point_probability - cost
    edge_lcb = float(decision.probability_lcb) - cost
    if edge < -1e-12 or edge_lcb < -1e-12:
        return None
    return BuyLimitQuote(
        price=_round_price(price),
        fee_per_contract=fee,
        cost_per_contract=cost,
        edge=edge,
        edge_lcb=edge_lcb,
        would_cross=crosses,
        contracts=contracts,
    )


def with_buy_limit(
    decision: TradeDecision,
    config: StrategyConfig,
) -> TradeDecision:
    quote = buy_limit_for_decision(decision, config)
    if quote is None:
        return replace(
            decision,
            approved=False,
            recommended_contracts=0.0,
            expected_profit=0.0,
            reasons=[
                *decision.reasons,
                (
                    "no buy-limit price preserves lower-bound edge "
                    f"{config.limit_price_edge_lcb_buffer:.3f} after fees"
                ),
            ],
        )
    return replace(
        decision,
        limit_price=quote.price,
        limit_fee_per_contract=quote.fee_per_contract,
        limit_cost_per_contract=quote.cost_per_contract,
        limit_edge=quote.edge,
        limit_edge_lcb=quote.edge_lcb,
        expected_profit=quote.edge * decision.recommended_contracts,
    )


def _floor_to_tick(value: float, tick: float) -> float:
    return _round_price(math.floor((value + 1e-12) / tick) * tick)


def _round_price(value: float) -> float:
    return round(value + 1e-12, 6)
