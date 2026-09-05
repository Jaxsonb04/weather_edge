from __future__ import annotations

import math
from dataclasses import dataclass, replace

from .config import StrategyConfig
from .fees import quadratic_fee_average_per_contract, quadratic_fee_total
from .models import MarketBin, TradeDecision


@dataclass(frozen=True)
class ArbitrageLeg:
    market: MarketBin
    side: str
    contracts: float
    price: float
    fee_per_contract: float
    cost_per_contract: float
    decision: TradeDecision

    @property
    def spend(self) -> float:
        return self.contracts * self.cost_per_contract


@dataclass(frozen=True)
class ArbitrageOpportunity:
    kind: str
    label: str
    approved: bool
    contracts: float
    payout_per_contract_set: float
    guaranteed_payout: float
    total_spend: float
    guaranteed_profit: float
    return_on_spend: float
    legs: list[ArbitrageLeg]
    reasons: list[str]
    config: StrategyConfig
    min_profit: float

    @property
    def decisions(self) -> list[TradeDecision]:
        return [leg.decision for leg in self.legs]

    def decisions_for_recording(self) -> list[TradeDecision]:
        if self.approved:
            return self.decisions
        opportunity_reasons = [f"arbitrage guardrail: {reason}" for reason in self.reasons]
        return [
            replace(
                leg.decision,
                approved=False,
                recommended_contracts=0.0,
                expected_profit=0.0,
                reasons=[*opportunity_reasons, *leg.decision.reasons],
            )
            for leg in self.legs
        ]

    def with_contracts(self, contracts: float) -> "ArbitrageOpportunity":
        leg_specs = [(leg.market, leg.side) for leg in self.legs]
        return _build_opportunity(
            kind=self.kind,
            label=self.label,
            leg_specs=leg_specs,
            payout_per_contract_set=self.payout_per_contract_set,
            contracts=contracts,
            config=self.config,
            reasons=list(self.reasons),
            min_profit=self.min_profit,
        )


def build_arbitrage_opportunities(
    markets: list[MarketBin],
    *,
    config: StrategyConfig,
    bankroll: float,
    max_spend: float | None = None,
    min_profit: float = 0.01,
) -> list[ArbitrageOpportunity]:
    """Scan active markets for same-bin and full-ladder paper arbitrage.

    Every approved opportunity is an equal-contract portfolio. That equalizes
    the guaranteed payoff for complete-ladder YES sets and same-bin YES/NO
    boxes; for full-ladder NO sets, exactly one NO loses and all other NO legs
    pay out when the ladder coverage is complete.
    """

    if bankroll < 0:
        raise ValueError("bankroll cannot be negative")
    if max_spend is not None and max_spend < 0:
        raise ValueError("max_spend cannot be negative")
    if min_profit < 0:
        raise ValueError("min_profit cannot be negative")

    active_markets = [market for market in markets if market.status == "active"]
    spend_budget = _spend_budget(config, bankroll, max_spend)
    opportunities: list[ArbitrageOpportunity] = []

    for market in active_markets:
        opportunity = _sized_opportunity(
            kind="BOX_YES_NO",
            label=f"{market.yes_sub_title} YES+NO box",
            leg_specs=[(market, "YES"), (market, "NO")],
            payout_per_contract_set=1.0,
            config=config,
            spend_budget=spend_budget,
            min_profit=min_profit,
        )
        if opportunity.approved:
            opportunities.append(opportunity)

    coverage_reasons = _ladder_coverage_reasons(active_markets)
    ordered_markets = sorted(active_markets, key=lambda market: market.sort_key)
    opportunities.append(
        _sized_opportunity(
            kind="FULL_LADDER_YES",
            label="full ladder BUY_YES set",
            leg_specs=[(market, "YES") for market in ordered_markets],
            payout_per_contract_set=1.0,
            config=config,
            spend_budget=spend_budget,
            min_profit=min_profit,
            preflight_reasons=coverage_reasons,
        )
    )
    opportunities.append(
        _sized_opportunity(
            kind="FULL_LADDER_NO",
            label="full ladder BUY_NO set",
            leg_specs=[(market, "NO") for market in ordered_markets],
            payout_per_contract_set=max(0.0, float(len(ordered_markets) - 1)),
            config=config,
            spend_budget=spend_budget,
            min_profit=min_profit,
            preflight_reasons=coverage_reasons,
        )
    )

    opportunities.sort(
        key=lambda opportunity: (
            opportunity.approved,
            opportunity.guaranteed_profit,
            opportunity.return_on_spend,
        ),
        reverse=True,
    )
    return opportunities


def _sized_opportunity(
    *,
    kind: str,
    label: str,
    leg_specs: list[tuple[MarketBin, str]],
    payout_per_contract_set: float,
    config: StrategyConfig,
    spend_budget: float,
    min_profit: float,
    preflight_reasons: list[str] | None = None,
) -> ArbitrageOpportunity:
    reasons = list(preflight_reasons or [])
    reasons.extend(_leg_rejection_reasons(leg_specs))
    max_contracts = _max_contracts(leg_specs, config)
    if max_contracts <= 0:
        reasons.append("sizing produced zero contracts from visible ask depth")
    if spend_budget <= 0:
        reasons.append("arbitrage spend budget is zero")

    contracts = 0.0
    if not reasons:
        contracts = _contracts_for_group_budget(
            leg_specs,
            payout_per_contract_set,
            min(max_contracts, config.max_contracts_per_market),
            spend_budget,
            config,
        )
        if not config.allow_fractional_contracts:
            contracts = float(int(contracts))
        if contracts <= 0:
            reasons.append("arbitrage sizing produced zero whole contracts")

    opportunity = _build_opportunity(
        kind=kind,
        label=label,
        leg_specs=leg_specs,
        payout_per_contract_set=payout_per_contract_set,
        contracts=contracts,
        config=config,
        reasons=reasons,
        min_profit=min_profit,
    )
    if opportunity.approved:
        return opportunity
    return opportunity


def _build_opportunity(
    *,
    kind: str,
    label: str,
    leg_specs: list[tuple[MarketBin, str]],
    payout_per_contract_set: float,
    contracts: float,
    config: StrategyConfig,
    reasons: list[str],
    min_profit: float,
) -> ArbitrageOpportunity:
    payout = contracts * payout_per_contract_set
    legs = [
        _leg_for_contracts(
            market,
            side,
            contracts,
            kind=kind,
            payout_per_contract_set=payout_per_contract_set,
            config=config,
        )
        for market, side in leg_specs
    ]
    total_spend = sum(leg.spend for leg in legs)
    guaranteed_profit = payout - total_spend
    return_on_spend = 0.0 if total_spend <= 0 else guaranteed_profit / total_spend
    final_reasons = list(reasons)
    if contracts > 0 and guaranteed_profit <= 0:
        final_reasons.append(
            f"not profitable after fees: spend ${total_spend:.2f} >= payout ${payout:.2f}"
        )
    elif contracts > 0 and guaranteed_profit < min_profit:
        final_reasons.append(
            f"guaranteed profit ${guaranteed_profit:.2f} below min ${min_profit:.2f}"
        )
    if legs and contracts > 0:
        expected_profit_per_leg = guaranteed_profit / len(legs)
        legs = [
            replace(
                leg,
                decision=replace(
                    leg.decision,
                    edge=return_on_spend,
                    edge_lcb=return_on_spend,
                    expected_profit=expected_profit_per_leg,
                ),
            )
            for leg in legs
        ]

    return ArbitrageOpportunity(
        kind=kind,
        label=label,
        approved=not final_reasons and contracts > 0,
        contracts=contracts,
        payout_per_contract_set=payout_per_contract_set,
        guaranteed_payout=payout,
        total_spend=total_spend,
        guaranteed_profit=guaranteed_profit,
        return_on_spend=return_on_spend,
        legs=legs,
        reasons=final_reasons,
        config=config,
        min_profit=min_profit,
    )


def _leg_for_contracts(
    market: MarketBin,
    side: str,
    contracts: float,
    *,
    kind: str,
    payout_per_contract_set: float,
    config: StrategyConfig,
) -> ArbitrageLeg:
    price = market.side_ask(side)
    fee = quadratic_fee_average_per_contract(
        price,
        contracts,
        maker=False,
        fee_multiplier=config.fee_multiplier,
        taker_rate=config.taker_fee_rate,
        maker_rate=config.maker_fee_rate,
        series_ticker=market.ticker,
    )
    cost = price + fee
    side = side.upper()
    reason = f"{kind} arbitrage leg; portfolio payout per contract set ${payout_per_contract_set:.2f}"
    decision = TradeDecision(
        ticker=market.ticker,
        label=market.yes_sub_title,
        action=f"ARBITRAGE_BUY_{side}",
        approved=contracts > 0,
        probability=0.0,
        probability_lcb=0.0,
        yes_bid=market.yes_bid,
        yes_ask=market.yes_ask,
        spread=market.side_spread(side),
        fee_per_contract=fee,
        cost_per_contract=cost,
        edge=0.0,
        edge_lcb=0.0,
        kelly_fraction=0.0,
        recommended_contracts=contracts,
        expected_profit=0.0,
        reasons=[reason],
        yes_ask_size=market.yes_ask_size,
        side=side,
        entry_bid=market.side_bid(side),
        entry_ask=price,
        entry_bid_size=market.side_bid_size(side),
        entry_ask_size=market.side_ask_size(side),
        strike_type=market.strike_type,
        floor_strike=market.floor_strike,
        cap_strike=market.cap_strike,
        trade_quality_score=100.0 if contracts > 0 else 0.0,
    )
    return ArbitrageLeg(
        market=market,
        side=side,
        contracts=contracts,
        price=price,
        fee_per_contract=fee,
        cost_per_contract=cost,
        decision=decision,
    )


def _spend_budget(config: StrategyConfig, bankroll: float, max_spend: float | None) -> float:
    event_budget = bankroll * config.max_event_risk_pct
    if max_spend is None:
        return max(0.0, event_budget)
    if event_budget <= 0:
        return max(0.0, max_spend)
    return max(0.0, min(event_budget, max_spend))


def _leg_rejection_reasons(leg_specs: list[tuple[MarketBin, str]]) -> list[str]:
    reasons: list[str] = []
    if not leg_specs:
        return ["no active markets available for arbitrage"]
    for market, side in leg_specs:
        side = side.upper()
        ask = market.side_ask(side)
        ask_size = market.side_ask_size(side)
        if market.status != "active":
            reasons.append(f"{market.ticker} market status is {market.status}, not active")
        if ask <= 0.0 or ask >= 1.0:
            reasons.append(f"{market.ticker} {side} ask is not tradeable")
        if ask_size <= 0.0:
            reasons.append(f"{market.ticker} {side} ask size is zero")
    return reasons


def _max_contracts(leg_specs: list[tuple[MarketBin, str]], config: StrategyConfig) -> float:
    if not leg_specs:
        return 0.0
    visible_sizes = [
        market.side_ask_size(side)
        for market, side in leg_specs
        if market.side_ask_size(side) > 0.0
    ]
    if len(visible_sizes) != len(leg_specs):
        return 0.0
    max_contracts = min(visible_sizes)
    max_contracts = min(max_contracts, config.max_contracts_per_market)
    if not config.allow_fractional_contracts:
        max_contracts = float(int(max_contracts))
    return max(0.0, max_contracts)


def _contracts_for_group_budget(
    leg_specs: list[tuple[MarketBin, str]],
    payout_per_contract_set: float,
    max_contracts: float,
    spend_budget: float,
    config: StrategyConfig,
) -> float:
    if max_contracts <= 0 or spend_budget <= 0 or payout_per_contract_set <= 0:
        return 0.0
    lo = 0.0
    hi = max_contracts
    for _ in range(54):
        mid = (lo + hi) / 2.0
        spend = _group_spend(leg_specs, mid, config)
        if spend <= spend_budget:
            lo = mid
        else:
            hi = mid
    return lo


def _group_spend(
    leg_specs: list[tuple[MarketBin, str]],
    contracts: float,
    config: StrategyConfig,
) -> float:
    return sum(
        contracts * market.side_ask(side)
        + quadratic_fee_total(
            market.side_ask(side),
            contracts,
            maker=False,
            fee_multiplier=config.fee_multiplier,
            taker_rate=config.taker_fee_rate,
            maker_rate=config.maker_fee_rate,
            series_ticker=market.ticker,
        )
        for market, side in leg_specs
    )


def _ladder_coverage_reasons(markets: list[MarketBin]) -> list[str]:
    if len(markets) < 2:
        return ["full ladder coverage requires at least two active temperature bins"]

    reasons: list[str] = []
    intervals = []
    for market in sorted(markets, key=lambda row: row.sort_key):
        if market.strike_type not in {"less", "between", "greater"}:
            reasons.append(f"full ladder coverage cannot use {market.ticker} strike type {market.strike_type!r}")
            continue
        lo, hi = market.continuous_interval()
        intervals.append((market, lo, hi))

    if not intervals:
        return reasons or ["full ladder coverage has no usable active temperature bins"]

    first_market, first_lo, _ = intervals[0]
    last_market, _, last_hi = intervals[-1]
    if not math.isinf(first_lo) or first_lo > 0:
        reasons.append(f"full ladder coverage missing lower tail before {first_market.yes_sub_title}")
    if not math.isinf(last_hi) or last_hi < 0:
        reasons.append(f"full ladder coverage missing upper tail after {last_market.yes_sub_title}")

    for (left_market, _, left_hi), (right_market, right_lo, _) in zip(intervals, intervals[1:]):
        if left_hi < right_lo - 1e-9:
            reasons.append(
                "full ladder coverage gap between "
                f"{left_market.yes_sub_title} and {right_market.yes_sub_title}"
            )
        elif left_hi > right_lo + 1e-9:
            reasons.append(
                "full ladder coverage overlap between "
                f"{left_market.yes_sub_title} and {right_market.yes_sub_title}"
            )

    return reasons
