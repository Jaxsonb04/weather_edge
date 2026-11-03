from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, replace

from .arbitrage import ArbitrageOpportunity
from .config import normalize_risk_profile_name
from .joint_kelly import build_joint_positions, joint_kelly_optimum
from .models import MarketBin, TradeDecision

# Bound on the FULL-Kelly solve behind _joint_resize_directional: no leverage,
# so the whole book may stake at most the bankroll. A book whose optimum would
# stake more is left at its per-leg sizes, because a split forced by a bound
# reflects the bound, not how the legs overlap. The bound is deliberately not
# the day's directional budget. That budget limits the fractional per-leg
# stakes, which the greedy pass enforces, and it shrinks as earlier cities'
# scans spend it, so using it here made a leg's trim depend on scan order. It
# is also far too small for full Kelly: one favorite's full-Kelly stake (27.6%
# of bankroll for a NO at 0.83 that the model gives 0.877) is well past the
# live 8%, so a cap that small binds on almost every solve.
_JOINT_KELLY_MAX_TOTAL_FRACTION = 1.0


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
    target_date: str | None = None
    account_id: str | None = None
    region: str | None = None
    pending: bool = False
    logical_position_id: str | int | None = None
    is_partial_child: bool = False


@dataclass(frozen=True)
class PortfolioDisposition:
    """Auditable outcome for every candidate evaluated by an allocator."""

    ticker: str
    target_date: str
    side: str
    sleeve: str
    status: str
    reason: str | None
    contracts: float = 0.0


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
    dispositions: list[PortfolioDisposition] = field(default_factory=list)

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
        # Audit TC-6 (2026-09-07): this was 0.05 of the directional budget =
        # $4.00 across all 15 cities. The live profile sets
        # `yes_estimation_shrink`, so a live YES leg is hard-capped at
        # `bankroll * yes_max_position_risk_pct` = $5.00 (risk.py,
        # `yes_budget`) -- the sleeve was therefore smaller than a SINGLE
        # maximum-size YES leg, and the loop below DROPS a leg that does not
        # fit rather than scaling it down (unlike the exploration sleeve). A
        # concentration cap that cannot hold one position is an invisible
        # veto, not a sleeve.
        # 0.20 is the research book's own convex fraction, chosen for parity
        # rather than for yield: research allocates bankroll * 0.25 * 0.20 =
        # $50 against a $15 maximum research YES leg (it leaves
        # `yes_estimation_shrink` off, so `max_position_risk_pct` binds) =
        # 3.3 positions; at 0.20 live gets $16.00 against a $5.00 maximum leg
        # = 3.2 positions. Same concentration semantics, not a looser one, and
        # 80% of the day's directional budget still belongs to the NO book.
        # This number has NO production evidence behind it and is inert today:
        # the live book has never traded YES (608/608 orders NO since
        # 2026-07-01) and 0 of 86,820 live YES decision rows on 2026-09-05..07
        # were approved. The upstream signal gate, not this sleeve, is what
        # keeps the book NO-only. Revisit with real data the first time a YES
        # candidate is signal-approved.
        yes_sleeve=bankroll * 0.08 * 0.20,
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
    existing_directional_spend: float = 0.0,
) -> PortfolioPlan:
    limits = portfolio_limits_for_profile(risk_profile, bankroll)
    run_id = run_id or f"PF-{uuid.uuid4().hex[:12]}"
    reasons: list[str] = []
    selected: list[PortfolioLeg] = []
    selected_arbitrage: list[ArbitrageOpportunity] = []
    directional_spend = max(0.0, float(existing_directional_spend))
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

    # Phase 2c: trim the directional legs by a joint solve across the event's
    # bin ladder. The legs of one ladder are not independent bets, so a leg
    # whose risk the other legs partly duplicate keeps only its joint share of
    # its per-leg Kelly size. Trim only (audit 2026-11-01): the joint step
    # never raises a leg above its per-leg size, so it can never add capital.
    # Opt-in; a strict no-op when disabled, without a ladder, or with fewer
    # than two directional legs.
    if joint_kelly_enabled and bin_yes_probs:
        selected = _joint_resize_directional(
            selected,
            bin_yes_probs,
            bankroll=limits.bankroll,
            remaining_budget=max(0.0, limits.max_daily_loss - existing_directional_spend),
            yes_sleeve=limits.yes_sleeve,
            explore_sleeve=limits.explore_sleeve,
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
    remaining_budget: float,
    yes_sleeve: float,
    explore_sleeve: float,
) -> list[PortfolioLeg]:
    """Trim each directional leg by its joint ratio; never grow one.

    Solves full Kelly on the event's bin ladder (``joint_kelly_optimum``) for
    all the directional legs together, and for each leg alone. A leg's joint
    ratio is its stake in the joint solve over its stake alone: below 1 when
    the other legs partly duplicate its risk, above 1 when they diversify it.
    Fractional Kelly is a fraction of the full-Kelly solution, so the leg's
    fractional joint size is that ratio times its per-leg Kelly budget or its
    per-leg spend, whichever is larger. The budget already carries the per-leg
    sizer's fractional Kelly, LCB-weighted sizing
    probability and posterior trust multiplier (risk.py). The leg is cut to
    that many whole contracts (at least one) only when that is fewer than it
    would place now. A leg is never raised above its per-leg size, and an
    untrimmed leg keeps its decision untouched. A trimmed leg is stamped
    ``binding_constraint="joint_kelly"``. (Before the 2026-11-01 audit this
    step replaced each leg with its raw full-Kelly share, which skipped every
    per-leg control and took a zero-edge_lcb live NO from 6 contracts to 93.)

    The solve never sees the day's remaining budget, so which legs it trims
    does not depend on how many cities were scanned first; that budget only
    backs the fail-safe checks. Arbitrage legs are untouched. A no-op with
    fewer than two directional legs, since one leg has nothing to share risk
    with. Fails safe, returning the original legs, on any ladder/leg mismatch;
    when the full-Kelly book would stake more than the bankroll or has no
    unique optimum, since its split then does not measure how the legs
    overlap; and if the trimmed book would breach the day's remaining
    worst-case-loss, directional or sleeve budgets.
    """

    directional = [leg for leg in legs if leg.sleeve != "arbitrage"]
    others = [leg for leg in legs if leg.sleeve == "arbitrage"]
    if len(directional) < 2 or bankroll <= 0:
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
    joint = joint_kelly_optimum(
        positions, scenario_probs, max_total_fraction=_JOINT_KELLY_MAX_TOTAL_FRACTION
    )
    if joint is None:
        return legs  # leveraged or not unique: the split does not measure overlap

    resized: list[PortfolioLeg] = []
    for key, position, leg in zip(leg_keys, positions, directional):
        decision = leg.decision
        # The denominator is the same solve for this leg alone on the same
        # ladder, not kelly_fraction_spent(decision.probability, cost). The
        # ladder is normalized over the event's bins, so its probability need
        # not equal the decision's own; dividing like by like means only the
        # other legs move the ratio.
        alone = joint_kelly_optimum(
            [position], scenario_probs, max_total_fraction=_JOINT_KELLY_MAX_TOTAL_FRACTION
        )
        alone_fraction = alone[key] if alone else 0.0
        if alone_fraction <= 0.0:
            # The ladder sees no stand-alone edge at this cost, so there is no
            # Kelly stake for a joint ratio to scale.
            resized.append(leg)
            continue
        joint_ratio = joint[key] / alone_fraction
        # The per-leg target is the leg's Kelly budget (the evaluator's
        # kelly_fraction already includes fractional Kelly, the LCB-weighted
        # sizing probability and the trust multiplier), or its per-leg spend
        # when that is larger: the comfort boost scales the spend and not
        # kelly_fraction, and whole-contract rounding can round up. Using the
        # larger of the two means the ratio, not the base, decides a trim. A
        # cap that holds the leg below its Kelly budget (the position-risk or
        # event cap, the live YES shrink) stays a ceiling rather than being
        # scaled.
        target_spend = joint_ratio * max(decision.kelly_fraction * bankroll, leg.spend)
        # Round like the live per-leg sizer (round_contracts), and never below
        # one contract: the resize never drops a leg the greedy pass selected.
        contracts = max(1, round(target_spend / decision.cost_per_contract))
        # Compare whole contracts: placement truncates a fractional count (an
        # event-cap scale leaves one), so cutting 5.3 to 5 would change only
        # the journal, not the order.
        if contracts >= math.floor(decision.recommended_contracts + 1e-9):
            resized.append(leg)
            continue
        # Journal the lever that set the size: the joint solve, not the per-leg
        # Kelly budget or cap the decision was stamped with.
        trimmed = replace(
            decision,
            recommended_contracts=float(contracts),
            expected_profit=decision.edge * float(contracts),
            binding_constraint="joint_kelly",
        )
        resized.append(_portfolio_leg(trimmed, sleeve=leg.sleeve, growth_score=leg.growth_score))

    if all(new is old for new, old in zip(resized, directional)):
        return legs
    # No displayed-ask cap here: it was a taker-era assumption. Where an order
    # actually TAKES (market entry or a crossing limit), the paper trader
    # clamps contracts to the displayed ask at the execution gate
    # (paper._clamp_to_displayed_ask); a resting maker quote's fill is gated by
    # future traded volume, not the ask displayed at entry. The per-leg
    # ceiling above, the checks below and the shared-account per-position cap
    # at placement remain the binding risk ceilings.
    candidate = [*others, *resized]
    # Defensive only: a trim-only resize never spends more than the greedy
    # book, so these checks cannot fire today. If one ever does, return the
    # greedy book unchanged rather than a partly trimmed one.
    if _worst_case_loss(candidate) > remaining_budget + 1e-9:
        return legs
    if sum(leg.spend for leg in resized) > remaining_budget + 1e-9:
        return legs
    if sum(leg.spend for leg in resized if leg.sleeve == "yes_convex") > yes_sleeve + 1e-9:
        return legs
    if (
        sum(leg.spend for leg in resized if leg.sleeve == "research_explore")
        > explore_sleeve + 1e-9
    ):
        return legs
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


def decision_pnl_at_settlement(
    decision: TradeDecision,
    settlement_high_f: float,
) -> float:
    """Public settlement-payoff helper shared by replayable allocators."""

    return _decision_pnl_at_settlement(decision, settlement_high_f)


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
