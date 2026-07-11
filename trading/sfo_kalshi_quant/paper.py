from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace

from .arbitrage import ArbitrageOpportunity
from .config import StrategyConfig
from .db import PaperStore
from .fees import (
    contracts_for_budget,
    quadratic_fee_average_per_contract,
)
from .execution import buy_limit_for_decision, with_buy_limit
from .models import TradeDecision


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
        maker = False  # Paper stake books immediately at the visible ask.
        contracts = contracts_for_budget(
            decision.ask,
            stake_dollars,
            maker=maker,
            fee_multiplier=self.config.fee_multiplier,
            taker_rate=self.config.taker_fee_rate,
            maker_rate=self.config.maker_fee_rate,
            series_ticker=decision.ticker,
        )
        contracts = min(contracts, self.config.max_contracts_per_market)
        if decision.ask_size > 0:
            contracts = min(contracts, decision.ask_size)
        if not self.config.allow_fractional_contracts:
            contracts = float(int(contracts))
        fee_per_contract = quadratic_fee_average_per_contract(
            decision.ask,
            contracts,
            maker=maker,
            fee_multiplier=self.config.fee_multiplier,
            taker_rate=self.config.taker_fee_rate,
            maker_rate=self.config.maker_fee_rate,
            series_ticker=decision.ticker,
        )
        cost_per_contract = decision.ask + fee_per_contract
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
            if self.store.has_open_paper_position(
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
                # rejected a leg (None), abort loudly rather than book a partial,
                # unbounded box. Preflight (has_open_paper_position) makes this
                # unreachable in normal operation since arb legs are YES+NO.
                if order_id is None:
                    raise RuntimeError(
                        "arbitrage leg rejected by the open-position guard; "
                        "aborting partially-recorded box"
                    )
                order_ids.append(order_id)
        except Exception:
            # The SQLite store autocommits each insert, so there is no safe
            # partial rollback API here. Preflight checks above keep expected
            # failure modes before the first insert.
            raise
        return order_ids

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
