from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, replace

from .arbitrage import ArbitrageOpportunity
from .config import normalize_risk_profile_name
from .joint_kelly import build_joint_positions, joint_kelly_fractions
from .models import MarketBin, TradeDecision


@dataclass(frozen=True)
class PortfolioLimits:
    risk_profile: str
    bankroll: float
    max_daily_loss: float
    yes_sleeve: float
    explore_sleeve: float


@dataclass(frozen=True)
class PortfolioLeg:
    sleeve: str
    decision: TradeDecision
    spend: float
    expected_profit: float
    growth_score: float


@dataclass(frozen=True)
class PortfolioPlan:
    run_id: str
    risk_profile: str
    approved: bool
    legs: list[PortfolioLeg]
    arbitrage_opportunities: list[ArbitrageOpportunity]
    total_spend: float
    worst_case_loss: float
    expected_profit: float
    reasons: list[str]
    limits: PortfolioLimits

    @property
    def decisions(self) -> list[TradeDecision]:
        return [leg.decision for leg in self.legs]


def portfolio_limits_for_profile(profile: str | None, bankroll: float) -> PortfolioLimits:
    normalized = normalize_risk_profile_name(profile)
    bankroll = max(0.0, float(bankroll))
    if normalized == "research":
        return PortfolioLimits(
            risk_profile=normalized,
            bankroll=bankroll,
            max_daily_loss=bankroll * 0.25,
            yes_sleeve=bankroll * 0.25 * 0.20,
            explore_sleeve=bankroll * 0.25 * 0.05,
        )
    return PortfolioLimits(
        risk_profile="live",
        bankroll=bankroll,
        max_daily_loss=bankroll * 0.08,
        yes_sleeve=bankroll * 0.08 * 0.05,
        explore_sleeve=0.0,
    )


def allocate_portfolio(
    decisions: list[TradeDecision],
    *,
    arbitrage_opportunities: list[ArbitrageOpportunity] | None = None,
    bankroll: float,
    risk_profile: str | None = None,
    run_id: str | None = None,
    bin_yes_probs: dict[str, float] | None = None,
    joint_kelly_enabled: bool = False,
) -> PortfolioPlan:
    limits = portfolio_limits_for_profile(risk_profile, bankroll)
    run_id = run_id or f"PF-{uuid.uuid4().hex[:12]}"
    reasons: list[str] = []
    selected: list[PortfolioLeg] = []
    selected_arbitrage: list[ArbitrageOpportunity] = []
    directional_spend = 0.0
    yes_spend = 0.0
    explore_spend = 0.0

    for opportunity in sorted(
        arbitrage_opportunities or [],
        key=lambda row: (row.guaranteed_profit, row.return_on_spend),
        reverse=True,
    ):
        if not opportunity.approved:
            continue
        candidate_legs = [
            _portfolio_leg(
                _tag_decision(decision, run_id=run_id, sleeve="arbitrage", growth_score=opportunity.return_on_spend),
                sleeve="arbitrage",
                growth_score=opportunity.return_on_spend,
            )
            for decision in opportunity.decisions
        ]
        if _worst_case_loss([*selected, *candidate_legs]) <= limits.max_daily_loss + 1e-9:
            selected.extend(candidate_legs)
            selected_arbitrage.append(opportunity)
        else:
            reasons.append(f"arbitrage {opportunity.kind} skipped: exceeds daily loss cap")

    candidates = [
        decision
        for decision in decisions
        if decision.approved and decision.recommended_contracts > 0 and decision.cost_per_contract > 0
    ]
    candidates.sort(key=lambda decision: _growth_score(decision, limits.bankroll), reverse=True)

    for decision in candidates:
        sleeve = _sleeve_for_decision(decision, limits.risk_profile)
        if sleeve is None:
            continue
        spend = _spend(decision)
        adjusted = decision
        if sleeve == "yes_convex":
            if yes_spend + spend > limits.yes_sleeve + 1e-9:
                reasons.append(f"{decision.ticker} skipped: YES sleeve is full")
                continue
        elif sleeve == "research_explore":
            remaining = limits.explore_sleeve - explore_spend
            if remaining <= 0:
                reasons.append(f"{decision.ticker} skipped: research exploration sleeve is full")
                continue
            if spend > remaining:
                adjusted = _scale_decision(decision, remaining)
                if adjusted is None:
                    reasons.append(f"{decision.ticker} skipped: exploration sleeve cannot buy a contract")
                    continue
                spend = _spend(adjusted)
        if sleeve != "arbitrage" and directional_spend + spend > limits.max_daily_loss + 1e-9:
            reasons.append(f"{decision.ticker} skipped: directional risk budget is full")
            continue

        growth_score = _growth_score(adjusted, limits.bankroll)
        leg = _portfolio_leg(
            _tag_decision(adjusted, run_id=run_id, sleeve=sleeve, growth_score=growth_score),
            sleeve=sleeve,
            growth_score=growth_score,
        )
        if _worst_case_loss([*selected, leg]) > limits.max_daily_loss + 1e-9:
            reasons.append(f"{decision.ticker} skipped: scenario loss exceeds daily cap")
            continue
        selected.append(leg)
        directional_spend += spend
        if sleeve == "yes_convex":
            yes_spend += spend
        elif sleeve == "research_explore":
            explore_spend += spend

    # Phase 2c: re-size the directional legs jointly across the bin ladder, so a
    # basket of hedged bins is grown-optimal rather than a stack of independent
    # per-bin Kelly bets. Opt-in; a strict no-op when disabled or without a ladder.
    if joint_kelly_enabled and bin_yes_probs:
        selected = _joint_resize_directional(
            selected,
            bin_yes_probs,
            bankroll=limits.bankroll,
            max_daily_loss=limits.max_daily_loss,
        )

    total_spend = sum(leg.spend for leg in selected)
    expected_profit = sum(leg.expected_profit for leg in selected)
    worst_case = _worst_case_loss(selected)
    if not selected:
        reasons.append("no portfolio legs passed allocation gates")
    return PortfolioPlan(
        run_id=run_id,
        risk_profile=limits.risk_profile,
        approved=bool(selected),
        legs=selected,
        arbitrage_opportunities=selected_arbitrage,
        total_spend=total_spend,
        worst_case_loss=worst_case,
        expected_profit=expected_profit,
        reasons=reasons,
        limits=limits,
    )


def _sleeve_for_decision(decision: TradeDecision, profile: str) -> str | None:
    side = decision.side.upper()
    if side == "YES":
        if decision.edge_lcb >= 0.0 and decision.trade_quality_score >= 50.0:
            return "yes_convex"
        if profile == "research" and decision.edge > 0.0:
            return "research_explore"
        return None
    if side == "NO":
        if decision.edge_lcb >= 0.0:
            return "no_core"
        if profile == "research" and decision.edge > 0.0:
            return "research_explore"
    return None


def _joint_resize_directional(
    legs: list[PortfolioLeg],
    bin_yes_probs: dict[str, float],
    *,
    bankroll: float,
    max_daily_loss: float,
) -> list[PortfolioLeg]:
    """Re-size the non-arbitrage legs with the growth-optimal joint allocation.

    Reuses the greedy selection but replaces each directional leg's per-bin Kelly
    contract count with its share of the joint (cross-bin-hedged) allocation,
    bounded by the leg's own liquidity and the portfolio worst-case-loss cap.
    Arbitrage legs are untouched. Fails safe: on any ladder/leg mismatch, or if the
    re-size would breach the worst-case cap, the original legs are returned.
    """

    directional = [leg for leg in legs if leg.sleeve != "arbitrage"]
    others = [leg for leg in legs if leg.sleeve == "arbitrage"]
    if not directional or bankroll <= 0:
        return legs

    bin_keys = sorted(bin_yes_probs)
    leg_keys = [f"{leg.decision.ticker}|{leg.decision.side}|{i}" for i, leg in enumerate(directional)]
    specs: list[tuple[str, str, str, float]] = []
    for key, leg in zip(leg_keys, directional):
        decision = leg.decision
        if decision.ticker not in bin_yes_probs or decision.cost_per_contract <= 0:
            return legs  # ladder does not describe this leg -> do not risk a bad re-size
        specs.append((key, decision.ticker, decision.side, decision.cost_per_contract))

    positions, scenario_probs = build_joint_positions(bin_keys, bin_yes_probs, specs)
    cap = max_daily_loss / bankroll if bankroll > 0 else 0.0
    fractions = joint_kelly_fractions(positions, scenario_probs, total_fraction_cap=cap)

    resized: list[PortfolioLeg] = []
    for key, leg in zip(leg_keys, directional):
        decision = leg.decision
        joint_spend = fractions.get(key, 0.0) * bankroll
        # No displayed-ask cap here: it was a taker-era assumption. Where an
        # order actually TAKES (market entry or a crossing limit), the paper
        # trader clamps contracts to the displayed ask at the execution gate
        # (paper._clamp_to_displayed_ask); a resting maker quote's fill is
        # gated by future traded volume, not the ask displayed at entry. The
        # worst-case-loss cap below and the shared-account per-position cap at
        # placement remain the binding risk ceilings.
        new_decision = _scale_decision(decision, joint_spend)
        if new_decision is None:
            resized.append(leg)  # keep the original rather than drop a selected leg
            continue
        resized.append(_portfolio_leg(new_decision, sleeve=leg.sleeve, growth_score=leg.growth_score))

    candidate = [*others, *resized]
    if _worst_case_loss(candidate) > max_daily_loss + 1e-9:
        return legs  # fail safe: keep the disciplined greedy sizing
    return candidate


def _portfolio_leg(decision: TradeDecision, *, sleeve: str, growth_score: float) -> PortfolioLeg:
    return PortfolioLeg(
        sleeve=sleeve,
        decision=decision,
        spend=_spend(decision),
        expected_profit=decision.expected_profit,
        growth_score=growth_score,
    )


def _spend(decision: TradeDecision) -> float:
    return max(0.0, decision.recommended_contracts * decision.cost_per_contract)


def _scale_decision(decision: TradeDecision, spend_budget: float) -> TradeDecision | None:
    if spend_budget <= 0 or decision.cost_per_contract <= 0:
        return None
    contracts = math.floor(spend_budget / decision.cost_per_contract)
    if contracts <= 0:
        return None
    return replace(
        decision,
        recommended_contracts=float(contracts),
        expected_profit=decision.edge * float(contracts),
    )


def _growth_score(decision: TradeDecision, bankroll: float) -> float:
    if bankroll <= 0 or decision.cost_per_contract <= 0 or decision.recommended_contracts <= 0:
        return -math.inf
    p = min(0.999999, max(0.000001, decision.probability))
    loss = _spend(decision)
    profit = decision.recommended_contracts * max(0.0, 1.0 - decision.cost_per_contract)
    if loss >= bankroll:
        return -math.inf
    return p * math.log1p(profit / bankroll) + (1.0 - p) * math.log1p(-loss / bankroll)


def _tag_decision(
    decision: TradeDecision,
    *,
    run_id: str,
    sleeve: str,
    growth_score: float,
) -> TradeDecision:
    return replace(
        decision,
        reasons=[*decision.reasons, f"portfolio {run_id}: sleeve={sleeve}, growth={growth_score:.6f}"],
    )


def _worst_case_loss(legs: list[PortfolioLeg]) -> float:
    if not legs:
        return 0.0
    scenario_values = _scenario_values([leg.decision for leg in legs])
    if not scenario_values:
        return sum(leg.spend for leg in legs)
    worst_pnl = min(
        sum(_decision_pnl_at_settlement(leg.decision, high) for leg in legs)
        for high in scenario_values
    )
    return max(0.0, -worst_pnl)


def _scenario_values(decisions: list[TradeDecision]) -> list[float]:
    values: set[float] = set()
    finite_intervals: list[tuple[float, float]] = []
    for decision in decisions:
        market = _market_from_decision(decision)
        lo, hi = market.continuous_interval()
        if math.isfinite(lo) and math.isfinite(hi):
            finite_intervals.append((lo, hi))
            values.add(round((lo + hi) / 2.0))
        elif math.isfinite(hi):
            values.add(math.floor(hi - 1.0))
        elif math.isfinite(lo):
            values.add(math.ceil(lo + 1.0))
    if finite_intervals:
        finite_intervals.sort()
        values.add(finite_intervals[0][0] - 1.0)
        values.add(finite_intervals[-1][1] + 1.0)
        previous_hi = finite_intervals[0][1]
        for lo, hi in finite_intervals[1:]:
            if lo > previous_hi:
                values.add((previous_hi + lo) / 2.0)
            previous_hi = max(previous_hi, hi)
    return sorted(values)


def _decision_pnl_at_settlement(decision: TradeDecision, settlement_high_f: float) -> float:
    market = _market_from_decision(decision)
    yes_wins = market.resolves_yes(settlement_high_f)
    side_wins = yes_wins if decision.side.upper() == "YES" else not yes_wins
    if side_wins:
        return decision.recommended_contracts * (1.0 - decision.cost_per_contract)
    return -_spend(decision)


def _market_from_decision(decision: TradeDecision) -> MarketBin:
    return MarketBin(
        ticker=decision.ticker,
        event_ticker="",
        title=decision.label,
        yes_sub_title=decision.label,
        strike_type=decision.strike_type or "",
        floor_strike=decision.floor_strike,
        cap_strike=decision.cap_strike,
        yes_bid=decision.yes_bid,
        yes_ask=decision.yes_ask,
        no_bid=0.0,
        no_ask=0.0,
        yes_bid_size=0.0,
        yes_ask_size=0.0,
        status="active",
    )
