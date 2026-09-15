from __future__ import annotations

import math
from dataclasses import dataclass, replace

from .config import StrategyConfig
from .fees import quadratic_fee_average_per_contract, quadratic_fee_total
from .models import BucketProbability, MarketBin, TradeDecision
from .probability import raw_equivalent_source_spread_f
from .risk import TradeEvaluator


@dataclass(frozen=True)
class TailBasketLeg:
    kind: str
    market: MarketBin
    decision: TradeDecision

    @property
    def spend(self) -> float:
        return self.decision.recommended_contracts * self.decision.cost_per_contract


@dataclass(frozen=True)
class TailBasketScenario:
    label: str
    market_ticker: str
    probability: float | None
    pnl: float


@dataclass(frozen=True)
class TailBasket:
    approved: bool
    predicted_high_f: float
    tail_distance_f: float
    plausible_low_f: float
    plausible_high_f: float
    center_label: str | None
    tail_yes_probability: float
    total_spend: float
    expected_profit: float
    worst_case_loss: float
    legs: list[TailBasketLeg]
    scenarios: list[TailBasketScenario]
    reasons: list[str]

    @property
    def decisions(self) -> list[TradeDecision]:
        return [leg.decision for leg in self.legs]

    def decisions_for_recording(self) -> list[TradeDecision]:
        if self.approved:
            return self.decisions
        basket_reasons = [f"basket guardrail: {reason}" for reason in self.reasons]
        return [
            replace(
                decision,
                approved=False,
                recommended_contracts=0.0,
                expected_profit=0.0,
                reasons=[*basket_reasons, *decision.reasons],
            )
            for decision in self.decisions
        ]


def _comfort_edge_sigma_proxy(source_spread_f: float | None) -> float | None:
    """Raw-scale uncertainty proxy for the comfort-edge band.

    ``risk._comfort_edge`` floors this at ``comfort_edge_sigma_floor_f`` (3.0 F)
    and scales the block/full band off it -- constants fitted when
    ``source_spread_f`` was the RAW cross-model range. Passing the debiased
    statistic straight through would shrink the coin-flip block band and inflate
    the far-tail size boost on every city at once.
    """

    if source_spread_f is None:
        return None
    return raw_equivalent_source_spread_f(source_spread_f)


def build_tail_basket(
    markets: list[MarketBin],
    probabilities: dict[str, BucketProbability],
    *,
    predicted_high_f: float,
    evaluator: TradeEvaluator,
    bankroll: float,
    tail_distance_f: float = 3.0,
    tail_stake: float | None = None,
    center_stake: float | None = None,
    max_tail_yes_probability: float = 0.20,
    max_basket_spend: float | None = None,
    max_worst_case_loss: float | None = None,
    source_spread_f: float | None = None,
) -> TailBasket:
    """Build a paper-only basket: far-tail NOs plus optional center YES.

    The basket is deliberately a portfolio guardrail around the existing
    side-aware evaluator. Every leg still has to pass the normal market, fee,
    spread, and confidence gates before it can be placed.
    """

    if tail_distance_f <= 0:
        raise ValueError("tail_distance_f must be positive")
    if tail_stake is not None and tail_stake <= 0:
        raise ValueError("tail_stake must be positive")
    if center_stake is not None and center_stake < 0:
        raise ValueError("center_stake cannot be negative")
    if max_tail_yes_probability < 0:
        raise ValueError("max_tail_yes_probability cannot be negative")

    ordered_markets = sorted(markets, key=lambda market: market.sort_key)
    plausible_low = predicted_high_f - tail_distance_f
    plausible_high = predicted_high_f + tail_distance_f
    reasons: list[str] = []

    center = _center_market(ordered_markets, predicted_high_f)
    tail_markets = _edge_tail_markets(ordered_markets, plausible_low, plausible_high)
    if not tail_markets:
        reasons.append(
            f"no listed edge bucket is fully outside forecast +/- {tail_distance_f:.1f}F"
        )

    legs: list[TailBasketLeg] = []
    tail_yes_probability = 0.0
    for market in tail_markets:
        probability = probabilities.get(market.ticker)
        if probability is None:
            reasons.append(f"missing probability for tail market {market.ticker}")
            continue
        tail_yes_probability += probability.probability
        decision = evaluator.evaluate_market(
            market,
            probability,
            bankroll=bankroll,
            side="NO",
            source_spread_f=source_spread_f,
            # Thread the forecast so the live profile's warm/hot regime block and
            # comfort-edge bind on basket legs too -- otherwise a forecast_high_f
            # of None silently disables both gates and the basket can place NO
            # legs on exactly the anti-calibrated days live is configured to skip.
            forecast_high_f=predicted_high_f,
            # The comfort-edge band's sigma floor/multipliers were tuned against
            # the RAW cross-model spread; FC-1 shrank that statistic, so map it
            # back onto the raw scale before using it as the uncertainty proxy.
            forecast_sigma_f=_comfort_edge_sigma_proxy(source_spread_f),
        )
        decision = _with_budget_stake(decision, tail_stake, evaluator.config)
        legs.append(TailBasketLeg(kind="TAIL_NO", market=market, decision=decision))

    if center is not None and center_stake is not None and center_stake > 0:
        probability = probabilities.get(center.ticker)
        if probability is None:
            reasons.append(f"missing probability for center market {center.ticker}")
        else:
            decision = evaluator.evaluate_market(
                center,
                probability,
                bankroll=bankroll,
                side="YES",
                source_spread_f=source_spread_f,
                # Same forecast threading as the tail legs: the center YES sits at
                # the forecast, so the regime block (and the new YES coin-flip
                # guard) must see forecast_high_f or they silently no-op.
                forecast_high_f=predicted_high_f,
                forecast_sigma_f=_comfort_edge_sigma_proxy(source_spread_f),
            )
            decision = _with_budget_stake(decision, center_stake, evaluator.config)
            legs.append(TailBasketLeg(kind="CENTER_YES", market=center, decision=decision))

    approved_tail_legs = [
        leg
        for leg in legs
        if leg.kind == "TAIL_NO" and leg.decision.approved and leg.decision.recommended_contracts > 0
    ]
    approved_legs = [
        leg
        for leg in legs
        if leg.decision.approved and leg.decision.recommended_contracts > 0
    ]
    if not approved_tail_legs:
        reasons.append("no tail NO leg passed the normal trade gates")
    if tail_yes_probability > max_tail_yes_probability + 1e-12:
        reasons.append(
            f"tail probability {tail_yes_probability:.3f} exceeds max "
            f"{max_tail_yes_probability:.3f}"
        )

    total_spend = sum(leg.spend for leg in approved_legs)
    expected_profit = sum(leg.decision.expected_profit for leg in approved_legs)
    scenarios = _settlement_scenarios(ordered_markets, probabilities, approved_legs)
    worst_case_loss = max(0.0, -min((scenario.pnl for scenario in scenarios), default=0.0))

    if total_spend <= 0:
        reasons.append("basket spend is zero after sizing")
    if max_basket_spend is not None and total_spend > max_basket_spend + 1e-9:
        reasons.append(
            f"basket spend ${total_spend:.2f} exceeds max ${max_basket_spend:.2f}"
        )
    if max_worst_case_loss is not None and worst_case_loss > max_worst_case_loss + 1e-9:
        reasons.append(
            f"worst-case loss ${worst_case_loss:.2f} exceeds max ${max_worst_case_loss:.2f}"
        )

    return TailBasket(
        approved=not reasons,
        predicted_high_f=predicted_high_f,
        tail_distance_f=tail_distance_f,
        plausible_low_f=plausible_low,
        plausible_high_f=plausible_high,
        center_label=center.yes_sub_title if center is not None else None,
        tail_yes_probability=round(tail_yes_probability, 12),
        total_spend=total_spend,
        expected_profit=expected_profit,
        worst_case_loss=worst_case_loss,
        legs=legs,
        scenarios=scenarios,
        reasons=reasons,
    )


def _edge_tail_markets(
    markets: list[MarketBin],
    plausible_low_f: float,
    plausible_high_f: float,
) -> list[MarketBin]:
    lower = []
    upper = []
    for market in markets:
        lo, hi = market.continuous_interval()
        if hi <= plausible_low_f:
            lower.append(market)
        elif lo >= plausible_high_f:
            upper.append(market)
    tails = []
    if lower:
        tails.append(min(lower, key=lambda market: market.sort_key))
    if upper:
        tails.append(max(upper, key=lambda market: market.sort_key))
    return tails


def _center_market(markets: list[MarketBin], predicted_high_f: float) -> MarketBin | None:
    if not markets:
        return None
    return min(
        markets,
        key=lambda market: _distance_to_interval(predicted_high_f, market.continuous_interval()),
    )


def _distance_to_interval(value: float, interval: tuple[float, float]) -> float:
    lo, hi = interval
    if lo <= value < hi:
        return 0.0
    if value < lo:
        return lo - value
    return value - hi


def _with_budget_stake(
    decision: TradeDecision,
    stake_dollars: float | None,
    config: StrategyConfig,
) -> TradeDecision:
    if stake_dollars is None:
        return decision
    if not decision.approved:
        return decision
    contracts = _contracts_for_budget(decision.ask, stake_dollars, config)
    contracts = min(contracts, config.max_contracts_per_market)
    if decision.ask_size > 0:
        contracts = min(contracts, decision.ask_size)
    if not config.allow_fractional_contracts:
        contracts = float(int(contracts))
    if contracts <= 0:
        return replace(
            decision,
            approved=False,
            recommended_contracts=0.0,
            expected_profit=0.0,
            reasons=["basket stake cannot buy one whole contract", *decision.reasons],
        )

    fee_per_contract = quadratic_fee_average_per_contract(
        decision.ask,
        contracts,
        maker=False,
        fee_multiplier=config.fee_multiplier,
        taker_rate=config.taker_fee_rate,
        maker_rate=config.maker_fee_rate,
    )
    cost_per_contract = decision.ask + fee_per_contract
    edge = decision.probability - cost_per_contract
    edge_lcb = decision.probability_lcb - cost_per_contract
    reasons = list(decision.reasons)
    if cost_per_contract >= 1.0:
        reasons.append(f"all-in cost {cost_per_contract:.2f} meets or exceeds the $1 contract payout")
    if edge < config.min_edge:
        reasons.append(f"edge {edge:.3f} below min {config.min_edge:.3f} after basket sizing")
    # Basket legs hold to settlement, so a negative lower-bound edge is exactly
    # the documented failure mode (3/190). Require a non-negative LCB even on
    # profiles whose general min_edge_lcb is negative for data collection.
    basket_edge_lcb_floor = max(0.0, config.min_edge_lcb)
    if edge_lcb < basket_edge_lcb_floor:
        reasons.append(
            f"lower-bound edge {edge_lcb:.3f} below basket floor "
            f"{basket_edge_lcb_floor:.3f} after basket sizing"
        )
    return replace(
        decision,
        approved=not reasons,
        fee_per_contract=fee_per_contract,
        cost_per_contract=cost_per_contract,
        edge=edge,
        edge_lcb=edge_lcb,
        recommended_contracts=contracts,
        expected_profit=edge * contracts,
        reasons=reasons,
    )


def _contracts_for_budget(price: float, budget: float, config: StrategyConfig) -> float:
    if price <= 0 or price >= 1 or budget <= 0:
        return 0.0
    lo = 0.0
    hi = budget / price
    for _ in range(48):
        mid = (lo + hi) / 2.0
        cost = price * mid + quadratic_fee_total(
            price,
            mid,
            maker=False,
            fee_multiplier=config.fee_multiplier,
            taker_rate=config.taker_fee_rate,
            maker_rate=config.maker_fee_rate,
        )
        if cost <= budget:
            lo = mid
        else:
            hi = mid
    return lo


def _settlement_scenarios(
    markets: list[MarketBin],
    probabilities: dict[str, BucketProbability],
    legs: list[TailBasketLeg],
) -> list[TailBasketScenario]:
    scenarios = []
    for market in markets:
        probability = probabilities.get(market.ticker)
        pnl = sum(_leg_pnl_for_market(leg.decision, market) for leg in legs)
        scenarios.append(
            TailBasketScenario(
                label=market.yes_sub_title,
                market_ticker=market.ticker,
                probability=None if probability is None else probability.probability,
                pnl=pnl,
            )
        )
    return scenarios


def _leg_pnl_for_market(decision: TradeDecision, outcome_market: MarketBin) -> float:
    contracts = decision.recommended_contracts
    if contracts <= 0:
        return 0.0
    spent = contracts * decision.cost_per_contract
    outcome_is_this_market = decision.ticker == outcome_market.ticker
    if decision.side == "YES":
        payout = contracts if outcome_is_this_market else 0.0
    else:
        payout = 0.0 if outcome_is_this_market else contracts
    if math.isnan(payout):  # pragma: no cover - defensive guard
        payout = 0.0
    return payout - spent
