from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from .config import StrategyConfig, temperature_cohort
from .consensus import MarketConsensus
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
        forecast_high_f: float | None = None,
        forecast_sigma_f: float | None = None,
        market_consensus: MarketConsensus | None = None,
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
        # Floor the modelled uncertainty: a day-ahead 2F bin can never be known
        # with zero error (the live calibration gap is ~0.28), yet intraday
        # conditioning or a saturated normal-CDF can collapse the side LCB onto
        # the point estimate -- a literal 1.0 -- which NULLIFIES the edge_lcb gate
        # (the primary defense against model overconfidence) and erases the sizing
        # haircut. Hold the LCB at least min_probability_uncertainty below the
        # point estimate so degenerate certainty is gated and sized as the
        # uncertain bet it is. Capped at the ABSOLUTE bound (1 - u), not relative
        # to the point estimate, so it bites only near-certain extremes and leaves
        # ordinary favorites (LCB ~0.85-0.93) untouched. No-op at 0.0 (the frozen
        # baseline); set per profile.
        if self.config.min_probability_uncertainty > 0.0:
            side_probability_lcb = min(
                side_probability_lcb, 1.0 - self.config.min_probability_uncertainty
            )
        residual_probability = _side_optional_probability(probability.residual_probability, side)
        ensemble_probability = _side_optional_probability(probability.ensemble_probability, side)
        model_probability = _side_model_probability(probability, side)
        market_probability = _side_optional_probability(probability.market_probability, side)
        intraday_probability = _side_optional_probability(probability.intraday_probability, side)

        if market.status != "active":
            reasons.append(f"market status is {market.status}, not active")
        if self.config.blocked_forecast_cohorts:
            # The model is anti-calibrated warm AND under-predicts there (it
            # forecasts ~67F on days that actually hit 75F), so a model-only cohort
            # check never fires on the disguised-warm days that drive the losses.
            # Block on the WARMER of the model forecast and the market-implied
            # consensus high (the crowd is better-calibrated warm and prices money),
            # so genuinely-warm days the model disguises as normal still get fenced
            # off. Falls back to model-only when no usable consensus is present.
            candidate_highs: list[tuple[str, float]] = []
            if forecast_high_f is not None:
                candidate_highs.append(("forecast", forecast_high_f))
            if (
                self.config.regime_block_uses_market_implied_high
                and market_consensus is not None
                and market_consensus.available
                and market_consensus.implied_high_f is not None
            ):
                candidate_highs.append(("market-implied", market_consensus.implied_high_f))
            if candidate_highs:
                regime_source, effective_high = max(candidate_highs, key=lambda pair: pair[1])
                effective_cohort = temperature_cohort(effective_high)
                if effective_cohort in self.config.blocked_forecast_cohorts:
                    reasons.append(
                        f"{regime_source} high {effective_high:.1f}F is in the "
                        f"{effective_cohort} regime, blocked for this profile "
                        f"(forecaster anti-calibrated there pending recalibration)"
                    )
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

        # Comfortable far-tail NO entry: block coin-flip NO bets sitting near the
        # forecast (the documented loss source) and size up genuine far tails.
        # Pure gate+sizing -- the positive after-fee edge_lcb floor above still
        # binds, so this never admits a negative-EV bet.
        comfort_block_reason, comfort_size_multiplier = _comfort_edge_assessment(
            side=side,
            market=market,
            forecast_high_f=forecast_high_f,
            forecast_sigma_f=forecast_sigma_f,
            config=self.config,
        )
        if comfort_block_reason is not None:
            reasons.append(comfort_block_reason)

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
        # Defend Kelly against degenerate certainty: intraday conditioning or a
        # saturated normal-CDF can drive side_probability AND its LCB to a literal
        # 1.0, removing the uncertainty haircut and betting the whole budget on a
        # day-ahead 2F bin (the over-sizing behind the 2026-06-18 NO favorites).
        # Cap the SIZING probability away from the [0, 1] extremes -- this never
        # touches the edge/edge_lcb gate above, so a genuinely safe favorite still
        # trades, it just cannot max-size off false certainty. No-op when
        # min_probability_uncertainty is 0.0 (the frozen baseline).
        uncertainty_floor = self.config.min_probability_uncertainty
        if uncertainty_floor > 0.0:
            sizing_probability = min(
                1.0 - uncertainty_floor, max(uncertainty_floor, sizing_probability)
            )
        kelly = kelly_fraction_spent(sizing_probability, cost)
        kelly *= self.config.fractional_kelly
        risk_budget = bankroll * self.config.max_position_risk_pct
        kelly_budget = bankroll * kelly
        if kelly_budget <= risk_budget:
            spend_budget, budget_label = kelly_budget, "kelly_budget"
        else:
            spend_budget, budget_label = risk_budget, "position_risk_cap"
        # Lean harder into far-tail NO favorites: scale the chosen per-position
        # budget by the comfort multiplier (>= 1.0; exactly 1.0 for every other
        # bet). Applied to the spend budget rather than Kelly so it lifts size
        # whether Kelly or the position-risk cap was the binding constraint.
        # NOTE: this is DELIBERATELY allowed to exceed the nominal per-position
        # risk cap by up to comfort_edge_max_size_boost x -- the per-position cap
        # is the BASE the boost scales, not a ceiling on the result. The true
        # ceilings that still bind are the per-event cap (_apply_event_risk_cap),
        # max_contracts_per_market, and displayed ask_size. The positive
        # after-fee edge_lcb gate already passed, so a larger size is still a
        # positive-EV bet, never a manufactured negative-EV one.
        if comfort_size_multiplier != 1.0:
            spend_budget *= comfort_size_multiplier
            if budget_label == "position_risk_cap":
                budget_label = "comfort_far_tail_boost"
        # Market-consensus guard: don't bet HARD against a confident, liquid
        # market. When our point forecast disagrees with the market-implied
        # consensus high and the ladder is tight + deep + well-formed, the bet's
        # whole edge rides on the model out-forecasting a crowd that put money on
        # the other view -- so shrink the size. It never blocks a trade (the gate
        # decisions above are untouched) and only fires when explicitly enabled.
        consensus_size_multiplier = _consensus_guard_assessment(
            market_consensus=market_consensus,
            forecast_high_f=forecast_high_f,
            config=self.config,
        )
        if consensus_size_multiplier != 1.0:
            spend_budget *= consensus_size_multiplier
            budget_label = "consensus_guard_haircut"
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
        forecast_high_f: float | None = None,
        forecast_sigma_f: float | None = None,
        market_consensus: MarketConsensus | None = None,
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
                        forecast_high_f=forecast_high_f,
                        forecast_sigma_f=forecast_sigma_f,
                        market_consensus=market_consensus,
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


def _interval_gap_f(value: float, interval: tuple[float, float]) -> float:
    """Degrees F between a point forecast and a market bin's interval.

    0 when the forecast sits inside the bin, growing as the bin moves into a
    tail. For an unbounded tail bin the forecast is always on the finite side,
    so the gap is the distance to that finite edge.
    """

    lo, hi = interval
    if lo <= value < hi:
        return 0.0
    if value < lo:
        return lo - value
    return value - hi


def _comfort_edge_assessment(
    *,
    side: str,
    market: MarketBin,
    forecast_high_f: float | None,
    forecast_sigma_f: float | None,
    config: StrategyConfig,
) -> tuple[str | None, float]:
    """(block_reason, size_multiplier) for the comfortable-edge coin-flip rule.

    Both sides are shaped by distance from the point forecast, scaled by the
    day's uncertainty (a multiple of forecast sigma, floored so a calm day cannot
    collapse the band below the irreducible single-model error):
      - NO: a bin comfortably out in the tail -- where a forecast miss of a few F
        cannot reach -- is the high-confidence favorite and is SIZE-BOOSTED;
        a bin near the forecast is a coin-flip and is BLOCKED (the documented
        loss source).
      - YES: a YES bet on the bin at/near the forecast is the diffuse-favorite
        coin-flip (the forecaster puts only ~46% on its own predicted bin and is
        overconfident in the mid-range), so it is BLOCKED inside the same band;
        a far-from-forecast YES is a longshot the normal YES gates already
        handle, so YES is never size-boosted, only coin-flip-blocked.
    Returns (None, 1.0) when the rule is off or inapplicable.
    """

    if not config.comfort_edge_enabled or forecast_high_f is None or side not in ("NO", "YES"):
        return None, 1.0
    sigma = max(config.comfort_edge_sigma_floor_f, forecast_sigma_f or 0.0)
    block_distance = config.comfort_edge_block_sigma_mult * sigma
    distance = _interval_gap_f(forecast_high_f, market.continuous_interval())

    if side == "YES":
        if distance < block_distance:
            return (
                f"comfort-edge: YES bin sits {distance:.1f}F from the {forecast_high_f:.1f}F "
                f"forecast, inside the {block_distance:.1f}F coin-flip band (far longshots only)"
            ), 1.0
        return None, 1.0

    full_distance = config.comfort_edge_full_sigma_mult * sigma
    if distance < block_distance:
        return (
            f"comfort-edge: NO bin sits {distance:.1f}F from the {forecast_high_f:.1f}F "
            f"forecast, inside the {block_distance:.1f}F coin-flip band (far-tail NO only)"
        ), 1.0
    if full_distance <= block_distance:
        fraction = 1.0
    else:
        fraction = (distance - block_distance) / (full_distance - block_distance)
    fraction = _unit(fraction)
    multiplier = 1.0 + fraction * (config.comfort_edge_max_size_boost - 1.0)
    return None, multiplier


def _consensus_guard_assessment(
    *,
    market_consensus: MarketConsensus | None,
    forecast_high_f: float | None,
    config: StrategyConfig,
) -> float:
    """Size multiplier (<= 1.0) for the "don't bet hard against a confident,
    liquid market" guard.

    Returns 1.0 (no effect) unless the guard is enabled AND our point forecast
    disagrees with the market-implied consensus high by at least ``guard_gap_f``
    while the ladder is confident (tight implied spread), liquid (enough
    two-sided bins), and well-formed (small overround). In that case the bet is
    riding on the model out-forecasting a crowd that put real money on a
    different high, so its size is cut to ``guard_size_haircut``. It only shrinks
    size and never adds a rejection reason; like any sizing reduction in this
    engine (e.g. yes_estimation_shrink), a haircut that drives a marginal bet
    below one whole contract surfaces as the usual zero-contracts non-trade, not
    as a guard block. It never creates a bet.
    """

    if not config.market_consensus_guard_enabled:
        return 1.0
    if market_consensus is None or not market_consensus.available:
        return 1.0
    if forecast_high_f is None or market_consensus.implied_high_f is None:
        return 1.0
    if market_consensus.implied_stdev_f is None:
        return 1.0

    gap_f = abs(forecast_high_f - market_consensus.implied_high_f)
    if gap_f < config.market_consensus_guard_gap_f:
        return 1.0
    if market_consensus.implied_stdev_f > config.market_consensus_guard_max_stdev_f:
        return 1.0
    if market_consensus.liquid_bin_count < config.market_consensus_guard_min_bins:
        return 1.0
    # A well-formed book carries implied mass near 1.0; a large |overround| means
    # the ladder is overlapping/inconsistent (positive) or thin/incomplete
    # (negative) -- either way not a confident crowd we should defer to.
    if abs(market_consensus.overround) > config.market_consensus_guard_max_overround:
        return 1.0

    return _unit(config.market_consensus_guard_size_haircut)


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
