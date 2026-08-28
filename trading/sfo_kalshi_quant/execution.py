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
    if config.limit_taker_cross_enabled:
        # Opportunistic taker cross: when the after-fee LOWER-BOUND edge at the
        # displayed ask clears ``limit_taker_cross_min_edge_lcb``, an immediate
        # ask-capped fill realizes the approved edge instead of depending on
        # sparse aggressor flow. Only already-approved candidates reach this
        # point, so no decision gate is bypassed.
        #
        # This runs for a NATURAL cross too (a one-tick spread, where bid+1 is
        # already the ask). That case is a taker fill either way, so gating it
        # on the MAKER reservation buffer only refused the fill outright: in
        # production every approved live candidate on 2026-07-26/27 had a
        # one-tick spread and an after-fee lower-bound edge of 0.002-0.007,
        # so all 23 were approved and then silently never placed.
        taker = _taker_cross_quote(decision, config)
        if taker is not None:
            return taker
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
        if crosses or not config.limit_resting_reservation_fallback:
            return None
        return _reservation_resting_quote(decision, config, inside_price)
    return BuyLimitQuote(
        price=_round_price(price),
        fee_per_contract=fee,
        cost_per_contract=cost,
        edge=edge,
        edge_lcb=edge_lcb,
        would_cross=crosses,
        contracts=float(decision.recommended_contracts),
    )


def _taker_cross_quote(
    decision: TradeDecision,
    config: StrategyConfig,
) -> BuyLimitQuote | None:
    """Whole-contract taker fill at the displayed ask, or None to rest."""

    try:
        ask = float(decision.ask)
        ask_size = float(decision.ask_size)
        contracts = float(decision.recommended_contracts)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not math.isfinite(ask)
        or not math.isfinite(ask_size)
        or not math.isfinite(contracts)
        or not 0.0 < ask < 1.0
    ):
        return None
    contracts = float(math.floor(min(contracts, ask_size) + 1e-12))
    if contracts < 1.0:
        return None
    price = _floor_to_tick(ask, float(config.limit_price_tick))
    fee = quadratic_fee_average_per_contract(
        price,
        contracts,
        maker=False,
        fee_multiplier=config.fee_multiplier,
        taker_rate=config.taker_fee_rate,
        maker_rate=config.maker_fee_rate,
        series_ticker=decision.ticker,
    )
    cost = price + fee
    edge = decision.probability - cost
    edge_lcb = decision.probability_lcb - cost
    if edge_lcb + 1e-12 < config.limit_taker_cross_min_edge_lcb:
        return None
    if contracts * cost + 1e-9 < config.limit_taker_cross_min_notional:
        return None
    return BuyLimitQuote(
        price=_round_price(price),
        fee_per_contract=fee,
        cost_per_contract=cost,
        edge=edge,
        edge_lcb=edge_lcb,
        would_cross=True,
        contracts=contracts,
    )


def _reservation_resting_quote(
    decision: TradeDecision,
    config: StrategyConfig,
    inside_price: float,
) -> BuyLimitQuote | None:
    """Rest at the highest tick that preserves the LCB buffer, or None.

    The maker path's reservation-price rule ("never pay more than the lower
    confidence bound supports after fees and the buffer") previously DROPPED a
    candidate whose bid+1 quote violated the buffer. Resting deeper at a price
    that satisfies the buffer by construction risks nothing new: when it fills
    the position carries at least the buffered lower-bound edge, and when it
    does not the book is exactly where dropping would have left it.
    """

    tick = float(config.limit_price_tick)
    price = _floor_to_tick(inside_price - tick, tick)
    while price >= tick - 1e-12:
        fee = quadratic_fee_average_per_contract(
            price,
            decision.recommended_contracts,
            maker=True,
            fee_multiplier=config.fee_multiplier,
            taker_rate=config.taker_fee_rate,
            maker_rate=config.maker_fee_rate,
            series_ticker=decision.ticker,
        )
        cost = price + fee
        edge_lcb = decision.probability_lcb - cost
        if edge_lcb + 1e-12 >= config.limit_price_edge_lcb_buffer:
            return BuyLimitQuote(
                price=_round_price(price),
                fee_per_contract=fee,
                cost_per_contract=cost,
                edge=decision.probability - cost,
                edge_lcb=edge_lcb,
                would_cross=False,
                contracts=float(decision.recommended_contracts),
            )
        price = _floor_to_tick(price - tick, tick)
    return None


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
    requested_contracts = contracts
    point_probability = (
        float(decision.model_probability)
        if config.edge_gate_uses_model_probability
        and decision.model_probability is not None
        else float(decision.probability)
    )
    inside_price = _floor_to_tick(visible_bid + tick, tick)
    crosses = inside_price >= visible_ask - 1e-12
    if not crosses and config.research_target_taker_cross:
        # The target book's documented floor is exactly non-negative after-fee
        # point and LCB edge. When that floor holds at the displayed ask AND
        # at least the configured executable notional is displayed, an
        # immediate whole-contract taker fill realizes that bounded slice now
        # instead of requiring the thin top level to absorb the ENTIRE policy
        # request. The floor check runs against the exact taker cost of the
        # partial quantity, so signal and risk gates remain unchanged. If the
        # slice is too small or either edge floor fails, the full request keeps
        # the existing maker path.
        try:
            displayed_ask_size = float(decision.ask_size)
        except (TypeError, ValueError, OverflowError):
            displayed_ask_size = 0.0
        if math.isfinite(displayed_ask_size) and displayed_ask_size > 0.0:
            displayed_size = float(
                math.floor(min(contracts, displayed_ask_size) + 1e-12)
            )
        else:
            displayed_size = 0.0
        if displayed_size >= 1.0:
            taker_price = _floor_to_tick(visible_ask, tick)
            taker_fee = quadratic_fee_average_per_contract(
                taker_price,
                displayed_size,
                maker=False,
                fee_multiplier=config.fee_multiplier,
                taker_rate=config.taker_fee_rate,
                maker_rate=config.maker_fee_rate,
                series_ticker=decision.ticker,
            )
            taker_cost = taker_price + taker_fee
            if (
                displayed_size * taker_cost + 1e-9
                >= config.limit_taker_cross_min_notional
                and point_probability - taker_cost >= -1e-12
                and float(decision.probability_lcb) - taker_cost >= -1e-12
            ):
                crosses = True
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
    if (
        crosses
        and config.research_target_taker_cross
        and contracts * cost + 1e-9 < config.limit_taker_cross_min_notional
    ):
        # A natural one-tick cross can be executable yet still fall below the
        # profile's notional floor (for example one 90c contract). Keep the
        # full policy request alive as a maker at the next lower tick instead
        # of bypassing the minimum or dropping the candidate.
        crosses = False
        contracts = requested_contracts
        price = _floor_to_tick(visible_ask - tick, tick)
        if price < tick - 1e-12:
            return None
        fee = quadratic_fee_average_per_contract(
            price,
            contracts,
            maker=True,
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
