from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from .config import StrategyConfig
from .fees import (
    expected_profit_per_yes_contract,
    kelly_fraction_spent,
    quadratic_fee_per_contract,
)
from .models import BucketProbability, MarketBin, TradeDecision


class TradeEvaluator:
    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or StrategyConfig()

    def evaluate_market(
        self,
        market: MarketBin,
        probability: BucketProbability,
        *,
        bankroll: float,
        side: str = "YES",
        source_spread_f: float | None = None,
    ) -> TradeDecision:
        side = _normalize_side(side)
        reasons: list[str] = []
        ask = market.side_ask(side)
        bid = market.side_bid(side)
        bid_size = market.side_bid_size(side)
        ask_size = market.side_ask_size(side)
        spread = market.side_spread(side)
        side_probability = _side_probability(probability, side)
        side_probability_lcb = _side_probability_lcb(probability, side)
        residual_probability = _side_optional_probability(probability.residual_probability, side)
        ensemble_probability = _side_optional_probability(probability.ensemble_probability, side)
        model_probability = _side_model_probability(probability, side)
        market_probability = _side_optional_probability(probability.market_probability, side)
        intraday_probability = _side_optional_probability(probability.intraday_probability, side)

        if market.status != "active":
            reasons.append(f"market status is {market.status}, not active")
        if (
            source_spread_f is not None
            and source_spread_f > self.config.max_source_spread_f + 1e-9
        ):
            reasons.append(
                f"forecast source spread {source_spread_f:.1f}F exceeds max "
                f"{self.config.max_source_spread_f:.1f}F; point blend is unreliable"
            )
        if ask <= 0 or ask >= 1:
            reasons.append(f"{side} ask is not tradeable")
        if market.status == "active":
            if bid < self.config.min_yes_bid:
                reasons.append(f"{side} bid {bid:.2f} below min {self.config.min_yes_bid:.2f}; no exit support")
            if bid_size < self.config.min_yes_bid_size:
                reasons.append(
                    f"{side} bid size {bid_size:.2f} below min {self.config.min_yes_bid_size:.2f}"
                )
            if 0.0 < ask < 1.0 and ask_size < self.config.min_ask_size:
                reasons.append(
                    f"{side} ask size {ask_size:.2f} below min {self.config.min_ask_size:.2f}; "
                    f"no displayed entry liquidity"
                )
        if spread > self.config.max_spread + 1e-9:
            reasons.append(f"spread {spread:.2f} exceeds max {self.config.max_spread:.2f}")
        if 0.0 < ask < 1.0 and spread / ask > self.config.max_spread_fraction_of_cost + 1e-9:
            reasons.append(
                f"spread {spread:.2f} is {spread / ask:.0%} of {side} ask {ask:.2f}; "
                f"exit would start beyond the {self.config.max_spread_fraction_of_cost:.0%} stop band"
            )

        if market_probability is not None:
            model_market_gap = abs(model_probability - market_probability)
            if model_market_gap > self.config.max_model_market_gap + 1e-9:
                reasons.append(
                    f"model/market gap {model_market_gap:.3f} exceeds max {self.config.max_model_market_gap:.3f}"
                )
        if side_probability < self.config.min_posterior_probability:
            reasons.append(
                f"posterior probability {side_probability:.3f} below min "
                f"{self.config.min_posterior_probability:.3f}"
            )

        fee = quadratic_fee_per_contract(
            ask,
            fee_multiplier=self.config.fee_multiplier,
            taker_rate=self.config.taker_fee_rate,
            maker_rate=self.config.maker_fee_rate,
        )
        cost = ask + fee
        if cost >= 1.0:
            reasons.append(f"all-in cost {cost:.2f} meets or exceeds the $1 contract payout")
        # By default the point edge sees the market-blended posterior, which on
        # a liquid market is dragged toward the book and erases the model's
        # disagreement (its edge source) before the gate measures it. When
        # enabled (research profiles), measure the POINT edge against the pure
        # model probability so liquid-market disagreement is not self-cancelled.
        # The lower-bound edge gate intentionally stays on the conservative
        # blended LCB: that is the proven floor against model overconfidence
        # (the 3/190 failure mode), so a deep-negative-LCB trade is still
        # blocked even when the model is bullish. Sizing also stays on the
        # blended, LCB-weighted probability below.
        if self.config.edge_gate_uses_model_probability and model_probability is not None:
            edge_probability = model_probability
        else:
            edge_probability = side_probability
        edge = expected_profit_per_yes_contract(edge_probability, ask, fee)
        edge_lcb = side_probability_lcb - cost

        if edge < self.config.min_edge:
            reasons.append(f"edge {edge:.3f} below min {self.config.min_edge:.3f}")
        if edge_lcb < self.config.min_edge_lcb:
            reasons.append(f"lower-bound edge {edge_lcb:.3f} below min {self.config.min_edge_lcb:.3f}")
        cheap_tail_reasons = _cheap_tail_rejection_reasons(
            side=side,
            ask=ask,
            bid=bid,
            bid_size=bid_size,
            probability=side_probability,
            probability_lcb=side_probability_lcb,
            edge_lcb=edge_lcb,
            model_probability=model_probability,
            market_probability=market_probability,
            ensemble_probability=ensemble_probability,
            config=self.config,
        )
        reasons.extend(cheap_tail_reasons)
        reasons.extend(
            _yes_tail_rejection_reasons(
                side=side,
                cost=cost,
                probability=side_probability,
                edge_lcb=edge_lcb,
                config=self.config,
            )
        )

        trade_quality_score = _trade_quality_score(
            market,
            probability=side_probability,
            probability_lcb=side_probability_lcb,
            bid=bid,
            bid_size=bid_size,
            spread=spread,
            residual_probability=residual_probability,
            ensemble_probability=ensemble_probability,
            market_probability=market_probability,
            observed_high_f=probability.observed_high_f,
            edge=edge,
            edge_lcb=edge_lcb,
            model_probability=model_probability,
            config=self.config,
        )

        sizing_probability = (
            self.config.kelly_lcb_weight * side_probability_lcb
            + (1.0 - self.config.kelly_lcb_weight) * side_probability
        )
        kelly = kelly_fraction_spent(sizing_probability, cost)
        kelly *= self.config.fractional_kelly
        risk_budget = bankroll * self.config.max_position_risk_pct
        kelly_budget = bankroll * kelly
        if kelly_budget <= risk_budget:
            spend_budget, budget_label = kelly_budget, "kelly_budget"
        else:
            spend_budget, budget_label = risk_budget, "position_risk_cap"
        if self.config.yes_estimation_shrink and side == "YES":
            # Size YES off the conservative lower bound, shrunk for estimation
            # error and scaled by payout, then hard-capped at the tighter YES cap.
            yes_kelly = (
                kelly_fraction_spent(side_probability_lcb, cost) * self.config.fractional_kelly
            )
            yes_spend = bankroll * yes_kelly * _yes_sizing_factor(
                side_probability, side_probability_lcb, cost, self.config
            )
            yes_budget = min(yes_spend, bankroll * self.config.yes_max_position_risk_pct)
            if yes_budget < spend_budget:
                spend_budget, budget_label = yes_budget, "yes_estimation_shrink"

        contracts = 0.0
        binding_constraint: str | None = None
        if cost > 0 and not reasons:
            allowances: dict[str, float] = {
                budget_label: spend_budget / cost,
                "max_contracts_per_market": float(self.config.max_contracts_per_market),
            }
            if ask_size > 0:
                allowances["ask_size"] = float(ask_size)
            # The lever with the fewest allowed contracts is what actually caps
            # the size; surface it so the dashboard can distinguish a thin-edge
            # (kelly) throttle from a thin-book (ask_size) or a configured cap.
            binding_constraint = min(allowances, key=allowances.__getitem__)
            contracts = min(allowances.values())
            if not self.config.allow_fractional_contracts:
                # round() instead of int() (truncation) so a raw 1.7 sizes to 2,
                # not 1; int() systematically under-stakes by 13-41%.
                contracts = float(
                    round(contracts) if self.config.round_contracts else int(contracts)
                )
            if contracts <= 0:
                # Distinguish a genuine Kelly-zero (the blended sizing
                # probability has no positive edge over cost -- the intentional
                # gate/sizing split on research profiles) from a budget/cap that
                # merely rounded to zero, so the reason is not misleading.
                if kelly <= 0.0:
                    reasons.append(
                        f"Kelly fraction is zero: blended sizing probability "
                        f"{sizing_probability:.4f} does not exceed all-in cost "
                        f"{cost:.4f}; no positive lower-bound edge to size"
                    )
                else:
                    reasons.append("risk sizing produced zero contracts")
                binding_constraint = None

        expected_profit = edge * contracts
        return TradeDecision(
            ticker=market.ticker,
            label=market.yes_sub_title,
            action=f"BUY_{side}",
            approved=not reasons,
            probability=side_probability,
            probability_lcb=side_probability_lcb,
            yes_bid=market.yes_bid,
            yes_ask=market.yes_ask,
            spread=spread,
            fee_per_contract=fee,
            cost_per_contract=cost,
            edge=edge,
            edge_lcb=edge_lcb,
            kelly_fraction=kelly,
            recommended_contracts=contracts,
            expected_profit=expected_profit,
            reasons=reasons,
            yes_ask_size=market.yes_ask_size,
            side=side,
            entry_bid=bid,
            entry_ask=ask,
            entry_bid_size=bid_size,
            entry_ask_size=ask_size,
            strike_type=market.strike_type,
            floor_strike=market.floor_strike,
            cap_strike=market.cap_strike,
            residual_probability=residual_probability,
            ensemble_probability=ensemble_probability,
            model_probability=model_probability,
            market_probability=market_probability,
            intraday_probability=intraday_probability,
            remaining_heat_risk=probability.remaining_heat_risk,
            trade_quality_score=trade_quality_score,
            binding_constraint=binding_constraint,
        )

    def rank(
        self,
        markets: list[MarketBin],
        probabilities: dict[str, BucketProbability],
        *,
        bankroll: float,
        sides: tuple[str, ...] = ("YES",),
        source_spread_f: float | None = None,
    ) -> list[TradeDecision]:
        normalized_sides = tuple(_normalize_side(side) for side in sides)
        decisions = []
        for market in markets:
            if market.ticker not in probabilities:
                continue
            for side in normalized_sides:
                decisions.append(
                    self.evaluate_market(
                        market,
                        probabilities[market.ticker],
                        bankroll=bankroll,
                        side=side,
                        source_spread_f=source_spread_f,
                    )
                )
        decisions.sort(
            key=lambda decision: (
                decision.approved,
                decision.trade_quality_score,
                decision.edge_lcb,
                decision.edge,
            ),
            reverse=True,
        )
        return _apply_event_risk_cap(decisions, bankroll, self.config)


def _cheap_tail_rejection_reasons(
    *,
    side: str,
    ask: float,
    bid: float,
    bid_size: float,
    probability: float,
    probability_lcb: float,
    edge_lcb: float,
    model_probability: float,
    market_probability: float | None,
    ensemble_probability: float | None,
    config: StrategyConfig,
) -> list[str]:
    if ask <= 0.0 or ask > config.cheap_tail_max_ask:
        return []

    failures: list[str] = []
    if bid < config.cheap_tail_min_yes_bid:
        failures.append(f"bid {bid:.2f}<{config.cheap_tail_min_yes_bid:.2f}")
    if bid_size < config.cheap_tail_min_yes_bid_size:
        failures.append(f"bid size {bid_size:.2f}<{config.cheap_tail_min_yes_bid_size:.2f}")
    if probability_lcb < config.cheap_tail_min_probability_lcb:
        failures.append(
            f"p_lcb {probability_lcb:.3f}<"
            f"{config.cheap_tail_min_probability_lcb:.3f}"
        )
    if edge_lcb < config.cheap_tail_min_edge_lcb:
        failures.append(f"edge_lcb {edge_lcb:.3f}<{config.cheap_tail_min_edge_lcb:.3f}")
    if market_probability is not None:
        model_market_gap = abs(model_probability - market_probability)
        if model_market_gap > config.cheap_tail_max_model_market_gap + 1e-9:
            failures.append(
                f"model/market gap {model_market_gap:.3f}>"
                f"{config.cheap_tail_max_model_market_gap:.3f}"
            )
    if (
        ensemble_probability is not None
        and ensemble_probability < config.cheap_tail_min_ensemble_probability
    ):
        failures.append(
            f"ensemble p {ensemble_probability:.3f}<"
            f"{config.cheap_tail_min_ensemble_probability:.3f}"
        )

    if not failures:
        return []
    return [f"1c/2c tail requires exceptional support ({side}): " + ", ".join(failures)]


def _yes_tail_rejection_reasons(
    *,
    side: str,
    cost: float,
    probability: float,
    edge_lcb: float,
    config: StrategyConfig,
) -> list[str]:
    """Extra YES-side gates: a positive lower-bound edge, and an EV cushion on
    cheap (fee-dominated) YES. YES longshots were the live loss source; these are
    a deliberately conservative prior on the side the engine is worst at."""

    if not (config.yes_estimation_shrink and side.upper() == "YES"):
        return []
    failures: list[str] = []
    if edge_lcb <= 0.0:
        failures.append(f"YES lower-bound edge {edge_lcb:.3f} is not positive")
    if cost < 0.15 and probability < 2.0 * cost:
        failures.append(
            f"cheap YES (cost {cost:.2f}) needs point probability >= 2x cost "
            f"({2.0 * cost:.2f}); have {probability:.2f}"
        )
    return failures


def _yes_sizing_factor(
    probability: float,
    probability_lcb: float,
    cost: float,
    config: StrategyConfig,
) -> float:
    """Baker-McHale estimation-error shrink times payout scale for YES tails.

    Any probability uncertainty makes the growth-optimal bet strictly below naive
    Kelly; the shrink is quadratic in the implied sigma (Baker & McHale, 2013).
    The payout scale (``cost``) sizes a cheap longshot proportionally smaller -- a
    5c YES at ~1/20th of an even-money bet -- which is exactly where the
    favorite-longshot bias and fee drag bite hardest.
    """

    if cost <= 0 or cost >= 1:
        return 0.0
    edge_e = (probability - cost) / cost
    if edge_e <= 0:
        return 0.0
    sigma = max(0.0, (probability - probability_lcb) / config.confidence_z)
    k_shrink = edge_e**2 / (edge_e**2 + (sigma / (1.0 - cost)) ** 2)
    payout_scale = cost
    return k_shrink * payout_scale


def _trade_quality_score(
    market: MarketBin,
    *,
    probability: float,
    probability_lcb: float,
    bid: float,
    bid_size: float,
    spread: float,
    residual_probability: float | None,
    ensemble_probability: float | None,
    market_probability: float | None,
    observed_high_f: float | None,
    edge: float,
    edge_lcb: float,
    model_probability: float,
    config: StrategyConfig,
) -> float:
    if probability <= 0.0:
        return 0.0

    gap = 0.0
    if market_probability is not None:
        gap = abs(model_probability - market_probability)

    ensemble_gap = None
    if residual_probability is not None and ensemble_probability is not None:
        ensemble_gap = abs(residual_probability - ensemble_probability)

    score = 0.0
    score += 22.0 * _unit((edge - config.min_edge) / 0.15)
    score += 22.0 * _unit((edge_lcb - config.min_edge_lcb) / 0.15)
    score += 14.0 * _unit((bid_size - config.min_yes_bid_size) / 49.0)
    score += 10.0 * _unit((bid - config.min_yes_bid) / 0.05)
    score += 10.0 * (1.0 - _unit(spread / max(config.max_spread, 0.01)))
    score += 10.0 * (1.0 - _unit(gap / max(config.max_model_market_gap, 0.01)))
    score += 7.0 * (0.65 if ensemble_gap is None else 1.0 - _unit(ensemble_gap / 0.25))
    score += 3.0 * _time_to_close_quality(market)
    score += 2.0 if observed_high_f is not None else 0.0
    return round(_unit(score / 100.0) * 100.0, 1)


def _normalize_side(side: str) -> str:
    normalized = side.upper()
    if normalized not in {"YES", "NO"}:
        raise ValueError(f"unsupported trade side {side!r}; expected YES or NO")
    return normalized


def _side_probability(probability: BucketProbability, side: str) -> float:
    if side == "YES":
        return probability.probability
    return _unit(1.0 - probability.probability)


def _side_probability_lcb(probability: BucketProbability, side: str) -> float:
    if side == "YES":
        return probability.lower_confidence
    yes_uncertainty = max(0.0, probability.probability - probability.lower_confidence)
    return _unit(1.0 - probability.probability - yes_uncertainty)


def _side_optional_probability(value: float | None, side: str) -> float | None:
    if value is None:
        return None
    if side == "YES":
        return value
    return _unit(1.0 - value)


def _side_model_probability(probability: BucketProbability, side: str) -> float:
    model_probability = probability.model_probability
    if model_probability is None:
        model_probability = probability.probability
    if side == "YES":
        return model_probability
    return _unit(1.0 - model_probability)


def _time_to_close_quality(market: MarketBin) -> float:
    raw = market.raw or {}
    close_time = raw.get("close_time") or raw.get("expected_expiration_time") or raw.get("expiration_time")
    if not close_time:
        return 0.5
    try:
        close_dt = datetime.fromisoformat(str(close_time).replace("Z", "+00:00"))
    except ValueError:
        return 0.5
    hours = (close_dt - datetime.now(UTC)).total_seconds() / 3600.0
    if hours < 0:
        return 0.0
    if hours < 2:
        return 0.35
    if hours < 6:
        return 0.70
    if hours <= 36:
        return 1.0
    if hours <= 72:
        return 0.80
    return 0.55


def _unit(value: float) -> float:
    return max(0.0, min(1.0, value))


def _apply_event_risk_cap(
    decisions: list[TradeDecision],
    bankroll: float,
    config: StrategyConfig,
) -> list[TradeDecision]:
    max_event_spend = bankroll * config.max_event_risk_pct
    if max_event_spend <= 0:
        return [
            replace(decision, recommended_contracts=0.0, expected_profit=0.0)
            if decision.approved
            else decision
            for decision in decisions
        ]
    approved_spend = sum(
        decision.recommended_contracts * decision.cost_per_contract
        for decision in decisions
        if decision.approved
    )
    if approved_spend <= max_event_spend:
        return decisions
    scale = max_event_spend / approved_spend
    return [
        replace(
            decision,
            recommended_contracts=decision.recommended_contracts * scale,
            expected_profit=decision.edge * decision.recommended_contracts * scale,
        )
        if decision.approved
        else decision
        for decision in decisions
    ]
