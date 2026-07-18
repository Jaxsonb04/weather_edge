from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from typing import NoReturn

from .arbitrage import ArbitrageOpportunity
from .config import StrategyConfig
from .consensus import MarketConsensus
from .db import (
    PaperStore,
    ResearchDecisionEvidence,
    ResearchDecisionIdentity,
)
from .fees import (
    contracts_for_budget,
    quadratic_fee_average_per_contract,
)
from .execution import buy_limit_for_decision, target_research_quote, with_buy_limit
from .models import EventSnapshot, ForecastSnapshot, IntradaySnapshot, TradeDecision
from .research_policy import MOTION_POLICY, TARGET_POLICY, ResearchSleevePolicy
from .research_portfolio import ResearchPlans


class ArbitrageContainmentError(RuntimeError):
    """A partial arbitrage group still has active financial exposure."""


def with_motion_taker_execution(
    decision: TradeDecision,
    config: StrategyConfig,
) -> TradeDecision | None:
    """Reprice one motion entry at the contemporaneous visible ask.

    Motion is evidence-seeking but never invents liquidity: exactly one whole
    contract must be displayed, point edge must remain positive after the exact
    taker fee, and the fixed research lower-bound floor still applies.
    """

    if not decision.approved:
        return None
    try:
        ask = float(decision.ask)
        ask_size = float(decision.ask_size)
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0.0 < ask < 1.0 or ask_size + 1e-9 < 1.0:
        return None
    fee = quadratic_fee_average_per_contract(
        ask,
        1.0,
        maker=False,
        fee_multiplier=config.fee_multiplier,
        taker_rate=config.taker_fee_rate,
        maker_rate=config.maker_fee_rate,
        series_ticker=decision.ticker,
    )
    cost = ask + fee
    point_probability = (
        float(decision.model_probability)
        if config.edge_gate_uses_model_probability
        and decision.model_probability is not None
        else float(decision.probability)
    )
    edge = point_probability - cost
    edge_lcb = float(decision.probability_lcb) - cost
    if edge <= 0.0 or edge_lcb + 1e-12 < config.min_edge_lcb:
        return None
    return replace(
        decision,
        fee_per_contract=fee,
        cost_per_contract=cost,
        edge=edge,
        edge_lcb=edge_lcb,
        recommended_contracts=1.0,
        expected_profit=edge,
        limit_price=None,
        limit_fee_per_contract=None,
        limit_cost_per_contract=None,
        limit_edge=None,
        limit_edge_lcb=None,
    )


def with_target_research_execution(
    decision: TradeDecision,
    config: StrategyConfig,
) -> TradeDecision | None:
    quote = target_research_quote(decision, config)
    if quote is None:
        return None
    return replace(
        decision,
        fee_per_contract=quote.fee_per_contract,
        cost_per_contract=quote.cost_per_contract,
        edge=quote.edge,
        edge_lcb=quote.edge_lcb,
        recommended_contracts=quote.contracts,
        expected_profit=quote.edge * quote.contracts,
        limit_price=quote.price,
        limit_fee_per_contract=quote.fee_per_contract,
        limit_cost_per_contract=quote.cost_per_contract,
        limit_edge=quote.edge,
        limit_edge_lcb=quote.edge_lcb,
    )


def prepare_research_sleeve_decisions(
    structural_decisions: list[TradeDecision],
    config: StrategyConfig,
) -> tuple[list[TradeDecision], list[TradeDecision]]:
    """Apply exact target/motion edge and quote policy to structural signals."""

    target: list[TradeDecision] = []
    motion: list[TradeDecision] = []
    for decision in structural_decisions:
        if not decision.approved:
            structural_reason = (
                decision.entry_block_reason
                or (decision.reasons[0] if decision.reasons else None)
                or "research structural gate rejected candidate"
            )
            blocked = replace(
                decision,
                approved=False,
                signal_approved=False,
                entry_block_reason=structural_reason,
                recommended_contracts=0.0,
                expected_profit=0.0,
            )
            target.append(blocked)
            motion.append(blocked)
            continue
        target_quote = with_target_research_execution(decision, config)
        if target_quote is None:
            target.append(
                replace(
                    decision,
                    approved=False,
                    signal_approved=True,
                    entry_block_reason=(
                        "target requires non-negative point and after-fee LCB edge"
                    ),
                    recommended_contracts=0.0,
                    expected_profit=0.0,
                )
            )
        else:
            target.append(target_quote)
        motion_quote = with_motion_taker_execution(decision, config)
        if motion_quote is None:
            motion.append(
                replace(
                    decision,
                    approved=False,
                    signal_approved=True,
                    entry_block_reason=(
                        "motion requires point-positive visible-ask taker edge "
                        "and lower-bound edge >= -0.07"
                    ),
                    recommended_contracts=0.0,
                    expected_profit=0.0,
                )
            )
        else:
            motion.append(motion_quote)
    return target, motion


@dataclass(frozen=True)
class ResearchExecutionResult:
    """Exact evidence and filled/reserved order ids for both research books."""

    target_decision_ids: tuple[int, ...]
    motion_decision_ids: tuple[int, ...]
    target_order_ids: tuple[int, ...]
    motion_order_ids: tuple[int, ...]


@dataclass(frozen=True)
class _ResearchAttempt:
    policy: ResearchSleevePolicy
    decision: TradeDecision
    identity: ResearchDecisionIdentity
    admission_pending: bool


class PaperTrader:
    def __init__(
        self,
        store: PaperStore,
        config: StrategyConfig | None = None,
        *,
        risk_profile: str | None = None,
        entry_mode: str = "market",
        series_ticker: str | None = None,
    ) -> None:
        self.store = store
        self.config = config or StrategyConfig()
        self.risk_profile = risk_profile
        self.entry_mode = _normalize_entry_mode(entry_mode)
        # Scopes the per-target exposure cap to one city's event. None keeps
        # the legacy single-book semantics (cap across the whole date).
        self.series_ticker = series_ticker

    def with_paper_stake(self, decision: TradeDecision, stake_dollars: float | None) -> TradeDecision:
        if stake_dollars is None or not decision.approved:
            return decision
        if stake_dollars <= 0:
            raise ValueError("paper stake must be greater than zero")
        if decision.ask <= 0:
            return decision

        entry_price = decision.ask
        maker = False
        if self.entry_mode == "limit":
            quote = buy_limit_for_decision(decision, self.config)
            if quote is None:
                return with_buy_limit(decision, self.config)
            entry_price = quote.price
            maker = not quote.would_cross

        contracts = contracts_for_budget(
            entry_price,
            stake_dollars,
            maker=maker,
            fee_multiplier=self.config.fee_multiplier,
            taker_rate=self.config.taker_fee_rate,
            maker_rate=self.config.maker_fee_rate,
            series_ticker=decision.ticker,
        )
        contracts = min(contracts, self.config.max_contracts_per_market)
        if not maker and decision.ask_size > 0:
            contracts = min(contracts, decision.ask_size)
        if not self.config.allow_fractional_contracts:
            contracts = float(int(contracts))
        fee_per_contract = quadratic_fee_average_per_contract(
            entry_price,
            contracts,
            maker=maker,
            fee_multiplier=self.config.fee_multiplier,
            taker_rate=self.config.taker_fee_rate,
            maker_rate=self.config.maker_fee_rate,
            series_ticker=decision.ticker,
        )
        cost_per_contract = entry_price + fee_per_contract
        edge = decision.probability - cost_per_contract
        edge_lcb = decision.probability_lcb - cost_per_contract
        return replace(
            decision,
            fee_per_contract=fee_per_contract,
            cost_per_contract=cost_per_contract,
            edge=edge,
            edge_lcb=edge_lcb,
            recommended_contracts=contracts,
            expected_profit=edge * contracts,
        )

    def with_paper_stakes(
        self,
        decisions: list[TradeDecision],
        stake_dollars: float | None,
    ) -> list[TradeDecision]:
        return [self.with_paper_stake(decision, stake_dollars) for decision in decisions]

    def with_daily_budget(
        self,
        decisions: list[TradeDecision],
        daily_budget: float | None,
    ) -> list[TradeDecision]:
        if daily_budget is None:
            return decisions
        if daily_budget < 0:
            raise ValueError("daily budget cannot be negative")
        approved = [
            decision
            for decision in decisions
            if decision.approved and decision.cost_per_contract > 0
        ]
        if not approved:
            return decisions
        if daily_budget == 0:
            approved_keys = {_decision_key(decision) for decision in approved}
            return [
                replace(decision, recommended_contracts=0.0, expected_profit=0.0)
                if _decision_key(decision) in approved_keys
                else decision
                for decision in decisions
            ]
        total_risk_spend = sum(
            decision.recommended_contracts * decision.cost_per_contract
            for decision in approved
        )
        if total_risk_spend <= daily_budget:
            return decisions
        scale = daily_budget / total_risk_spend
        approved_keys = {_decision_key(decision) for decision in approved}
        adjusted: list[TradeDecision] = []
        for decision in decisions:
            if _decision_key(decision) in approved_keys:
                contracts = decision.recommended_contracts * scale
                adjusted.append(
                    replace(
                        decision,
                        recommended_contracts=contracts,
                        expected_profit=decision.edge * contracts,
                    )
                )
            else:
                adjusted.append(decision)
        return adjusted

    def with_entry_mode(self, decisions: list[TradeDecision]) -> list[TradeDecision]:
        if self.entry_mode != "limit":
            return decisions
        return [
            with_buy_limit(decision, self.config)
            if decision.approved and decision.recommended_contracts > 0
            else decision
            for decision in decisions
        ]

    def execute_research_plans(
        self,
        target_date: str,
        plans: ResearchPlans,
        *,
        source_decisions: list[TradeDecision],
        motion_source_decisions: list[TradeDecision] | None = None,
        objective_day: str,
        lead_bucket: str,
        scan_run_id: str,
        observed_high_state: str,
        forecast: ForecastSnapshot | None = None,
        intraday: IntradaySnapshot | None = None,
        event: EventSnapshot | None = None,
        market_consensus: MarketConsensus | None = None,
        forecast_snapshot_id: int | None = None,
        market_snapshot_id: int | None = None,
        admit_orders: bool = True,
        admit_target_orders: bool | None = None,
        admit_motion_orders: bool | None = None,
    ) -> ResearchExecutionResult:
        """Persist and independently admit target and motion plans.

        The caller supplies one already-built scan context.  This method only
        clones those immutable inputs into per-sleeve audit rows; it never
        fetches forecasts, markets, or probabilities and never touches the
        legacy research-shadow placement path.
        """

        if self.risk_profile != "research":
            raise ValueError("research plans require the research profile")
        canonical = StrategyConfig()
        # Equality against the actual profile is performed by the evidence and
        # admission APIs.  This local type check keeps malformed callers from
        # reaching either journal path.
        if not isinstance(self.config, type(canonical)):
            raise ValueError("research plans require a strategy configuration")
        source_by_key = {
            _decision_key(decision): decision for decision in source_decisions
        }
        motion_source_by_key = {
            _decision_key(decision): decision
            for decision in (
                source_decisions
                if motion_source_decisions is None
                else motion_source_decisions
            )
        }
        target_admission_enabled = (
            admit_orders if admit_target_orders is None else admit_target_orders
        )
        motion_admission_enabled = (
            admit_orders if admit_motion_orders is None else admit_motion_orders
        )
        target_attempts = self._prepare_research_plan(
            target_date,
            plans.target,
            policy=TARGET_POLICY,
            source_by_key=source_by_key,
            objective_day=objective_day,
            lead_bucket=lead_bucket,
            scan_run_id=scan_run_id,
            observed_high_state=observed_high_state,
            intraday=intraday,
            admit_orders=target_admission_enabled,
        )
        motion_attempts = self._prepare_research_plan(
            target_date,
            plans.motion,
            policy=MOTION_POLICY,
            source_by_key=motion_source_by_key,
            objective_day=objective_day,
            lead_bucket=lead_bucket,
            scan_run_id=scan_run_id,
            observed_high_state=observed_high_state,
            intraday=intraday,
            admit_orders=motion_admission_enabled,
        )
        attempts = [*target_attempts, *motion_attempts]
        decision_ids = self.store.record_research_decision_batch(
            target_date,
            [
                ResearchDecisionEvidence(
                    decision=attempt.decision,
                    identity=attempt.identity,
                    admission_pending=attempt.admission_pending,
                )
                for attempt in attempts
            ],
            forecast=forecast,
            intraday=intraday,
            event=event,
            market_consensus=market_consensus,
            strategy_config=self.config,
            forecast_snapshot_id=forecast_snapshot_id,
            market_snapshot_id=market_snapshot_id,
        )
        target_decision_ids: list[int] = []
        motion_decision_ids: list[int] = []
        target_order_ids: list[int] = []
        motion_order_ids: list[int] = []
        for attempt, decision_id in zip(attempts, decision_ids, strict=True):
            sleeve_decision_ids = (
                target_decision_ids
                if attempt.policy is TARGET_POLICY
                else motion_decision_ids
            )
            sleeve_order_ids = (
                target_order_ids
                if attempt.policy is TARGET_POLICY
                else motion_order_ids
            )
            sleeve_decision_ids.append(decision_id)
            if not attempt.admission_pending:
                continue
            order_id = self.store.record_research_order_atomic(
                target_date,
                attempt.decision,
                admission=attempt.identity.admission(decision_id),
                strategy_config=self.config,
            )
            if order_id is not None:
                sleeve_order_ids.append(order_id)
            else:
                self.store.mark_research_decision_admission_blocked(
                    decision_id,
                    "atomic research admission rejected",
                )
        return ResearchExecutionResult(
            target_decision_ids=tuple(target_decision_ids),
            motion_decision_ids=tuple(motion_decision_ids),
            target_order_ids=tuple(target_order_ids),
            motion_order_ids=tuple(motion_order_ids),
        )

    def _prepare_research_plan(
        self,
        target_date: str,
        plan,
        *,
        policy: ResearchSleevePolicy,
        source_by_key: dict[tuple[str, str], TradeDecision],
        objective_day: str,
        lead_bucket: str,
        scan_run_id: str,
        observed_high_state: str,
        intraday: IntradaySnapshot | None,
        admit_orders: bool,
    ) -> list[_ResearchAttempt]:
        selected_by_key = {
            _decision_key(leg.decision): leg.decision for leg in plan.legs
        }
        attempts: list[_ResearchAttempt] = []
        for disposition in plan.dispositions:
            key = (str(disposition.ticker), str(disposition.side).upper())
            base = selected_by_key.get(key) or source_by_key.get(key)
            if base is None:
                continue
            selected = disposition.status == "selected" and key in selected_by_key
            prepared = _prepare_research_disposition(
                base,
                policy=policy,
                selected=selected,
                reason=disposition.reason,
                config=self.config,
            )
            if policy is MOTION_POLICY and selected and prepared.approved:
                reentry_reason = self.store.motion_reentry_block_reason(
                    target_date=target_date,
                    market_ticker=prepared.ticker,
                    side=prepared.side,
                    scan_run_id=scan_run_id,
                    executable_price=float(prepared.ask),
                    probability=float(prepared.probability),
                    intraday_observed_high_f=(
                        intraday.observed_high_f if intraday is not None else None
                    ),
                    intraday_is_complete=(
                        bool(intraday.is_complete) if intraday is not None else False
                    ),
                )
                if reentry_reason is not None:
                    prepared = replace(
                        prepared,
                        approved=False,
                        signal_approved=True,
                        entry_block_reason=reentry_reason,
                        recommended_contracts=0.0,
                        expected_profit=0.0,
                        reasons=[*prepared.reasons, reentry_reason],
                    )
                    selected = False
            if selected and prepared.approved and not admit_orders:
                block_reason = "research order admission disabled"
                prepared = replace(
                    prepared,
                    approved=False,
                    signal_approved=True,
                    entry_block_reason=block_reason,
                    recommended_contracts=0.0,
                    expected_profit=0.0,
                    reasons=[*prepared.reasons, block_reason],
                )
                selected = False
            executable_price = (
                float(prepared.limit_price)
                if prepared.limit_price is not None
                else float(prepared.ask)
            )
            reentry_fingerprint = research_reentry_fingerprint(
                account_id=policy.account_id,
                sleeve=policy.sleeve.value,
                scan_run_id=scan_run_id,
                ticker=prepared.ticker,
                side=prepared.side,
                executable_price=executable_price,
                probability=prepared.probability,
                observed_high_state=observed_high_state,
            )
            identity = ResearchDecisionIdentity.for_policy(
                policy,
                objective_day=objective_day,
                lead_bucket=lead_bucket,
                scan_run_id=scan_run_id,
                reentry_fingerprint=reentry_fingerprint,
            )
            attempts.append(
                _ResearchAttempt(
                    policy=policy,
                    decision=prepared,
                    identity=identity,
                    admission_pending=bool(
                        admit_orders and selected and prepared.approved
                    ),
                )
            )
        return attempts

    def place_approved(
        self,
        target_date: str,
        decisions: list[TradeDecision],
        *,
        stake_dollars: float | None = None,
        daily_budget: float | None = None,
        bankroll: float | None = None,
        group_id: str | None = None,
    ) -> list[int]:
        if stake_dollars is not None and daily_budget is not None:
            raise ValueError("use either paper stake or daily budget, not both")
        if bankroll is not None:
            pause_reason = self.store.paper_entry_pause_reason(
                self.risk_profile,
                bankroll=bankroll,
                target_date=target_date,
            )
            if pause_reason is not None:
                return []
        if daily_budget is not None:
            decisions = self.with_daily_budget(decisions, daily_budget)
        exposure_remaining = self._target_exposure_remaining(target_date, bankroll)
        order_ids = []
        for decision in decisions:
            adjusted = self.with_paper_stake(decision, stake_dollars)
            if not adjusted.approved or adjusted.recommended_contracts <= 0:
                continue
            adjusted = self._normalize_contracts(adjusted)
            if adjusted is None:
                continue
            if adjusted.cost_per_contract >= 1.0 or adjusted.cost_per_contract <= 0:
                continue
            shadow_id: int | None = None
            if _is_research_shadow_candidate(self.risk_profile, adjusted):
                sample_probability = _clamp_probability(
                    self.config.research_shadow_sample_probability
                )
                sampled = _deterministic_sample(
                    target_date,
                    adjusted,
                    sample_probability=sample_probability,
                )
                shadow_id = self.store.record_research_shadow_order(
                    target_date,
                    adjusted,
                    risk_profile=self.risk_profile,
                    sample_probability=sample_probability,
                    sampled=sampled,
                    strategy_config=self.config,
                )
                # Negative-confidence experiments are evidence rows only. They
                # never consume the shared account merely to manufacture volume.
                if adjusted.edge_lcb < 0.0 or not sampled:
                    continue
                if self.store.has_losing_closed_negative_lcb_research_entry(
                    target_date,
                    adjusted.ticker,
                    adjusted.side,
                ):
                    continue
                adjusted = self._fit_research_shadow_sample(
                    target_date,
                    adjusted,
                    bankroll,
                )
                if adjusted is None:
                    continue
            # Deliberately side-agnostic: one open position per market. Holding
            # YES and NO on the same bucket locks in the combined entry costs
            # plus fees, so an open position blocks the opposite side too; the
            # monitor's exit rules manage the existing leg instead.
            if self.store.has_active_paper_entry(
                target_date,
                adjusted.ticker,
                risk_profile=self.risk_profile,
            ):
                continue
            entries = self.store.entries_for_market_side(
                target_date,
                adjusted.ticker,
                adjusted.side,
                risk_profile=self.risk_profile,
            )
            if entries >= self.config.max_entries_per_market_side:
                continue
            if exposure_remaining is not None:
                adjusted = self._fit_to_exposure(adjusted, exposure_remaining)
                if adjusted is None:
                    continue
            status = "PAPER_FILLED"
            entry_mode = "market"
            if self.entry_mode == "limit":
                quote = buy_limit_for_decision(adjusted, self.config)
                if quote is None:
                    continue
                # A crossing limit is an instant taker fill against the
                # visible ask, so it can never take more than the displayed
                # depth. A RESTING quote is deliberately NOT ask-capped: its
                # fill is gated by FUTURE traded volume via the queue-ahead
                # fill model, not by the ask displayed at entry (sizing no
                # longer applies this taker-era cap -- see RiskManager).
                if quote.would_cross:
                    adjusted = _clamp_to_displayed_ask(adjusted)
                    if adjusted is None:
                        continue
                adjusted = with_buy_limit(adjusted, self.config)
                if not adjusted.approved:
                    continue
                status = "PAPER_FILLED" if quote.would_cross else "PAPER_LIMIT_RESTING"
                entry_mode = "limit"
            else:
                # Market entry takes immediately at the displayed ask; cap the
                # size at what the book actually displays.
                adjusted = _clamp_to_displayed_ask(adjusted)
                if adjusted is None:
                    continue
            if bankroll is not None:
                adjusted = self._fit_to_account_policy(target_date, adjusted)
                if adjusted is None:
                    continue
                # Fees depend on final contract count. Recompute once after
                # sizing while preserving the single bid+tick/taker price rule.
                if self.entry_mode == "limit":
                    quote = buy_limit_for_decision(adjusted, self.config)
                    if quote is None:
                        continue
                    adjusted = with_buy_limit(adjusted, self.config)
                    status = "PAPER_FILLED" if quote.would_cross else "PAPER_LIMIT_RESTING"
            order_id = self.store.record_paper_order(
                target_date,
                adjusted,
                risk_profile=self.risk_profile,
                status=status,
                entry_mode=entry_mode,
                group_id=group_id,
                strategy_config=self.config,
            )
            # None == the open-position guard index rejected a concurrent
            # duplicate the side-agnostic check above raced past. Skip it and do
            # not consume exposure for a position that was never recorded.
            if order_id is None:
                continue
            order_ids.append(order_id)
            if shadow_id is not None:
                self.store.link_research_shadow_order(shadow_id, order_id)
            if exposure_remaining is not None:
                exposure_remaining -= adjusted.recommended_contracts * adjusted.cost_per_contract
        return order_ids

    def _fit_to_account_policy(
        self, target_date: str, decision: TradeDecision
    ) -> TradeDecision | None:
        cost = float(decision.limit_cost_per_contract or decision.cost_per_contract)
        requested = float(decision.recommended_contracts) * cost
        capacity = self.store.account_policy_capacity(
            target_date=target_date,
            market_ticker=decision.ticker,
            risk_profile=self.risk_profile,
            requested_spend=requested,
        )
        allowed = float(capacity["allowed_spend"])
        if allowed <= 0:
            return None
        contracts = min(float(decision.recommended_contracts), allowed / cost)
        if not self.config.allow_fractional_contracts:
            contracts = float(int(contracts))
        while contracts > 0:
            entry_price = float(decision.limit_price or decision.ask)
            maker = decision.limit_price is not None and entry_price < float(decision.ask) - 1e-12
            fee = quadratic_fee_average_per_contract(
                entry_price,
                contracts,
                maker=maker,
                fee_multiplier=self.config.fee_multiplier,
                taker_rate=self.config.taker_fee_rate,
                maker_rate=self.config.maker_fee_rate,
                series_ticker=decision.ticker,
            )
            exact_cost = entry_price + fee
            if contracts * exact_cost <= allowed + 1e-9:
                break
            contracts = contracts - 1.0 if not self.config.allow_fractional_contracts else allowed / exact_cost
        if contracts <= 0 or contracts * exact_cost < 5.0 - 1e-9:
            return None
        edge = decision.probability - exact_cost
        edge_lcb = decision.probability_lcb - exact_cost
        changes = {
            "recommended_contracts": contracts,
            "fee_per_contract": fee,
            "cost_per_contract": exact_cost,
            "edge": edge,
            "edge_lcb": edge_lcb,
            "expected_profit": edge * contracts,
        }
        if decision.limit_price is not None:
            changes.update(
                limit_fee_per_contract=fee,
                limit_cost_per_contract=exact_cost,
                limit_edge=edge,
                limit_edge_lcb=edge_lcb,
            )
        return replace(decision, **changes)

    def record_research_shadow_candidates(
        self,
        target_date: str,
        decisions: list[TradeDecision],
        *,
        sample_probability: float | None = None,
        sampled: bool = False,
    ) -> list[int]:
        """Record research exploration signals even when paper entry is blocked.

        Shadow rows are a data-collection ledger, not a paper PnL ledger. They
        preserve the signal size and reason context so Strategy Lab can learn
        from paused/rejected paper-entry windows without opening risk.
        """

        if (self.risk_profile or "").strip().lower().replace("_", "-") != "research":
            return []
        probability = _clamp_probability(
            self.config.research_shadow_sample_probability
            if sample_probability is None
            else sample_probability
        )
        shadow_ids: list[int] = []
        for decision in decisions:
            if decision.recommended_contracts <= 0:
                continue
            if not _is_research_shadow_candidate(
                self.risk_profile,
                decision,
                include_positive_lcb=True,
                require_explore_sleeve=False,
            ):
                continue
            shadow_ids.append(
                self.store.record_research_shadow_order(
                    target_date,
                    decision,
                    risk_profile=self.risk_profile,
                    sample_probability=probability,
                    sampled=sampled,
                    strategy_config=self.config,
                )
            )
        return shadow_ids

    def place_arbitrage(
        self,
        target_date: str,
        opportunity: ArbitrageOpportunity,
        *,
        bankroll: float | None = None,
    ) -> list[int]:
        """Record an approved arbitrage portfolio as one preflighted group.

        A same-market YES+NO box deliberately violates the normal side-agnostic
        single-position guard inside one portfolio. Existing open exposure still
        blocks the group before any new leg is recorded.
        """

        if not opportunity.approved or not opportunity.legs:
            return []
        if bankroll is not None:
            pause_reason = self.store.paper_entry_pause_reason(
                self.risk_profile,
                bankroll=bankroll,
                target_date=target_date,
            )
            if pause_reason is not None:
                return []
        decisions = list(opportunity.decisions)
        if any(not decision.approved or decision.recommended_contracts <= 0 for decision in decisions):
            return []
        if any(decision.cost_per_contract <= 0 or decision.cost_per_contract >= 1 for decision in decisions):
            return []

        tickers = {decision.ticker for decision in decisions}
        for ticker in tickers:
            if self.store.has_active_paper_entry(
                target_date,
                ticker,
                risk_profile=self.risk_profile,
            ):
                return []
        for decision in decisions:
            entries = self.store.entries_for_market_side(
                target_date,
                decision.ticker,
                decision.side,
                risk_profile=self.risk_profile,
            )
            if entries >= self.config.max_entries_per_market_side:
                return []

        adjusted = opportunity
        exposure_remaining = self._target_exposure_remaining(target_date, bankroll)
        if exposure_remaining is not None:
            adjusted = self._fit_arbitrage_to_exposure(opportunity, exposure_remaining)
            if adjusted is None:
                return []

        normalized = self._normalize_arbitrage_contracts(adjusted)
        if normalized is None:
            return []

        # Tag every leg with one group id so the monitor treats the portfolio
        # as a single guaranteed-payoff structure: it must hold all legs to
        # settlement instead of closing one leg on an intraday take-profit /
        # stop-loss, which would convert the locked payout into naked risk.
        group_id = f"ARB-{uuid.uuid4().hex[:12]}"
        order_ids: list[int] = []
        try:
            for decision in normalized.decisions:
                order_id = self.store.record_paper_order(
                    target_date,
                    decision,
                    risk_profile=self.risk_profile,
                    group_id=group_id,
                    strategy_config=self.config,
                )
                # A box must record every leg or none. If the open-position guard
                # rejected a leg (None), compensate any already-booked execution
                # and leave a visible degraded audit group. This can still happen
                # if another writer wins after the active-entry preflight.
                if order_id is None:
                    self._compensate_partial_arbitrage(
                        order_ids,
                        group_id=group_id,
                        reason="arbitrage leg rejected after preflight",
                    )
                    return []
                order_ids.append(order_id)
        except ArbitrageContainmentError:
            raise
        except Exception:
            self._compensate_partial_arbitrage(
                order_ids,
                group_id=group_id,
                reason="arbitrage group recording failed mid-box",
            )
            raise
        return order_ids

    def _compensate_partial_arbitrage(
        self,
        order_ids: list[int],
        *,
        group_id: str,
        reason: str,
    ) -> None:
        """Drive every partial leg to a terminal state before degrading the group."""

        def fail_containment(message: str) -> NoReturn:
            # Fatal containment must remove the guaranteed-group signal before
            # control unwinds. Any unresolved active leg is then managed as
            # directional risk on the very next monitor pass.
            self.store.mark_arbitrage_group_degraded(
                order_ids,
                group_id=group_id,
                reason=f"{reason}; fatal containment: {message}",
            )
            raise ArbitrageContainmentError(message)

        for order_id in order_ids:
            for _attempt in range(5):
                row = self.store.paper_order(order_id)
                if row is None:
                    fail_containment(f"arbitrage containment lost order {order_id}")
                status = str(row["status"])
                if status not in {"PAPER_LIMIT_RESTING", "PAPER_FILLED"}:
                    break
                if status == "PAPER_LIMIT_RESTING":
                    # The returned row is authoritative: a fill can win between
                    # our stale read and cancel's BEGIN IMMEDIATE transaction.
                    self.store.cancel_resting_limit_order(order_id, reason=reason)
                    continue

                exit_price = row["entry_bid"]
                if exit_price is None or not 0.0 < float(exit_price) < 1.0:
                    fail_containment(
                        f"filled arbitrage leg {order_id} has no executable stored side bid"
                    )
                try:
                    # Cross back out at the executable side bid, never the entry
                    # ask. The ordinary close path records spread loss, exit fee,
                    # realized PnL, and account-ledger proceeds.
                    self.store.close_paper_order(order_id, float(exit_price))
                except (ValueError, RuntimeError):
                    # A concurrent resolver may have won. Re-read and confirm;
                    # an actually-still-open leg loops and retries.
                    continue
            current = self.store.paper_order(order_id)
            if current is None or str(current["status"]) in {
                "PAPER_LIMIT_RESTING",
                "PAPER_FILLED",
            }:
                fail_containment(
                    f"arbitrage leg {order_id} remains active after containment attempts"
                )

        # Successful containment also keeps the partial group visibly degraded.
        self.store.mark_arbitrage_group_degraded(
            order_ids,
            group_id=group_id,
            reason=reason,
        )

    def _target_exposure_remaining(self, target_date: str, bankroll: float | None) -> float | None:
        """Cumulative per-target risk cap, persisted across scans via the DB.

        With no daily paper budget, this is the guard that keeps repeated
        15-minute scans from stacking unbounded exposure onto one settlement
        date.
        """

        if bankroll is None or bankroll <= 0:
            return None
        cap = bankroll * self.config.max_target_exposure_pct
        if cap <= 0:
            return None
        spent = self.store.paper_spend_for_target(
            target_date,
            risk_profile=self.risk_profile,
            series_ticker=self.series_ticker,
        )
        return max(0.0, cap - spent)

    def _normalize_contracts(self, decision: TradeDecision) -> TradeDecision | None:
        """Round paper orders down to whole contracts like the live exchange.

        Event-cap scaling can leave fractional sizes even when the evaluator
        floors; fractional dust also breaks the ceil-to-cent fee model.
        """

        if self.config.allow_fractional_contracts:
            return decision
        contracts = float(int(decision.recommended_contracts))
        if contracts < 1:
            return None
        if contracts == decision.recommended_contracts:
            return decision
        return replace(
            decision,
            recommended_contracts=contracts,
            expected_profit=decision.edge * contracts,
        )

    def _fit_to_exposure(
        self,
        decision: TradeDecision,
        exposure_remaining: float,
    ) -> TradeDecision | None:
        cost = decision.cost_per_contract
        spend = decision.recommended_contracts * cost
        if spend <= exposure_remaining + 1e-9:
            return decision
        contracts = exposure_remaining / cost
        if not self.config.allow_fractional_contracts:
            contracts = float(int(contracts))
        if contracts <= 0:
            return None
        return replace(
            decision,
            recommended_contracts=contracts,
            expected_profit=decision.edge * contracts,
        )

    def _fit_research_shadow_sample(
        self,
        target_date: str,
        decision: TradeDecision,
        bankroll: float | None,
    ) -> TradeDecision | None:
        contracts = min(
            float(decision.recommended_contracts),
            float(self.config.research_shadow_max_contracts),
        )
        if not self.config.allow_fractional_contracts:
            contracts = float(int(contracts))
        if contracts <= 0:
            return None
        adjusted = replace(
            decision,
            recommended_contracts=contracts,
            expected_profit=decision.edge * contracts,
        )
        if bankroll is None or bankroll <= 0:
            return adjusted
        remaining = (
            bankroll * self.config.research_shadow_daily_loss_pct
            - self.store.research_shadow_sample_spend_for_target(
                target_date,
                risk_profile=self.risk_profile,
            )
        )
        if remaining <= 0:
            return None
        return self._fit_to_exposure(adjusted, remaining)

    def _normalize_arbitrage_contracts(
        self,
        opportunity: ArbitrageOpportunity,
    ) -> ArbitrageOpportunity | None:
        if self.config.allow_fractional_contracts:
            return opportunity
        contracts = float(int(opportunity.contracts))
        if contracts < 1:
            return None
        if contracts == opportunity.contracts:
            return opportunity
        adjusted = opportunity.with_contracts(contracts)
        return adjusted if adjusted.approved else None

    def _fit_arbitrage_to_exposure(
        self,
        opportunity: ArbitrageOpportunity,
        exposure_remaining: float,
    ) -> ArbitrageOpportunity | None:
        if opportunity.total_spend <= exposure_remaining + 1e-9:
            return opportunity
        if exposure_remaining <= 0:
            return None
        lo = 0.0
        hi = opportunity.contracts
        for _ in range(54):
            mid = (lo + hi) / 2.0
            candidate = opportunity.with_contracts(mid)
            if candidate.total_spend <= exposure_remaining:
                lo = mid
            else:
                hi = mid
        contracts = lo
        if not self.config.allow_fractional_contracts:
            contracts = float(int(contracts))
        if contracts <= 0:
            return None
        adjusted = opportunity.with_contracts(contracts)
        if not adjusted.approved or adjusted.total_spend > exposure_remaining + 1e-9:
            return None
        return adjusted


def _prepare_research_disposition(
    decision: TradeDecision,
    *,
    policy: ResearchSleevePolicy,
    selected: bool,
    reason: str | None,
    config: StrategyConfig,
) -> TradeDecision:
    if not selected:
        block_reason = (
            decision.entry_block_reason
            or reason
            or f"{policy.sleeve.value} research not selected"
        )
        return replace(
            decision,
            approved=False,
            signal_approved=(
                decision.signal_approved
                if decision.signal_approved is not None
                else decision.approved
            ),
            entry_block_reason=block_reason,
            recommended_contracts=0.0,
            expected_profit=0.0,
            reasons=[*decision.reasons, block_reason],
        )
    if policy is MOTION_POLICY:
        prepared = with_motion_taker_execution(decision, config)
        if prepared is not None:
            return prepared
        block_reason = "motion visible-ask taker quote is not executable"
    else:
        prepared = with_target_research_execution(decision, config)
        if prepared is not None and prepared.recommended_contracts > 0:
            return prepared
        block_reason = "target canonical maker-or-taker quote is not executable"
    return replace(
        decision,
        approved=False,
        signal_approved=True,
        entry_block_reason=block_reason,
        recommended_contracts=0.0,
        expected_profit=0.0,
        reasons=[*decision.reasons, block_reason],
    )


def research_reentry_fingerprint(
    *,
    account_id: str,
    sleeve: str,
    scan_run_id: str,
    ticker: str,
    side: str,
    executable_price: float,
    probability: float,
    observed_high_state: str,
) -> str:
    """Hash the exact versioned opportunity identity from the design."""

    payload = {
        "account_id": account_id,
        "sleeve": sleeve,
        "scan_run_id": scan_run_id,
        "ticker": ticker,
        "side": side.upper(),
        "executable_price_cents": round(executable_price * 100),
        "probability_bucket": round(probability * 50),
        "observed_high_state": observed_high_state,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _normalize_entry_mode(entry_mode: str) -> str:
    normalized = entry_mode.strip().lower().replace("_", "-")
    if normalized in {"market", "market-order", "immediate"}:
        return "market"
    if normalized in {"limit", "limit-order", "paper-limit"}:
        return "limit"
    raise ValueError("entry mode must be market or limit")


def _decision_key(decision: TradeDecision) -> tuple[str, str]:
    return (decision.ticker, decision.side)


def _clamp_to_displayed_ask(decision: TradeDecision) -> TradeDecision | None:
    """Cap a TAKER fill at the ask depth the book displays right now.

    Applies only where the order takes immediately -- market entry or a
    crossing limit -- because an instant fill cannot take more contracts than
    the visible ask offers. Resting maker quotes are deliberately not capped
    here: their fill is gated by future traded volume through the queue-ahead
    fill model, so the displayed ask at entry is irrelevant to their size.
    Downstream repricing (with_buy_limit / _fit_to_account_policy) recomputes
    exact fees for the clamped count.
    """

    ask_size = float(decision.ask_size or 0.0)
    if ask_size <= 0 or decision.recommended_contracts <= ask_size:
        return decision
    contracts = float(int(ask_size))
    if contracts <= 0:
        return None
    return replace(
        decision,
        recommended_contracts=contracts,
        expected_profit=decision.edge * contracts,
    )


def _is_research_shadow_candidate(
    risk_profile: str | None,
    decision: TradeDecision,
    *,
    include_positive_lcb: bool = False,
    require_explore_sleeve: bool = True,
) -> bool:
    if (risk_profile or "").strip().lower().replace("_", "-") != "research":
        return False
    signal_approved = decision.signal_approved if decision.signal_approved is not None else decision.approved
    if not signal_approved or decision.edge <= 0.0:
        return False
    if not include_positive_lcb and decision.edge_lcb >= 0.0:
        return False
    if require_explore_sleeve and not any(
        "sleeve=research_explore" in reason for reason in decision.reasons
    ):
        return False
    return True


def _clamp_probability(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _deterministic_sample(
    target_date: str,
    decision: TradeDecision,
    *,
    sample_probability: float,
) -> bool:
    probability = _clamp_probability(sample_probability)
    if probability <= 0.0:
        return False
    if probability >= 1.0:
        return True
    key = f"{target_date}|{decision.ticker}|{decision.side.upper()}|{decision.action}"
    raw = hashlib.sha256(key.encode("utf-8")).digest()[:8]
    value = int.from_bytes(raw, "big") / float(2**64 - 1)
    return value < probability
