from __future__ import annotations

import math
from dataclasses import dataclass, replace

from .config import StrategyConfig
from .fees import quadratic_fee_average_per_contract
from .models import TradeDecision
from .research_entry_risk import target_entry_spend_limit


@dataclass(frozen=True)
class BuyLimitQuote:
    price: float
    fee_per_contract: float
    cost_per_contract: float
    edge: float
    edge_lcb: float
    would_cross: bool
    contracts: float
    # Crossing taker quotes only. The resting depth the immediate paper fill
    # may consume -- the listing's displayed best-ask size, a FRESH ladder's
    # level-1 size, or level-1 + level-2 size for a two-level cross -- and
    # how many ladder levels the price walks (1 or 2). None otherwise.
    displayed_depth: float | None = None
    levels_used: int | None = None


_TWO_LEVEL_REASON_PREFIX = "execution: two-level taker cross"


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
    # Both scans now quote before `record_decisions` (audit TC-15), so a
    # missing or non-numeric book on an approved decision has to degrade to
    # "no quote" rather than raise out of the recording path and lose the
    # whole city's snapshot batch for that tick. `_taker_cross_quote` and
    # `target_research_quote` already guard their conversions this way.
    try:
        visible_ask = float(decision.ask)
        quoted_bid = float(decision.bid)
    except (TypeError, ValueError, OverflowError):
        return None
    # `max(0.0, nan)` is 0.0, so the NaN has to be caught before the clamp or a
    # malformed book would quietly quote as if the bid were zero.
    if not math.isfinite(visible_ask) or not math.isfinite(quoted_bid):
        return None
    visible_bid = max(0.0, quoted_bid)
    if visible_ask <= 0.0 or visible_ask >= 1.0:
        return None
    tick = float(config.limit_price_tick)
    if tick <= 0:
        raise ValueError("limit price tick must be greater than zero")

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
    """Whole-contract taker fill at the displayed ask, or None to rest.

    Depth-aware since 2026-09-13 (``limit_taker_cross_max_levels >= 2``). A
    FRESH pre-entry ask ladder on the decision -- a public orderbook fetched
    after the listing whose best offer is still the displayed ask the
    candidate was approved against (``_fresh_ask_ladder``) -- replaces the
    older listing size as the executable book:

    * fresh level-1 size covers the request: the single-level cross at the
      displayed ask for the whole request; nothing is booked a tick worse;
    * otherwise ONE order at the second ladder level for
      min(request, level-1 + level-2 size) is preferred over the level-1
      cross truncated to the fresh level-1 size, provided the after-fee
      lower-bound edge at the worse price still clears
      ``limit_taker_cross_min_edge_lcb``, the notional floor holds and
      ``_preferred_taker_level`` accepts it. Hard cap at two levels.

    With no fresh ladder (none attached, a stale best price, a malformed
    ladder, or ``limit_taker_cross_max_levels < 2``) this is exactly the
    historical cross truncated to the listing's displayed best-ask size. The
    ladder can never block a trade.
    """

    try:
        ask = float(decision.ask)
        ask_size = float(decision.ask_size)
        requested = float(decision.recommended_contracts)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not math.isfinite(ask)
        or not math.isfinite(ask_size)
        or not math.isfinite(requested)
        or not 0.0 < ask < 1.0
    ):
        return None
    tick = float(config.limit_price_tick)
    ladder = (
        _fresh_ask_ladder(decision, ask=ask, tick=tick)
        if int(config.limit_taker_cross_max_levels) >= 2
        else None
    )
    level_one_depth = ask_size if ladder is None else ladder[0][1]
    level_one = _taker_quote_at_level(
        decision,
        config,
        price=_floor_to_tick(ask, tick),
        contracts=float(math.floor(min(requested, level_one_depth) + 1e-12)),
        displayed_depth=level_one_depth,
        levels_used=1,
    )
    if ladder is None or level_one_depth + 1e-12 >= requested:
        return level_one
    (_, size_one), (price_two, size_two) = ladder
    level_two = _taker_quote_at_level(
        decision,
        config,
        price=_floor_to_tick(price_two, tick),
        contracts=float(math.floor(min(requested, size_one + size_two) + 1e-12)),
        displayed_depth=size_one + size_two,
        levels_used=2,
    )
    return _preferred_taker_level(level_one, level_two)


def _fresh_ask_ladder(
    decision: TradeDecision,
    *,
    ask: float,
    tick: float,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """The first two ask-ladder levels when execution may use them, else None.

    The ladder is a separate fetch from the listing that priced the decision.
    It is used only when its best offer IS the displayed ask the candidate
    was approved against -- anything else is a moved or stale book, and
    mixing two snapshots would price from one and size from the other --
    both levels carry positive size, and the second level is strictly worse.
    """

    ladder = decision.ask_levels
    if not ladder or len(ladder) < 2:
        return None
    try:
        price_one, size_one = (float(value) for value in ladder[0])
        price_two, size_two = (float(value) for value in ladder[1])
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(
        math.isfinite(value) for value in (price_one, size_one, price_two, size_two)
    ):
        return None
    if size_one <= 0.0 or size_two <= 0.0 or not 0.0 < price_two < 1.0:
        return None
    if abs(_floor_to_tick(price_one, tick) - _floor_to_tick(ask, tick)) > 1e-9:
        return None
    if price_two <= price_one + 1e-12:
        return None
    return (price_one, size_one), (price_two, size_two)


def _preferred_taker_level(
    level_one: BuyLimitQuote | None,
    level_two: BuyLimitQuote | None,
) -> BuyLimitQuote | None:
    """Walk to the second level only when it buys more without booking less.

    The paper ledger books EVERY contract of a two-level order at the level-2
    cost (the exchange would fill the level-1 slice at level 1, which is why
    that booking is conservative), so walking is only justified when it buys
    strictly more contracts AND the booked lower-bound expected profit does
    not fall. Trading booked EV for size would make the evidence ledger worse
    than the status quo on thin-edge names.
    """

    if level_two is None:
        return level_one
    if level_one is None:
        return level_two
    if level_two.contracts <= level_one.contracts + 1e-12:
        return level_one
    if (
        level_two.contracts * level_two.edge_lcb + 1e-9
        < level_one.contracts * level_one.edge_lcb
    ):
        return level_one
    return level_two


def _taker_quote_at_level(
    decision: TradeDecision,
    config: StrategyConfig,
    *,
    price: float,
    contracts: float,
    displayed_depth: float,
    levels_used: int,
) -> BuyLimitQuote | None:
    """Exact-fee taker quote for ``contracts`` at ``price``, or None."""

    if contracts < 1.0 or not 0.0 < price < 1.0:
        return None
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
    # Executable-notional floor. At the live value ($1) this refuses a cross
    # whose whole-contract size is worth less than a dollar -- in practice the
    # one-contract-of-displayed-depth case, since one contract of a favorite
    # costs $0.74-0.96 -- and leaves it on the maker path. That is deliberate
    # and measured, not an oversight: see the production fill rates and the
    # entry-slot substitution recorded at
    # LIVE_PROFILE_OVERRIDES["limit_taker_cross_min_notional"]. A two-level
    # order is judged on ITS notional: level-1 + level-2 depth is what turns
    # a sub-$1 one-contract slice into an executable order.
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
        displayed_depth=displayed_depth,
        levels_used=levels_used,
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
    after-fee LCB edge, not the 2-point buffer. Only when the profile ALSO
    enables ``limit_resting_reservation_fallback`` may target execution rest
    at the bid or one tick below it after the improving quote fails either
    edge floor; the research profile keeps that fallback off (see
    ``_target_reservation_resting_quote``), so such a candidate is dropped.

    Because ``research_replay`` and the published backtest re-quote history
    through this function, disabling the fallback also reclassifies the
    research orders it once placed (resting at or below the bid) as
    ``no_trade`` on replay; historical replay figures shift without any
    data change.
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
    if (
        not math.isfinite(point_probability)
        or not 0.0 <= point_probability <= 1.0
        or not math.isfinite(float(decision.probability_lcb))
        or not 0.0 <= float(decision.probability_lcb) <= 1.0
    ):
        return None
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
    cannot_fund_contract = (
        config.research_target_taker_cross
        and target_entry_spend_limit(cost, decision.probability_lcb) + 1e-9 < cost
    )
    if edge < -1e-12 or edge_lcb < -1e-12 or cannot_fund_contract:
        # Rest behind the bid only when the profile opts in. The research
        # profile sets limit_resting_reservation_fallback=False, so this
        # returns None there (2026-09-13); see the fallback's docstring.
        if (
            config.research_target_taker_cross
            and config.limit_resting_reservation_fallback
        ):
            return _target_reservation_resting_quote(
                decision,
                config,
                contracts=requested_contracts,
                point_probability=point_probability,
                visible_bid=visible_bid,
                visible_ask=visible_ask,
            )
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


def _target_reservation_resting_quote(
    decision: TradeDecision,
    config: StrategyConfig,
    *,
    contracts: float,
    point_probability: float,
    visible_bid: float,
    visible_ask: float,
) -> BuyLimitQuote | None:
    """Try the bid and one tick below it after the improving quote fails.

    This bounded fallback preserves both target edge floors at exact maker
    fees. It does not chase a distant reservation price or manufacture a fill;
    the normal queue, public-tape evidence, and resting TTL still apply.

    Gated on ``limit_resting_reservation_fallback`` since 2026-09-13, which
    the research profile sets False. Between 2026-09-12 and then it ran on
    every research quote whose bid+1 failed an edge floor, i.e. exactly the
    thinnest edges (edge_lcb in [-0.02, 0) at bid+1). Resting at or below
    the bid moves AWAY from the seller flow that fills a NO bid. Evidence
    provenance: the seller-flow counts come from the owner's 2026-09-06
    decision re-check on the production tape (33 expired research orders:
    NO-seller prints at <= our price 7, at +1c 14, at +2c 97, 0-10
    contracts queued ahead, so the limiter is absence of flow at our price,
    not queue position); that tape is not in this repository and the counts
    are not reproducible from public data. The expiry share is: the public
    strategy_research.json (2026-09-13T01:20Z) reports 317 of 437 research
    orders expired (72.5%), each having reserved its cost against the daily
    budget for the 15-minute TTL. Every one of those was a TTL expiry: the
    stale-quote guard did not exist yet. From the 2026-09-13 release the goal
    report splits ``ttl_expired_orders`` from ``stale_cancelled_orders``, and
    only the TTL-only count is comparable with this figure. No shipped profile
    reaches this function
    now (live: research_target_taker_cross=False; research: fallback off);
    it stays for a profile that opts in and is covered by opted-in tests.
    """

    tick = float(config.limit_price_tick)
    top_price = min(
        _floor_to_tick(visible_bid, tick),
        _floor_to_tick(visible_ask - tick, tick),
    )
    minimum_price = max(tick, _floor_to_tick(visible_bid - tick, tick))
    for price in (top_price, _floor_to_tick(top_price - tick, tick)):
        if price < minimum_price - 1e-12:
            break
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
        if (
            edge >= -1e-12
            and edge_lcb >= -1e-12
            # A zero (or vanishing) LCB margin cannot fund even one contract
            # under fractional Kelly. Try the remaining permitted tick before
            # handing an unusable reservation quote to the allocator.
            and target_entry_spend_limit(cost, decision.probability_lcb) + 1e-9 >= cost
        ):
            return BuyLimitQuote(
                price=price,
                fee_per_contract=fee,
                cost_per_contract=cost,
                edge=edge,
                edge_lcb=edge_lcb,
                would_cross=False,
                contracts=contracts,
            )
    return None


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
    # A crossing quote is capped at displayed ask depth, so the order that
    # will actually be placed is `quote.contracts`, not the policy request.
    # Reporting the request (audit TC-15) overstated live `expected_profit` by
    # ~20x in decision_snapshots. Mirrors `with_target_research_execution`.
    # Resting quotes carry the full request, so this is a no-op for them.
    clamped = quote.contracts < decision.recommended_contracts
    # The two-level marker is re-derived on every quote (the account-policy
    # fit re-quotes at the final size), so drop a prior one before deciding.
    reasons = [
        reason
        for reason in decision.reasons
        if not reason.startswith(_TWO_LEVEL_REASON_PREFIX)
    ]
    if clamped:
        reasons.append(
            "execution: displayed ask depth capped size "
            f"{decision.recommended_contracts:g} -> {quote.contracts:g}"
            " contracts"
        )
    if quote.levels_used == 2 and decision.ask_levels and len(decision.ask_levels) >= 2:
        (price_one, size_one), (price_two, size_two) = decision.ask_levels[:2]
        reasons.append(
            f"{_TWO_LEVEL_REASON_PREFIX} {price_one:g}x{size_one:g} + "
            f"{price_two:g}x{size_two:g} -> {quote.contracts:g} contracts "
            f"booked at {quote.price:g}"
        )
    return replace(
        decision,
        limit_price=quote.price,
        limit_fee_per_contract=quote.fee_per_contract,
        limit_cost_per_contract=quote.cost_per_contract,
        limit_edge=quote.edge,
        limit_edge_lcb=quote.edge_lcb,
        recommended_contracts=quote.contracts,
        expected_profit=quote.edge * quote.contracts,
        taker_levels_used=quote.levels_used,
        binding_constraint=(
            "visible_ask_depth" if clamped else decision.binding_constraint
        ),
        # Overwriting `recommended_contracts` in place would otherwise make the
        # sizing model's pre-clamp request unrecoverable from the row, and the
        # arithmetic that FOUND this defect (7,561 requested vs 427 executable
        # over 89 live rows) unreproducible from data recorded afterwards.
        # `reasons` is serialized into both `reasons_json` and the
        # `diagnostics_json` signal payload, so this one string preserves the
        # request AND marks which rows use the new definition -- live
        # decision_snapshots carry no strategy/policy fingerprint (both columns
        # are NULL for all 121,248 live rows on 2026-09-06..07), so `created_at`
        # is otherwise the only thing separating the two conventions.
        reasons=reasons if reasons != decision.reasons else decision.reasons,
    )


def _floor_to_tick(value: float, tick: float) -> float:
    return _round_price(math.floor((value + 1e-12) / tick) * tick)


def _round_price(value: float) -> float:
    return round(value + 1e-12, 6)
