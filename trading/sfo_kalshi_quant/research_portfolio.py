"""Scenario-disciplined allocation for the isolated paper-research sleeves.

This module is deliberately pure: it turns immutable scan opportunities and
already-logical exposure into plans.  Database admission and order placement
remain separate so the allocator can be replayed exactly.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from typing import Sequence

from .account import REGION_BY_SERIES
from .cities import city_for_market_ticker
from .models import TradeDecision
from .portfolio import (
    PortfolioDisposition,
    PortfolioLeg,
    PortfolioLimits,
    PortfolioPlan,
    decision_pnl_at_settlement,
)
from .research_policy import MOTION_POLICY, TARGET_POLICY, ResearchSleevePolicy


@dataclass(frozen=True)
class ResearchOpportunity:
    """One fully-gated market-side opportunity from a shared research scan."""

    decision: TradeDecision
    target_date: str
    lead_days: int
    pending: bool = False


@dataclass(frozen=True)
class ResearchPlans:
    target: PortfolioPlan
    motion: PortfolioPlan
    target_pnl: float
    realized_today: float
    remaining_target: float
    available_conservative_expected_profit: float
    target_feasible_from_current_opportunity_set: bool


@dataclass(frozen=True)
class _ExposureSummary:
    city_target: dict[tuple[str, str, str, str], float]
    region_day: dict[tuple[str, str, str, str], float]
    aggregate_by_book: dict[tuple[str, str], float]

    @property
    def aggregate(self) -> float:
        return sum(self.aggregate_by_book.values())


def city_target_worst_case_loss(
    legs: Sequence[PortfolioLeg],
    settlement_bins: Sequence[int],
) -> float:
    """Maximum settlement loss for logical legs on one city-target.

    Filled positions use their typed bracket payoff in every integer scenario.
    A pending maker order reserves its entire entry loss in every scenario.
    Partial-close child rows are deliberately ignored because the logical root
    is the sole exposure leg.
    """

    logical_legs = [leg for leg in legs if not leg.is_partial_child]
    if not logical_legs:
        return 0.0
    bins = tuple(settlement_bins)
    if not bins:
        return sum(leg.spend for leg in logical_legs)
    worst = max(
        -sum(
            -leg.spend if leg.pending else decision_pnl_at_settlement(leg.decision, high)
            for leg in logical_legs
        )
        for high in bins
    )
    return max(0.0, worst)


def allocate_research_plans(
    opportunities: Sequence[ResearchOpportunity],
    *,
    target_available_cash: float = TARGET_POLICY.reference_equity,
    motion_available_cash: float = MOTION_POLICY.reference_equity,
    target_active_legs: Sequence[PortfolioLeg] = (),
    motion_active_legs: Sequence[PortfolioLeg] = (),
    realized_today: float = 0.0,
    motion_realized_today: float = 0.0,
    run_id: str | None = None,
) -> ResearchPlans:
    """Build independent target and motion plans from one opportunity set."""

    _validate_account_inputs(
        target_available_cash=target_available_cash,
        motion_available_cash=motion_available_cash,
        realized_today=realized_today,
        motion_realized_today=motion_realized_today,
    )
    run_id = run_id or f"RPF-{uuid.uuid4().hex[:12]}"
    target_legs: list[PortfolioLeg] = []
    target_dispositions: list[PortfolioDisposition] = []
    target_cash_used = 0.0
    target_paused_reason = _pause_reason(TARGET_POLICY, realized_today)
    for opportunity in sorted(opportunities, key=_target_priority):
        rejection = _target_rejection(opportunity)
        if rejection is not None:
            target_dispositions.append(
                _disposition(opportunity, "target", "rejected", rejection)
            )
            continue
        if target_paused_reason is not None:
            target_dispositions.append(
                _disposition(opportunity, "target", "capacity_blocked", target_paused_reason)
            )
            continue
        decision = _target_sized_decision(opportunity.decision)
        if decision is None:
            target_dispositions.append(
                _disposition(
                    opportunity,
                    "target",
                    "capacity_blocked",
                    "target position cap cannot fund one contract",
                )
            )
            continue
        leg = _research_leg(opportunity, decision, sleeve="target")
        if target_cash_used + leg.spend > target_available_cash + 1e-9:
            target_dispositions.append(
                _disposition(
                    opportunity,
                    "target",
                    "capacity_blocked",
                    "target cash cap",
                )
            )
            continue
        risk_reason = _risk_cap_reason(
            [*target_active_legs, *target_legs, leg],
            candidate=leg,
            policy=TARGET_POLICY,
        )
        if risk_reason is not None:
            target_dispositions.append(
                _disposition(opportunity, "target", "capacity_blocked", risk_reason)
            )
            continue
        target_legs.append(leg)
        target_cash_used += leg.spend
        target_dispositions.append(
            _disposition(
                opportunity,
                "target",
                "selected",
                None,
                contracts=leg.decision.recommended_contracts,
            )
        )

    target_plan = _plan(
        run_id=f"{run_id}-target",
        sleeve="research-target",
        legs=target_legs,
        bankroll=TARGET_POLICY.reference_equity,
        aggregate_cap=TARGET_POLICY.max_aggregate_risk_pct,
        dispositions=target_dispositions,
        exposure_legs=[*target_active_legs, *target_legs],
    )
    motion_legs: list[PortfolioLeg] = []
    motion_dispositions: list[PortfolioDisposition] = []
    motion_cash_used = 0.0
    motion_paused_reason = _pause_reason(MOTION_POLICY, motion_realized_today)
    for opportunity in sorted(opportunities, key=_motion_priority):
        rejection = _motion_rejection(opportunity)
        if rejection is not None:
            motion_dispositions.append(
                _disposition(opportunity, "motion", "rejected", rejection)
            )
            continue
        if motion_paused_reason is not None:
            motion_dispositions.append(
                _disposition(
                    opportunity,
                    "motion",
                    "capacity_blocked",
                    motion_paused_reason,
                )
            )
            continue
        decision = replace(
            opportunity.decision,
            recommended_contracts=1.0,
            expected_profit=opportunity.decision.edge,
        )
        spend = decision.cost_per_contract
        if motion_cash_used + spend > motion_available_cash + 1e-9:
            motion_dispositions.append(
                _disposition(opportunity, "motion", "capacity_blocked", "motion cash cap")
            )
            continue
        leg = _research_leg(opportunity, decision, sleeve="motion")
        risk_reason = _risk_cap_reason(
            [*motion_active_legs, *motion_legs, leg],
            candidate=leg,
            policy=MOTION_POLICY,
        )
        if risk_reason is not None:
            motion_dispositions.append(
                _disposition(opportunity, "motion", "capacity_blocked", risk_reason)
            )
            continue
        motion_legs.append(leg)
        motion_cash_used += spend
        motion_dispositions.append(
            _disposition(opportunity, "motion", "selected", None, contracts=1.0)
        )
    motion_plan = _plan(
        run_id=f"{run_id}-motion",
        sleeve="research-motion",
        legs=motion_legs,
        bankroll=MOTION_POLICY.reference_equity,
        aggregate_cap=MOTION_POLICY.max_aggregate_risk_pct,
        dispositions=motion_dispositions,
        exposure_legs=[*motion_active_legs, *motion_legs],
    )
    remaining = max(0.0, TARGET_POLICY.target_pnl - realized_today)
    available = sum(
        max(0.0, leg.decision.edge_lcb) * leg.decision.recommended_contracts
        for leg in target_legs
    )
    return ResearchPlans(
        target=target_plan,
        motion=motion_plan,
        target_pnl=TARGET_POLICY.target_pnl,
        realized_today=realized_today,
        remaining_target=remaining,
        available_conservative_expected_profit=available,
        target_feasible_from_current_opportunity_set=available + 1e-9 >= remaining,
    )


def _plan(
    *,
    run_id: str,
    sleeve: str,
    legs: list[PortfolioLeg],
    bankroll: float,
    aggregate_cap: float,
    dispositions: list[PortfolioDisposition],
    exposure_legs: Sequence[PortfolioLeg],
) -> PortfolioPlan:
    total_spend = sum(leg.spend for leg in legs)
    return PortfolioPlan(
        run_id=run_id,
        risk_profile=sleeve,
        approved=bool(legs),
        legs=legs,
        arbitrage_opportunities=[],
        total_spend=total_spend,
        worst_case_loss=_exposure_summary(exposure_legs).aggregate,
        expected_profit=sum(leg.expected_profit for leg in legs),
        reasons=[] if legs else ["no portfolio legs passed allocation gates"],
        limits=PortfolioLimits(
            risk_profile=sleeve,
            bankroll=bankroll,
            max_daily_loss=bankroll * aggregate_cap,
            yes_sleeve=0.0,
            explore_sleeve=0.0,
        ),
        dispositions=dispositions,
    )


def _target_rejection(opportunity: ResearchOpportunity) -> str | None:
    decision = opportunity.decision
    invalid = _common_rejection(opportunity)
    if invalid is not None:
        return invalid
    if opportunity.lead_days < TARGET_POLICY.min_lead_days:
        return "target requires day-ahead lead"
    if decision.edge < 0:
        return "point edge is negative"
    if decision.edge_lcb < 0:
        return "after-fee lower-bound edge is negative"
    return None


def _motion_rejection(opportunity: ResearchOpportunity) -> str | None:
    decision = opportunity.decision
    invalid = _common_rejection(opportunity)
    if invalid is not None:
        return invalid
    if opportunity.lead_days < MOTION_POLICY.min_lead_days:
        return "motion target is in the past"
    if decision.edge <= 0:
        return "point edge is not positive"
    if decision.edge_lcb < -0.07:
        return "lower-bound edge is below motion floor"
    if decision.recommended_contracts < 1.0:
        return "candidate liquidity cannot support one contract"
    return None


def _common_rejection(opportunity: ResearchOpportunity) -> str | None:
    decision = opportunity.decision
    if not decision.approved:
        return "candidate gates rejected"
    numeric = (
        decision.edge,
        decision.edge_lcb,
        decision.cost_per_contract,
        decision.recommended_contracts,
        decision.probability,
    )
    if any(_finite_float(value) is None for value in numeric):
        return "candidate numeric evidence is invalid"
    if not 0.0 < decision.cost_per_contract < 1.0:
        return "candidate price is outside the executable range"
    if decision.recommended_contracts <= 0:
        return "candidate has no executable quantity"
    if str(decision.side).upper() not in {"YES", "NO"}:
        return "candidate side is invalid"
    if (
        isinstance(opportunity.lead_days, bool)
        or not isinstance(opportunity.lead_days, int)
        or opportunity.lead_days < 0
        or not _is_iso_date(opportunity.target_date)
    ):
        return "candidate target metadata is invalid"
    if city_for_market_ticker(decision.ticker) is None:
        return "candidate city series is unknown"
    if not _has_typed_bracket(decision):
        return "candidate typed bracket is invalid"
    return None


def _has_typed_bracket(decision: TradeDecision) -> bool:
    if decision.strike_type == "less":
        return _finite_float(decision.cap_strike) is not None
    if decision.strike_type == "greater":
        return _finite_float(decision.floor_strike) is not None
    floor = _finite_float(decision.floor_strike)
    cap = _finite_float(decision.cap_strike)
    return floor is not None and cap is not None and floor <= cap


def _is_iso_date(value: object) -> bool:
    try:
        raw = str(value)
        return date.fromisoformat(raw).isoformat() == raw
    except (TypeError, ValueError):
        return False


def _target_sized_decision(decision: TradeDecision) -> TradeDecision | None:
    max_contracts = min(
        float(decision.recommended_contracts),
        TARGET_POLICY.reference_equity
        * TARGET_POLICY.max_position_risk_pct
        / float(decision.cost_per_contract),
    )
    contracts = math.floor(max_contracts + 1e-12)
    if contracts < 1:
        return None
    return replace(
        decision,
        recommended_contracts=float(contracts),
        expected_profit=float(decision.edge) * float(contracts),
    )


def _research_leg(
    opportunity: ResearchOpportunity,
    decision: TradeDecision,
    *,
    sleeve: str,
) -> PortfolioLeg:
    policy = TARGET_POLICY if sleeve == "target" else MOTION_POLICY
    city = city_for_market_ticker(decision.ticker)
    assert city is not None  # guarded by _common_rejection
    spend = decision.recommended_contracts * decision.cost_per_contract
    return PortfolioLeg(
        sleeve=sleeve,
        decision=decision,
        spend=spend,
        expected_profit=decision.expected_profit,
        growth_score=_expected_log_growth(decision, policy.reference_equity),
        target_date=opportunity.target_date,
        account_id=policy.account_id,
        region=REGION_BY_SERIES.get(city.series_ticker, "unknown"),
        pending=opportunity.pending,
    )


def _target_priority(
    opportunity: ResearchOpportunity,
) -> tuple[float, float, str, str, str]:
    decision = opportunity.decision
    cost = _finite_float(decision.cost_per_contract)
    edge_lcb = _finite_float(decision.edge_lcb)
    conservative_per_worst_dollar = (
        edge_lcb / cost
        if edge_lcb is not None and cost is not None and cost > 0
        else -math.inf
    )
    growth = _expected_log_growth(decision, TARGET_POLICY.reference_equity)
    return (
        -conservative_per_worst_dollar,
        -growth,
        str(opportunity.target_date),
        str(decision.ticker),
        str(decision.side).upper(),
    )


def _expected_log_growth(decision: TradeDecision, bankroll: float) -> float:
    cost = _finite_float(decision.cost_per_contract)
    contracts = _finite_float(decision.recommended_contracts)
    probability = _finite_float(decision.probability)
    if (
        not math.isfinite(bankroll)
        or bankroll <= 0
        or cost is None
        or cost <= 0
        or contracts is None
        or contracts <= 0
        or probability is None
    ):
        return -math.inf
    loss = contracts * cost
    if loss >= bankroll:
        return -math.inf
    p = min(0.999999, max(0.000001, probability))
    profit = contracts * (1.0 - cost)
    return p * math.log1p(profit / bankroll) + (1.0 - p) * math.log1p(-loss / bankroll)


def _finite_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _pause_reason(policy: ResearchSleevePolicy, realized_pnl: float) -> str | None:
    if policy is TARGET_POLICY and realized_pnl >= policy.target_pnl - 1e-9:
        return "target attained: new target risk is locked for the objective day"
    loss_limit = -policy.reference_equity * policy.daily_loss_pause_pct
    if realized_pnl <= loss_limit + 1e-9:
        return f"{policy.sleeve.value} daily-loss pause"
    return None


def _validate_account_inputs(**values: float) -> None:
    for name, value in values.items():
        if not math.isfinite(float(value)):
            raise ValueError(f"{name} must be finite")
        if name.endswith("available_cash") and value < 0:
            raise ValueError(f"{name} must be non-negative")


def _risk_cap_reason(
    legs: Sequence[PortfolioLeg],
    *,
    candidate: PortfolioLeg,
    policy: ResearchSleevePolicy,
) -> str | None:
    summary = _exposure_summary(
        legs,
        default_account=policy.account_id,
        default_sleeve=policy.sleeve.value,
    )
    series = _series_for_leg(candidate)
    target = candidate.target_date or "__unknown__"
    account = candidate.account_id or policy.account_id
    sleeve = candidate.sleeve or policy.sleeve.value
    region = candidate.region or REGION_BY_SERIES.get(series, "unknown")
    equity = policy.reference_equity
    if candidate.spend > policy.max_position_risk_pct * equity + 1e-9:
        return "position scenario-loss cap"
    if summary.city_target.get((account, sleeve, series, target), 0.0) > (
        policy.max_city_target_risk_pct * equity + 1e-9
    ):
        return "city-target scenario-loss cap"
    if summary.region_day.get((account, sleeve, region, target), 0.0) > (
        policy.max_region_day_risk_pct * equity + 1e-9
    ):
        return "region-day scenario-loss cap"
    if summary.aggregate_by_book.get((account, sleeve), 0.0) > (
        policy.max_aggregate_risk_pct * equity + 1e-9
    ):
        return "aggregate scenario-loss cap"
    return None


def _exposure_summary(
    legs: Sequence[PortfolioLeg],
    *,
    default_account: str = "",
    default_sleeve: str = "",
) -> _ExposureSummary:
    groups: dict[tuple[str, str, str, str], list[PortfolioLeg]] = defaultdict(list)
    for leg in legs:
        if leg.is_partial_child:
            continue
        account = leg.account_id or default_account
        sleeve = leg.sleeve or default_sleeve
        series = _series_for_leg(leg)
        target = leg.target_date or "__unknown__"
        groups[(account, sleeve, series, target)].append(leg)

    city_losses: dict[tuple[str, str, str, str], float] = {}
    region_losses: dict[tuple[str, str, str, str], float] = defaultdict(float)
    aggregate: dict[tuple[str, str], float] = defaultdict(float)
    for key, group in groups.items():
        account, sleeve, series, target = key
        loss = city_target_worst_case_loss(group, _settlement_bins(group))
        city_losses[key] = loss
        explicit_regions = {leg.region for leg in group if leg.region}
        region = (
            next(iter(explicit_regions))
            if len(explicit_regions) == 1
            else REGION_BY_SERIES.get(series, "unknown")
        )
        region_losses[(account, sleeve, region, target)] += loss
        aggregate[(account, sleeve)] += loss
    return _ExposureSummary(city_losses, dict(region_losses), dict(aggregate))


def _series_for_leg(leg: PortfolioLeg) -> str:
    city = city_for_market_ticker(leg.decision.ticker)
    if city is not None:
        return city.series_ticker
    return leg.decision.ticker.split("-", 1)[0].upper()


def _settlement_bins(legs: Sequence[PortfolioLeg]) -> tuple[int, ...]:
    strikes: list[float] = []
    for leg in legs:
        for strike in (leg.decision.floor_strike, leg.decision.cap_strike):
            if strike is not None and math.isfinite(float(strike)):
                strikes.append(float(strike))
    if not strikes:
        return ()
    low = math.floor(min(strikes)) - 1
    high = math.ceil(max(strikes)) + 1
    return tuple(range(low, high + 1))


def _motion_priority(opportunity: ResearchOpportunity) -> tuple[float, str, str, str]:
    decision = opportunity.decision
    edge = _finite_float(decision.edge)
    return (
        -(edge if edge is not None else -math.inf),
        str(opportunity.target_date),
        str(decision.ticker),
        str(decision.side).upper(),
    )


def _disposition(
    opportunity: ResearchOpportunity,
    sleeve: str,
    status: str,
    reason: str | None,
    *,
    contracts: float = 0.0,
) -> PortfolioDisposition:
    return PortfolioDisposition(
        ticker=opportunity.decision.ticker,
        target_date=opportunity.target_date,
        side=opportunity.decision.side.upper(),
        sleeve=sleeve,
        status=status,
        reason=reason,
        contracts=contracts,
    )
