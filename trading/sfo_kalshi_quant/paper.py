from __future__ import annotations

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
    ) -> None:
        self.store = store
        self.config = config or StrategyConfig()
        self.risk_profile = risk_profile
        self.entry_mode = _normalize_entry_mode(entry_mode)

    def with_paper_stake(self, decision: TradeDecision, stake_dollars: float | None) -> TradeDecision:
        if stake_dollars is None or not decision.approved:
            return decision
        if stake_dollars <= 0:
            raise ValueError("paper stake must be greater than zero")
        if decision.ask <= 0:
            return decision
        contracts = contracts_for_budget(decision.ask, stake_dollars)
        contracts = min(contracts, self.config.max_contracts_per_market)
        if decision.ask_size > 0:
            contracts = min(contracts, decision.ask_size)
        if not self.config.allow_fractional_contracts:
            contracts = float(int(contracts))
        fee_per_contract = quadratic_fee_average_per_contract(decision.ask, contracts)
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
            order_ids.extend(self._fill_crossed_resting_limits(target_date, decision))
            adjusted = self.with_paper_stake(decision, stake_dollars)
            if not adjusted.approved or adjusted.recommended_contracts <= 0:
                continue
            adjusted = self._normalize_contracts(adjusted)
            if adjusted is None:
                continue
            if adjusted.cost_per_contract >= 1.0 or adjusted.cost_per_contract <= 0:
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
                adjusted = with_buy_limit(adjusted, self.config)
                if not adjusted.approved:
                    continue
                status = "PAPER_FILLED" if quote.would_cross else "PAPER_LIMIT_RESTING"
                entry_mode = "limit"
            order_ids.append(
                self.store.record_paper_order(
                    target_date,
                    adjusted,
                    risk_profile=self.risk_profile,
                    status=status,
                    entry_mode=entry_mode,
                    group_id=group_id,
                )
            )
            if exposure_remaining is not None:
                exposure_remaining -= adjusted.recommended_contracts * adjusted.cost_per_contract
        return order_ids

    def _fill_crossed_resting_limits(self, target_date: str, decision: TradeDecision) -> list[int]:
        if self.entry_mode != "limit":
            return []
        ask = float(decision.ask)
        if ask <= 0.0 or ask >= 1.0:
            return []
        filled: list[int] = []
        for row in self.store.resting_limit_orders(
            target_date,
            decision.ticker,
            decision.side,
            risk_profile=self.risk_profile,
        ):
            limit_price = row["limit_price"] if row["limit_price"] is not None else row["entry_price"]
            if limit_price is None:
                continue
            if ask <= float(limit_price) + 1e-12:
                updated = self.store.fill_resting_limit_order(int(row["id"]))
                if updated is not None and updated["status"] == "PAPER_FILLED":
                    filled.append(int(row["id"]))
        return filled

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
                order_ids.append(
                    self.store.record_paper_order(
                        target_date,
                        decision,
                        risk_profile=self.risk_profile,
                        group_id=group_id,
                    )
                )
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
        spent = self.store.paper_spend_for_target(target_date, risk_profile=self.risk_profile)
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
