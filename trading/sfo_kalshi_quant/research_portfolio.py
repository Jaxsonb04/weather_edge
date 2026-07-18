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


MIN_EXECUTABLE_CONTRACT_COST = 0.01
MAX_EXECUTABLE_CONTRACT_COST = 0.99
MAX_TARGET_CONTRACTS = math.floor(
    TARGET_POLICY.reference_equity
    * TARGET_POLICY.max_position_risk_pct
    / MIN_EXECUTABLE_CONTRACT_COST
)


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


@dataclass(frozen=True)
class _PreparedTarget:
    source: "_PreparedOpportunity"
    rejection: str | None
    sized_decision: TradeDecision | None


@dataclass(frozen=True)
class _PreparedOpportunity:
    original: ResearchOpportunity
    normalized: ResearchOpportunity | None
    rejection: str | None


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

    (
        target_available_cash,
        motion_available_cash,
        realized_today,
        motion_realized_today,
    ) = _normalize_account_inputs(
        target_available_cash=target_available_cash,
        motion_available_cash=motion_available_cash,
        realized_today=realized_today,
        motion_realized_today=motion_realized_today,
    )
    run_id = run_id or f"RPF-{uuid.uuid4().hex[:12]}"
    prepared_opportunities = [
        _normalize_opportunity(opportunity) for opportunity in opportunities
    ]
    normalized_target_active, target_active_reason = _normalize_active_legs(
        target_active_legs,
        policy=TARGET_POLICY,
    )
    normalized_motion_active, motion_active_reason = _normalize_active_legs(
        motion_active_legs,
        policy=MOTION_POLICY,
    )
    target_legs: list[PortfolioLeg] = []
    target_dispositions: list[PortfolioDisposition] = []
    target_cash_used = 0.0
    target_paused_reason = _pause_reason(TARGET_POLICY, realized_today)
    prepared_targets = [_prepare_target(source) for source in prepared_opportunities]
    for prepared in sorted(prepared_targets, key=_target_priority):
        audit_opportunity = prepared.source.original
        opportunity = prepared.source.normalized
        if prepared.rejection is not None:
            target_dispositions.append(
                _disposition(
                    audit_opportunity,
                    "target",
                    "rejected",
                    prepared.rejection,
                )
            )
            continue
        assert opportunity is not None
        if target_active_reason is not None:
            target_dispositions.append(
                _disposition(
                    audit_opportunity,
                    "target",
                    "capacity_blocked",
                    target_active_reason,
                )
            )
            continue
        if target_paused_reason is not None:
            target_dispositions.append(
                _disposition(
                    audit_opportunity,
                    "target",
                    "capacity_blocked",
                    target_paused_reason,
                )
            )
            continue
        decision = prepared.sized_decision
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
        leg, capacity_reason = _fit_target_leg(
            opportunity,
            desired=decision,
            remaining_cash=target_available_cash - target_cash_used,
            exposure_legs=[*normalized_target_active, *target_legs],
        )
        if leg is None:
            target_dispositions.append(
                _disposition(
                    audit_opportunity,
                    "target",
                    "capacity_blocked",
                    capacity_reason,
                )
            )
            continue
        target_legs.append(leg)
        target_cash_used += leg.spend
        target_dispositions.append(
            _disposition(
                audit_opportunity,
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
        exposure_legs=(
            [*normalized_target_active, *target_legs]
            if target_active_reason is None
            else []
        ),
        plan_reason=target_active_reason,
        worst_case_override=(
            TARGET_POLICY.reference_equity
            if target_active_reason is not None
            else None
        ),
    )
    motion_legs: list[PortfolioLeg] = []
    motion_dispositions: list[PortfolioDisposition] = []
    motion_cash_used = 0.0
    motion_paused_reason = _pause_reason(MOTION_POLICY, motion_realized_today)
    for source in sorted(prepared_opportunities, key=_motion_priority):
        audit_opportunity = source.original
        opportunity = source.normalized
        rejection = source.rejection
        if rejection is None and opportunity is not None:
            rejection = _motion_rejection(opportunity)
        if rejection is not None:
            motion_dispositions.append(
                _disposition(audit_opportunity, "motion", "rejected", rejection)
            )
            continue
        assert opportunity is not None
        if motion_active_reason is not None:
            motion_dispositions.append(
                _disposition(
                    audit_opportunity,
                    "motion",
                    "capacity_blocked",
                    motion_active_reason,
                )
            )
            continue
        if motion_paused_reason is not None:
            motion_dispositions.append(
                _disposition(
                    audit_opportunity,
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
                _disposition(
                    audit_opportunity,
                    "motion",
                    "capacity_blocked",
                    "motion cash cap",
                )
            )
            continue
        leg = _research_leg(opportunity, decision, sleeve="motion")
        risk_reason = _risk_cap_reason(
            [*normalized_motion_active, *motion_legs, leg],
            candidate=leg,
            policy=MOTION_POLICY,
        )
        if risk_reason is not None:
            motion_dispositions.append(
                _disposition(
                    audit_opportunity,
                    "motion",
                    "capacity_blocked",
                    risk_reason,
                )
            )
            continue
        motion_legs.append(leg)
        motion_cash_used += spend
        motion_dispositions.append(
            _disposition(
                audit_opportunity,
                "motion",
                "selected",
                None,
                contracts=1.0,
            )
        )
    motion_plan = _plan(
        run_id=f"{run_id}-motion",
        sleeve="research-motion",
        legs=motion_legs,
        bankroll=MOTION_POLICY.reference_equity,
        aggregate_cap=MOTION_POLICY.max_aggregate_risk_pct,
        dispositions=motion_dispositions,
        exposure_legs=(
            [*normalized_motion_active, *motion_legs]
            if motion_active_reason is None
            else []
        ),
        plan_reason=motion_active_reason,
        worst_case_override=(
            MOTION_POLICY.reference_equity
            if motion_active_reason is not None
            else None
        ),
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
    plan_reason: str | None = None,
    worst_case_override: float | None = None,
) -> PortfolioPlan:
    total_spend = sum(leg.spend for leg in legs)
    return PortfolioPlan(
        run_id=run_id,
        risk_profile=sleeve,
        approved=bool(legs),
        legs=legs,
        arbitrage_opportunities=[],
        total_spend=total_spend,
        worst_case_loss=(
            worst_case_override
            if worst_case_override is not None
            else _exposure_summary(exposure_legs).aggregate
        ),
        expected_profit=sum(leg.expected_profit for leg in legs),
        reasons=(
            [plan_reason]
            if plan_reason is not None
            else ([] if legs else ["no portfolio legs passed allocation gates"])
        ),
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
    if opportunity.lead_days < TARGET_POLICY.min_lead_days:
        return "target requires day-ahead lead"
    if decision.edge < 0:
        return "point edge is negative"
    if decision.edge_lcb < 0:
        return "after-fee lower-bound edge is negative"
    return None


def _motion_rejection(opportunity: ResearchOpportunity) -> str | None:
    decision = opportunity.decision
    if opportunity.lead_days < MOTION_POLICY.min_lead_days:
        return "motion target is in the past"
    if decision.edge <= 0:
        return "point edge is not positive"
    if decision.edge_lcb < -0.07:
        return "lower-bound edge is below motion floor"
    if decision.recommended_contracts < 1.0:
        return "candidate liquidity cannot support one contract"
    return None


def _normalize_opportunity(opportunity: ResearchOpportunity) -> _PreparedOpportunity:
    if not isinstance(opportunity, ResearchOpportunity):
        return _PreparedOpportunity(
            opportunity,
            None,
            "candidate opportunity type is invalid",
        )
    if opportunity.pending is not True and opportunity.pending is not False:
        return _PreparedOpportunity(
            opportunity,
            None,
            "candidate pending flag is invalid",
        )
    if (
        isinstance(opportunity.lead_days, bool)
        or not isinstance(opportunity.lead_days, int)
        or opportunity.lead_days < 0
        or not _is_iso_date(opportunity.target_date)
    ):
        return _PreparedOpportunity(
            opportunity,
            None,
            "candidate target metadata is invalid",
        )
    decision, rejection = _normalize_decision(opportunity.decision)
    if rejection is not None:
        return _PreparedOpportunity(opportunity, None, rejection)
    assert decision is not None
    if decision.approved is not True:
        return _PreparedOpportunity(
            opportunity,
            None,
            "candidate gates rejected",
        )
    if city_for_market_ticker(decision.ticker) is None:
        return _PreparedOpportunity(
            opportunity,
            None,
            "candidate city series is unknown",
        )
    return _PreparedOpportunity(
        opportunity,
        replace(opportunity, decision=decision),
        None,
    )


def _normalize_decision(
    decision: object,
) -> tuple[TradeDecision | None, str | None]:
    if not isinstance(decision, TradeDecision):
        return None, "candidate decision type is invalid"
    if not isinstance(decision.ticker, str) or not decision.ticker.strip():
        return None, "candidate ticker is invalid"
    if not isinstance(decision.side, str):
        return None, "candidate side is invalid"
    side = decision.side.strip().upper()
    if side not in {"YES", "NO"}:
        return None, "candidate side is invalid"
    if decision.strike_type not in {"less", "greater", "between"}:
        return None, "candidate strike type is invalid"

    required_names = (
        "probability",
        "probability_lcb",
        "yes_bid",
        "yes_ask",
        "spread",
        "fee_per_contract",
        "cost_per_contract",
        "edge",
        "edge_lcb",
        "kelly_fraction",
        "recommended_contracts",
        "expected_profit",
        "yes_ask_size",
        "trade_quality_score",
    )
    optional_names = (
        "entry_bid",
        "entry_ask",
        "entry_bid_size",
        "entry_ask_size",
        "floor_strike",
        "cap_strike",
        "residual_probability",
        "ensemble_probability",
        "model_probability",
        "market_probability",
        "intraday_probability",
        "remaining_heat_risk",
        "limit_price",
        "limit_fee_per_contract",
        "limit_cost_per_contract",
        "limit_edge",
        "limit_edge_lcb",
    )
    normalized: dict[str, float | None] = {}
    for name in required_names:
        parsed = _plain_finite_float(getattr(decision, name))
        if parsed is None:
            return None, f"candidate numeric field {name} is invalid"
        normalized[name] = parsed
    for name in optional_names:
        raw = getattr(decision, name)
        if raw is None:
            normalized[name] = None
            continue
        parsed = _plain_finite_float(raw)
        if parsed is None:
            return None, f"candidate numeric field {name} is invalid"
        normalized[name] = parsed

    probability_names = (
        "probability",
        "probability_lcb",
        "residual_probability",
        "ensemble_probability",
        "model_probability",
        "market_probability",
        "intraday_probability",
    )
    if any(
        normalized[name] is not None
        and not 0.0 <= float(normalized[name]) <= 1.0
        for name in probability_names
    ):
        return None, "candidate probability is outside [0, 1]"
    price_names = ("yes_bid", "yes_ask", "entry_bid", "entry_ask", "limit_price")
    if any(
        normalized[name] is not None
        and not 0.0 <= float(normalized[name]) <= 1.0
        for name in price_names
    ):
        return None, "candidate price is outside [0, 1]"
    if not MIN_EXECUTABLE_CONTRACT_COST <= float(
        normalized["cost_per_contract"]
    ) <= MAX_EXECUTABLE_CONTRACT_COST:
        return None, "candidate cost is outside the executable 1-99 cent range"
    limit_cost = normalized["limit_cost_per_contract"]
    if limit_cost is not None and not MIN_EXECUTABLE_CONTRACT_COST <= float(
        limit_cost
    ) <= MAX_EXECUTABLE_CONTRACT_COST:
        return None, "candidate limit cost is outside the executable 1-99 cent range"
    for name in ("entry_ask", "limit_price"):
        value = normalized[name]
        if value is not None and not MIN_EXECUTABLE_CONTRACT_COST <= float(
            value
        ) <= MAX_EXECUTABLE_CONTRACT_COST:
            return None, f"candidate {name} is outside the executable 1-99 cent range"
    nonnegative_names = (
        "spread",
        "fee_per_contract",
        "kelly_fraction",
        "recommended_contracts",
        "yes_ask_size",
        "entry_bid_size",
        "entry_ask_size",
        "limit_fee_per_contract",
    )
    if any(
        normalized[name] is not None and float(normalized[name]) < 0.0
        for name in nonnegative_names
    ):
        return None, "candidate size, spread, fee, or Kelly value is negative"
    if float(normalized["recommended_contracts"]) <= 0.0:
        return None, "candidate has no executable quantity"

    floor = normalized["floor_strike"]
    cap = normalized["cap_strike"]
    if decision.strike_type == "less":
        if floor is not None or cap is None:
            return None, "less strike requires only a finite cap"
    elif decision.strike_type == "greater":
        if floor is None or cap is not None:
            return None, "greater strike requires only a finite floor"
    elif floor is None or cap is None or floor > cap:
        return None, "between strike requires ordered finite bounds"

    contracts = float(normalized["recommended_contracts"])
    edge = float(normalized["edge"])
    normalized["expected_profit"] = edge * contracts
    return replace(decision, side=side, **normalized), None


def _normalize_active_legs(
    legs: Sequence[PortfolioLeg],
    *,
    policy: ResearchSleevePolicy,
) -> tuple[list[PortfolioLeg], str | None]:
    normalized: list[PortfolioLeg] = []
    for index, leg in enumerate(legs):
        prefix = f"{policy.sleeve.value} active exposure is invalid at leg {index}"
        if not isinstance(leg, PortfolioLeg):
            return [], f"{prefix}: leg type"
        if leg.sleeve != policy.sleeve.value:
            return [], f"{prefix}: sleeve identity"
        if leg.account_id != policy.account_id:
            return [], f"{prefix}: account identity"
        if not _is_iso_date(leg.target_date):
            return [], f"{prefix}: target date"
        if type(leg.pending) is not bool or type(leg.is_partial_child) is not bool:
            return [], f"{prefix}: pending/child flags"
        if leg.logical_position_id is not None and not _valid_logical_position_id(
            leg.logical_position_id
        ):
            return [], f"{prefix}: logical identity"
        if leg.is_partial_child and leg.logical_position_id is None:
            return [], f"{prefix}: child logical identity"
        if leg.is_partial_child and leg.pending:
            return [], f"{prefix}: pending partial child"
        spend = _plain_finite_float(leg.spend)
        expected_profit = _plain_finite_float(leg.expected_profit)
        growth_score = _plain_finite_float(leg.growth_score)
        if spend is None or spend <= 0:
            return [], f"{prefix}: spend"
        if expected_profit is None or growth_score is None:
            return [], f"{prefix}: scores"
        decision, rejection = _normalize_decision(leg.decision)
        if rejection is not None or decision is None:
            return [], f"{prefix}: {rejection or 'decision'}"
        city = city_for_market_ticker(decision.ticker)
        if city is None:
            return [], f"{prefix}: city series"
        canonical_region = REGION_BY_SERIES.get(city.series_ticker, "unknown")
        if leg.region is not None and leg.region != canonical_region:
            return [], f"{prefix}: region identity"
        exact_spend = decision.recommended_contracts * decision.cost_per_contract
        if not math.isclose(spend, exact_spend, rel_tol=1e-9, abs_tol=1e-9):
            return [], f"{prefix}: spend does not match contracts times cost"
        normalized.append(
            replace(
                leg,
                decision=decision,
                spend=spend,
                expected_profit=expected_profit,
                growth_score=growth_score,
                target_date=str(leg.target_date),
                region=canonical_region,
            )
        )

    roots: dict[str | int, PortfolioLeg] = {}
    for index, leg in enumerate(normalized):
        logical_id = leg.logical_position_id
        if leg.is_partial_child or logical_id is None:
            continue
        if logical_id in roots:
            return [], (
                f"{policy.sleeve.value} active exposure is invalid at leg {index}: "
                "duplicate logical root"
            )
        roots[logical_id] = leg
    for index, child in enumerate(normalized):
        if not child.is_partial_child:
            continue
        root = roots.get(child.logical_position_id)
        if root is None:
            return [], (
                f"{policy.sleeve.value} active exposure is invalid at leg {index}: "
                "orphan partial child"
            )
        if _logical_leg_identity(child) != _logical_leg_identity(root):
            return [], (
                f"{policy.sleeve.value} active exposure is invalid at leg {index}: "
                "partial child identity mismatch"
            )
    return normalized, None


def _valid_logical_position_id(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, str) and bool(value.strip())


def _logical_leg_identity(leg: PortfolioLeg) -> tuple[object, ...]:
    decision = leg.decision
    return (
        leg.sleeve,
        leg.account_id,
        leg.target_date,
        decision.ticker,
        decision.side,
        decision.strike_type,
        decision.floor_strike,
        decision.cap_strike,
    )


def _plain_finite_float(value: object) -> float | None:
    """Accept only plain Python int/float evidence, never coercible objects."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


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
    contracts = min(MAX_TARGET_CONTRACTS, math.floor(max_contracts + 1e-12))
    if contracts < 1:
        return None
    return replace(
        decision,
        recommended_contracts=float(contracts),
        expected_profit=float(decision.edge) * float(contracts),
    )


def _prepare_target(source: _PreparedOpportunity) -> _PreparedTarget:
    opportunity = source.normalized
    rejection = source.rejection
    if rejection is None and opportunity is not None:
        rejection = _target_rejection(opportunity)
    sized = (
        None
        if rejection is not None or opportunity is None
        else _target_sized_decision(opportunity.decision)
    )
    return _PreparedTarget(source, rejection, sized)


def _fit_target_leg(
    opportunity: ResearchOpportunity,
    *,
    desired: TradeDecision,
    remaining_cash: float,
    exposure_legs: Sequence[PortfolioLeg],
) -> tuple[PortfolioLeg | None, str]:
    """Return the largest integer target quantity that satisfies every cap.

    Scenario loss can include offsets from existing mutually exclusive legs,
    so the exact descending re-evaluation is intentionally preferred to a
    ratio-based clip that assumes spend and settlement loss are identical.
    """

    cost = float(desired.cost_per_contract)
    desired_contracts = min(
        MAX_TARGET_CONTRACTS,
        int(desired.recommended_contracts),
    )
    cash_ratio = max(0.0, remaining_cash) / cost
    if not math.isfinite(cash_ratio) or cash_ratio >= desired_contracts:
        cash_contracts = desired_contracts
    else:
        cash_contracts = math.floor(cash_ratio + 1e-9)
    upper = min(desired_contracts, cash_contracts)
    if upper < 1:
        return None, "target cash cap cannot fund one contract"

    last_reason = "target scenario-loss capacity cannot fund one contract"
    for contracts in range(upper, 0, -1):
        decision = replace(
            desired,
            recommended_contracts=float(contracts),
            expected_profit=float(desired.edge) * float(contracts),
        )
        leg = _research_leg(opportunity, decision, sleeve="target")
        reason = _risk_cap_reason(
            [*exposure_legs, leg],
            candidate=leg,
            policy=TARGET_POLICY,
        )
        if reason is None:
            return leg, ""
        last_reason = reason
    return None, last_reason


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
    prepared: _PreparedTarget,
) -> tuple[float, float, str, str, str]:
    opportunity = prepared.source.normalized or prepared.source.original
    decision = prepared.sized_decision
    if decision is None:
        return (
            math.inf,
            math.inf,
            str(opportunity.target_date),
            str(opportunity.decision.ticker),
            str(opportunity.decision.side).upper(),
        )
    cost = float(decision.cost_per_contract)
    edge_lcb = float(decision.edge_lcb)
    contracts = float(decision.recommended_contracts)
    conservative_profit = edge_lcb * contracts
    worst_case_dollar = cost * contracts
    conservative_per_worst_dollar = (
        conservative_profit / worst_case_dollar if worst_case_dollar > 0 else -math.inf
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


def _normalize_account_inputs(**values: object) -> tuple[float, float, float, float]:
    normalized: dict[str, float] = {}
    for name, value in values.items():
        parsed = _plain_finite_float(value)
        if parsed is None:
            raise ValueError(f"{name} must be a finite plain int or float")
        if name.endswith("available_cash") and parsed < 0:
            raise ValueError(f"{name} must be non-negative")
        normalized[name] = parsed
    return (
        normalized["target_available_cash"],
        normalized["motion_available_cash"],
        normalized["realized_today"],
        normalized["motion_realized_today"],
    )


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
    transitions: set[int] = set()
    for leg in legs:
        decision = leg.decision
        if decision.strike_type == "less":
            assert decision.cap_strike is not None
            transitions.add(math.ceil(decision.cap_strike))
        elif decision.strike_type == "greater":
            assert decision.floor_strike is not None
            transitions.add(math.floor(decision.floor_strike) + 1)
        else:
            assert decision.floor_strike is not None
            assert decision.cap_strike is not None
            transitions.add(math.ceil(decision.floor_strike))
            transitions.add(math.floor(decision.cap_strike) + 1)
    if not transitions:
        return ()
    # Every typed payoff is constant between adjacent transitions.  One value
    # below the first transition plus each transition start therefore covers
    # every distinct integer-settlement payoff vector without expanding across
    # the numeric strike range.
    return tuple(sorted({min(transitions) - 1, *transitions}))


def _motion_priority(source: _PreparedOpportunity) -> tuple[float, str, str, str]:
    opportunity = source.normalized or source.original
    decision = opportunity.decision
    edge = _plain_finite_float(decision.edge)
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
        ticker=str(getattr(opportunity.decision, "ticker", "<invalid>")),
        target_date=str(opportunity.target_date),
        side=_audit_side(getattr(opportunity.decision, "side", None)),
        sleeve=sleeve,
        status=status,
        reason=reason,
        contracts=contracts,
    )


def _audit_side(value: object) -> str:
    if not isinstance(value, str):
        return "<INVALID>"
    normalized = value.strip().upper()
    return normalized if normalized in {"YES", "NO"} else "<INVALID>"
